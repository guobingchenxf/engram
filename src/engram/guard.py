"""Write-path policy: trust, provenance, sensitivity and attestation.

``MemoryService`` orchestrates; this module decides whether a write/update/
import is allowed.  Keeping the policy here makes the write path independently
reviewable and lets the storage layer stay a pure persistence concern.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .config import MemoryConfig
from .errors import AttestationViolation, SafetyViolation
from .models import (
    MemoryRecord,
    MemoryType,
    SafetyAssessment,
    Sensitivity,
    SourceType,
)
from .provenance import (
    AttestationConfigurationError,
    AttestationVerdict,
    ProvenanceAttestor,
)
from .safety import SafetyGuard
from .storage import SQLiteMemoryStore
from .trace import TraceLogger, fingerprint

DURABLE_TYPES = {MemoryType.SEMANTIC, MemoryType.PROCEDURAL}
_SENSITIVITY_RANK = {
    Sensitivity.PUBLIC: 0,
    Sensitivity.PERSONAL: 1,
    Sensitivity.SENSITIVE: 2,
    Sensitivity.SECRET: 3,
}

ParentLookup = Callable[[str], MemoryRecord | None]


@dataclass(frozen=True)
class ProvenanceInfo:
    derived_from: list[str]
    taint: list[str]
    verified: bool
    missing: list[str]
    parents: list[MemoryRecord] = field(default_factory=list)
    invalid: list[str] = field(default_factory=list)


def stronger(left: Sensitivity, right: Sensitivity) -> Sensitivity:
    return left if _SENSITIVITY_RANK[left] >= _SENSITIVITY_RANK[right] else right


class WriteGuard:
    def __init__(
        self,
        config: MemoryConfig,
        safety: SafetyGuard,
        attestor: ProvenanceAttestor | None,
        trace: TraceLogger,
        store: SQLiteMemoryStore,
        *,
        embed: Callable[[str], list[float]],
    ) -> None:
        self.config = config
        self.safety = safety
        self.attestor = attestor
        self.trace = trace
        self.store = store
        self.embed = embed

    # ------------------------------------------------------------------
    # Provenance
    # ------------------------------------------------------------------
    def derive_provenance(
        self,
        *,
        tenant_id: str,
        user_id: str,
        source_type: SourceType,
        derived_from: list[str],
        request_type: MemoryType,
        lookup: ParentLookup | None = None,
    ) -> ProvenanceInfo:
        parent_lookup = lookup or (
            lambda parent_id: self.store.get(parent_id, tenant_id=tenant_id)
        )
        unique = list(dict.fromkeys(derived_from))
        parents: list[MemoryRecord] = []
        missing: list[str] = []
        invalid: list[str] = []
        for parent_id in unique:
            parent = parent_lookup(parent_id)
            if (
                parent is None
                or parent.user_id != user_id
                or parent.tenant_id != tenant_id
            ):
                missing.append(parent_id)
            else:
                parents.append(parent)

        taint: set[str] = set()
        for parent in parents:
            taint.update(str(item) for item in parent.metadata.get("taint", []))
            if not parent.source_type.is_trusted:
                taint.add(parent.source_type.value)
        if not source_type.is_trusted:
            taint.add(source_type.value)

        if self.attestor is not None:
            for parent in parents:
                has_attestation = isinstance(
                    parent.metadata.get("attestation"), dict
                )
                if not has_attestation:
                    if (
                        source_type == SourceType.AGENT
                        and self.config.require_signed_parents
                    ):
                        invalid.append(parent.id)
                    continue
                verdict = self.verify_attestation(
                    parent, tenant_id=tenant_id, lookup=parent_lookup
                )
                if not verdict.verified:
                    invalid.append(parent.id)

        verified = (
            not taint
            and not missing
            and not invalid
            and (
                source_type in {SourceType.USER, SourceType.SYSTEM}
                or (source_type == SourceType.AGENT and bool(parents))
            )
        )
        return ProvenanceInfo(
            derived_from=unique,
            taint=sorted(taint),
            verified=verified,
            missing=missing,
            parents=parents,
            invalid=invalid,
        )

    def provenance_reasons(
        self, provenance: ProvenanceInfo, source_type: SourceType
    ) -> list[str]:
        reasons: list[str] = []
        if not source_type.is_trusted:
            reasons.append("untrusted_source")
        if source_type == SourceType.AGENT and not provenance.derived_from:
            reasons.append("agent_memory_without_provenance")
        if provenance.taint:
            reasons.append("tainted_provenance")
        if provenance.missing:
            reasons.append("unknown_provenance_parent")
        if provenance.invalid:
            reasons.append("invalid_parent_attestation")
        if not reasons and not provenance.verified:
            reasons.append("unverified_provenance")
        return reasons

    # ------------------------------------------------------------------
    # Attestation
    # ------------------------------------------------------------------
    def sign_attestation(
        self, record: MemoryRecord, parents: list[MemoryRecord]
    ) -> dict[str, Any]:
        assert self.attestor is not None
        try:
            return self.attestor.sign(record, parents)
        except AttestationConfigurationError as exc:
            raise AttestationViolation(str(exc)) from exc

    def verify_attestation(
        self,
        record: MemoryRecord,
        *,
        tenant_id: str,
        lookup: ParentLookup | None = None,
    ) -> AttestationVerdict:
        assert self.attestor is not None
        parent_lookup = lookup or (
            lambda parent_id: self.store.get(parent_id, tenant_id=tenant_id)
        )
        return self.attestor.verify(record, parent_lookup)

    def apply_attestation(
        self,
        record: MemoryRecord,
        *,
        parents: list[MemoryRecord],
        supplied: dict[str, Any] | None,
        tenant_id: str,
        lookup: ParentLookup | None = None,
    ) -> MemoryRecord:
        if supplied is not None:
            if self.attestor is None:
                raise AttestationViolation("attestation_not_configured")
            record.metadata["attestation"] = dict(supplied)
            verdict = self.verify_attestation(
                record, tenant_id=tenant_id, lookup=lookup
            )
            if not verdict.verified:
                self.trace.log(
                    "write_denied",
                    tenant_id=tenant_id,
                    user_id=record.user_id,
                    reasons=["attestation_invalid", *verdict.reasons],
                    content_fingerprint=fingerprint(record.content),
                    content_length=len(record.content),
                )
                raise AttestationViolation(
                    "attestation_invalid: "
                    + ("; ".join(verdict.reasons) or verdict.status)
                )
            chain_ok = record.source_type in {
                SourceType.USER,
                SourceType.SYSTEM,
            } or (record.source_type == SourceType.AGENT and bool(parents))
            record.metadata["provenance_verified"] = (
                verdict.verified
                and chain_ok
                and not bool(record.metadata.get("taint"))
            )
            record.metadata["attestation_status"] = verdict.status
            record.metadata["attestation_hash"] = verdict.attestation_hash
            return record

        if self.attestor is None:
            return record
        if self.attestor.local_key is None:
            if record.source_type == SourceType.AGENT:
                raise AttestationViolation("no_local_attestation_key")
            return record
        if (
            record.source_type == SourceType.AGENT
            and self.config.require_attestation_for_agent
        ):
            raise AttestationViolation("agent_write_requires_attestation")
        attestation = self.sign_attestation(record, parents)
        chain_ok = record.source_type in {
            SourceType.USER,
            SourceType.SYSTEM,
        } or (record.source_type == SourceType.AGENT and bool(parents))
        record.metadata["attestation"] = attestation
        record.metadata["attestation_hash"] = attestation["attestation_hash"]
        record.metadata["attestation_status"] = "verified"
        record.metadata["provenance_verified"] = chain_ok and not bool(
            record.metadata.get("taint")
        )
        return record

    def reattest_update(
        self, record: MemoryRecord, *, tenant_id: str
    ) -> MemoryRecord:
        assert self.attestor is not None
        if self.attestor.local_key is None:
            if record.metadata.get("attestation"):
                raise AttestationViolation(
                    "cannot_re_attest_without_local_key"
                )
            return record
        derived = record.metadata.get("derived_from", [])
        if not isinstance(derived, list):
            derived = []
        provenance = self.derive_provenance(
            tenant_id=tenant_id,
            user_id=record.user_id,
            source_type=record.source_type,
            derived_from=[str(item) for item in derived],
            request_type=record.type,
        )
        if provenance.missing:
            raise AttestationViolation("unknown_provenance_parent")
        if provenance.invalid:
            raise AttestationViolation("invalid_parent_attestation")
        if self.config.provenance_enabled and record.type in DURABLE_TYPES:
            reasons = self.provenance_reasons(provenance, record.source_type)
            if reasons:
                raise SafetyViolation("; ".join(reasons))
        record.metadata.pop("attestation", None)
        record.metadata.pop("attestation_hash", None)
        attestation = self.sign_attestation(record, provenance.parents)
        record.metadata["attestation"] = attestation
        record.metadata["attestation_hash"] = attestation["attestation_hash"]
        record.metadata["attestation_status"] = "verified"
        record.metadata["provenance_verified"] = not bool(provenance.taint)
        return record

    # ------------------------------------------------------------------
    # Sensitivity
    # ------------------------------------------------------------------
    def apply_sensitivity(
        self,
        content: str,
        assessment: SafetyAssessment,
        *,
        confirmed: bool,
        metadata: dict[str, Any],
    ) -> str:
        if not assessment.requires_confirmation:
            return content
        if confirmed:
            metadata["sensitive_confirmed"] = True
            return content
        if self.config.redact_sensitive and assessment.redacted_content is not None:
            metadata["redacted"] = True
            return assessment.redacted_content
        raise SafetyViolation("sensitive_content_requires_confirmation")

    # ------------------------------------------------------------------
    # Update / import authorization
    # ------------------------------------------------------------------
    def authorize_durable_promotion(
        self, record: MemoryRecord, new_type: MemoryType
    ) -> None:
        if not record.source_type.is_trusted:
            raise SafetyViolation("untrusted_source_cannot_write_durable_memory")
        if record.metadata.get("taint"):
            raise SafetyViolation("tainted_memory_cannot_write_durable_memory")
        if self.attestor is not None and isinstance(
            record.metadata.get("attestation"), dict
        ):
            verdict = self.verify_attestation(
                record, tenant_id=record.tenant_id
            )
            if not verdict.verified:
                raise AttestationViolation(
                    "invalid_provenance_attestation: "
                    + ("; ".join(verdict.reasons) or verdict.status)
                )
        elif self.config.provenance_enabled and not record.metadata.get(
            "provenance_verified"
        ):
            raise SafetyViolation(
                "unverified_provenance_cannot_write_durable_memory"
            )
        if self.config.safety_enabled:
            assessment = self.safety.assess(
                record.content,
                source_type=record.source_type,
                requested_type=new_type,
                sensitivity_hint=record.sensitivity,
            )
            if not assessment.allowed:
                raise SafetyViolation("; ".join(assessment.reasons))

    def authorize_imported(
        self,
        record: MemoryRecord,
        *,
        lookup: ParentLookup,
    ) -> None:
        if self.config.safety_enabled:
            assessment = self.safety.assess(
                record.content,
                source_type=record.source_type,
                requested_type=record.type,
                sensitivity_hint=record.sensitivity,
            )
            if not assessment.allowed:
                raise SafetyViolation("; ".join(assessment.reasons))
        else:
            assessment = SafetyAssessment(
                allowed=True,
                sensitivity=record.sensitivity,
            )

        metadata = dict(record.metadata)
        content = self.apply_sensitivity(
            record.content, assessment, confirmed=False, metadata=metadata
        )
        provenance = self.derive_provenance(
            tenant_id=record.tenant_id,
            user_id=record.user_id,
            source_type=record.source_type,
            derived_from=list(metadata.get("derived_from", [])),
            request_type=record.type,
            lookup=lookup,
        )
        if provenance.missing:
            raise SafetyViolation("unknown_provenance_parent")
        if provenance.invalid:
            raise AttestationViolation("invalid_parent_attestation")
        if self.config.provenance_enabled and record.type in DURABLE_TYPES:
            reasons = self.provenance_reasons(provenance, record.source_type)
            if reasons:
                raise SafetyViolation("; ".join(reasons))

        metadata["derived_from"] = provenance.derived_from
        metadata["taint"] = provenance.taint
        metadata["provenance_verified"] = provenance.verified

        record.content = content
        if self.attestor is not None:
            supplied = metadata.get("attestation")
            if isinstance(supplied, dict):
                verdict = self.verify_attestation(
                    record, tenant_id=record.tenant_id, lookup=lookup
                )
                if not verdict.verified:
                    raise AttestationViolation(
                        "invalid_import_attestation: "
                        + ("; ".join(verdict.reasons) or verdict.status)
                    )
                metadata["attestation_status"] = verdict.status
                metadata["attestation_hash"] = verdict.attestation_hash
                metadata["provenance_verified"] = (
                    verdict.verified
                    and provenance.verified
                    and not provenance.taint
                )
            elif record.source_type == SourceType.AGENT:
                if self.config.require_attestation_for_agent:
                    raise AttestationViolation(
                        "agent_import_requires_attestation"
                    )
                metadata["attestation_status"] = "missing"
            elif self.attestor.local_key is not None and record.source_type in {
                SourceType.USER,
                SourceType.SYSTEM,
            }:
                attestation = self.sign_attestation(record, provenance.parents)
                metadata["attestation"] = attestation
                metadata["attestation_hash"] = attestation["attestation_hash"]
                metadata["attestation_status"] = "verified"
                metadata["provenance_verified"] = not provenance.taint

        record.sensitivity = stronger(record.sensitivity, assessment.sensitivity)
        record.metadata = metadata
        record.embedding = self.embed(record.content)

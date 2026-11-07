"""Signed provenance attestations for durable memory writes.

Why this module exists
----------------------
The previous provenance check was *self-declared*: a client could pass
``derived_from=["some-id"]`` and the service marked the write verified.  That
is not provenance; it is a claim.  This module turns the claim into an
attestation:

* the payload binds the memory identity, tenant/user, content hash, type,
  source, sensitivity, parent ids and parent attestation hashes;
* the issuer signs the canonical payload with an HMAC-SHA256 key;
* verification recomputes the payload from the *stored* record and the
  *stored* parents, so changing content, parents, tenant, source or type breaks
  the signature;
* a parent chain is verified recursively, so a forged or missing parent is
  rejected instead of trusted.

HMAC is used deliberately: it is stdlib-only, works offline, and matches the
project's zero-dependency deployment.  The ``alg`` field is explicit so an
Ed25519 implementation can be added later without changing stored records.
HMAC is a shared-secret MAC: the verifier must hold the key.  For a single
trusted issuer (the Engram service, or one agent runtime) that is sufficient;
for third-party public verification, replace the signer with Ed25519.
"""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from .models import MemoryRecord, ensure_utc, utcnow

ATTESTATION_VERSION = "engram-attestation/1"
ATTESTATION_ALG = "hmac-sha256"

ParentLookup = Callable[[str], MemoryRecord | None]


class AttestationError(Exception):
    """Base class for attestation failures."""


class AttestationConfigurationError(AttestationError):
    """The signer/verifier is misconfigured."""


@dataclass(frozen=True)
class AttestationKey:
    """A trusted issuer key.

    ``secret`` is used for HMAC.  ``allowed_tenants`` and
    ``allowed_source_types`` restrict what a key may attest; an empty tuple
    means "no restriction".
    """

    key_id: str
    secret: str = field(repr=False)
    issuer: str
    allowed_source_types: tuple[str, ...] = ()
    allowed_tenants: tuple[str, ...] = ()
    not_before: datetime | None = None
    not_after: datetime | None = None

    def allows(
        self,
        *,
        tenant_id: str,
        source_type: str,
        at: datetime | None = None,
    ) -> bool:
        if self.allowed_tenants and tenant_id not in self.allowed_tenants:
            return False
        if self.allowed_source_types and source_type not in self.allowed_source_types:
            return False
        current = ensure_utc(at) or utcnow()
        before = ensure_utc(self.not_before)
        after = ensure_utc(self.not_after)
        if before is not None and current < before:
            return False
        return not (after is not None and current > after)


@dataclass
class AttestationVerdict:
    """Result of verifying one record and its parent chain."""

    status: str
    reasons: list[str] = field(default_factory=list)
    key_id: str | None = None
    issuer: str | None = None
    depth: int = 0
    root_ids: list[str] = field(default_factory=list)
    attestation_hash: str | None = None

    @property
    def verified(self) -> bool:
        return self.status == "verified"

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "reasons": list(self.reasons),
            "key_id": self.key_id,
            "issuer": self.issuer,
            "depth": self.depth,
            "root_ids": list(self.root_ids),
            "attestation_hash": self.attestation_hash,
        }


def canonical_json(payload: Any) -> str:
    """Stable JSON used for signatures and audit hashes."""
    return json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        default=str,
    )


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def content_hash(content: str) -> str:
    return sha256_text(content)


def parent_hash(parent: MemoryRecord) -> str:
    """Hash binding a parent, using its attestation when it has one."""
    attestation = parent.metadata.get("attestation")
    if isinstance(attestation, dict):
        value = attestation.get("attestation_hash")
        if isinstance(value, str) and value:
            return value
    return "sha256:" + content_hash(parent.content)


def build_payload(
    record: MemoryRecord,
    parents: Sequence[MemoryRecord],
    *,
    issuer: str,
    key_id: str,
    issued_at: str = "",
) -> dict[str, Any]:
    """The exact bytes that the issuer signs, derived only from stored state."""
    created = ensure_utc(record.created_at) or utcnow()
    derived_from = record.metadata.get("derived_from", [])
    if not isinstance(derived_from, list):
        derived_from = []
    return {
        "v": ATTESTATION_VERSION,
        "tenant_id": record.tenant_id,
        "user_id": record.user_id,
        "memory_id": record.id,
        "type": record.type.value,
        "source_type": record.source_type.value,
        "source": record.source or "",
        "sensitivity": record.sensitivity.value,
        "content_sha256": content_hash(record.content),
        "created_at": created.isoformat(),
        "derived_from": sorted({str(item) for item in derived_from}),
        "parent_hashes": sorted(parent_hash(parent) for parent in parents),
        "issuer": issuer,
        "key_id": key_id,
        "issued_at": issued_at,
    }


def _signature(secret: str, body: bytes) -> str:
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _attestation_hash(attestation: dict[str, Any]) -> str:
    base = {key: value for key, value in attestation.items() if key != "attestation_hash"}
    return sha256_text(canonical_json(base))


def sign_attestation(
    record: MemoryRecord,
    parents: Sequence[MemoryRecord],
    key: AttestationKey,
) -> dict[str, Any]:
    """Create a signed attestation for ``record``.

    ``record.metadata["derived_from"]`` must already contain the parent ids.
    The returned dict is safe to store under ``metadata["attestation"]``.
    """
    issued_at = utcnow().isoformat()
    payload = build_payload(
        record,
        parents,
        issuer=key.issuer,
        key_id=key.key_id,
        issued_at=issued_at,
    )
    body = canonical_json(payload).encode("utf-8")
    attestation: dict[str, Any] = {
        "v": ATTESTATION_VERSION,
        "alg": ATTESTATION_ALG,
        "key_id": key.key_id,
        "issuer": key.issuer,
        "signature": _signature(key.secret, body),
        "payload_sha256": hashlib.sha256(body).hexdigest(),
        "issued_at": issued_at,
        "content_sha256": payload["content_sha256"],
        "parent_hashes": payload["parent_hashes"],
    }
    attestation["attestation_hash"] = _attestation_hash(attestation)
    return attestation


class ProvenanceAttestor:
    """Sign and verify memory provenance.

    ``keys`` is the verification trust store.  ``local_key`` is the key this
    service uses to sign writes it accepts itself.  A service may both sign and
    verify with the same key; in a multi-service deployment each issuer has its
    own key and only the public/verification material is shared.
    """

    def __init__(
        self,
        keys: Sequence[AttestationKey] = (),
        *,
        local_key: AttestationKey | None = None,
        require_signed_parents: bool = True,
        max_depth: int = 8,
    ) -> None:
        self.keys = {key.key_id: key for key in keys}
        if local_key is not None:
            self.keys.setdefault(local_key.key_id, local_key)
        self.local_key = local_key
        self.require_signed_parents = require_signed_parents
        self.max_depth = max(1, max_depth)

    @property
    def enabled(self) -> bool:
        return bool(self.keys)

    def sign(
        self,
        record: MemoryRecord,
        parents: Sequence[MemoryRecord],
        *,
        key: AttestationKey | None = None,
    ) -> dict[str, Any]:
        signing_key = key or self.local_key
        if signing_key is None:
            raise AttestationConfigurationError("no local attestation key configured")
        if not signing_key.allows(
            tenant_id=record.tenant_id,
            source_type=record.source_type.value,
            at=utcnow(),
        ):
            raise AttestationConfigurationError(
                "local key is not allowed to attest this tenant/source"
            )
        return sign_attestation(record, parents, signing_key)

    def verify(
        self,
        record: MemoryRecord,
        lookup: ParentLookup,
        *,
        depth: int = 0,
        seen: set[str] | None = None,
    ) -> AttestationVerdict:
        """Verify ``record`` and, recursively, its parent chain."""
        seen = set(seen or ())
        if record.id in seen:
            return AttestationVerdict(
                "cycle",
                ["cycle_in_parent_chain"],
                depth=depth,
            )
        if depth > self.max_depth:
            return AttestationVerdict(
                "chain_too_deep",
                [f"max_depth_exceeded:{self.max_depth}"],
                depth=depth,
            )
        seen.add(record.id)

        attestation = record.metadata.get("attestation")
        if not isinstance(attestation, dict):
            return AttestationVerdict("missing", ["attestation_missing"], depth=depth)
        if attestation.get("v") != ATTESTATION_VERSION:
            return AttestationVerdict(
                "unsupported_version",
                [f"unsupported_attestation_version:{attestation.get('v')}"],
                depth=depth,
            )
        if attestation.get("alg") != ATTESTATION_ALG:
            return AttestationVerdict(
                "unsupported_algorithm",
                [f"unsupported_attestation_alg:{attestation.get('alg')}"],
                depth=depth,
            )

        key_id = attestation.get("key_id")
        key = self.keys.get(str(key_id)) if key_id else None
        if key is None:
            return AttestationVerdict(
                "unknown_key",
                [f"unknown_attestation_key:{key_id}"],
                key_id=str(key_id) if key_id else None,
                depth=depth,
            )
        if attestation.get("issuer") != key.issuer:
            return AttestationVerdict(
                "wrong_issuer",
                ["attestation_issuer_does_not_match_key"],
                key_id=key.key_id,
                issuer=str(attestation.get("issuer")),
                depth=depth,
            )
        issued_at_raw = str(attestation.get("issued_at", ""))
        try:
            issued_at = ensure_utc(datetime.fromisoformat(issued_at_raw))
        except ValueError:
            issued_at = None
        if issued_at is None:
            return AttestationVerdict(
                "invalid_issued_at",
                ["attestation_issued_at_missing_or_invalid"],
                key_id=key.key_id,
                issuer=key.issuer,
                depth=depth,
            )
        if not key.allows(
            tenant_id=record.tenant_id,
            source_type=record.source_type.value,
            at=issued_at,
        ):
            return AttestationVerdict(
                "key_not_allowed",
                ["attestation_key_not_allowed_for_tenant_or_source"],
                key_id=key.key_id,
                issuer=key.issuer,
                depth=depth,
            )

        derived_from = record.metadata.get("derived_from", [])
        if not isinstance(derived_from, list):
            derived_from = []
        parents: list[MemoryRecord] = []
        missing: list[str] = []
        for parent_id in dict.fromkeys(str(item) for item in derived_from):
            parent = lookup(parent_id)
            if parent is None:
                missing.append(parent_id)
            else:
                parents.append(parent)
        if missing:
            return AttestationVerdict(
                "parent_missing",
                [f"parent_missing:{item}" for item in missing],
                key_id=key.key_id,
                issuer=key.issuer,
                depth=depth,
            )

        payload = build_payload(
            record,
            parents,
            issuer=key.issuer,
            key_id=key.key_id,
            issued_at=issued_at_raw,
        )
        body = canonical_json(payload).encode("utf-8")
        expected = _signature(key.secret, body)
        supplied = str(attestation.get("signature", ""))
        if not hmac.compare_digest(expected, supplied):
            return AttestationVerdict(
                "invalid_signature",
                ["signature_does_not_match_stored_record"],
                key_id=key.key_id,
                issuer=key.issuer,
                depth=depth,
            )
        if attestation.get("payload_sha256") != hashlib.sha256(body).hexdigest():
            return AttestationVerdict(
                "invalid_signature",
                ["payload_hash_mismatch"],
                key_id=key.key_id,
                issuer=key.issuer,
                depth=depth,
            )
        if attestation.get("content_sha256") != payload["content_sha256"]:
            return AttestationVerdict(
                "content_tampered",
                ["content_hash_mismatch"],
                key_id=key.key_id,
                issuer=key.issuer,
                depth=depth,
            )
        if attestation.get("parent_hashes") != payload["parent_hashes"]:
            return AttestationVerdict(
                "parent_changed",
                ["parent_hash_mismatch"],
                key_id=key.key_id,
                issuer=key.issuer,
                depth=depth,
            )
        if attestation.get("attestation_hash") != _attestation_hash(attestation):
            return AttestationVerdict(
                "tampered",
                ["attestation_hash_mismatch"],
                key_id=key.key_id,
                issuer=key.issuer,
                depth=depth,
            )

        roots: list[str] = []
        max_depth = depth
        for parent in parents:
            verdict = self.verify(parent, lookup, depth=depth + 1, seen=seen)
            max_depth = max(max_depth, verdict.depth)
            if not verdict.verified:
                if self.require_signed_parents:
                    return AttestationVerdict(
                        "parent_unverified",
                        [
                            f"parent_{parent.id}_{verdict.status}",
                            *verdict.reasons,
                        ],
                        key_id=key.key_id,
                        issuer=key.issuer,
                        depth=max_depth,
                    )
                continue
            roots.extend(verdict.root_ids or [parent.id])

        return AttestationVerdict(
            "verified",
            [],
            key_id=key.key_id,
            issuer=key.issuer,
            depth=max_depth,
            root_ids=sorted(set(roots)) if parents else [record.id],
            attestation_hash=str(attestation.get("attestation_hash")),
        )

    def verify_many(
        self,
        records: Iterable[MemoryRecord],
        lookup: ParentLookup,
    ) -> dict[str, AttestationVerdict]:
        return {record.id: self.verify(record, lookup) for record in records}


def parse_attestation_keys(raw: str | None) -> tuple[AttestationKey, ...]:
    """Parse ``MEMORY_ATTESTATION_KEYS`` JSON into a trust store.

    Example::

        [{"key_id":"agent-1","secret":"...","issuer":"agent-runtime",
          "allowed_source_types":["agent"], "allowed_tenants":["tenant-a"]}]
    """
    if not raw or not raw.strip():
        return ()
    payload = json.loads(raw)
    if not isinstance(payload, list):
        raise TypeError("MEMORY_ATTESTATION_KEYS must be a JSON array")
    keys: list[AttestationKey] = []
    for item in payload:
        if not isinstance(item, dict):
            raise TypeError("each attestation key must be an object")
        keys.append(
            AttestationKey(
                key_id=str(item["key_id"]),
                secret=str(item["secret"]),
                issuer=str(item.get("issuer", item["key_id"])),
                allowed_source_types=tuple(
                    str(value) for value in item.get("allowed_source_types", [])
                ),
                allowed_tenants=tuple(
                    str(value) for value in item.get("allowed_tenants", [])
                ),
                not_before=_parse_datetime(item.get("not_before")),
                not_after=_parse_datetime(item.get("not_after")),
            )
        )
    return tuple(keys)


def _parse_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    parsed = datetime.fromisoformat(str(value))
    return ensure_utc(parsed)

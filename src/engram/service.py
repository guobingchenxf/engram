"""High-level memory service.

This is the only object most callers need: it wires storage, embeddings,
extraction, retrieval, lifecycle and safety together, and emits a trace for
every state change.
"""

from __future__ import annotations

import threading
from collections import OrderedDict
from datetime import datetime
from typing import Any
from uuid import uuid4

from .config import MemoryConfig
from .embeddings import EmbeddingProvider, cosine_similarity, get_embedding_provider
from .errors import (
    AttestationViolation,
    MemoryNotFound,
    SafetyViolation,
    TenantIsolationError,
)
from .extraction import MemoryExtractor
from .guard import WriteGuard
from .lifecycle import LifecycleManager
from .llm import LLMClient, get_llm
from .models import (
    ConflictRecord,
    ExtractedMemory,
    ExtractRequest,
    MemoryQuery,
    MemoryRecord,
    MemorySearchResult,
    MemoryType,
    MemoryUpdateRequest,
    MemoryWriteRequest,
    SafetyAssessment,
    Sensitivity,
    SourceType,
    ensure_utc,
    utcnow,
)
from .provenance import AttestationKey, ProvenanceAttestor
from .retrieval import RetrievalService
from .safety import SafetyGuard
from .storage import SQLiteMemoryStore
from .text import content_hash, normalize_content, tokenize
from .trace import TraceLogger, fingerprint

_DURABLE_TYPES = {MemoryType.SEMANTIC, MemoryType.PROCEDURAL}
_PROTECTED_METADATA = {
    "taint",
    "provenance_verified",
    "derived_from",
    "attestation",
    "attestation_hash",
}
_SENSITIVITY_RANK = {
    Sensitivity.PUBLIC: 0,
    Sensitivity.PERSONAL: 1,
    Sensitivity.SENSITIVE: 2,
    Sensitivity.SECRET: 3,
}


def _stronger(left: Sensitivity, right: Sensitivity) -> Sensitivity:
    return left if _SENSITIVITY_RANK[left] >= _SENSITIVITY_RANK[right] else right


class MemoryService:
    def __init__(self, config: MemoryConfig | None = None) -> None:
        self.config = config or MemoryConfig.from_env()
        self.tenant_id = self.config.tenant_id
        self.store = SQLiteMemoryStore(self.config.db_path)
        self.embedder: EmbeddingProvider = get_embedding_provider(self.config)
        self.llm: LLMClient | None = get_llm(self.config)
        self.trace = TraceLogger(self.config.trace_path)
        self.safety = SafetyGuard(self.config)
        self.extractor = MemoryExtractor(self.llm, self.config)
        self.retrieval = RetrievalService(self.store, self.embedder, self.config)
        self.lifecycle = LifecycleManager(self.store, self.embedder, self.config)
        local_key = None
        if self.config.attestation_secret:
            local_key = AttestationKey(
                key_id=self.config.attestation_key_id,
                secret=self.config.attestation_secret,
                issuer=self.config.attestation_issuer,
            )
        self.attestor: ProvenanceAttestor | None = (
            ProvenanceAttestor(
                self.config.trusted_attestation_keys,
                local_key=local_key,
                require_signed_parents=self.config.require_signed_parents,
            )
            if self.config.attestation_active
            else None
        )
        self.guard = WriteGuard(
            self.config,
            self.safety,
            self.attestor,
            self.trace,
            self.store,
            embed=self._embed,
        )
        self._write_locks: dict[tuple[str, str], threading.Lock] = {}
        self._write_locks_guard = threading.Lock()
        self._import_lock = threading.Lock()
        self._embedding_cache: OrderedDict[str, list[float]] = OrderedDict()
        self._embedding_cache_guard = threading.Lock()
        self._embedding_cache_size = 4096

    # ------------------------------------------------------------------
    # Tenant boundary
    # ------------------------------------------------------------------
    def _tenant(self, requested: str | None) -> str:
        if requested is not None and requested != self.tenant_id:
            raise TenantIsolationError(
                f"request tenant {requested!r} does not match service tenant "
                f"{self.tenant_id!r}"
            )
        return self.tenant_id

    def assert_tenant(self, tenant_id: str | None) -> str:
        """Validate a request tenant without performing a store operation."""
        return self._tenant(tenant_id)

    def close(self) -> None:
        """Release cached DB connections and provider sockets."""
        self.store.close()
        for component in (self.llm, self.embedder):
            close = getattr(component, "close", None)
            if callable(close):
                close()
        self.trace.close()

    def _lock_for(self, user_id: str) -> threading.Lock:
        key = (self.tenant_id, user_id)
        with self._write_locks_guard:
            lock = self._write_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._write_locks[key] = lock
            return lock

    def _embed(self, content: str) -> list[float]:
        with self._embedding_cache_guard:
            cached = self._embedding_cache.get(content)
            if cached is not None:
                self._embedding_cache.move_to_end(content)
                return cached
        vector = self.embedder.embed([content])[0]
        with self._embedding_cache_guard:
            self._embedding_cache[content] = vector
            self._embedding_cache.move_to_end(content)
            while len(self._embedding_cache) > self._embedding_cache_size:
                self._embedding_cache.popitem(last=False)
        return vector

    def _prefetch_embeddings(self, contents: list[str]) -> None:
        missing = []
        with self._embedding_cache_guard:
            for content in contents:
                if content not in self._embedding_cache:
                    missing.append(content)
        if not missing:
            return
        vectors = self.embedder.embed(missing)
        with self._embedding_cache_guard:
            for content, vector in zip(missing, vectors):
                self._embedding_cache[content] = vector
                self._embedding_cache.move_to_end(content)
            while len(self._embedding_cache) > self._embedding_cache_size:
                self._embedding_cache.popitem(last=False)

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------
    def write(self, request: MemoryWriteRequest) -> MemoryRecord:
        with self._lock_for(request.user_id):
            return self._write_locked(request)

    def _write_locked(self, request: MemoryWriteRequest) -> MemoryRecord:
        tenant_id = self._tenant(request.tenant_id)
        content = _truncate(request.content.strip(), self.config.max_content_chars)
        if not content:
            raise ValueError("content must not be empty")

        if self.config.safety_enabled:
            assessment = self.safety.assess(
                content,
                source_type=request.source_type,
                requested_type=request.type,
                sensitivity_hint=request.sensitivity,
            )
        else:
            assessment = SafetyAssessment(
                allowed=True,
                sensitivity=request.sensitivity or Sensitivity.PERSONAL,
            )
        if not assessment.allowed:
            self.trace.log(
                "write_denied",
                tenant_id=tenant_id,
                user_id=request.user_id,
                reasons=assessment.reasons,
                content_fingerprint=fingerprint(content),
                content_length=len(content),
            )
            raise SafetyViolation("; ".join(assessment.reasons))

        # Redaction and provider embedding happen before the SQLite transaction:
        # an OpenAI embedding call must not hold a database write lock.
        metadata = dict(request.metadata)
        for key in _PROTECTED_METADATA:
            metadata.pop(key, None)
        content = self._apply_sensitivity_policy(
            content,
            assessment,
            confirmed=request.confirmed,
            metadata=metadata,
        )
        embedding = self._embed(content)

        with self.store.transaction() as session:
            lookup = lambda parent_id: session.get(
                parent_id, tenant_id=tenant_id
            )
            if request.idempotency_key:
                prior_id = session.get_idempotent(
                    tenant_id, request.user_id, request.idempotency_key
                )
                if prior_id:
                    prior = session.get(prior_id, tenant_id=tenant_id)
                    if prior is not None:
                        return prior
            if request.id and session.get_any(request.id) is not None:
                raise SafetyViolation("memory_id_exists")

            provenance = self._derive_provenance(
                tenant_id=tenant_id,
                user_id=request.user_id,
                source_type=request.source_type,
                derived_from=request.derived_from,
                request_type=request.type,
                lookup=lookup,
            )
            if provenance.missing:
                self.trace.log(
                    "write_denied",
                    tenant_id=tenant_id,
                    user_id=request.user_id,
                    reasons=["unknown_provenance_parent"],
                    content_fingerprint=fingerprint(content),
                    content_length=len(content),
                )
                raise SafetyViolation("unknown_provenance_parent")
            if provenance.invalid and self.attestor is not None:
                self.trace.log(
                    "write_denied",
                    tenant_id=tenant_id,
                    user_id=request.user_id,
                    reasons=["invalid_parent_attestation"],
                    content_fingerprint=fingerprint(content),
                    content_length=len(content),
                )
                raise AttestationViolation("invalid_parent_attestation")

            metadata["derived_from"] = provenance.derived_from
            metadata["taint"] = provenance.taint
            metadata["provenance_verified"] = provenance.verified

            if self.config.provenance_enabled and request.type in _DURABLE_TYPES:
                reasons = self._provenance_reasons(
                    provenance, request.source_type
                )
                if reasons:
                    self.trace.log(
                        "write_denied",
                        tenant_id=tenant_id,
                        user_id=request.user_id,
                        reasons=reasons,
                        content_fingerprint=fingerprint(content),
                        content_length=len(content),
                    )
                    raise SafetyViolation("; ".join(reasons))

            record = MemoryRecord(
                id=request.id or uuid4().hex,
                tenant_id=tenant_id,
                user_id=request.user_id,
                type=request.type,
                content=content,
                entities=request.entities,
                source_type=request.source_type,
                source=request.source,
                created_at=request.created_at or utcnow(),
                confidence=(
                    request.confidence
                    if request.confidence is not None
                    else self._default_confidence(request.source_type)
                ),
                importance=(
                    request.importance if request.importance is not None else 0.5
                ),
                ttl_seconds=request.ttl_seconds,
                sensitivity=assessment.sensitivity,
                metadata=metadata,
                embedding=embedding,
            )
            record = self._apply_attestation(
                record,
                parents=provenance.parents,
                supplied=request.attestation,
                tenant_id=tenant_id,
                lookup=lookup,
            )

            duplicate = self._find_duplicate(record, store=session)
            if duplicate is not None:
                self._merge_duplicate(duplicate, record, store=session)
                self.trace.log(
                    "write_merged",
                    tenant_id=record.tenant_id,
                    user_id=record.user_id,
                    memory_id=duplicate.id,
                    existing_fingerprint=fingerprint(duplicate.content),
                    new_fingerprint=fingerprint(record.content),
                )
                if request.idempotency_key:
                    session.put_idempotent(
                        tenant_id,
                        request.user_id,
                        request.idempotency_key,
                        duplicate.id,
                    )
                return duplicate

            session.insert(record)
            conflicts = (
                self.lifecycle.on_write(record, store=session)
                if self.config.enable_conflicts
                else []
            )
            session.log_event(
                "write",
                tenant_id=tenant_id,
                actor=request.source or request.source_type.value,
                memory_id=record.id,
                user_id=record.user_id,
                payload={
                    "type": record.type.value,
                    "conflicts": len(conflicts),
                    "attestation": record.metadata.get(
                        "attestation_status", "disabled"
                    ),
                },
            )
            if request.idempotency_key:
                session.put_idempotent(
                    tenant_id,
                    request.user_id,
                    request.idempotency_key,
                    record.id,
                )
        self.trace.log(
            "write",
            tenant_id=tenant_id,
            user_id=record.user_id,
            memory_id=record.id,
            type=record.type.value,
            confidence=record.confidence,
            importance=record.importance,
            conflicts=len(conflicts),
        )
        return record

    def write_text(
        self,
        user_id: str,
        content: str,
        **kwargs: Any,
    ) -> MemoryRecord:
        return self.write(MemoryWriteRequest(user_id=user_id, content=content, **kwargs))

    def extract(self, request: ExtractRequest) -> list[ExtractedMemory]:
        return self.extractor.extract(
            request.text,
            user_id=request.user_id,
            source_type=request.source_type.value,
        )

    def extract_and_write(self, request: ExtractRequest) -> list[MemoryRecord]:
        tenant_id = self._tenant(request.tenant_id)
        candidates = self.extract(request)
        if not request.auto_write:
            return []
        if candidates:
            self._prefetch_embeddings([candidate.content for candidate in candidates])

        records: list[MemoryRecord] = []
        for candidate in candidates:
            try:
                records.append(
                    self._write_extracted(candidate, request, tenant_id=tenant_id)
                )
            except SafetyViolation as exc:
                self.trace.log(
                    "extract_skipped",
                    tenant_id=self.tenant_id,
                    user_id=request.user_id,
                    reason=str(exc),
                    content_fingerprint=fingerprint(candidate.content),
                    content_length=len(candidate.content),
                )
        return records

    def _write_extracted(
        self,
        candidate: ExtractedMemory,
        request: ExtractRequest,
        *,
        tenant_id: str,
    ) -> MemoryRecord:
        return self.write(
            MemoryWriteRequest(
                user_id=request.user_id,
                tenant_id=tenant_id,
                content=candidate.content,
                type=candidate.type,
                source_type=request.source_type,
                source=request.source,
                entities=candidate.entities,
                confidence=candidate.confidence,
                importance=candidate.importance,
                sensitivity=candidate.sensitivity,
                ttl_seconds=candidate.ttl_seconds,
                metadata={
                    "rationale": candidate.rationale or "",
                    "explicit": candidate.explicit,
                },
            )
        )

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------
    def search(self, query: MemoryQuery) -> MemorySearchResult:
        tenant_id = self._tenant(query.tenant_id)
        scoped = query.model_copy(update={"tenant_id": tenant_id})
        result = self.retrieval.search(scoped)
        self.store.log_event(
            "search",
            tenant_id=tenant_id,
            actor="reader",
            user_id=query.user_id,
            payload={
                "query_fingerprint": fingerprint(query.query),
                "query_length": len(query.query),
                "hits": len(result.hits),
                "candidates": result.candidates,
                "tokens": result.total_tokens,
                "truncated": result.truncated,
            },
        )
        self.trace.log(
            "search",
            tenant_id=tenant_id,
            user_id=query.user_id,
            query_fingerprint=fingerprint(query.query),
            query_length=len(query.query),
            hits=len(result.hits),
            candidates=result.candidates,
            tokens=result.total_tokens,
            truncated=result.truncated,
        )
        return result

    def build_context(
        self,
        user_id: str,
        query: str,
        *,
        token_budget: int | None = None,
        top_k: int = 8,
    ) -> str:
        result = self.search(
            MemoryQuery(
                user_id=user_id,
                query=query,
                top_k=top_k,
                token_budget=token_budget or self.config.default_token_budget,
            )
        )
        return result.context

    # ------------------------------------------------------------------
    # Updates / deletes
    # ------------------------------------------------------------------
    def update(self, memory_id: str, request: MemoryUpdateRequest) -> MemoryRecord:
        tenant_id = self._tenant(request.tenant_id)
        record = self.store.get(memory_id, tenant_id=tenant_id)
        if record is None:
            raise MemoryNotFound(memory_id)
        with self._lock_for(record.user_id):
            return self._update_locked(memory_id, request)

    def _update_locked(
        self, memory_id: str, request: MemoryUpdateRequest
    ) -> MemoryRecord:
        tenant_id = self._tenant(request.tenant_id)
        record = self.store.get(memory_id, tenant_id=tenant_id)
        if record is None:
            raise MemoryNotFound(memory_id)

        patch = request.model_dump(exclude_unset=True)
        patch.pop("confirmed", None)
        if not patch:
            return record

        if patch.get("metadata") is not None:
            incoming = dict(patch["metadata"])
            protected = _PROTECTED_METADATA & set(incoming)
            if protected:
                self.trace.log(
                    "update_denied",
                    tenant_id=tenant_id,
                    user_id=record.user_id,
                    memory_id=record.id,
                    reasons=["protected_metadata"],
                )
                raise SafetyViolation("protected_metadata")

        new_type = patch.get("type") or record.type
        if new_type != record.type and new_type in _DURABLE_TYPES:
            self._authorize_durable_promotion(record, new_type)
        if self.config.provenance_enabled and new_type in _DURABLE_TYPES:
            provenance = self._derive_provenance(
                tenant_id=tenant_id,
                user_id=record.user_id,
                source_type=record.source_type,
                derived_from=list(record.metadata.get("derived_from", [])),
                request_type=new_type,
            )
            reasons = self._provenance_reasons(
                provenance, record.source_type
            )
            if reasons:
                self.trace.log(
                    "update_denied",
                    tenant_id=tenant_id,
                    user_id=record.user_id,
                    memory_id=record.id,
                    reasons=reasons,
                )
                raise SafetyViolation("; ".join(reasons))

        metadata = dict(record.metadata)
        if patch.get("metadata") is not None:
            metadata.update(patch["metadata"])

        if "content" in patch and patch["content"] is not None:
            content = _truncate(str(patch["content"]).strip(), self.config.max_content_chars)
            if not content:
                raise ValueError("content must not be empty")
            if self.config.safety_enabled:
                assessment = self.safety.assess(
                    content,
                    source_type=record.source_type,
                    requested_type=new_type,
                    sensitivity_hint=request.sensitivity or record.sensitivity,
                )
            else:
                assessment = SafetyAssessment(
                    allowed=True,
                    sensitivity=request.sensitivity or record.sensitivity,
                )
            if not assessment.allowed:
                self.trace.log(
                    "update_denied",
                    tenant_id=tenant_id,
                    user_id=record.user_id,
                    memory_id=record.id,
                    reasons=assessment.reasons,
                )
                raise SafetyViolation("; ".join(assessment.reasons))
            record.content = self._apply_sensitivity_policy(
                content,
                assessment,
                confirmed=request.confirmed,
                metadata=metadata,
            )
            record.embedding = self._embed(record.content)
            record.sensitivity = _stronger(record.sensitivity, assessment.sensitivity)

        if "summary" in patch:
            record.summary = patch["summary"]
        if "type" in patch and patch["type"] is not None:
            record.type = patch["type"]
        if "entities" in patch and patch["entities"] is not None:
            record.entities = list(patch["entities"])
        if "confidence" in patch and patch["confidence"] is not None:
            record.confidence = float(patch["confidence"])
        if "importance" in patch and patch["importance"] is not None:
            record.importance = float(patch["importance"])
        if "sensitivity" in patch and patch["sensitivity"] is not None:
            record.sensitivity = _stronger(record.sensitivity, patch["sensitivity"])
        if "valid_to" in patch:
            record.valid_to = ensure_utc(patch["valid_to"])
        if "ttl_seconds" in patch:
            record.ttl_seconds = patch["ttl_seconds"]
        record.metadata = metadata

        if self.attestor is not None:
            record = self._reattest_update(record, tenant_id=tenant_id)

        record.updated_at = utcnow()
        with self.store.transaction() as session:
            session.update(record)
            session.log_event(
                "update",
                tenant_id=tenant_id,
                actor="local",
                memory_id=record.id,
                user_id=record.user_id,
                payload={
                    "fields": sorted(patch.keys()),
                    "attestation": record.metadata.get(
                        "attestation_status", "disabled"
                    ),
                },
            )
        self.trace.log(
            "update",
            tenant_id=tenant_id,
            user_id=record.user_id,
            memory_id=record.id,
            fields=sorted(patch.keys()),
        )
        return record

    def _reattest_update(self, record: MemoryRecord, **kwargs: Any) -> MemoryRecord:
        return self.guard.reattest_update(record, **kwargs)

    def delete(self, memory_id: str) -> None:
        record = self.store.get(memory_id, tenant_id=self.tenant_id)
        if record is None:
            raise MemoryNotFound(memory_id)
        with self._lock_for(record.user_id):
            with self.store.transaction() as session:
                session.delete(memory_id, tenant_id=self.tenant_id)
                session.log_event(
                    "delete",
                    tenant_id=self.tenant_id,
                    actor="local",
                    memory_id=memory_id,
                    user_id=record.user_id,
                )
            self.trace.log(
                "delete",
                tenant_id=self.tenant_id,
                user_id=record.user_id,
                memory_id=memory_id,
            )

    def forget(
        self,
        user_id: str,
        *,
        memory_type: MemoryType | None = None,
        before: datetime | None = None,
    ) -> int:
        with self.store.transaction() as session:
            deleted = session.forget(
                user_id,
                tenant_id=self.tenant_id,
                memory_type=memory_type,
                before=before,
            )
            session.log_event(
                "forget",
                tenant_id=self.tenant_id,
                actor="local",
                user_id=user_id,
                payload={
                    "deleted": deleted,
                    "type": memory_type.value if memory_type else None,
                },
            )
        self.trace.log("forget", tenant_id=self.tenant_id, user_id=user_id, deleted=deleted)
        return deleted

    # ------------------------------------------------------------------
    # Maintenance / stats
    # ------------------------------------------------------------------
    def maintenance(self, *, prune: bool = False) -> dict[str, int]:
        result = self.lifecycle.run_maintenance(
            tenant_id=self.tenant_id, prune=prune
        )
        self.store.log_event(
            "maintenance",
            tenant_id=self.tenant_id,
            actor="local",
            payload=result,
        )
        self.trace.log("maintenance", tenant_id=self.tenant_id, **result)
        return result

    def stats(self, user_id: str | None = None) -> dict[str, Any]:
        return self.store.stats(user_id, tenant_id=self.tenant_id)

    def conflicts(
        self,
        *,
        user_id: str | None = None,
        status: str | None = None,
    ) -> list[ConflictRecord]:
        return self.store.list_conflicts(
            tenant_id=self.tenant_id, user_id=user_id, status=status
        )

    def resolve_conflict(self, conflict_id: str, strategy: str) -> ConflictRecord | None:
        conflict = self.lifecycle.resolve_conflict(
            conflict_id, strategy, tenant_id=self.tenant_id
        )
        self.store.log_event(
            "resolve_conflict",
            tenant_id=self.tenant_id,
            actor="local",
            payload={"conflict_id": conflict_id, "strategy": strategy},
        )
        self.trace.log(
            "resolve_conflict",
            tenant_id=self.tenant_id,
            conflict_id=conflict_id,
            strategy=strategy,
            resolved=conflict is not None,
        )
        return conflict

    def export(self, user_id: str | None = None) -> list[dict[str, Any]]:
        return self.store.export(user_id, tenant_id=self.tenant_id)

    def import_records(self, records: list[dict[str, Any]]) -> int:
        with self._import_lock:
            return self._import_records_locked(records)

    def _import_records_locked(self, records: list[dict[str, Any]]) -> int:
        tenant_id = self.tenant_id
        batch: dict[str, MemoryRecord] = {}
        for item in records:
            record = MemoryRecord.model_validate(item)
            if record.tenant_id not in {tenant_id, "default"}:
                raise TenantIsolationError(
                    f"import record {record.id!r} belongs to another tenant"
                )
            record.tenant_id = tenant_id
            if record.id in batch:
                raise ValueError(f"duplicate import id: {record.id}")
            batch[record.id] = record

        with self.store.transaction() as session:
            def lookup(parent_id: str) -> MemoryRecord | None:
                return batch.get(parent_id) or session.get(
                    parent_id, tenant_id=tenant_id
                )

            prepared: list[MemoryRecord] = []
            for record in batch.values():
                existing = session.get_any(record.id)
                if existing is not None and (
                    existing.tenant_id != tenant_id
                    or existing.user_id != record.user_id
                ):
                    self.trace.log(
                        "import_denied",
                        tenant_id=tenant_id,
                        memory_id=record.id,
                        reason="cross_tenant_or_user_overwrite",
                    )
                    raise SafetyViolation("import_cross_user_overwrite")
                self._authorize_imported(record, lookup=lookup)
                prepared.append(record)

            for record in prepared:
                session.upsert(record)
            session.log_event(
                "import",
                tenant_id=tenant_id,
                actor="admin",
                payload={"count": len(prepared)},
            )
        self.trace.log("import", tenant_id=tenant_id, count=len(prepared))
        return len(prepared)

    def recent_events(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.store.recent_events(limit, tenant_id=self.tenant_id)

    def audit_head(self) -> dict[str, Any]:
        return self.store.audit_head(self.tenant_id)

    def verify_audit(self) -> dict[str, Any]:
        return self.store.verify_audit_chain(self.tenant_id).to_dict()

    def verify_record_attestation(
        self, memory_id: str
    ) -> dict[str, Any] | None:
        record = self.store.get(memory_id, tenant_id=self.tenant_id)
        if record is None:
            return None
        if self.attestor is None:
            return {"status": "disabled", "reasons": []}
        verdict = self.attestor.verify(
            record,
            lambda parent_id: self.store.get(
                parent_id, tenant_id=self.tenant_id
            ),
        )
        return verdict.to_dict()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _derive_provenance(self, **kwargs: Any) -> Any:
        return self.guard.derive_provenance(**kwargs)

    def _provenance_reasons(self, provenance: Any, source_type: SourceType) -> list[str]:
        return self.guard.provenance_reasons(provenance, source_type)

    def _apply_attestation(self, record: MemoryRecord, **kwargs: Any) -> MemoryRecord:
        return self.guard.apply_attestation(record, **kwargs)

    def _sign_attestation(self, record: MemoryRecord, parents: list[MemoryRecord]) -> dict[str, Any]:
        return self.guard.sign_attestation(record, parents)

    def _verify_attestation(self, record: MemoryRecord, **kwargs: Any) -> Any:
        return self.guard.verify_attestation(record, **kwargs)

    def _apply_sensitivity_policy(self, content: str, assessment: SafetyAssessment, **kwargs: Any) -> str:
        return self.guard.apply_sensitivity(content, assessment, **kwargs)

    def _authorize_durable_promotion(self, record: MemoryRecord, new_type: MemoryType) -> None:
        self.guard.authorize_durable_promotion(record, new_type)

    def _authorize_imported(self, record: MemoryRecord, **kwargs: Any) -> None:
        self.guard.authorize_imported(record, **kwargs)

    def _find_duplicate(
        self, record: MemoryRecord, *, store: Any = None
    ) -> MemoryRecord | None:
        target = store or self.store
        exact = target.find_by_content_hash(
            record.user_id,
            content_hash(record.content),
            tenant_id=record.tenant_id,
            memory_type=record.type,
        )
        if exact is not None and exact.id != record.id:
            return exact

        tokens = tokenize(
            record.content + " " + " ".join(map(str, record.entities))
        )
        candidates = target.find_by_tokens(
            record.user_id,
            tokens,
            tenant_id=record.tenant_id,
            limit=64,
        )
        if not candidates:
            candidates = target.search_candidates(
                record.user_id,
                tenant_id=record.tenant_id,
                types=[record.type],
                include_expired=False,
                limit=64,
            )
        normalized = normalize_content(record.content)
        best: tuple[float, MemoryRecord] | None = None
        for candidate in candidates:
            if candidate.id == record.id:
                continue
            if candidate.type != record.type:
                continue
            if normalize_content(candidate.content) == normalized:
                return candidate
            if candidate.embedding and record.embedding:
                similarity = cosine_similarity(candidate.embedding, record.embedding)
                if similarity >= self.config.dedupe_threshold:
                    if self.lifecycle.detector.detect(candidate, record) is not None:
                        continue
                    if best is None or similarity > best[0]:
                        best = (similarity, candidate)
        return best[1] if best else None

    def _merge_duplicate(
        self, existing: MemoryRecord, new: MemoryRecord, *, store: Any = None
    ) -> None:
        target = store or self.store
        existing.confidence = max(existing.confidence, new.confidence)
        existing.importance = max(existing.importance, new.importance)
        existing.entities = list(
            dict.fromkeys([*existing.entities, *new.entities])
        )
        existing.last_accessed = utcnow()
        existing.access_count += 1
        existing.updated_at = utcnow()
        merge_count = int(existing.metadata.get("merge_count", 0)) + 1
        existing.metadata["merge_count"] = merge_count
        target.update(existing)
        target.log_event(
            "merge",
            tenant_id=existing.tenant_id,
            actor=new.source or new.source_type.value,
            memory_id=existing.id,
            user_id=existing.user_id,
            payload={"incoming_fingerprint": fingerprint(new.content)},
        )

    def _default_confidence(self, source_type: SourceType) -> float:
        if source_type == SourceType.USER:
            return 0.85
        if source_type == SourceType.SYSTEM:
            return 0.90
        if source_type == SourceType.AGENT:
            return 0.70
        return 0.50


def _truncate(text: str, max_chars: int) -> str:
    return text[:max_chars].strip() if len(text) > max_chars else text

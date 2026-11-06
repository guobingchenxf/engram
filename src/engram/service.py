"""High-level memory service.

This is the only object most callers need: it wires storage, embeddings,
extraction, retrieval, lifecycle and safety together, and emits a trace for
every state change.
"""

from __future__ import annotations

import re
from datetime import datetime
from typing import Any

from .config import MemoryConfig
from .embeddings import EmbeddingProvider, cosine_similarity, get_embedding_provider
from .errors import MemoryNotFound, SafetyViolation
from .extraction import MemoryExtractor
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
from .retrieval import RetrievalService
from .safety import SafetyGuard
from .storage import SQLiteMemoryStore
from .trace import TraceLogger


class MemoryService:
    def __init__(self, config: MemoryConfig | None = None) -> None:
        self.config = config or MemoryConfig.from_env()
        self.store = SQLiteMemoryStore(self.config.db_path)
        self.embedder: EmbeddingProvider = get_embedding_provider(self.config)
        self.llm: LLMClient | None = get_llm(self.config)
        self.trace = TraceLogger(self.config.trace_path)
        self.safety = SafetyGuard(self.config)
        self.extractor = MemoryExtractor(self.llm, self.config)
        self.retrieval = RetrievalService(self.store, self.embedder, self.config)
        self.lifecycle = LifecycleManager(self.store, self.embedder, self.config)

    # ------------------------------------------------------------------
    # Writes
    # ------------------------------------------------------------------
    def write(self, request: MemoryWriteRequest) -> MemoryRecord:
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
                user_id=request.user_id,
                reasons=assessment.reasons,
                content=content[:200],
            )
            raise SafetyViolation("; ".join(assessment.reasons))

        metadata = dict(request.metadata)
        if assessment.requires_confirmation:
            metadata.setdefault("requires_confirmation", True)

        derived_from = list(dict.fromkeys(request.derived_from))
        parents = [
            parent
            for parent_id in derived_from
            if (parent := self.store.get(parent_id)) is not None
        ]
        taint: set[str] = set()
        for parent in parents:
            taint.update(str(item) for item in parent.metadata.get("taint", []))
            if not parent.source_type.is_trusted:
                taint.add(parent.source_type.value)
        if not request.source_type.is_trusted:
            taint.add(request.source_type.value)

        unverified_agent = (
            request.source_type == SourceType.AGENT
            and request.type in {MemoryType.SEMANTIC, MemoryType.PROCEDURAL}
            and not derived_from
        )
        metadata["derived_from"] = derived_from
        metadata["taint"] = sorted(taint)
        metadata["provenance_verified"] = (
            request.source_type in {SourceType.USER, SourceType.SYSTEM}
            or (
                request.source_type == SourceType.AGENT
                and bool(derived_from)
                and not taint
            )
        )

        if self.config.provenance_enabled and request.type in {
            MemoryType.SEMANTIC,
            MemoryType.PROCEDURAL,
        }:
            reasons: list[str] = []
            if not request.source_type.is_trusted:
                reasons.append("untrusted_source")
            if unverified_agent:
                reasons.append("agent_memory_without_provenance")
            if taint:
                reasons.append("tainted_provenance")
            if reasons:
                self.trace.log(
                    "write_denied",
                    user_id=request.user_id,
                    reasons=reasons,
                    content=content[:200],
                )
                raise SafetyViolation("; ".join(reasons))

        record = MemoryRecord(
            user_id=request.user_id,
            type=request.type,
            content=content,
            entities=request.entities,
            source_type=request.source_type,
            source=request.source,
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
            embedding=self.embedder.embed([content])[0],
        )

        duplicate = self._find_duplicate(record)
        if duplicate is not None:
            self._merge_duplicate(duplicate, record)
            self.trace.log(
                "write_merged",
                user_id=record.user_id,
                memory_id=duplicate.id,
                existing_content=duplicate.content[:200],
                new_content=record.content[:200],
            )
            return duplicate

        self.store.insert(record)
        conflicts = self.lifecycle.on_write(record) if self.config.enable_conflicts else []
        self.store.log_event(
            "write",
            memory_id=record.id,
            user_id=record.user_id,
            payload={"type": record.type.value, "conflicts": len(conflicts)},
        )
        self.trace.log(
            "write",
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
        candidates = self.extract(request)
        if not request.auto_write:
            return []

        records: list[MemoryRecord] = []
        for candidate in candidates:
            try:
                records.append(
                    self._write_extracted(candidate, request)
                )
            except SafetyViolation as exc:
                self.trace.log(
                    "extract_skipped",
                    user_id=request.user_id,
                    reason=str(exc),
                    content=candidate.content[:200],
                )
        return records

    def _write_extracted(
        self,
        candidate: ExtractedMemory,
        request: ExtractRequest,
    ) -> MemoryRecord:
        return self.write(
            MemoryWriteRequest(
                user_id=request.user_id,
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
        result = self.retrieval.search(query)
        self.store.log_event(
            "search",
            user_id=query.user_id,
            payload={
                "query": query.query,
                "hits": len(result.hits),
                "candidates": result.candidates,
                "tokens": result.total_tokens,
            },
        )
        self.trace.log(
            "search",
            user_id=query.user_id,
            query=query.query,
            hits=len(result.hits),
            candidates=result.candidates,
            tokens=result.total_tokens,
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
        record = self.store.get(memory_id)
        if record is None:
            raise MemoryNotFound(memory_id)

        patch = request.model_dump(exclude_unset=True)
        if not patch:
            return record

        if "content" in patch and patch["content"] is not None:
            content = _truncate(str(patch["content"]).strip(), self.config.max_content_chars)
            if not content:
                raise ValueError("content must not be empty")
            if self.config.safety_enabled:
                assessment = self.safety.assess(
                    content,
                    source_type=record.source_type,
                    requested_type=patch.get("type") or record.type,
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
                    user_id=record.user_id,
                    memory_id=record.id,
                    reasons=assessment.reasons,
                )
                raise SafetyViolation("; ".join(assessment.reasons))
            record.content = content
            record.embedding = self.embedder.embed([content])[0]
            record.sensitivity = assessment.sensitivity

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
            record.sensitivity = patch["sensitivity"]
        if "valid_to" in patch:
            record.valid_to = ensure_utc(patch["valid_to"])
        if "ttl_seconds" in patch:
            record.ttl_seconds = patch["ttl_seconds"]
        if "metadata" in patch and patch["metadata"] is not None:
            record.metadata = dict(patch["metadata"])

        record.updated_at = utcnow()
        self.store.update(record)
        self.store.log_event(
            "update",
            memory_id=record.id,
            user_id=record.user_id,
            payload={"fields": sorted(patch.keys())},
        )
        self.trace.log(
            "update",
            user_id=record.user_id,
            memory_id=record.id,
            fields=sorted(patch.keys()),
        )
        return record

    def delete(self, memory_id: str) -> None:
        record = self.store.get(memory_id)
        if record is None:
            raise MemoryNotFound(memory_id)
        self.store.delete(memory_id)
        self.store.log_event("delete", memory_id=memory_id, user_id=record.user_id)
        self.trace.log("delete", user_id=record.user_id, memory_id=memory_id)

    def forget(
        self,
        user_id: str,
        *,
        memory_type: MemoryType | None = None,
        before: datetime | None = None,
    ) -> int:
        types = [memory_type] if memory_type else None
        records = self.store.list_memories(
            user_id, types=types, include_expired=True, limit=10000
        )
        cutoff = ensure_utc(before)
        deleted = 0
        for record in records:
            if cutoff is not None:
                created = ensure_utc(record.created_at)
                if created is None or created >= cutoff:
                    continue
            if self.store.delete(record.id):
                deleted += 1
        self.store.log_event(
            "forget",
            user_id=user_id,
            payload={"deleted": deleted, "type": memory_type.value if memory_type else None},
        )
        self.trace.log("forget", user_id=user_id, deleted=deleted)
        return deleted

    # ------------------------------------------------------------------
    # Maintenance / stats
    # ------------------------------------------------------------------
    def maintenance(self, *, prune: bool = False) -> dict[str, int]:
        result = self.lifecycle.run_maintenance(prune=prune)
        self.store.log_event("maintenance", payload=result)
        self.trace.log("maintenance", **result)
        return result

    def stats(self, user_id: str | None = None) -> dict[str, Any]:
        return self.store.stats(user_id)

    def conflicts(
        self,
        *,
        user_id: str | None = None,
        status: str | None = None,
    ) -> list[ConflictRecord]:
        return self.store.list_conflicts(user_id=user_id, status=status)

    def resolve_conflict(self, conflict_id: str, strategy: str) -> ConflictRecord | None:
        conflict = self.lifecycle.resolve_conflict(conflict_id, strategy)
        self.trace.log(
            "resolve_conflict",
            conflict_id=conflict_id,
            strategy=strategy,
            resolved=conflict is not None,
        )
        return conflict

    def export(self, user_id: str | None = None) -> list[dict[str, Any]]:
        return self.store.export(user_id)

    def import_records(self, records: list[dict[str, Any]]) -> int:
        count = self.store.import_records(records)
        self.trace.log("import", count=count)
        return count

    def recent_events(self, limit: int = 100) -> list[dict[str, Any]]:
        return self.store.recent_events(limit)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _find_duplicate(self, record: MemoryRecord) -> MemoryRecord | None:
        candidates = self.store.search_candidates(
            record.user_id,
            types=[record.type],
            include_expired=False,
            limit=250,
        )
        normalized = _normalize(record.content)
        best: tuple[float, MemoryRecord] | None = None

        for candidate in candidates:
            if candidate.id == record.id:
                continue
            if _normalize(candidate.content) == normalized:
                return candidate
            if candidate.embedding and record.embedding:
                similarity = cosine_similarity(candidate.embedding, record.embedding)
                if similarity >= self.config.dedupe_threshold:
                    # A near-duplicate that contradicts or explicitly changes the
                    # old memory must go through the lifecycle, not be merged away.
                    if self.lifecycle.detector.detect(candidate, record) is not None:
                        continue
                    if best is None or similarity > best[0]:
                        best = (similarity, candidate)
        return best[1] if best else None

    def _merge_duplicate(self, existing: MemoryRecord, new: MemoryRecord) -> None:
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
        self.store.update(existing)
        self.store.log_event(
            "merge",
            memory_id=existing.id,
            user_id=existing.user_id,
            payload={"incoming": new.content[:200]},
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


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()

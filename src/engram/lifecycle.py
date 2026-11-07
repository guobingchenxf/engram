"""Memory lifecycle: conflict detection, supersede, decay and expiry."""

from __future__ import annotations

import math
import re
from datetime import datetime
from typing import Any

from .config import MemoryConfig
from .embeddings import EmbeddingProvider, cosine_similarity
from .models import (
    ConflictRecord,
    MemoryRecord,
    ensure_utc,
    utcnow,
)
from .storage import SQLiteMemoryStore
from .text import tokenize

_CHANGE_MARKERS = re.compile(
    r"(现在|如今|改成|改为|改用|换成|更新|提升到|升到|降到|不再|换了|改主意|"
    r"instead|now|changed|no longer|switch(ed)? to)",
    re.IGNORECASE,
)
_NEGATIONS = re.compile(r"(不|没|别|never|not|no longer|don't|do not)", re.IGNORECASE)
_VALUE_PATTERNS = (
    re.compile(r"(?:目标|goal)\s*(?:是|为|is|:)\s*(.+)", re.IGNORECASE),
    re.compile(r"(?:偏好|prefer|like)\s*[:：]?\s*(.+)", re.IGNORECASE),
    re.compile(r"(?:决定|结论|decided|decision)\s*(?:是|为|is|:)\s*(.+)", re.IGNORECASE),
)


class ConflictDetector:
    def __init__(self, embedder: EmbeddingProvider, config: MemoryConfig) -> None:
        self.embedder = embedder
        self.config = config

    def detect(self, existing: MemoryRecord, new: MemoryRecord) -> str | None:
        if existing.id == new.id or existing.user_id != new.user_id:
            return None
        if existing.type != new.type or not existing.is_active():
            return None

        if existing.content.strip().lower() == new.content.strip().lower():
            return None  # exact duplicate, not a conflict

        existing_entities = set(existing.entities)
        new_entities = set(new.entities)
        shared_entities = existing_entities & new_entities

        # "My language preference is Chinese" and "I prefer Sichuan food" are
        # different attributes, not a contradiction.  Attribute/entity overlap
        # is the evidence that two statements talk about the same fact; raw
        # lexical similarity is not enough.
        same_attribute = _comparable_attributes(existing.content, new.content)
        subject_known = bool(shared_entities) or same_attribute

        # A change marker only means "this is a correction" when we know both
        # statements are about the same subject.  "现在" alone must not
        # supersede an unrelated memory.
        if _CHANGE_MARKERS.search(new.content) and subject_known:
            return "explicit_change"

        # "Project Alpha is due Nov 15" and "Project Beta is due Dec 1" are
        # different subjects, not a contradiction.  This guard applies to
        # ordinary contradictions; an explicit change about the same attribute
        # (e.g. MMoE -> DIN) is handled above.
        if existing_entities and new_entities and not shared_entities:
            return None

        if not subject_known:
            return None

        existing_negative = bool(_NEGATIONS.search(existing.content))
        new_negative = bool(_NEGATIONS.search(new.content))
        if existing_negative != new_negative:
            return "negation_mismatch"

        existing_numbers = _numbers(existing.content)
        new_numbers = _numbers(new.content)
        if existing_numbers and new_numbers and existing_numbers != new_numbers:
            return "numeric_conflict"

        existing_value = _extract_value(existing.content)
        new_value = _extract_value(new.content)
        if existing_value and new_value and existing_value != new_value:
            return "value_conflict"

        return None

    def _similarity(self, existing: MemoryRecord, new: MemoryRecord) -> float:
        if existing.embedding and new.embedding:
            return cosine_similarity(existing.embedding, new.embedding)
        return _jaccard(existing.content, new.content)


class LifecycleManager:
    def __init__(
        self,
        store: SQLiteMemoryStore,
        embedder: EmbeddingProvider,
        config: MemoryConfig,
    ) -> None:
        self.store = store
        self.config = config
        self.detector = ConflictDetector(embedder, config)

    def on_write(
        self, new_record: MemoryRecord, *, store: Any = None
    ) -> list[ConflictRecord]:
        """Detect conflicts for a newly inserted record and supersede when safe."""
        target = store or self.store
        tokens = tokenize(
            new_record.content + " " + " ".join(map(str, new_record.entities))
        )
        merged: dict[str, MemoryRecord] = {}
        if tokens:
            for record in target.find_by_tokens(
                new_record.user_id,
                tokens,
                tenant_id=new_record.tenant_id,
                limit=64,
                shared_only=True,
            ):
                merged[record.id] = record
        if new_record.entities:
            for record in target.find_by_entities(
                new_record.user_id,
                new_record.entities,
                tenant_id=new_record.tenant_id,
                limit=100,
            ):
                merged.setdefault(record.id, record)
        merged.pop(new_record.id, None)
        if not merged:
            for record in target.search_candidates(
                new_record.user_id,
                tenant_id=new_record.tenant_id,
                types=[new_record.type],
                include_expired=False,
                limit=64,
            ):
                merged[record.id] = record

        conflicts: list[ConflictRecord] = []
        for existing in merged.values():
            if existing.id == new_record.id or existing.type != new_record.type:
                continue
            reason = self.detector.detect(existing, new_record)
            if reason is None:
                continue

            should_supersede = (
                new_record.source_type.is_trusted
                and (
                    new_record.confidence >= existing.confidence
                    or reason == "explicit_change"
                )
            )
            if should_supersede:
                existing.valid_to = utcnow()
                existing.superseded_by = new_record.id
                target.update(existing, sync_indexes=False)
                if existing.id not in new_record.supersedes:
                    new_record.supersedes.append(existing.id)

            conflict = ConflictRecord(
                user_id=new_record.user_id,
                tenant_id=new_record.tenant_id,
                existing_id=existing.id,
                new_id=new_record.id,
                reason=reason,
            )
            target.add_conflict(conflict)
            conflicts.append(conflict)

        if new_record.supersedes:
            target.update(new_record, sync_indexes=False)
        return conflicts

    def expire(
        self, now: datetime | None = None, *, tenant_id: str = "default"
    ) -> int:
        with self.store.transaction() as session:
            return self._expire(session, now, tenant_id=tenant_id)

    def _expire(
        self,
        session: Any,
        now: datetime | None,
        *,
        tenant_id: str,
    ) -> int:
        current = ensure_utc(now) or utcnow()
        count = 0
        for record in self._all_records(tenant_id, store=session):
            if record.valid_to is None and record.is_expired(current):
                record.valid_to = current
                session.update(record, sync_indexes=False)
                count += 1
        return count

    def decay(
        self, now: datetime | None = None, *, tenant_id: str = "default"
    ) -> int:
        with self.store.transaction() as session:
            return self._decay(session, now, tenant_id=tenant_id)

    def _decay(
        self,
        session: Any,
        now: datetime | None,
        *,
        tenant_id: str,
    ) -> int:
        current = ensure_utc(now) or utcnow()
        count = 0
        for record in self._all_records(tenant_id, store=session):
            if not record.is_active(current):
                continue
            reference = (
                ensure_utc(record.metadata.get("last_decayed_at"))
                or ensure_utc(record.last_accessed)
                or ensure_utc(record.created_at)
                or current
            )
            age_days = max(0.0, (current - reference).total_seconds() / 86400.0)
            decayed = record.importance * math.exp(
                -self.config.decay_lambda * age_days
            )
            pruned = decayed < self.config.prune_importance_threshold
            changed = abs(decayed - record.importance) > 1e-6
            if not changed and not pruned:
                continue
            record.metadata["last_decayed_at"] = current.isoformat()
            if pruned:
                record.valid_to = current
                record.metadata["pruned"] = True
                count += 1
            record.importance = max(0.0, min(1.0, decayed))
            session.update(record, sync_indexes=False)
        return count

    def prune(
        self, now: datetime | None = None, *, tenant_id: str = "default"
    ) -> int:
        with self.store.transaction() as session:
            return self._prune(session, now, tenant_id=tenant_id)

    def _prune(
        self,
        session: Any,
        now: datetime | None,
        *,
        tenant_id: str,
    ) -> int:
        current = ensure_utc(now) or utcnow()
        removed = 0
        for record in self._all_records(tenant_id, store=session):
            expired = record.valid_to is not None and record.valid_to <= current
            prunable = (
                record.metadata.get("pruned")
                or record.importance < self.config.prune_importance_threshold
            )
            if expired and prunable and session.delete(
                record.id, tenant_id=tenant_id
            ):
                removed += 1
        return removed

    def run_maintenance(
        self,
        now: datetime | None = None,
        *,
        tenant_id: str = "default",
        prune: bool = False,
    ) -> dict[str, int]:
        with self.store.transaction() as session:
            expired = self._expire(session, now, tenant_id=tenant_id)
            decayed = self._decay(session, now, tenant_id=tenant_id)
            removed = (
                self._prune(session, now, tenant_id=tenant_id) if prune else 0
            )
        return {"expired": expired, "decayed": decayed, "pruned": removed}

    def resolve_conflict(
        self, conflict_id: str, strategy: str, *, tenant_id: str = "default"
    ) -> ConflictRecord | None:
        conflict = self.store.get_conflict(conflict_id, tenant_id=tenant_id)
        if conflict is None:
            return None

        existing = self.store.get(conflict.existing_id, tenant_id=tenant_id)
        new = self.store.get(conflict.new_id, tenant_id=tenant_id)
        now = utcnow()

        if strategy == "keep_new" and existing and new:
            if new.superseded_by == existing.id:
                _undo_supersede(new, existing.id)
            existing.valid_to = now
            existing.superseded_by = new.id
            if existing.id not in new.supersedes:
                new.supersedes.append(existing.id)
            self.store.update(existing)
            self.store.update(new)
        elif strategy == "keep_old" and existing and new:
            _undo_supersede(existing, new.id)
            new.valid_to = now
            new.superseded_by = existing.id
            new.supersedes = [item for item in new.supersedes if item != existing.id]
            self.store.update(existing)
            self.store.update(new)
        elif strategy == "keep_both" and existing and new:
            _undo_supersede(existing, new.id)
            new.supersedes = [item for item in new.supersedes if item != existing.id]
            self.store.update(existing)
            self.store.update(new)
        elif strategy != "keep_both":
            raise ValueError(f"unknown strategy: {strategy}")

        return self.store.resolve_conflict(
            conflict_id, strategy, tenant_id=tenant_id
        )

    def _all_records(
        self, tenant_id: str, *, store: Any = None
    ) -> list[MemoryRecord]:
        target = store or self.store
        return [
            MemoryRecord.model_validate(item)
            for item in target.export(tenant_id=tenant_id)
        ]


def _undo_supersede(record: MemoryRecord, by_id: str) -> None:
    """Revive a record only when this conflict caused its supersession."""
    if record.superseded_by == by_id:
        record.superseded_by = None
        record.valid_to = None


def _jaccard(left: str, right: str) -> float:
    left_tokens = set(re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]", left.lower()))
    right_tokens = set(re.findall(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]", right.lower()))
    if not left_tokens or not right_tokens:
        return 0.0
    return len(left_tokens & right_tokens) / len(left_tokens | right_tokens)


def _extract_value(text: str) -> str | None:
    for pattern in _VALUE_PATTERNS:
        match = pattern.search(text)
        if match:
            value = match.group(1).strip("。；;，, ")
            if value:
                return value[:120]
    return None


def _numbers(text: str) -> set[str]:
    return set(re.findall(r"\d+(?:\.\d+)?", text))


_ATTRIBUTE_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("语言", re.compile(r"语言")),
    ("目标", re.compile(r"目标|goal", re.IGNORECASE)),
    ("薪资", re.compile(r"薪资|工资|salary", re.IGNORECASE)),
    ("时间", re.compile(r"每周|每天|每月|weekly|daily", re.IGNORECASE)),
    ("项目", re.compile(r"项目|project", re.IGNORECASE)),
    ("AUC", re.compile(r"\bAUC\b", re.IGNORECASE)),
    ("参数量", re.compile(r"参数量")),
    ("延迟", re.compile(r"延迟|latency", re.IGNORECASE)),
    ("模型", re.compile(r"模型")),
    ("偏好域", re.compile(r"中文|英文|日文|解释")),
    ("地点", re.compile(r"城市|地点|住址|实习|杭州|上海|北京|深圳")),
    (
        "工具",
        re.compile(
            r"\bPython\b|\bGo\b|\bJava\b|\bRust\b|\bC\+\+\b|FastAPI|VS Code|"
            r"编辑器|工具|做实验|做服务",
            re.IGNORECASE,
        ),
    ),
)


def _attribute(text: str) -> str | None:
    for name, pattern in _ATTRIBUTE_PATTERNS:
        if pattern.search(text):
            return name
    return None


def _comparable_attributes(left: str, right: str) -> bool:
    left_attribute = _attribute(left)
    right_attribute = _attribute(right)
    return bool(left_attribute and right_attribute and left_attribute == right_attribute)

"""Memory lifecycle: conflict detection, supersede, decay and expiry."""

from __future__ import annotations

import math
import re
from datetime import datetime

from .config import MemoryConfig
from .embeddings import EmbeddingProvider, cosine_similarity
from .models import (
    ConflictRecord,
    MemoryRecord,
    ensure_utc,
    utcnow,
)
from .storage import SQLiteMemoryStore

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

        similarity = self._similarity(existing, new)
        existing_entities = set(existing.entities)
        new_entities = set(new.entities)
        shared_entities = existing_entities & new_entities

        if _CHANGE_MARKERS.search(new.content) and (similarity >= 0.20 or shared_entities):
            return "explicit_change"

        # "Project Alpha is due Nov 15" and "Project Beta is due Dec 1" are
        # different subjects, not a contradiction. Only treat two facts as
        # comparable when they share a subject/entity.
        if existing_entities and new_entities and not shared_entities:
            return None

        # "My language preference is Chinese" and "I prefer Sichuan food" are
        # different attributes, not a contradiction.
        if not _comparable_attributes(existing.content, new.content):
            return None

        existing_negative = bool(_NEGATIONS.search(existing.content))
        new_negative = bool(_NEGATIONS.search(new.content))
        if existing_negative != new_negative and (similarity >= 0.40 or shared_entities):
            return "negation_mismatch"

        existing_numbers = _numbers(existing.content)
        new_numbers = _numbers(new.content)
        if (
            existing_numbers
            and new_numbers
            and existing_numbers != new_numbers
            and (similarity >= 0.25 or shared_entities)
        ):
            return "numeric_conflict"

        existing_value = _extract_value(existing.content)
        new_value = _extract_value(new.content)
        if (
            existing_value
            and new_value
            and existing_value != new_value
            and (similarity >= 0.30 or shared_entities)
        ):
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

    def on_write(self, new_record: MemoryRecord) -> list[ConflictRecord]:
        """Detect conflicts for a newly inserted record and supersede when safe."""
        candidates = self.store.search_candidates(
            new_record.user_id,
            types=[new_record.type],
            include_expired=False,
            limit=250,
        )
        conflicts: list[ConflictRecord] = []

        for existing in candidates:
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
                self.store.update(existing)
                if existing.id not in new_record.supersedes:
                    new_record.supersedes.append(existing.id)

            conflict = ConflictRecord(
                user_id=new_record.user_id,
                existing_id=existing.id,
                new_id=new_record.id,
                reason=reason,
            )
            self.store.add_conflict(conflict)
            conflicts.append(conflict)

        if new_record.supersedes:
            self.store.update(new_record)
        return conflicts

    def expire(self, now: datetime | None = None) -> int:
        current = ensure_utc(now) or utcnow()
        count = 0
        for record in self._all_records():
            if record.valid_to is None and record.is_expired(current):
                record.valid_to = current
                self.store.update(record)
                count += 1
        return count

    def decay(self, now: datetime | None = None) -> int:
        current = ensure_utc(now) or utcnow()
        count = 0
        for record in self._all_records():
            if not record.is_active(current):
                continue
            reference = ensure_utc(record.last_accessed) or ensure_utc(record.created_at) or current
            age_days = max(0.0, (current - reference).total_seconds() / 86400.0)
            decayed = record.importance * math.exp(-self.config.decay_lambda * age_days)
            if decayed < self.config.prune_importance_threshold:
                record.valid_to = current
                record.metadata["pruned"] = True
                count += 1
            if abs(decayed - record.importance) > 1e-6:
                record.importance = max(0.0, min(1.0, decayed))
                self.store.update(record)
        return count

    def prune(self, now: datetime | None = None) -> int:
        current = ensure_utc(now) or utcnow()
        removed = 0
        for record in self._all_records():
            expired = record.valid_to is not None and record.valid_to <= current
            prunable = (
                record.metadata.get("pruned")
                or record.importance < self.config.prune_importance_threshold
            )
            if expired and prunable and self.store.delete(record.id):
                removed += 1
        return removed

    def run_maintenance(
        self,
        now: datetime | None = None,
        *,
        prune: bool = False,
    ) -> dict[str, int]:
        expired = self.expire(now)
        decayed = self.decay(now)
        removed = self.prune(now) if prune else 0
        return {"expired": expired, "decayed": decayed, "pruned": removed}

    def resolve_conflict(self, conflict_id: str, strategy: str) -> ConflictRecord | None:
        conflict = self.store.get_conflict(conflict_id)
        if conflict is None:
            return None

        existing = self.store.get(conflict.existing_id)
        new = self.store.get(conflict.new_id)
        now = utcnow()

        if strategy == "keep_new" and existing and new:
            existing.valid_to = now
            existing.superseded_by = new.id
            if existing.id not in new.supersedes:
                new.supersedes.append(existing.id)
            self.store.update(existing)
            self.store.update(new)
        elif strategy == "keep_old" and existing and new:
            new.valid_to = now
            new.superseded_by = existing.id
            self.store.update(new)
        elif strategy != "keep_both":
            raise ValueError(f"unknown strategy: {strategy}")

        return self.store.resolve_conflict(conflict_id, strategy)

    def _all_records(self) -> list[MemoryRecord]:
        return [MemoryRecord.model_validate(item) for item in self.store.export()]


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

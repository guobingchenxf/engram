"""SQLite persistence for memories, conflicts and audit events.

The store is deliberately simple: one SQLite file, JSON-encoded list/dict
fields, and embeddings stored as float32 blobs. It is not a production
multi-tenant database, but it is durable, inspectable and easy to test.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from .models import (
    ConflictRecord,
    MemoryRecord,
    MemoryType,
    ensure_utc,
    utcnow,
)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    type TEXT NOT NULL,
    content TEXT NOT NULL,
    summary TEXT,
    entities TEXT NOT NULL DEFAULT '[]',
    source_type TEXT NOT NULL,
    source TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    valid_from TEXT,
    valid_to TEXT,
    confidence REAL NOT NULL,
    importance REAL NOT NULL,
    last_accessed TEXT,
    access_count INTEGER NOT NULL DEFAULT 0,
    ttl_seconds INTEGER,
    sensitivity TEXT NOT NULL,
    supersedes TEXT NOT NULL DEFAULT '[]',
    superseded_by TEXT,
    metadata TEXT NOT NULL DEFAULT '{}',
    embedding BLOB
);

CREATE INDEX IF NOT EXISTS idx_memories_user ON memories(user_id);
CREATE INDEX IF NOT EXISTS idx_memories_user_type ON memories(user_id, type);
CREATE INDEX IF NOT EXISTS idx_memories_valid_to ON memories(valid_to);

CREATE TABLE IF NOT EXISTS conflicts (
    id TEXT PRIMARY KEY,
    user_id TEXT NOT NULL,
    existing_id TEXT NOT NULL,
    new_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolution TEXT
);

CREATE INDEX IF NOT EXISTS idx_conflicts_user ON conflicts(user_id);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    event TEXT NOT NULL,
    memory_id TEXT,
    user_id TEXT,
    payload TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS memory_entities (
    memory_id TEXT NOT NULL,
    entity TEXT NOT NULL,
    PRIMARY KEY (memory_id, entity)
);

CREATE INDEX IF NOT EXISTS idx_memory_entities_entity ON memory_entities(entity);
"""

_COLUMNS = (
    "id, user_id, type, content, summary, entities, source_type, source, "
    "created_at, updated_at, valid_from, valid_to, confidence, importance, "
    "last_accessed, access_count, ttl_seconds, sensitivity, supersedes, "
    "superseded_by, metadata, embedding"
)

_PLACEHOLDERS = ", ".join(["?"] * 22)


class SQLiteMemoryStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.db_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        try:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA foreign_keys=ON")
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _init_schema(self) -> None:
        with self._conn() as connection:
            connection.executescript(_SCHEMA)
            # Backfill the entity index for databases created before graph memory.
            rows = connection.execute("SELECT id, entities FROM memories").fetchall()
            for row in rows:
                for entity in json.loads(row["entities"] or "[]"):
                    key = str(entity).strip().lower()
                    if key:
                        connection.execute(
                            "INSERT OR IGNORE INTO memory_entities (memory_id, entity) VALUES (?, ?)",
                            (row["id"], key),
                        )

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    def insert(self, record: MemoryRecord) -> None:
        with self._conn() as connection:
            connection.execute(
                f"INSERT INTO memories ({_COLUMNS}) VALUES ({_PLACEHOLDERS})",
                self._record_to_params(record),
            )
        self.sync_entities(record.id, record.entities)

    def upsert(self, record: MemoryRecord) -> None:
        with self._conn() as connection:
            connection.execute(
                f"INSERT OR REPLACE INTO memories ({_COLUMNS}) VALUES ({_PLACEHOLDERS})",
                self._record_to_params(record),
            )
        self.sync_entities(record.id, record.entities)

    def get(self, memory_id: str) -> MemoryRecord | None:
        with self._conn() as connection:
            row = connection.execute(
                "SELECT * FROM memories WHERE id = ?", (memory_id,)
            ).fetchone()
        return self._row_to_record(row) if row else None

    def update(self, record: MemoryRecord) -> None:
        record.updated_at = utcnow()
        with self._conn() as connection:
            connection.execute(
                """
                UPDATE memories SET
                    user_id = ?, type = ?, content = ?, summary = ?, entities = ?,
                    source_type = ?, source = ?, created_at = ?, updated_at = ?,
                    valid_from = ?, valid_to = ?, confidence = ?, importance = ?,
                    last_accessed = ?, access_count = ?, ttl_seconds = ?,
                    sensitivity = ?, supersedes = ?, superseded_by = ?,
                    metadata = ?, embedding = ?
                WHERE id = ?
                """,
                (
                    record.user_id,
                    record.type.value,
                    record.content,
                    record.summary,
                    _json_dump(record.entities),
                    record.source_type.value,
                    record.source,
                    _dt(record.created_at),
                    _dt(record.updated_at),
                    _dt(record.valid_from),
                    _dt(record.valid_to),
                    record.confidence,
                    record.importance,
                    _dt(record.last_accessed),
                    record.access_count,
                    record.ttl_seconds,
                    record.sensitivity.value,
                    _json_dump(record.supersedes),
                    record.superseded_by,
                    _json_dump(record.metadata),
                    _embedding_to_blob(record.embedding),
                    record.id,
                ),
            )
        self.sync_entities(record.id, record.entities)

    def delete(self, memory_id: str) -> bool:
        with self._conn() as connection:
            connection.execute(
                "DELETE FROM memory_entities WHERE memory_id = ?", (memory_id,)
            )
            cursor = connection.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    def list_memories(
        self,
        user_id: str,
        *,
        types: Sequence[MemoryType] | None = None,
        include_expired: bool = False,
        limit: int = 1000,
    ) -> list[MemoryRecord]:
        records = self.search_candidates(
            user_id, types=types, include_expired=include_expired, limit=max(limit, 5000)
        )
        return records[:limit]

    def search_candidates(
        self,
        user_id: str,
        *,
        types: Sequence[MemoryType] | None = None,
        include_expired: bool = False,
        limit: int = 5000,
    ) -> list[MemoryRecord]:
        query = "SELECT * FROM memories WHERE user_id = ?"
        params: list[Any] = [user_id]

        if types:
            placeholders = ", ".join(["?"] * len(types))
            query += f" AND type IN ({placeholders})"
            params.extend(item.value for item in types)

        if not include_expired:
            query += " AND superseded_by IS NULL"

        query += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)

        with self._conn() as connection:
            rows = connection.execute(query, params).fetchall()

        records = [self._row_to_record(row) for row in rows]
        if not include_expired:
            records = [record for record in records if not record.is_expired()]
        return records

    def touch(self, memory_id: str, now: datetime | None = None) -> None:
        current = ensure_utc(now) or utcnow()
        with self._conn() as connection:
            connection.execute(
                """
                UPDATE memories
                SET last_accessed = ?, access_count = access_count + 1
                WHERE id = ?
                """,
                (_dt(current), memory_id),
            )

    # ------------------------------------------------------------------
    # Graph / entities
    # ------------------------------------------------------------------
    def sync_entities(self, memory_id: str, entities: Sequence[str]) -> None:
        normalized = {
            str(entity).strip().lower()
            for entity in entities
            if str(entity).strip()
        }
        with self._conn() as connection:
            connection.execute(
                "DELETE FROM memory_entities WHERE memory_id = ?", (memory_id,)
            )
            for entity in normalized:
                connection.execute(
                    "INSERT OR IGNORE INTO memory_entities (memory_id, entity) VALUES (?, ?)",
                    (memory_id, entity),
                )

    def find_by_entities(
        self,
        user_id: str,
        entities: Sequence[str],
        *,
        limit: int = 100,
    ) -> list[MemoryRecord]:
        normalized = [
            str(entity).strip().lower()
            for entity in entities
            if str(entity).strip()
        ]
        if not normalized:
            return []

        placeholders = ", ".join(["?"] * len(normalized))
        query = f"""
            SELECT m.*, COUNT(*) AS entity_matches
            FROM memories m
            JOIN memory_entities e ON e.memory_id = m.id
            WHERE m.user_id = ?
              AND m.superseded_by IS NULL
              AND e.entity IN ({placeholders})
            GROUP BY m.id
            ORDER BY entity_matches DESC, m.updated_at DESC
            LIMIT ?
        """
        params: list[Any] = [user_id, *normalized, limit]
        with self._conn() as connection:
            rows = connection.execute(query, params).fetchall()
        records = [self._row_to_record(row) for row in rows]
        return [record for record in records if not record.is_expired()]

    # ------------------------------------------------------------------
    # Conflicts
    # ------------------------------------------------------------------
    def add_conflict(self, conflict: ConflictRecord) -> None:
        with self._conn() as connection:
            connection.execute(
                """
                INSERT OR REPLACE INTO conflicts
                    (id, user_id, existing_id, new_id, reason, status, created_at, resolved_at, resolution)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    conflict.id,
                    conflict.user_id,
                    conflict.existing_id,
                    conflict.new_id,
                    conflict.reason,
                    conflict.status,
                    _dt(conflict.created_at),
                    _dt(conflict.resolved_at),
                    conflict.resolution,
                ),
            )

    def get_conflict(self, conflict_id: str) -> ConflictRecord | None:
        with self._conn() as connection:
            row = connection.execute(
                "SELECT * FROM conflicts WHERE id = ?", (conflict_id,)
            ).fetchone()
        return self._row_to_conflict(row) if row else None

    def list_conflicts(
        self,
        *,
        user_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[ConflictRecord]:
        query = "SELECT * FROM conflicts"
        clauses: list[str] = []
        params: list[Any] = []
        if user_id:
            clauses.append("user_id = ?")
            params.append(user_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        if clauses:
            query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)

        with self._conn() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._row_to_conflict(row) for row in rows]

    def resolve_conflict(self, conflict_id: str, resolution: str) -> ConflictRecord | None:
        conflict = self.get_conflict(conflict_id)
        if conflict is None:
            return None
        conflict.status = "resolved"
        conflict.resolution = resolution
        conflict.resolved_at = utcnow()
        self.add_conflict(conflict)
        return conflict

    # ------------------------------------------------------------------
    # Audit events
    # ------------------------------------------------------------------
    def log_event(
        self,
        event: str,
        *,
        memory_id: str | None = None,
        user_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        with self._conn() as connection:
            connection.execute(
                "INSERT INTO events (ts, event, memory_id, user_id, payload) VALUES (?, ?, ?, ?, ?)",
                (
                    utcnow().isoformat(),
                    event,
                    memory_id,
                    user_id,
                    _json_dump(payload or {}),
                ),
            )

    def recent_events(self, limit: int = 100) -> list[dict[str, Any]]:
        with self._conn() as connection:
            rows = connection.execute(
                "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,)
            ).fetchall()
        return [
            {
                "id": row["id"],
                "ts": row["ts"],
                "event": row["event"],
                "memory_id": row["memory_id"],
                "user_id": row["user_id"],
                "payload": json.loads(row["payload"]),
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Statistics / export
    # ------------------------------------------------------------------
    def stats(self, user_id: str | None = None) -> dict[str, Any]:
        query = "SELECT * FROM memories"
        params: list[Any] = []
        if user_id:
            query += " WHERE user_id = ?"
            params.append(user_id)

        with self._conn() as connection:
            rows = connection.execute(query, params).fetchall()
            conflict_query = "SELECT COUNT(*) AS count FROM conflicts WHERE status = 'open'"
            conflict_params: list[Any] = []
            if user_id:
                conflict_query += " AND user_id = ?"
                conflict_params.append(user_id)
            conflict_count = connection.execute(conflict_query, conflict_params).fetchone()["count"]

        records = [self._row_to_record(row) for row in rows]
        by_type: dict[str, int] = {}
        for record in records:
            by_type[record.type.value] = by_type.get(record.type.value, 0) + 1

        return {
            "user_id": user_id,
            "total": len(records),
            "active": sum(1 for record in records if record.is_active()),
            "expired": sum(1 for record in records if record.is_expired()),
            "conflicts_open": int(conflict_count),
            "by_type": by_type,
        }

    def export(self, user_id: str | None = None) -> list[dict[str, Any]]:
        query = "SELECT * FROM memories"
        params: list[Any] = []
        if user_id:
            query += " WHERE user_id = ?"
            params.append(user_id)
        with self._conn() as connection:
            rows = connection.execute(query, params).fetchall()
        return [self._row_to_record(row).model_dump(mode="json") for row in rows]

    def import_records(self, records: Sequence[dict[str, Any]]) -> int:
        count = 0
        for item in records:
            self.upsert(MemoryRecord.model_validate(item))
            count += 1
        return count

    def clear(self, user_id: str | None = None) -> None:
        with self._conn() as connection:
            if user_id:
                connection.execute(
                    "DELETE FROM memory_entities WHERE memory_id IN "
                    "(SELECT id FROM memories WHERE user_id = ?)",
                    (user_id,),
                )
                connection.execute("DELETE FROM memories WHERE user_id = ?", (user_id,))
                connection.execute("DELETE FROM conflicts WHERE user_id = ?", (user_id,))
                connection.execute("DELETE FROM events WHERE user_id = ?", (user_id,))
            else:
                connection.execute("DELETE FROM memory_entities")
                connection.execute("DELETE FROM memories")
                connection.execute("DELETE FROM conflicts")
                connection.execute("DELETE FROM events")

    # ------------------------------------------------------------------
    # Serialization helpers
    # ------------------------------------------------------------------
    def _record_to_params(self, record: MemoryRecord) -> tuple[Any, ...]:
        return (
            record.id,
            record.user_id,
            record.type.value,
            record.content,
            record.summary,
            _json_dump(record.entities),
            record.source_type.value,
            record.source,
            _dt(record.created_at),
            _dt(record.updated_at),
            _dt(record.valid_from),
            _dt(record.valid_to),
            record.confidence,
            record.importance,
            _dt(record.last_accessed),
            record.access_count,
            record.ttl_seconds,
            record.sensitivity.value,
            _json_dump(record.supersedes),
            record.superseded_by,
            _json_dump(record.metadata),
            _embedding_to_blob(record.embedding),
        )

    def _row_to_record(self, row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord(
            id=row["id"],
            user_id=row["user_id"],
            type=MemoryType(row["type"]),
            content=row["content"],
            summary=row["summary"],
            entities=json.loads(row["entities"] or "[]"),
            source_type=row["source_type"],
            source=row["source"],
            created_at=_parse_required_dt(row["created_at"]),
            updated_at=_parse_required_dt(row["updated_at"]),
            valid_from=_parse_dt(row["valid_from"]),
            valid_to=_parse_dt(row["valid_to"]),
            confidence=row["confidence"],
            importance=row["importance"],
            last_accessed=_parse_dt(row["last_accessed"]),
            access_count=row["access_count"],
            ttl_seconds=row["ttl_seconds"],
            sensitivity=row["sensitivity"],
            supersedes=json.loads(row["supersedes"] or "[]"),
            superseded_by=row["superseded_by"],
            metadata=json.loads(row["metadata"] or "{}"),
            embedding=_blob_to_embedding(row["embedding"]),
        )

    def _row_to_conflict(self, row: sqlite3.Row) -> ConflictRecord:
        return ConflictRecord(
            id=row["id"],
            user_id=row["user_id"],
            existing_id=row["existing_id"],
            new_id=row["new_id"],
            reason=row["reason"],
            status=row["status"],
            created_at=_parse_required_dt(row["created_at"]),
            resolved_at=_parse_dt(row["resolved_at"]),
            resolution=row["resolution"],
        )


def _dt(value: datetime | None) -> str | None:
    normalized = ensure_utc(value)
    return normalized.isoformat() if normalized else None


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    return ensure_utc(datetime.fromisoformat(value))


def _parse_required_dt(value: str | None) -> datetime:
    parsed = _parse_dt(value)
    if parsed is None:
        raise ValueError("required timestamp is missing from the database")
    return parsed


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def _embedding_to_blob(embedding: Sequence[float] | None) -> bytes | None:
    if embedding is None:
        return None
    return np.asarray(embedding, dtype=np.float32).tobytes()


def _blob_to_embedding(blob: Any) -> list[float] | None:
    if blob is None:
        return None
    return np.frombuffer(blob, dtype=np.float32).tolist()

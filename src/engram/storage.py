"""SQLite persistence for memories, conflicts and audit events.

The store now has two layers:

* ``SQLiteMemoryStore`` owns the database path, the per-thread connection
  cache, schema migration and transaction boundaries.
* ``StoreSession`` is a thin unit-of-work bound to one connection.  The write
  path uses a single session so memory insert, entity/token indexes, conflict
  updates and the audit event commit atomically.

Hot-path changes:
* exact dedupe uses a ``content_hash`` index instead of scanning;
* a ``memory_tokens`` inverted index pre-filters lexical candidates;
* stats are SQL aggregates; forget/prune are batched;
* connections are cached per thread and pragmas are set once.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections import Counter
from collections.abc import Iterator, Sequence
from contextlib import contextmanager, suppress
from datetime import datetime
from pathlib import Path
from typing import Any

import numpy as np

from .audit import (
    GENESIS_HASH,
    AuditVerifyResult,
    canonical_event,
    chain_hash,
    verify_chain,
)
from .models import (
    ConflictRecord,
    MemoryRecord,
    MemoryType,
    ensure_utc,
    utcnow,
)
from .text import content_hash, tokenize

SCHEMA_VERSION = 3

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    user_id TEXT NOT NULL,
    type TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL DEFAULT '',
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
    last_decayed_at TEXT,
    ttl_seconds INTEGER,
    sensitivity TEXT NOT NULL,
    supersedes TEXT NOT NULL DEFAULT '[]',
    superseded_by TEXT,
    metadata TEXT NOT NULL DEFAULT '{}',
    embedding BLOB
);

CREATE TABLE IF NOT EXISTS conflicts (
    id TEXT PRIMARY KEY,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    user_id TEXT NOT NULL,
    existing_id TEXT NOT NULL,
    new_id TEXT NOT NULL,
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'open',
    created_at TEXT NOT NULL,
    resolved_at TEXT,
    resolution TEXT
);

CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts TEXT NOT NULL,
    tenant_id TEXT NOT NULL DEFAULT 'default',
    actor TEXT,
    event TEXT NOT NULL,
    memory_id TEXT,
    user_id TEXT,
    payload TEXT NOT NULL DEFAULT '{}',
    prev_hash TEXT NOT NULL DEFAULT '',
    event_hash TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS memory_entities (
    memory_id TEXT NOT NULL,
    entity TEXT NOT NULL,
    PRIMARY KEY (memory_id, entity)
);

CREATE TABLE IF NOT EXISTS memory_tokens (
    memory_id TEXT NOT NULL,
    token TEXT NOT NULL,
    tf INTEGER NOT NULL,
    PRIMARY KEY (memory_id, token)
);

CREATE TABLE IF NOT EXISTS idempotency (
    tenant_id TEXT NOT NULL,
    user_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    memory_id TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (tenant_id, user_id, idempotency_key)
);
"""

_INDEXES = """
CREATE INDEX IF NOT EXISTS idx_memories_user ON memories(user_id);
CREATE INDEX IF NOT EXISTS idx_memories_user_type ON memories(user_id, type);
CREATE INDEX IF NOT EXISTS idx_memories_valid_to ON memories(valid_to);
CREATE INDEX IF NOT EXISTS idx_memories_tenant_user ON memories(tenant_id, user_id);
CREATE INDEX IF NOT EXISTS idx_memories_content_hash
    ON memories(tenant_id, user_id, type, content_hash);
CREATE INDEX IF NOT EXISTS idx_conflicts_tenant_user ON conflicts(tenant_id, user_id);
CREATE INDEX IF NOT EXISTS idx_events_tenant ON events(tenant_id, id);
CREATE INDEX IF NOT EXISTS idx_memory_entities_entity ON memory_entities(entity);
CREATE INDEX IF NOT EXISTS idx_memory_tokens_token ON memory_tokens(token);
"""

_MEMORY_COLUMNS = (
    "id, tenant_id, user_id, type, content, content_hash, summary, entities, "
    "source_type, source, created_at, updated_at, valid_from, valid_to, "
    "confidence, importance, last_accessed, access_count, last_decayed_at, "
    "ttl_seconds, sensitivity, supersedes, superseded_by, metadata, embedding"
)
_MEMORY_PLACEHOLDERS = ", ".join(["?"] * 25)


class SQLiteMemoryStore:
    def __init__(self, db_path: str | Path) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._connections: list[sqlite3.Connection] = []
        self._connections_lock = threading.Lock()
        self._init_schema()

    # ------------------------------------------------------------------
    # Connections / transactions
    # ------------------------------------------------------------------
    def _connect(self) -> sqlite3.Connection:
        connection = getattr(self._local, "connection", None)
        if connection is None:
            connection = sqlite3.connect(
                self.db_path,
                timeout=30.0,
                isolation_level=None,
            )
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute("PRAGMA synchronous=NORMAL")
            connection.execute("PRAGMA busy_timeout=5000")
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("PRAGMA cache_size=-20000")
            connection.execute("PRAGMA temp_store=MEMORY")
            connection.execute("PRAGMA mmap_size=268435456")
            self._local.connection = connection
            with self._connections_lock:
                self._connections.append(connection)
        return connection

    @contextmanager
    def _read_session(self) -> Iterator[StoreSession]:
        yield StoreSession(self, self._connect())

    @contextmanager
    def transaction(self, *, immediate: bool = True) -> Iterator[StoreSession]:
        """One connection, one transaction, one unit of work."""
        connection = self._connect()
        connection.execute("BEGIN IMMEDIATE" if immediate else "BEGIN")
        try:
            yield StoreSession(self, connection)
            connection.execute("COMMIT")
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except sqlite3.OperationalError:
                pass
            raise

    def close(self) -> None:
        with self._connections_lock:
            connections = list(self._connections)
            self._connections.clear()
        for connection in connections:
            try:
                connection.close()
            except sqlite3.Error:
                pass
        self._local = threading.local()

    def __del__(self) -> None:
        with suppress(Exception):
            self.close()

    # ------------------------------------------------------------------
    # Schema / migration
    # ------------------------------------------------------------------
    def _init_schema(self) -> None:
        connection = sqlite3.connect(self.db_path, timeout=30.0)
        connection.row_factory = sqlite3.Row
        try:
            connection.executescript(_SCHEMA)
            version = int(
                connection.execute("PRAGMA user_version").fetchone()[0]
            )
            if version < 1:
                _ensure_column(
                    connection,
                    "memories",
                    "tenant_id",
                    "TEXT NOT NULL DEFAULT 'default'",
                )
                _ensure_column(
                    connection,
                    "conflicts",
                    "tenant_id",
                    "TEXT NOT NULL DEFAULT 'default'",
                )
                _ensure_column(
                    connection,
                    "events",
                    "tenant_id",
                    "TEXT NOT NULL DEFAULT 'default'",
                )
                _ensure_column(connection, "events", "actor", "TEXT")
                _ensure_column(
                    connection, "events", "prev_hash", "TEXT NOT NULL DEFAULT ''"
                )
                _ensure_column(
                    connection, "events", "event_hash", "TEXT NOT NULL DEFAULT ''"
                )
                self._backfill_audit_chain(connection)
                self._backfill_entities(connection)
            if version < 2:
                _ensure_column(
                    connection,
                    "memories",
                    "content_hash",
                    "TEXT NOT NULL DEFAULT ''",
                )
                self._backfill_content_and_tokens(connection)
            if version < 3:
                _ensure_column(connection, "memories", "last_decayed_at", "TEXT")
            connection.executescript(_INDEXES)
            connection.execute(f"PRAGMA user_version={SCHEMA_VERSION}")
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    @staticmethod
    def _backfill_audit_chain(connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            "SELECT id, ts, tenant_id, actor, event, memory_id, user_id, payload, "
            "event_hash FROM events ORDER BY id ASC"
        ).fetchall()
        grouped: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            grouped.setdefault(str(row["tenant_id"] or "default"), []).append(row)
        for tenant, tenant_rows in grouped.items():
            if any(str(row["event_hash"] or "") for row in tenant_rows):
                continue
            prev = GENESIS_HASH
            for row in tenant_rows:
                try:
                    payload = json.loads(row["payload"] or "{}")
                except json.JSONDecodeError:
                    payload = {"legacy_payload": True}
                body = canonical_event(
                    ts=str(row["ts"]),
                    event=str(row["event"]),
                    memory_id=row["memory_id"],
                    user_id=row["user_id"],
                    tenant_id=tenant,
                    actor=row["actor"],
                    payload=payload,
                )
                digest = chain_hash(prev, body)
                connection.execute(
                    "UPDATE events SET prev_hash = ?, event_hash = ? WHERE id = ?",
                    (prev, digest, row["id"]),
                )
                prev = digest

    @staticmethod
    def _backfill_entities(connection: sqlite3.Connection) -> None:
        rows = connection.execute("SELECT id, entities FROM memories").fetchall()
        for row in rows:
            for entity in json.loads(row["entities"] or "[]"):
                key = str(entity).strip().lower()
                if key:
                    connection.execute(
                        "INSERT OR IGNORE INTO memory_entities "
                        "(memory_id, entity) VALUES (?, ?)",
                        (row["id"], key),
                    )

    @staticmethod
    def _backfill_content_and_tokens(connection: sqlite3.Connection) -> None:
        rows = connection.execute(
            "SELECT id, content, entities FROM memories"
        ).fetchall()
        connection.execute("DELETE FROM memory_tokens")
        for row in rows:
            connection.execute(
                "UPDATE memories SET content_hash = ? WHERE id = ?",
                (content_hash(str(row["content"])), row["id"]),
            )
            entities = json.loads(row["entities"] or "[]")
            text = str(row["content"]) + " " + " ".join(map(str, entities))
            counts = Counter(tokenize(text))
            connection.executemany(
                "INSERT OR REPLACE INTO memory_tokens (memory_id, token, tf) "
                "VALUES (?, ?, ?)",
                [(row["id"], token, count) for token, count in counts.items()],
            )

    # ------------------------------------------------------------------
    # Public single-operation API (opens a session internally)
    # ------------------------------------------------------------------
    def insert(self, record: MemoryRecord) -> None:
        with self.transaction() as session:
            session.insert(record)

    def upsert(self, record: MemoryRecord) -> None:
        with self.transaction() as session:
            session.upsert(record)

    def get(
        self, memory_id: str, *, tenant_id: str = "default"
    ) -> MemoryRecord | None:
        with self._read_session() as session:
            return session.get(memory_id, tenant_id=tenant_id)

    def get_any(self, memory_id: str) -> MemoryRecord | None:
        with self._read_session() as session:
            return session.get_any(memory_id)

    def update(
        self, record: MemoryRecord, *, sync_indexes: bool = True
    ) -> None:
        with self.transaction() as session:
            session.update(record, sync_indexes=sync_indexes)

    def delete(self, memory_id: str, *, tenant_id: str = "default") -> bool:
        with self.transaction() as session:
            return session.delete(memory_id, tenant_id=tenant_id)

    def list_memories(
        self,
        user_id: str,
        *,
        tenant_id: str = "default",
        types: Sequence[MemoryType] | None = None,
        include_expired: bool = False,
        limit: int = 1000,
    ) -> list[MemoryRecord]:
        with self._read_session() as session:
            return session.list_memories(
                user_id,
                tenant_id=tenant_id,
                types=types,
                include_expired=include_expired,
                limit=limit,
            )

    def search_candidates(
        self,
        user_id: str,
        *,
        tenant_id: str = "default",
        types: Sequence[MemoryType] | None = None,
        include_expired: bool = False,
        limit: int | None = None,
    ) -> list[MemoryRecord]:
        with self._read_session() as session:
            return session.search_candidates(
                user_id,
                tenant_id=tenant_id,
                types=types,
                include_expired=include_expired,
                limit=limit,
            )

    def find_by_content_hash(
        self,
        user_id: str,
        digest: str,
        *,
        tenant_id: str = "default",
        memory_type: MemoryType | None = None,
    ) -> MemoryRecord | None:
        with self._read_session() as session:
            return session.find_by_content_hash(
                user_id,
                digest,
                tenant_id=tenant_id,
                memory_type=memory_type,
            )

    def find_by_tokens(
        self,
        user_id: str,
        tokens: Sequence[str],
        *,
        tenant_id: str = "default",
        limit: int = 200,
        shared_only: bool = False,
    ) -> list[MemoryRecord]:
        with self._read_session() as session:
            return session.find_by_tokens(
                user_id,
                tokens,
                tenant_id=tenant_id,
                limit=limit,
                shared_only=shared_only,
            )

    def touch(
        self,
        memory_id: str,
        now: datetime | None = None,
        *,
        tenant_id: str = "default",
    ) -> None:
        with self.transaction() as session:
            session.touch(memory_id, now, tenant_id=tenant_id)

    def touch_many(
        self,
        memory_ids: Sequence[str],
        now: datetime | None = None,
        *,
        tenant_id: str = "default",
    ) -> None:
        if not memory_ids:
            return
        with self.transaction() as session:
            session.touch_many(memory_ids, now, tenant_id=tenant_id)

    def sync_entities(
        self, memory_id: str, entities: Sequence[str]
    ) -> None:
        with self.transaction() as session:
            session.sync_entities(memory_id, entities)

    def find_by_entities(
        self,
        user_id: str,
        entities: Sequence[str],
        *,
        tenant_id: str = "default",
        limit: int = 100,
    ) -> list[MemoryRecord]:
        with self._read_session() as session:
            return session.find_by_entities(
                user_id, entities, tenant_id=tenant_id, limit=limit
            )

    def add_conflict(self, conflict: ConflictRecord) -> None:
        with self.transaction() as session:
            session.add_conflict(conflict)

    def get_conflict(
        self, conflict_id: str, *, tenant_id: str = "default"
    ) -> ConflictRecord | None:
        with self._read_session() as session:
            return session.get_conflict(conflict_id, tenant_id=tenant_id)

    def list_conflicts(
        self,
        *,
        tenant_id: str = "default",
        user_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[ConflictRecord]:
        with self._read_session() as session:
            return session.list_conflicts(
                tenant_id=tenant_id,
                user_id=user_id,
                status=status,
                limit=limit,
            )

    def resolve_conflict(
        self, conflict_id: str, resolution: str, *, tenant_id: str = "default"
    ) -> ConflictRecord | None:
        with self.transaction() as session:
            return session.resolve_conflict(
                conflict_id, resolution, tenant_id=tenant_id
            )

    def log_event(
        self,
        event: str,
        *,
        tenant_id: str = "default",
        actor: str | None = None,
        memory_id: str | None = None,
        user_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> str:
        with self.transaction() as session:
            return session.log_event(
                event,
                tenant_id=tenant_id,
                actor=actor,
                memory_id=memory_id,
                user_id=user_id,
                payload=payload,
            )

    def verify_audit_chain(self, tenant_id: str = "default") -> AuditVerifyResult:
        with self._read_session() as session:
            return session.verify_audit_chain(tenant_id)

    def audit_head(self, tenant_id: str = "default") -> dict[str, Any]:
        with self._read_session() as session:
            return session.audit_head(tenant_id)

    def recent_events(
        self, limit: int = 100, *, tenant_id: str | None = None
    ) -> list[dict[str, Any]]:
        with self._read_session() as session:
            return session.recent_events(limit, tenant_id=tenant_id)

    def stats(
        self, user_id: str | None = None, *, tenant_id: str = "default"
    ) -> dict[str, Any]:
        with self._read_session() as session:
            return session.stats(user_id, tenant_id=tenant_id)

    def export(
        self, user_id: str | None = None, *, tenant_id: str = "default"
    ) -> list[dict[str, Any]]:
        with self._read_session() as session:
            return session.export(user_id, tenant_id=tenant_id)

    def forget(
        self,
        user_id: str,
        *,
        tenant_id: str = "default",
        memory_type: MemoryType | None = None,
        before: datetime | None = None,
    ) -> int:
        with self.transaction() as session:
            return session.forget(
                user_id,
                tenant_id=tenant_id,
                memory_type=memory_type,
                before=before,
            )

    def clear(
        self, user_id: str | None = None, *, tenant_id: str = "default"
    ) -> None:
        with self.transaction() as session:
            session.clear(user_id, tenant_id=tenant_id)

    def get_idempotent(
        self, tenant_id: str, user_id: str, key: str
    ) -> str | None:
        with self._read_session() as session:
            return session.get_idempotent(tenant_id, user_id, key)

    def put_idempotent(
        self,
        tenant_id: str,
        user_id: str,
        key: str,
        memory_id: str,
    ) -> None:
        with self.transaction() as session:
            session.put_idempotent(tenant_id, user_id, key, memory_id)


class StoreSession:
    """A unit of work bound to one SQLite connection."""

    def __init__(self, store: SQLiteMemoryStore, connection: sqlite3.Connection):
        self.store = store
        self.connection = connection

    # ------------------------------------------------------------------
    # CRUD
    # ------------------------------------------------------------------
    def insert(self, record: MemoryRecord) -> None:
        self.connection.execute(
            f"INSERT INTO memories ({_MEMORY_COLUMNS}) VALUES ({_MEMORY_PLACEHOLDERS})",
            _record_params(record),
        )
        self.sync_entities(record.id, record.entities)
        self.sync_tokens(record)

    def upsert(self, record: MemoryRecord) -> None:
        existing = self.get_any(record.id)
        if existing is not None and (
            existing.user_id != record.user_id
            or existing.tenant_id != record.tenant_id
        ):
            raise ValueError(
                "refusing to overwrite a memory owned by another tenant/user"
            )
        self.connection.execute(
            f"INSERT OR REPLACE INTO memories ({_MEMORY_COLUMNS}) "
            f"VALUES ({_MEMORY_PLACEHOLDERS})",
            _record_params(record),
        )
        self.sync_entities(record.id, record.entities)
        self.sync_tokens(record)

    def get(
        self, memory_id: str, *, tenant_id: str = "default"
    ) -> MemoryRecord | None:
        row = self.connection.execute(
            "SELECT * FROM memories WHERE id = ? AND tenant_id = ?",
            (memory_id, tenant_id),
        ).fetchone()
        return _row_to_record(row) if row else None

    def get_any(self, memory_id: str) -> MemoryRecord | None:
        row = self.connection.execute(
            "SELECT * FROM memories WHERE id = ?", (memory_id,)
        ).fetchone()
        return _row_to_record(row) if row else None

    def update(
        self, record: MemoryRecord, *, sync_indexes: bool = True
    ) -> None:
        record.updated_at = utcnow()
        cursor = self.connection.execute(
            """
            UPDATE memories SET
                tenant_id = ?, user_id = ?, type = ?, content = ?,
                content_hash = ?, summary = ?, entities = ?, source_type = ?,
                source = ?, created_at = ?, updated_at = ?, valid_from = ?,
                valid_to = ?, confidence = ?, importance = ?, last_accessed = ?,
                access_count = ?, last_decayed_at = ?, ttl_seconds = ?,
                sensitivity = ?, supersedes = ?, superseded_by = ?,
                metadata = ?, embedding = ?
            WHERE id = ? AND tenant_id = ?
            """,
            (
                record.tenant_id,
                record.user_id,
                record.type.value,
                record.content,
                content_hash(record.content),
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
                _dt(record.metadata.get("last_decayed_at")),
                record.ttl_seconds,
                record.sensitivity.value,
                _json_dump(record.supersedes),
                record.superseded_by,
                _json_dump(record.metadata),
                _embedding_to_blob(record.embedding),
                record.id,
                record.tenant_id,
            ),
        )
        if cursor.rowcount == 0:
            raise ValueError("memory disappeared or tenant mismatch")
        if sync_indexes:
            self.sync_entities(record.id, record.entities)
            self.sync_tokens(record)

    def delete(self, memory_id: str, *, tenant_id: str = "default") -> bool:
        cursor = self.connection.execute(
            "DELETE FROM memories WHERE id = ? AND tenant_id = ?",
            (memory_id, tenant_id),
        )
        if cursor.rowcount == 0:
            return False
        self.connection.execute(
            "DELETE FROM memory_entities WHERE memory_id = ?", (memory_id,)
        )
        self.connection.execute(
            "DELETE FROM memory_tokens WHERE memory_id = ?", (memory_id,)
        )
        return True

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------
    def list_memories(
        self,
        user_id: str,
        *,
        tenant_id: str = "default",
        types: Sequence[MemoryType] | None = None,
        include_expired: bool = False,
        limit: int = 1000,
    ) -> list[MemoryRecord]:
        records = self.search_candidates(
            user_id,
            tenant_id=tenant_id,
            types=types,
            include_expired=include_expired,
            limit=max(limit, 5000),
        )
        return records[:limit]

    def search_candidates(
        self,
        user_id: str,
        *,
        tenant_id: str = "default",
        types: Sequence[MemoryType] | None = None,
        include_expired: bool = False,
        limit: int | None = None,
    ) -> list[MemoryRecord]:
        query = "SELECT * FROM memories WHERE tenant_id = ? AND user_id = ?"
        params: list[Any] = [tenant_id, user_id]
        if types:
            placeholders = ", ".join(["?"] * len(types))
            query += f" AND type IN ({placeholders})"
            params.extend(item.value for item in types)
        if not include_expired:
            query += " AND superseded_by IS NULL"
        query += " ORDER BY updated_at DESC"
        if limit is not None:
            query += " LIMIT ?"
            params.append(limit)
        rows = self.connection.execute(query, params).fetchall()
        records = [_row_to_record(row) for row in rows]
        if not include_expired:
            records = [record for record in records if not record.is_expired()]
        return records

    def find_by_content_hash(
        self,
        user_id: str,
        digest: str,
        *,
        tenant_id: str = "default",
        memory_type: MemoryType | None = None,
    ) -> MemoryRecord | None:
        query = (
            "SELECT * FROM memories WHERE tenant_id = ? AND user_id = ? "
            "AND content_hash = ? AND superseded_by IS NULL"
        )
        params: list[Any] = [tenant_id, user_id, digest]
        if memory_type is not None:
            query += " AND type = ?"
            params.append(memory_type.value)
        query += " ORDER BY updated_at DESC LIMIT 5"
        for row in self.connection.execute(query, params).fetchall():
            record = _row_to_record(row)
            if not record.is_expired():
                return record
        return None

    def find_by_tokens(
        self,
        user_id: str,
        tokens: Sequence[str],
        *,
        tenant_id: str = "default",
        limit: int = 200,
        shared_only: bool = False,
    ) -> list[MemoryRecord]:
        normalized = [token for token in dict.fromkeys(tokens) if token]
        if not normalized:
            return []
        placeholders = ", ".join(["?"] * len(normalized))
        # Rare tokens are the discriminative ones.  Querying only the 8 rarest
        # avoids aggregating the whole corpus for a common token like "memory".
        df_rows = self.connection.execute(
            f"SELECT token, COUNT(*) AS df FROM memory_tokens "
            f"WHERE token IN ({placeholders}) GROUP BY token",
            normalized,
        ).fetchall()
        if shared_only:
            df_rows = [row for row in df_rows if int(row["df"]) >= 2]
        selected = [
            str(row["token"])
            for row in sorted(
                df_rows,
                key=lambda item: (int(item["df"]), str(item["token"])),
            )[:8]
        ]
        if not selected:
            return []
        # Per-token posting lists with a hard LIMIT: cost is O(8 * K), not
        # O(corpus), even when every memory contains a common token.
        ids: list[str] = []
        seen: set[str] = set()
        per_token = max(8, limit // max(1, len(selected)))
        for token in selected:
            for row in self.connection.execute(
                "SELECT memory_id FROM memory_tokens WHERE token = ? "
                "ORDER BY rowid DESC LIMIT ?",
                (token, per_token),
            ).fetchall():
                memory_id = str(row["memory_id"])
                if memory_id not in seen:
                    seen.add(memory_id)
                    ids.append(memory_id)
        if not ids:
            return []
        placeholders = ", ".join(["?"] * len(ids))
        rows = self.connection.execute(
            f"SELECT * FROM memories WHERE tenant_id = ? AND user_id = ? "
            f"AND superseded_by IS NULL AND id IN ({placeholders}) "
            f"ORDER BY updated_at DESC LIMIT ?",
            (tenant_id, user_id, *ids, limit),
        ).fetchall()
        records = [_row_to_record(row) for row in rows]
        return [record for record in records if not record.is_expired()]

    def touch(
        self,
        memory_id: str,
        now: datetime | None = None,
        *,
        tenant_id: str = "default",
    ) -> None:
        current = ensure_utc(now) or utcnow()
        self.connection.execute(
            "UPDATE memories SET last_accessed = ?, "
            "access_count = access_count + 1 WHERE id = ? AND tenant_id = ?",
            (_dt(current), memory_id, tenant_id),
        )

    def touch_many(
        self,
        memory_ids: Sequence[str],
        now: datetime | None = None,
        *,
        tenant_id: str = "default",
    ) -> None:
        if not memory_ids:
            return
        current = ensure_utc(now) or utcnow()
        placeholders = ", ".join(["?"] * len(memory_ids))
        self.connection.execute(
            f"UPDATE memories SET last_accessed = ?, "
            f"access_count = access_count + 1 "
            f"WHERE tenant_id = ? AND id IN ({placeholders})",
            (_dt(current), tenant_id, *memory_ids),
        )

    # ------------------------------------------------------------------
    # Graph / token indexes
    # ------------------------------------------------------------------
    def sync_entities(self, memory_id: str, entities: Sequence[str]) -> None:
        normalized = {
            str(entity).strip().lower()
            for entity in entities
            if str(entity).strip()
        }
        self.connection.execute(
            "DELETE FROM memory_entities WHERE memory_id = ?", (memory_id,)
        )
        self.connection.executemany(
            "INSERT OR IGNORE INTO memory_entities (memory_id, entity) "
            "VALUES (?, ?)",
            [(memory_id, entity) for entity in normalized],
        )

    def sync_tokens(self, record: MemoryRecord) -> None:
        text = record.content + " " + " ".join(map(str, record.entities))
        counts = Counter(tokenize(text))
        self.connection.execute(
            "DELETE FROM memory_tokens WHERE memory_id = ?", (record.id,)
        )
        self.connection.executemany(
            "INSERT OR REPLACE INTO memory_tokens (memory_id, token, tf) "
            "VALUES (?, ?, ?)",
            [(record.id, token, count) for token, count in counts.items()],
        )

    def find_by_entities(
        self,
        user_id: str,
        entities: Sequence[str],
        *,
        tenant_id: str = "default",
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
            WHERE m.tenant_id = ?
              AND m.user_id = ?
              AND m.superseded_by IS NULL
              AND e.entity IN ({placeholders})
            GROUP BY m.id
            ORDER BY entity_matches DESC, m.updated_at DESC
            LIMIT ?
        """
        params: list[Any] = [tenant_id, user_id, *normalized, limit]
        rows = self.connection.execute(query, params).fetchall()
        records = [_row_to_record(row) for row in rows]
        return [record for record in records if not record.is_expired()]

    # ------------------------------------------------------------------
    # Conflicts
    # ------------------------------------------------------------------
    def add_conflict(self, conflict: ConflictRecord) -> None:
        self.connection.execute(
            """
            INSERT OR REPLACE INTO conflicts
                (id, tenant_id, user_id, existing_id, new_id, reason, status,
                 created_at, resolved_at, resolution)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                conflict.id,
                conflict.tenant_id,
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

    def get_conflict(
        self, conflict_id: str, *, tenant_id: str = "default"
    ) -> ConflictRecord | None:
        row = self.connection.execute(
            "SELECT * FROM conflicts WHERE id = ? AND tenant_id = ?",
            (conflict_id, tenant_id),
        ).fetchone()
        return _row_to_conflict(row) if row else None

    def list_conflicts(
        self,
        *,
        tenant_id: str = "default",
        user_id: str | None = None,
        status: str | None = None,
        limit: int = 100,
    ) -> list[ConflictRecord]:
        query = "SELECT * FROM conflicts"
        clauses: list[str] = ["tenant_id = ?"]
        params: list[Any] = [tenant_id]
        if user_id:
            clauses.append("user_id = ?")
            params.append(user_id)
        if status:
            clauses.append("status = ?")
            params.append(status)
        query += " WHERE " + " AND ".join(clauses)
        query += " ORDER BY created_at DESC LIMIT ?"
        params.append(limit)
        rows = self.connection.execute(query, params).fetchall()
        return [_row_to_conflict(row) for row in rows]

    def resolve_conflict(
        self, conflict_id: str, resolution: str, *, tenant_id: str = "default"
    ) -> ConflictRecord | None:
        conflict = self.get_conflict(conflict_id, tenant_id=tenant_id)
        if conflict is None:
            return None
        conflict.status = "resolved"
        conflict.resolution = resolution
        conflict.resolved_at = utcnow()
        self.add_conflict(conflict)
        return conflict

    # ------------------------------------------------------------------
    # Audit
    # ------------------------------------------------------------------
    def log_event(
        self,
        event: str,
        *,
        tenant_id: str = "default",
        actor: str | None = None,
        memory_id: str | None = None,
        user_id: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> str:
        ts = utcnow().isoformat()
        body = canonical_event(
            ts=ts,
            event=event,
            memory_id=memory_id,
            user_id=user_id,
            tenant_id=tenant_id,
            actor=actor,
            payload=payload or {},
        )
        row = self.connection.execute(
            "SELECT event_hash FROM events WHERE tenant_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (tenant_id,),
        ).fetchone()
        previous = (
            str(row["event_hash"])
            if row is not None and row["event_hash"]
            else GENESIS_HASH
        )
        digest = chain_hash(previous, body)
        self.connection.execute(
            """
            INSERT INTO events
                (ts, tenant_id, actor, event, memory_id, user_id, payload,
                 prev_hash, event_hash)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                ts,
                tenant_id,
                actor,
                event,
                memory_id,
                user_id,
                _json_dump(payload or {}),
                previous,
                digest,
            ),
        )
        return digest

    def verify_audit_chain(self, tenant_id: str = "default") -> AuditVerifyResult:
        rows = self.connection.execute(
            "SELECT id, ts, tenant_id, actor, event, memory_id, user_id, payload, "
            "prev_hash, event_hash FROM events WHERE tenant_id = ? ORDER BY id ASC",
            (tenant_id,),
        ).fetchall()
        return verify_chain([dict(row) for row in rows], tenant_id=tenant_id)

    def audit_head(self, tenant_id: str = "default") -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT id, event_hash, ts FROM events WHERE tenant_id = ? "
            "ORDER BY id DESC LIMIT 1",
            (tenant_id,),
        ).fetchone()
        count_row = self.connection.execute(
            "SELECT COUNT(*) AS count FROM events WHERE tenant_id = ?",
            (tenant_id,),
        ).fetchone()
        count = int(count_row["count"]) if count_row else 0
        if row is None:
            return {
                "tenant_id": tenant_id,
                "count": 0,
                "head_hash": GENESIS_HASH,
                "last_event_id": None,
                "last_event_at": None,
            }
        return {
            "tenant_id": tenant_id,
            "count": count,
            "head_hash": str(row["event_hash"] or GENESIS_HASH),
            "last_event_id": row["id"],
            "last_event_at": row["ts"],
        }

    def recent_events(
        self, limit: int = 100, *, tenant_id: str | None = None
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM events"
        params: list[Any] = []
        if tenant_id is not None:
            query += " WHERE tenant_id = ?"
            params.append(tenant_id)
        query += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = self.connection.execute(query, params).fetchall()
        return [
            {
                "id": row["id"],
                "ts": row["ts"],
                "tenant_id": row["tenant_id"],
                "actor": row["actor"],
                "event": row["event"],
                "memory_id": row["memory_id"],
                "user_id": row["user_id"],
                "payload": json.loads(row["payload"]),
                "prev_hash": row["prev_hash"],
                "event_hash": row["event_hash"],
            }
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Statistics / export / deletion
    # ------------------------------------------------------------------
    def stats(
        self, user_id: str | None = None, *, tenant_id: str = "default"
    ) -> dict[str, Any]:
        now = utcnow().isoformat()
        clauses = ["tenant_id = ?"]
        params: list[Any] = [tenant_id]
        if user_id:
            clauses.append("user_id = ?")
            params.append(user_id)
        where = " AND ".join(clauses)
        row = self.connection.execute(
            f"""
            SELECT
                COUNT(*) AS total,
                COALESCE(SUM(CASE
                    WHEN superseded_by IS NULL
                     AND (valid_to IS NULL OR valid_to > ?)
                     AND (ttl_seconds IS NULL
                          OR julianday(created_at) + ttl_seconds / 86400.0
                             > julianday(?))
                    THEN 1 ELSE 0 END), 0) AS active,
                COALESCE(SUM(CASE
                    WHEN (valid_to IS NOT NULL AND valid_to <= ?)
                      OR (ttl_seconds IS NOT NULL
                          AND julianday(created_at) + ttl_seconds / 86400.0
                              <= julianday(?))
                    THEN 1 ELSE 0 END), 0) AS expired
            FROM memories WHERE {where}
            """,
            (now, now, now, now, *params),
        ).fetchone()
        type_rows = self.connection.execute(
            f"SELECT type, COUNT(*) AS count FROM memories WHERE {where} "
            "GROUP BY type",
            params,
        ).fetchall()
        conflict_query = (
            "SELECT COUNT(*) AS count FROM conflicts "
            "WHERE tenant_id = ? AND status = 'open'"
        )
        conflict_params: list[Any] = [tenant_id]
        if user_id:
            conflict_query += " AND user_id = ?"
            conflict_params.append(user_id)
        conflict_count = self.connection.execute(
            conflict_query, conflict_params
        ).fetchone()["count"]
        return {
            "user_id": user_id,
            "tenant_id": tenant_id,
            "total": int(row["total"] or 0),
            "active": int(row["active"] or 0),
            "expired": int(row["expired"] or 0),
            "conflicts_open": int(conflict_count),
            "by_type": {
                str(item["type"]): int(item["count"]) for item in type_rows
            },
        }

    def export(
        self, user_id: str | None = None, *, tenant_id: str = "default"
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM memories WHERE tenant_id = ?"
        params: list[Any] = [tenant_id]
        if user_id:
            query += " AND user_id = ?"
            params.append(user_id)
        rows = self.connection.execute(query, params).fetchall()
        return [_row_to_record(row).model_dump(mode="json") for row in rows]

    def forget(
        self,
        user_id: str,
        *,
        tenant_id: str = "default",
        memory_type: MemoryType | None = None,
        before: datetime | None = None,
    ) -> int:
        clauses = ["tenant_id = ?", "user_id = ?"]
        params: list[Any] = [tenant_id, user_id]
        if memory_type is not None:
            clauses.append("type = ?")
            params.append(memory_type.value)
        cutoff = ensure_utc(before)
        if cutoff is not None:
            clauses.append("created_at < ?")
            params.append(cutoff.isoformat())
        where = " AND ".join(clauses)
        ids = [
            row["id"]
            for row in self.connection.execute(
                f"SELECT id FROM memories WHERE {where}", params
            ).fetchall()
        ]
        if not ids:
            return 0
        placeholders = ", ".join(["?"] * len(ids))
        self.connection.execute(
            f"DELETE FROM memory_tokens WHERE memory_id IN ({placeholders})",
            ids,
        )
        self.connection.execute(
            f"DELETE FROM memory_entities WHERE memory_id IN ({placeholders})",
            ids,
        )
        self.connection.execute(
            f"DELETE FROM memories WHERE id IN ({placeholders})", ids
        )
        return len(ids)

    def clear(
        self, user_id: str | None = None, *, tenant_id: str = "default"
    ) -> None:
        if user_id:
            self.connection.execute(
                "DELETE FROM memory_tokens WHERE memory_id IN "
                "(SELECT id FROM memories WHERE tenant_id = ? AND user_id = ?)",
                (tenant_id, user_id),
            )
            self.connection.execute(
                "DELETE FROM memory_entities WHERE memory_id IN "
                "(SELECT id FROM memories WHERE tenant_id = ? AND user_id = ?)",
                (tenant_id, user_id),
            )
            for table in ("memories", "conflicts", "events", "idempotency"):
                self.connection.execute(
                    f"DELETE FROM {table} WHERE tenant_id = ? AND user_id = ?",
                    (tenant_id, user_id),
                )
        else:
            self.connection.execute(
                "DELETE FROM memory_tokens WHERE memory_id IN "
                "(SELECT id FROM memories WHERE tenant_id = ?)",
                (tenant_id,),
            )
            self.connection.execute(
                "DELETE FROM memory_entities WHERE memory_id IN "
                "(SELECT id FROM memories WHERE tenant_id = ?)",
                (tenant_id,),
            )
            for table in ("memories", "conflicts", "events", "idempotency"):
                self.connection.execute(
                    f"DELETE FROM {table} WHERE tenant_id = ?", (tenant_id,)
                )

    def get_idempotent(
        self, tenant_id: str, user_id: str, key: str
    ) -> str | None:
        row = self.connection.execute(
            "SELECT memory_id FROM idempotency "
            "WHERE tenant_id = ? AND user_id = ? AND idempotency_key = ?",
            (tenant_id, user_id, key),
        ).fetchone()
        return str(row["memory_id"]) if row else None

    def put_idempotent(
        self,
        tenant_id: str,
        user_id: str,
        key: str,
        memory_id: str,
    ) -> None:
        self.connection.execute(
            "INSERT OR REPLACE INTO idempotency "
            "(tenant_id, user_id, idempotency_key, memory_id, created_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (tenant_id, user_id, key, memory_id, utcnow().isoformat()),
        )


# ----------------------------------------------------------------------
# Serialization / migration helpers
# ----------------------------------------------------------------------
def _ensure_column(
    connection: sqlite3.Connection, table: str, column: str, ddl: str
) -> None:
    columns = {
        str(row["name"])
        for row in connection.execute(f"PRAGMA table_info({table})").fetchall()
    }
    if column not in columns:
        connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")


def _record_params(record: MemoryRecord) -> tuple[Any, ...]:
    return (
        record.id,
        record.tenant_id,
        record.user_id,
        record.type.value,
        record.content,
        content_hash(record.content),
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
        _dt(record.metadata.get("last_decayed_at")),
        record.ttl_seconds,
        record.sensitivity.value,
        _json_dump(record.supersedes),
        record.superseded_by,
        _json_dump(record.metadata),
        _embedding_to_blob(record.embedding),
    )


def _row_to_record(row: sqlite3.Row) -> MemoryRecord:
    return MemoryRecord(
        id=row["id"],
        user_id=row["user_id"],
        tenant_id=row["tenant_id"] or "default",
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


def _row_to_conflict(row: sqlite3.Row) -> ConflictRecord:
    return ConflictRecord(
        id=row["id"],
        user_id=row["user_id"],
        tenant_id=row["tenant_id"] or "default",
        existing_id=row["existing_id"],
        new_id=row["new_id"],
        reason=row["reason"],
        status=row["status"],
        created_at=_parse_required_dt(row["created_at"]),
        resolved_at=_parse_dt(row["resolved_at"]),
        resolution=row["resolution"],
    )


def _dt(value: datetime | str | None) -> str | None:
    if isinstance(value, str):
        value = datetime.fromisoformat(value) if value else None
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

"""Tenant isolation: reads, writes, mutation and audit are per tenant."""

from __future__ import annotations

import sqlite3

import pytest

from engram.config import MemoryConfig
from engram.errors import MemoryNotFound, SafetyViolation, TenantIsolationError
from engram.models import MemoryQuery, MemoryUpdateRequest
from engram.service import MemoryService


def _service(tmp_path, tenant: str) -> MemoryService:
    shared_db = str(tmp_path / "shared.db")
    return MemoryService(
        MemoryConfig().with_overrides(
            db_path=shared_db,
            trace_path=str(tmp_path / f"{tenant}.jsonl"),
            embedding_provider="hash",
            llm_provider="heuristic",
            require_auth=False,
            tenant_id=tenant,
        )
    )


def test_reads_and_writes_are_scoped_by_tenant(tmp_path) -> None:
    a = _service(tmp_path, "tenant-a")
    b = _service(tmp_path, "tenant-b")
    a.write_text("same-user", "tenant A prefers Chinese explanations")
    b.write_text("same-user", "tenant B prefers English explanations")

    result_a = a.search(
        MemoryQuery(
            user_id="same-user",
            query="Chinese explanations",
            min_relevance=0.0,
        )
    )
    result_b = b.search(
        MemoryQuery(
            user_id="same-user",
            query="English explanations",
            min_relevance=0.0,
        )
    )
    assert result_a.hits and result_b.hits
    assert all(hit.memory.tenant_id == "tenant-a" for hit in result_a.hits)
    assert all(hit.memory.tenant_id == "tenant-b" for hit in result_b.hits)
    assert "tenant A" in result_a.context
    assert "tenant B" not in result_a.context
    assert "tenant B" in result_b.context

    assert a.stats("same-user")["total"] == 1
    assert b.stats("same-user")["total"] == 1
    assert {row["tenant_id"] for row in a.export("same-user")} == {"tenant-a"}
    assert {row["tenant_id"] for row in b.export("same-user")} == {"tenant-b"}


def test_cross_tenant_update_and_delete_are_not_found(tmp_path) -> None:
    a = _service(tmp_path, "tenant-a")
    b = _service(tmp_path, "tenant-b")
    record = a.write_text("u1", "tenant A secret")

    with pytest.raises(MemoryNotFound):
        b.update(record.id, MemoryUpdateRequest(content="hijacked"))
    with pytest.raises(MemoryNotFound):
        b.delete(record.id)

    stored = a.store.get(record.id, tenant_id="tenant-a")
    assert stored is not None
    assert stored.content == "tenant A secret"


def test_request_tenant_mismatch_is_rejected(tmp_path) -> None:
    a = _service(tmp_path, "tenant-a")
    with pytest.raises(TenantIsolationError):
        a.search(
            MemoryQuery(
                user_id="u1",
                tenant_id="tenant-b",
                query="anything",
            )
        )
    with pytest.raises(TenantIsolationError):
        a.write_text("u1", "content", tenant_id="tenant-b")


def test_extract_and_explain_cannot_cross_tenants(tmp_path) -> None:
    from engram.models import ExtractRequest

    a = _service(tmp_path, "tenant-a")
    with pytest.raises(TenantIsolationError):
        a.extract_and_write(
            ExtractRequest(
                user_id="u1",
                text="我偏好中文解释，记住。",
                tenant_id="tenant-b",
            )
        )

    from fastapi.testclient import TestClient

    from engram.api import create_app

    config = MemoryConfig().with_overrides(
        db_path=str(tmp_path / "explain.db"),
        trace_path=str(tmp_path / "explain.jsonl"),
        embedding_provider="hash",
        llm_provider="heuristic",
        require_auth=False,
        tenant_id="tenant-a",
    )
    client = TestClient(create_app(config))
    response = client.post(
        "/v1/explain",
        json={
            "user_id": "u1",
            "tenant_id": "tenant-b",
            "query": "anything",
        },
    )
    assert response.status_code == 403


def test_import_cannot_cross_tenants(tmp_path) -> None:
    a = _service(tmp_path, "tenant-a")
    b = _service(tmp_path, "tenant-b")
    a.write_text("u1", "tenant A data")
    exported = a.export("u1")
    with pytest.raises(TenantIsolationError):
        b.import_records(exported)


def test_conflicts_and_audit_are_isolated(tmp_path) -> None:
    a = _service(tmp_path, "tenant-a")
    b = _service(tmp_path, "tenant-b")
    a.write_text("u1", "我的目标是在 2028 年拿到推荐算法实习")
    a.write_text("u1", "我的目标是在 2028 年拿到腾讯算法实习")
    b.write_text("u1", "我偏好中文解释")

    assert a.conflicts(user_id="u1")
    assert b.conflicts(user_id="u1") == []
    assert a.audit_head()["count"] > 0
    assert b.audit_head()["count"] > 0
    assert a.audit_head()["head_hash"] != b.audit_head()["head_hash"]
    assert a.verify_audit()["ok"] is True
    assert b.verify_audit()["ok"] is True


def test_cross_tenant_import_overwrite_is_rejected_by_store(tmp_path) -> None:
    a = _service(tmp_path, "tenant-a")
    b = _service(tmp_path, "tenant-b")
    record = a.write_text("u1", "tenant A data")
    # A forged record with the same id, but asking for tenant-b, must not
    # replace tenant-a's row.
    forged = record.model_dump(mode="json")
    forged["tenant_id"] = "tenant-b"
    with pytest.raises(SafetyViolation, match="import_cross_user_overwrite"):
        b.import_records([forged])
    stored = a.store.get(record.id, tenant_id="tenant-a")
    assert stored is not None
    assert stored.content == "tenant A data"


def test_default_tenant_legacy_record_is_scoped_to_service(tmp_path) -> None:
    a = _service(tmp_path, "tenant-a")
    payload = {
        "id": "legacy-1",
        "user_id": "u1",
        "content": "a legacy record without tenant",
        "type": "semantic",
    }
    assert a.import_records([payload]) == 1
    stored = a.store.get("legacy-1", tenant_id="tenant-a")
    assert stored is not None
    assert stored.tenant_id == "tenant-a"


def test_trace_is_tenant_scoped(tmp_path) -> None:
    shared_db = str(tmp_path / "trace.db")
    shared_trace = str(tmp_path / "trace.jsonl")
    configs = {
        tenant: MemoryConfig().with_overrides(
            db_path=shared_db,
            trace_path=shared_trace,
            embedding_provider="hash",
            llm_provider="heuristic",
            require_auth=False,
            tenant_id=tenant,
        )
        for tenant in ("tenant-a", "tenant-b")
    }
    a = MemoryService(configs["tenant-a"])
    b = MemoryService(configs["tenant-b"])
    a.write_text("u-a", "tenant A trace memory")
    b.write_text("u-b", "tenant B trace memory")

    a_records = a.trace.read(tenant_id="tenant-a")
    b_records = b.trace.read(tenant_id="tenant-b")
    assert a_records and all(row.get("tenant_id") == "tenant-a" for row in a_records)
    assert b_records and all(row.get("tenant_id") == "tenant-b" for row in b_records)
    assert not any(row.get("user_id") == "u-b" for row in a_records)
    assert not any(row.get("user_id") == "u-a" for row in b_records)


def test_api_tenant_mismatch_is_403(tmp_path) -> None:
    from fastapi.testclient import TestClient

    from engram.api import create_app

    config = MemoryConfig().with_overrides(
        db_path=str(tmp_path / "api.db"),
        trace_path=str(tmp_path / "api.jsonl"),
        embedding_provider="hash",
        llm_provider="heuristic",
        require_auth=False,
        tenant_id="tenant-a",
    )
    client = TestClient(create_app(config))
    response = client.post(
        "/v1/memories",
        json={
            "user_id": "u1",
            "tenant_id": "tenant-b",
            "content": "cross tenant write",
        },
    )
    assert response.status_code == 403
    assert response.json()["error"] == "tenant_isolation"


def test_legacy_schema_migrates_to_tenant_and_hashed_events(tmp_path) -> None:
    from engram.storage import SQLiteMemoryStore

    db = tmp_path / "legacy.db"
    with sqlite3.connect(db) as connection:
        connection.executescript(
            """
            CREATE TABLE memories (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL, type TEXT NOT NULL,
                content TEXT NOT NULL, summary TEXT, entities TEXT NOT NULL DEFAULT '[]',
                source_type TEXT NOT NULL, source TEXT, created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL, valid_from TEXT, valid_to TEXT,
                confidence REAL NOT NULL, importance REAL NOT NULL, last_accessed TEXT,
                access_count INTEGER NOT NULL DEFAULT 0, ttl_seconds INTEGER,
                sensitivity TEXT NOT NULL, supersedes TEXT NOT NULL DEFAULT '[]',
                superseded_by TEXT, metadata TEXT NOT NULL DEFAULT '{}', embedding BLOB
            );
            CREATE TABLE conflicts (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL, existing_id TEXT NOT NULL,
                new_id TEXT NOT NULL, reason TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'open',
                created_at TEXT NOT NULL, resolved_at TEXT, resolution TEXT
            );
            CREATE TABLE events (
                id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT NOT NULL,
                event TEXT NOT NULL, memory_id TEXT, user_id TEXT,
                payload TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE memory_entities (
                memory_id TEXT NOT NULL, entity TEXT NOT NULL,
                PRIMARY KEY (memory_id, entity)
            );
            """
        )
        connection.execute(
            """
            INSERT INTO memories
                (id, user_id, type, content, entities, source_type, created_at,
                 updated_at, confidence, importance, sensitivity, supersedes, metadata)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "old-1",
                "u1",
                "semantic",
                "legacy memory",
                "[]",
                "user",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:00:00+00:00",
                0.8,
                0.5,
                "personal",
                "[]",
                "{}",
            ),
        )
        connection.execute(
            "INSERT INTO events (ts, event, user_id, payload) VALUES (?, ?, ?, ?)",
            ("2026-01-01T00:00:00+00:00", "legacy", "u1", "{}"),
        )
        connection.commit()

    store = SQLiteMemoryStore(db)
    migrated = store.get("old-1")
    assert migrated is not None
    assert migrated.tenant_id == "default"
    assert store.verify_audit_chain("default").ok is True
    events = store.recent_events(10, tenant_id="default")
    assert events and events[0]["event_hash"]
    assert events[0]["prev_hash"] != ""

"""Tamper-evident audit chain: verification and tamper detection."""

from __future__ import annotations

import sqlite3

from engram.config import MemoryConfig
from engram.models import MemoryQuery
from engram.service import MemoryService


def _event_ids(config: MemoryConfig) -> list[int]:
    with sqlite3.connect(config.db_path) as connection:
        rows = connection.execute(
            "SELECT id FROM events ORDER BY id ASC"
        ).fetchall()
    return [int(row[0]) for row in rows]


def test_audit_chain_verifies_and_advances(service: MemoryService) -> None:
    service.write_text("u1", "The user prefers Chinese explanations.")
    service.write_text("u1", "The user prefers concise answers.")
    service.search(MemoryQuery(user_id="u1", query="preference"))

    head = service.audit_head()
    assert head["count"] == 3
    assert head["head_hash"] != "0" * 64
    verified = service.verify_audit()
    assert verified["ok"] is True
    assert verified["checked"] == 3
    assert verified["head_hash"] == head["head_hash"]


def test_tampering_with_payload_is_detected(
    service: MemoryService, config: MemoryConfig
) -> None:
    service.write_text("u1", "first")
    service.write_text("u1", "second")
    ids = _event_ids(config)
    assert len(ids) >= 2

    with sqlite3.connect(config.db_path) as connection:
        connection.execute(
            "UPDATE events SET payload = ? WHERE id = ?",
            ('{"tampered":true}', ids[1]),
        )
        connection.commit()

    result = service.verify_audit()
    assert result["ok"] is False
    assert result["first_bad_id"] == ids[1]
    assert result["reason"] == "event_hash_mismatch"


def test_deleting_an_event_is_detected(
    service: MemoryService, config: MemoryConfig
) -> None:
    service.write_text("u1", "one")
    service.write_text("u1", "two")
    service.write_text("u1", "three")
    ids = _event_ids(config)
    assert len(ids) >= 3

    with sqlite3.connect(config.db_path) as connection:
        connection.execute("DELETE FROM events WHERE id = ?", (ids[1],))
        connection.commit()

    result = service.verify_audit()
    assert result["ok"] is False
    assert result["first_bad_id"] == ids[2]
    assert result["reason"] == "prev_hash_mismatch"


def test_tampering_with_prev_hash_is_detected(
    service: MemoryService, config: MemoryConfig
) -> None:
    service.write_text("u1", "one")
    service.write_text("u1", "two")
    ids = _event_ids(config)
    with sqlite3.connect(config.db_path) as connection:
        connection.execute(
            "UPDATE events SET prev_hash = ? WHERE id = ?",
            ("f" * 64, ids[-1]),
        )
        connection.commit()
    result = service.verify_audit()
    assert result["ok"] is False
    assert result["first_bad_id"] == ids[-1]
    assert result["reason"] == "prev_hash_mismatch"


def test_audit_events_do_not_store_raw_queries(service: MemoryService) -> None:
    service.search(
        MemoryQuery(user_id="u1", query="my secret query 13800138000")
    )
    events = service.recent_events()
    search_events = [event for event in events if event["event"] == "search"]
    assert search_events
    payload = search_events[0]["payload"]
    assert "query" not in payload
    assert "query_fingerprint" in payload
    assert "13800138000" not in str(payload)

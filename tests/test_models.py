from __future__ import annotations

from datetime import timedelta

import pytest
from pydantic import ValidationError

from engram.models import MemoryRecord, MemoryType, utcnow


def test_memory_record_defaults() -> None:
    record = MemoryRecord(user_id="u1", content="hello")
    assert record.type == MemoryType.SEMANTIC
    assert 0.0 <= record.confidence <= 1.0
    assert record.is_active()


def test_memory_record_rejects_invalid_confidence() -> None:
    with pytest.raises(ValidationError):
        MemoryRecord(user_id="u1", content="hello", confidence=1.5)


def test_memory_record_ttl_expiry() -> None:
    record = MemoryRecord(
        user_id="u1",
        content="temporary",
        ttl_seconds=60,
        created_at=utcnow() - timedelta(minutes=5),
    )
    assert record.is_expired()
    assert not record.is_active()


def test_superseded_record_is_inactive() -> None:
    record = MemoryRecord(user_id="u1", content="old", superseded_by="new-id")
    assert not record.is_active()

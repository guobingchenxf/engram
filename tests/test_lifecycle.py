from __future__ import annotations

from datetime import timedelta

from engram.models import MemoryQuery, utcnow
from engram.service import MemoryService


def test_explicit_change_supersedes_old_memory(service: MemoryService) -> None:
    old = service.write_text("u1", "我偏好中文解释，记住。")
    new = service.write_text("u1", "我改主意了：现在更偏好英文解释。")

    old_after = service.store.get(old.id)
    assert old_after is not None
    assert old_after.superseded_by == new.id
    assert new.supersedes

    conflicts = service.conflicts(user_id="u1")
    assert conflicts

    result = service.search(MemoryQuery(user_id="u1", query="我的语言偏好"))
    assert "英文" in result.context
    assert "中文" not in result.context


def test_ttl_expiry_marks_record(service: MemoryService) -> None:
    record = service.write_text("u1", "temporary task state", ttl_seconds=60)
    record.created_at = utcnow() - timedelta(minutes=5)
    service.store.update(record)

    result = service.maintenance()
    assert result["expired"] >= 1
    loaded = service.store.get(record.id)
    assert loaded is not None
    assert loaded.valid_to is not None


def test_decay_prunes_low_importance(service: MemoryService) -> None:
    record = service.write_text("u1", "low value", importance=0.01)
    record.last_accessed = utcnow() - timedelta(days=365)
    service.store.update(record)

    service.maintenance()
    loaded = service.store.get(record.id)
    assert loaded is not None
    assert loaded.valid_to is not None

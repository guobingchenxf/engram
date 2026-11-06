from __future__ import annotations

import pytest

from engram.errors import MemoryNotFound, SafetyViolation
from engram.models import (
    ExtractRequest,
    MemoryQuery,
    MemoryUpdateRequest,
    SourceType,
)
from engram.service import MemoryService


def test_write_search_update_delete(service: MemoryService) -> None:
    record = service.write_text("u1", "I prefer Chinese explanations.", importance=0.8)
    assert service.store.get(record.id) is not None

    result = service.search(MemoryQuery(user_id="u1", query="Chinese explanations"))
    assert result.hits

    updated = service.update(
        record.id, MemoryUpdateRequest(content="I prefer English explanations.")
    )
    assert "English" in updated.content

    service.delete(record.id)
    with pytest.raises(MemoryNotFound):
        service.delete(record.id)


def test_duplicate_write_is_merged(service: MemoryService) -> None:
    first = service.write_text("u1", "I prefer Chinese explanations.")
    second = service.write_text("u1", "I prefer Chinese explanations.")
    assert first.id == second.id
    assert service.stats("u1")["total"] == 1


def test_secret_write_is_rejected(service: MemoryService) -> None:
    with pytest.raises(SafetyViolation):
        service.write_text(
            "u1",
            "My API key is sk-abcdefghijklmnop123456.",
        )


def test_extract_and_write(service: MemoryService) -> None:
    records = service.extract_and_write(
        ExtractRequest(
            user_id="u1",
            text="我偏好中文解释，记住。我的目标是 2028 届搜广推实习。",
            source_type=SourceType.USER,
        )
    )
    assert len(records) >= 2


def test_forget(service: MemoryService) -> None:
    service.write_text("u1", "memory one")
    service.write_text("u1", "memory two")
    assert service.forget("u1") == 2
    assert service.stats("u1")["total"] == 0

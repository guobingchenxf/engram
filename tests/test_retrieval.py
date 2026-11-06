from __future__ import annotations

from engram.models import MemoryQuery
from engram.service import MemoryService


def test_relevant_memory_is_retrieved(service: MemoryService) -> None:
    service.write_text(
        "u1",
        "I prefer Chinese explanations for technical topics.",
        importance=0.9,
    )
    service.write_text("u1", "My favorite food is sushi.", importance=0.2)

    result = service.search(
        MemoryQuery(user_id="u1", query="Chinese explanations", top_k=5, token_budget=200)
    )
    assert result.hits
    assert "Chinese" in result.context


def test_sensitive_memory_is_filtered_by_default(service: MemoryService) -> None:
    service.write_text("u1", "我的手机号是 13800138000。", importance=0.9)

    hidden = service.search(
        MemoryQuery(user_id="u1", query="手机号", top_k=5, token_budget=200)
    )
    assert hidden.hits == []

    visible = service.search(
        MemoryQuery(
            user_id="u1",
            query="手机号",
            top_k=5,
            token_budget=200,
            include_sensitive=True,
        )
    )
    assert visible.hits


def test_token_budget_is_respected(service: MemoryService) -> None:
    long_text = "This is a long memory. " * 200
    service.write_text("u1", long_text)
    result = service.search(
        MemoryQuery(user_id="u1", query="long memory", top_k=5, token_budget=60)
    )
    assert result.total_tokens <= 60

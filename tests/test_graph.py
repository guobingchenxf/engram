from __future__ import annotations

from engram.models import MemoryQuery
from engram.service import MemoryService


def test_entity_graph_retrieves_without_lexical_overlap(service: MemoryService) -> None:
    service.write_text(
        "u1",
        "The first project is due Friday.",
        entities=["Alpha"],
    )

    result = service.search(
        MemoryQuery(user_id="u1", query="Alpha", top_k=3, token_budget=200)
    )
    assert result.hits
    assert any(hit.graph > 0 for hit in result.hits)
    assert "first project" in result.context


def test_graph_can_be_disabled(service: MemoryService) -> None:
    service.write_text(
        "u1",
        "The first project is due Friday.",
        entities=["Alpha"],
    )
    disabled = MemoryService(service.config.with_overrides(graph_enabled=False))
    result = disabled.search(
        MemoryQuery(
            user_id="u1",
            query="Alpha",
            top_k=3,
            token_budget=200,
            min_relevance=0.3,
        )
    )
    assert result.hits == []


def test_find_by_entities(service: MemoryService) -> None:
    record = service.write_text(
        "u1",
        "The first project is due Friday.",
        entities=["Alpha", "deadline"],
    )
    matches = service.store.find_by_entities("u1", ["alpha"])
    assert [item.id for item in matches] == [record.id]

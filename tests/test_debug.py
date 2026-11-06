from __future__ import annotations

from engram.debug import CounterfactualDebugger
from engram.models import MemoryQuery
from engram.service import MemoryService


def test_leave_one_out_influence(service: MemoryService) -> None:
    record = service.write_text(
        "u1",
        "The user prefers Chinese explanations.",
        importance=0.9,
    )
    explanation = CounterfactualDebugger(service, "u1").explain(
        "Chinese explanations"
    )
    assert explanation.hits >= 1
    assert "Chinese" in explanation.base_answer
    top = explanation.influences[0]
    assert top.memory_id == record.id
    assert top.influence == 1.0
    assert top.answer_changed is True


def test_shapley_and_fidelity(service: MemoryService) -> None:
    service.write_text(
        "u1",
        "The user prefers Chinese explanations.",
        importance=0.9,
    )
    service.write_text(
        "u1",
        "The user's favorite food is sushi.",
        importance=0.2,
    )
    explanation = CounterfactualDebugger(service, "u1").explain(
        "Chinese explanations",
        method="shapley",
        samples=6,
    )
    assert explanation.method == "shapley"
    assert explanation.influences
    assert "fidelity@1" in explanation.fidelity
    assert "fidelity@2" in explanation.fidelity


def test_exclude_ids_filters_retrieval(service: MemoryService) -> None:
    record = service.write_text("u1", "The user prefers Chinese explanations.")
    result = service.search(
        MemoryQuery(
            user_id="u1",
            query="Chinese explanations",
            exclude_ids=[record.id],
        )
    )
    assert all(hit.memory.id != record.id for hit in result.hits)

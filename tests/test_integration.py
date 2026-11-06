from __future__ import annotations

from engram.integrations import DeepResearchAgent, MemoryToolbox
from engram.service import MemoryService


def test_memory_toolbox_exposes_tools(service: MemoryService) -> None:
    toolbox = MemoryToolbox(service, "u1")
    record = toolbox.write("I prefer Chinese explanations.")
    assert record.id
    assert toolbox.search("Chinese")
    tools = toolbox.tools()
    assert set(tools) == {"memory_search", "memory_write"}
    assert toolbox.search_context("Chinese").startswith("Relevant long-term memories")


def test_deep_research_agent_writes_conclusion(service: MemoryService) -> None:
    service.write_text(
        "u1",
        "The user is preparing for 2028 Agent internships and prefers Chinese explanations.",
        importance=0.8,
    )
    agent = DeepResearchAgent(service, "u1")
    report = agent.run("What should the user prepare next?")

    assert report.question
    assert report.subquestions
    assert report.context
    assert report.written_memory_id
    assert report.conclusion

    records = service.store.list_memories("u1")
    assert any(
        record.metadata.get("kind") == "research_conclusion" for record in records
    )


def test_deep_research_uses_external_searcher(service: MemoryService) -> None:
    agent = DeepResearchAgent(
        service,
        "u1",
        searcher=lambda _query: ["external evidence"],
    )
    report = agent.run("What should the user prepare next?")
    assert "external evidence" in report.evidence

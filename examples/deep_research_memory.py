"""Run a minimal Deep Research loop with the memory layer.

    python examples/deep_research_memory.py
"""

from __future__ import annotations

import json

from engram import MemoryConfig, MemoryService
from engram.integrations import DeepResearchAgent


def main() -> None:
    config = MemoryConfig.from_env().with_overrides(
        db_path="./data/research_demo.db",
        trace_path="./data/research_demo_trace.jsonl",
        embedding_provider="hash",
        llm_provider="heuristic",
    )
    service = MemoryService(config)
    service.store.clear()

    service.write_text(
        "demo-user",
        "The user is preparing for 2028 Agent internships and prefers Chinese explanations.",
        importance=0.8,
    )

    agent = DeepResearchAgent(service, "demo-user")
    report = agent.run("What should the user prepare next?")
    print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    print("\nstored memories:")
    for record in service.store.list_memories("demo-user"):
        print("-", record.content[:120])


if __name__ == "__main__":
    main()

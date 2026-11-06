"""Minimal Python usage example.

Run after `pip install -e .`:

    python examples/quickstart.py
"""

from __future__ import annotations

from engram import (
    ExtractRequest,
    MemoryConfig,
    MemoryQuery,
    MemoryService,
    MemoryUpdateRequest,
    MemoryWriteRequest,
)


def main() -> None:
    config = MemoryConfig.from_env().with_overrides(
        db_path="./data/example.db",
        trace_path="./data/example_trace.jsonl",
        embedding_provider="hash",
        llm_provider="heuristic",
    )
    service = MemoryService(config)
    service.store.clear()

    # 1. Explicit write
    record = service.write(
        MemoryWriteRequest(
            user_id="u1",
            content="我偏好中文解释，记住。",
            importance=0.8,
        )
    )
    print("written:", record.model_dump(mode="json"))

    # 2. Extract from natural language
    service.extract_and_write(
        ExtractRequest(
            user_id="u1",
            text="我的目标是 2028 届 Agent 实习。",
        )
    )

    # 3. Search
    result = service.search(
        MemoryQuery(user_id="u1", query="我的语言偏好是什么", token_budget=300)
    )
    print("context:\n", result.context)
    print("tokens:", result.total_tokens)

    # 4. Update and delete
    updated = service.update(
        record.id,
        MemoryUpdateRequest(content="我偏好英文解释。"),
    )
    print("updated:", updated.content)
    service.delete(updated.id)

    print("stats:", service.stats("u1"))


if __name__ == "__main__":
    main()

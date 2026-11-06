from __future__ import annotations

import pytest

from engram.config import MemoryConfig
from engram.service import MemoryService


@pytest.fixture()
def config(tmp_path) -> MemoryConfig:
    return MemoryConfig.from_env().with_overrides(
        db_path=str(tmp_path / "memory.db"),
        trace_path=str(tmp_path / "trace.jsonl"),
        embedding_provider="hash",
        llm_provider="heuristic",
    )


@pytest.fixture()
def service(config: MemoryConfig) -> MemoryService:
    return MemoryService(config)

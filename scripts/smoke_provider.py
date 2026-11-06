"""Smoke test a real OpenAI-compatible provider.

    OPENAI_API_KEY=... OPENAI_BASE_URL=... python scripts/smoke_provider.py

Without a key it exits cleanly with a "skipped" message, so it is safe to run
in CI or locally.
"""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from engram.config import MemoryConfig
from engram.embeddings import get_embedding_provider
from engram.llm import get_llm


def main() -> int:
    base = MemoryConfig.from_env()
    if not base.llm_api_key:
        print("skipped: OPENAI_API_KEY is not set")
        return 0

    config = base.with_overrides(
        embedding_provider="openai",
        llm_provider="openai",
    )
    embedder = get_embedding_provider(config)
    vectors = embedder.embed(["hello", "world"])
    print(f"embedding dimension: {len(vectors[0])}")

    llm = get_llm(config)
    if llm is None:
        print("LLM provider returned no client")
        return 1
    answer = llm.complete(
        [{"role": "user", "content": "Reply with exactly: ok"}],
        max_tokens=8,
    )
    print(f"llm response: {answer!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

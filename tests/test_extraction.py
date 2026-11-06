from __future__ import annotations

from engram.config import MemoryConfig
from engram.extraction import MemoryExtractor


class FakeLLM:
    def complete(self, messages, *, temperature=0.0, max_tokens=1024):
        return (
            "```json\n"
            '[{"content":"I prefer English explanations.",'
            '"type":"semantic","entities":["English"],'
            '"confidence":0.9,"importance":0.8,'
            '"sensitivity":"personal","ttl_seconds":null,'
            '"rationale":"test"}]\n'
            "```"
        )


def test_heuristic_extracts_preference_and_goal(config: MemoryConfig) -> None:
    extractor = MemoryExtractor(None, config)
    items = extractor.extract(
        "我偏好中文解释，记住。我的目标是 2028 届搜广推实习。",
        user_id="u1",
    )
    assert len(items) >= 2
    assert any("中文" in item.content for item in items)
    assert any("2028" in item.content for item in items)
    assert all(item.type.value == "semantic" for item in items)


def test_llm_path_parses_json(config: MemoryConfig) -> None:
    extractor = MemoryExtractor(FakeLLM(), config)  # type: ignore[arg-type]
    items = extractor.extract("I prefer English explanations.", user_id="u1")
    assert len(items) == 1
    assert "English" in items[0].content
    assert items[0].confidence == 0.9


def test_extractor_deduplicates_identical_sentences(config: MemoryConfig) -> None:
    extractor = MemoryExtractor(None, config)
    items = extractor.extract(
        "我偏好中文解释。我偏好中文解释。",
        user_id="u1",
    )
    assert len(items) == 1

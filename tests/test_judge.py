from __future__ import annotations

from engram.judge import LLMJudge


class FakeJudgeLLM:
    def complete(self, messages, *, temperature=0.0, max_tokens=1024):
        prompt = messages[-1]["content"]
        if "Which answer is better" in prompt:
            return '{"winner":"A","confidence":0.9,"rationale":"A looks better"}'
        return '{"score":0.92,"label":"pass","rationale":"matches","confidence":0.9}'


def test_heuristic_judge() -> None:
    judge = LLMJudge(None)
    result = judge.judge("capital of France", "Paris", reference="Paris")
    assert result.score == 1.0
    assert result.label == "pass"


def test_llm_judge_parses_json() -> None:
    judge = LLMJudge(FakeJudgeLLM())  # type: ignore[arg-type]
    result = judge.judge("q", "answer", reference="reference")
    assert result.score == 0.92
    assert result.label == "pass"


def test_position_bias_probe_detects_inconsistent_winner() -> None:
    judge = LLMJudge(FakeJudgeLLM())  # type: ignore[arg-type]
    result = judge.judge_pairwise("q", "answer A", "answer B")
    assert result["first_winner"] == "A"
    assert result["second_winner"] == "A"
    assert result["position_bias"] is True


def test_length_bias_probe_returns_scores() -> None:
    judge = LLMJudge(None)
    result = judge.length_bias_probe("q", "Paris", reference="Paris")
    assert "base_score" in result
    assert "padded_score" in result
    assert "delta" in result

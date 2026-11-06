"""LLM-as-a-judge utilities with bias probes.

The judge is optional: without an LLM it falls back to a deterministic
token-overlap scorer so tests and offline runs still work. The important part
for evaluation is not the score itself but the bias probes: position swapping
and length padding can reveal a judge that is not measuring quality.
"""

from __future__ import annotations

import re

from pydantic import BaseModel, Field

from .llm import LLMClient, extract_json

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")


class JudgeResult(BaseModel):
    score: float = Field(ge=0.0, le=1.0)
    label: str
    rationale: str = ""
    confidence: float = Field(default=0.6, ge=0.0, le=1.0)


class LLMJudge:
    def __init__(
        self,
        llm: LLMClient | None = None,
        *,
        pass_threshold: float = 0.80,
        partial_threshold: float = 0.50,
    ) -> None:
        self.llm = llm
        self.pass_threshold = pass_threshold
        self.partial_threshold = partial_threshold

    def judge(
        self,
        query: str,
        answer: str,
        reference: str | None = None,
        context: str | None = None,
    ) -> JudgeResult:
        if self.llm is None:
            return self._heuristic(query, answer, reference)

        prompt = _JUDGE_PROMPT.format(
            query=query,
            reference=reference or "(none)",
            context=(context or "(none)")[:3000],
            answer=answer[:4000],
        )
        try:
            raw = self.llm.complete(
                [
                    {
                        "role": "system",
                        "content": "You are a strict evaluator. Return only JSON.",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
                max_tokens=500,
            )
            payload = extract_json(raw)
            if isinstance(payload, dict):
                score = float(payload.get("score", 0.0))
                return JudgeResult(
                    score=max(0.0, min(1.0, score)),
                    label=str(payload.get("label", self._label(score))),
                    rationale=str(payload.get("rationale", ""))[:500],
                    confidence=max(
                        0.0, min(1.0, float(payload.get("confidence", 0.7)))
                    ),
                )
        except Exception:  # noqa: BLE001, S110 - judge failures must not break evaluation
            pass
        return self._heuristic(query, answer, reference)

    def judge_pairwise(
        self,
        query: str,
        answer_a: str,
        answer_b: str,
        reference: str | None = None,
    ) -> dict[str, object]:
        first = self._pairwise(query, answer_a, answer_b, reference)
        second = self._pairwise(query, answer_b, answer_a, reference)
        # In the swapped call, "A" refers to the original answer B.
        second_as_original = {"A": "B", "B": "A", "tie": "tie"}.get(second, "tie")
        consistent = first == second_as_original
        return {
            "first_winner": first,
            "second_winner": second,
            "winner": first,
            "consistent": consistent,
            "position_bias": not consistent,
        }

    def length_bias_probe(
        self,
        query: str,
        answer: str,
        reference: str | None = None,
        *,
        padding: str = "This is additional padding text.",
        repeats: int = 20,
    ) -> dict[str, float]:
        base = self.judge(query, answer, reference)
        padded = self.judge(
            query,
            answer + " " + " ".join([padding] * repeats),
            reference,
        )
        return {
            "base_score": base.score,
            "padded_score": padded.score,
            "delta": padded.score - base.score,
        }

    def _pairwise(
        self,
        query: str,
        answer_a: str,
        answer_b: str,
        reference: str | None,
    ) -> str:
        if self.llm is None:
            score_a = self._heuristic(query, answer_a, reference).score
            score_b = self._heuristic(query, answer_b, reference).score
            if abs(score_a - score_b) < 0.05:
                return "tie"
            return "A" if score_a > score_b else "B"

        prompt = _PAIRWISE_PROMPT.format(
            query=query,
            reference=reference or "(none)",
            answer_a=answer_a[:3000],
            answer_b=answer_b[:3000],
        )
        try:
            raw = self.llm.complete(
                [
                    {
                        "role": "system",
                        "content": "You are a strict evaluator. Return only JSON.",
                    },
                    {"role": "user", "content": prompt},
                ],
                temperature=0.0,
                max_tokens=200,
            )
            payload = extract_json(raw)
            if isinstance(payload, dict):
                winner = str(payload.get("winner", "tie")).upper()
                if winner in {"A", "B", "TIE"}:
                    return winner if winner != "TIE" else "tie"
        except Exception:  # noqa: BLE001, S110
            pass
        score_a = self._heuristic(query, answer_a, reference).score
        score_b = self._heuristic(query, answer_b, reference).score
        if abs(score_a - score_b) < 0.05:
            return "tie"
        return "A" if score_a > score_b else "B"

    def _heuristic(
        self,
        query: str,
        answer: str,
        reference: str | None,
    ) -> JudgeResult:
        answer_tokens = _tokens(answer)
        if reference is not None:
            reference_tokens = _tokens(reference)
            if not answer_tokens or not reference_tokens:
                score = 0.0
            else:
                overlap = answer_tokens & reference_tokens
                precision = len(overlap) / len(answer_tokens)
                recall = len(overlap) / len(reference_tokens)
                score = (
                    2 * precision * recall / (precision + recall)
                    if precision + recall
                    else 0.0
                )
            rationale = "token F1 against reference"
        else:
            query_tokens = _tokens(query)
            overlap_count = len(answer_tokens & query_tokens)
            score = (
                min(1.0, overlap_count / max(1, len(query_tokens)))
                if query_tokens
                else 0.0
            )
            rationale = "token overlap with query (no reference)"

        return JudgeResult(
            score=score,
            label=self._label(score),
            rationale=rationale,
            confidence=0.6,
        )

    def _label(self, score: float) -> str:
        if score >= self.pass_threshold:
            return "pass"
        if score >= self.partial_threshold:
            return "partial"
        return "fail"


def _tokens(text: str) -> set[str]:
    return set(_TOKEN_RE.findall(text.lower()))


_JUDGE_PROMPT = """
Score the answer from 0 to 1 against the query and reference.

Query:
{query}

Reference (may be none):
{reference}

Context (may be none):
{context}

Answer:
{answer}

Return only JSON:
{{"score": 0.0, "label": "pass|partial|fail", "rationale": "...", "confidence": 0.0}}
""".strip()


_PAIRWISE_PROMPT = """
Which answer is better for the query? A or B? If they are equivalent, return tie.

Query:
{query}

Reference (may be none):
{reference}

Answer A:
{answer_a}

Answer B:
{answer_b}

Return only JSON:
{{"winner": "A|B|tie", "confidence": 0.0, "rationale": "..."}}
""".strip()

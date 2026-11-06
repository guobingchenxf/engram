"""Counterfactual memory debugging.

Two attribution methods are provided:

* leave-one-out occlusion: remove each retrieved memory and compare answers;
* sampled Shapley: estimate each memory's marginal contribution over random
  permutations.

A `fidelity@k` probe removes the top-k attributed memories at once and checks
whether the answer actually changes. This is a small but useful faithfulness
check; full Shapley over all memories is intentionally out of scope.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .models import MemoryQuery
from .service import MemoryService

_CONTEXT_LINE = re.compile(r"^- \[[^\]]+\] (.*?) \(source: [^)]+\)$")
_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")

Answerer = Callable[[str, str], str]


@dataclass
class Influence:
    memory_id: str
    content: str
    influence: float
    answer_changed: bool
    without_answer: str


@dataclass
class Explanation:
    query: str
    base_answer: str
    base_context: str
    hits: int
    method: str = "leave_one_out"
    influences: list[Influence] = field(default_factory=list)
    fidelity: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "base_answer": self.base_answer,
            "base_context": self.base_context,
            "hits": self.hits,
            "method": self.method,
            "fidelity": self.fidelity,
            "influences": [
                {
                    "memory_id": item.memory_id,
                    "content": item.content,
                    "influence": item.influence,
                    "answer_changed": item.answer_changed,
                    "without_answer": item.without_answer,
                }
                for item in self.influences
            ],
        }


class CounterfactualDebugger:
    def __init__(self, service: MemoryService, user_id: str) -> None:
        self.service = service
        self.user_id = user_id

    def explain(
        self,
        query: str,
        *,
        top_k: int = 5,
        token_budget: int = 500,
        answerer: Answerer | None = None,
        method: str = "leave_one_out",
        samples: int = 12,
    ) -> Explanation:
        ask = answerer or extractive_answer
        base = self.service.search(
            MemoryQuery(
                user_id=self.user_id,
                query=query,
                top_k=top_k,
                token_budget=token_budget,
            )
        )
        base_answer = ask(query, base.context)
        if not base.hits:
            return Explanation(
                query=query,
                base_answer=base_answer,
                base_context=base.context,
                hits=0,
                method=method,
            )

        if method == "shapley":
            influences = self._shapley(
                query, base.hits, ask, token_budget, samples
            )
        else:
            influences = self._leave_one_out(query, base.hits, ask, token_budget)

        influences.sort(key=lambda item: item.influence, reverse=True)
        explanation = Explanation(
            query=query,
            base_answer=base_answer,
            base_context=base.context,
            hits=len(base.hits),
            method=method,
            influences=influences,
        )
        explanation.fidelity = {
            "fidelity@1": self.fidelity_at_k(explanation, query, ask, k=1),
            "fidelity@2": self.fidelity_at_k(explanation, query, ask, k=2),
        }
        return explanation

    def fidelity_at_k(
        self,
        explanation: Explanation,
        query: str,
        answerer: Answerer | None = None,
        *,
        k: int = 1,
    ) -> float:
        ask = answerer or extractive_answer
        top_ids = [
            item.memory_id
            for item in explanation.influences[: max(1, k)]
            if item.influence > 0
        ]
        if not top_ids:
            return 0.0
        without = self.service.search(
            MemoryQuery(
                user_id=self.user_id,
                query=query,
                top_k=explanation.hits,
                token_budget=500,
                exclude_ids=top_ids,
            )
        )
        return 1.0 if ask(query, without.context) != explanation.base_answer else 0.0

    def _leave_one_out(
        self,
        query: str,
        hits: list[Any],
        ask: Answerer,
        token_budget: int,
    ) -> list[Influence]:
        influences: list[Influence] = []
        for hit in hits:
            without = self.service.search(
                MemoryQuery(
                    user_id=self.user_id,
                    query=query,
                    top_k=len(hits),
                    token_budget=token_budget,
                    exclude_ids=[hit.memory.id],
                )
            )
            without_answer = ask(query, without.context)
            changed = without_answer != ask(
                query, self._base_context(query, hits, token_budget)
            )
            influences.append(
                Influence(
                    memory_id=hit.memory.id,
                    content=hit.memory.content,
                    influence=1.0 if changed else 0.0,
                    answer_changed=changed,
                    without_answer=without_answer,
                )
            )
        return influences

    def _shapley(
        self,
        query: str,
        hits: list[Any],
        ask: Answerer,
        token_budget: int,
        samples: int,
    ) -> list[Influence]:
        ids = [hit.memory.id for hit in hits]
        contents = {hit.memory.id: hit.memory.content for hit in hits}
        contributions = {memory_id: 0.0 for memory_id in ids}

        for order in _deterministic_orders(ids, max(1, samples)):
            prefix: list[str] = []
            previous = ask(query, self._context_for(query, prefix, token_budget))
            for memory_id in order:
                prefix.append(memory_id)
                current = ask(query, self._context_for(query, prefix, token_budget))
                contributions[memory_id] += 1.0 if current != previous else 0.0
                previous = current

        total = max(1, samples)
        return [
            Influence(
                memory_id=memory_id,
                content=contents[memory_id],
                influence=contributions[memory_id] / total,
                answer_changed=contributions[memory_id] > 0,
                without_answer="",
            )
            for memory_id in ids
        ]

    def _base_context(
        self, query: str, hits: list[Any], token_budget: int
    ) -> str:
        return self._context_for(query, [hit.memory.id for hit in hits], token_budget)

    def _context_for(
        self, query: str, ids: list[str], token_budget: int
    ) -> str:
        if not ids:
            return ""
        result = self.service.search(
            MemoryQuery(
                user_id=self.user_id,
                query=query,
                top_k=len(ids),
                token_budget=token_budget,
                include_ids=ids,
            )
        )
        return result.context


def _deterministic_orders(ids: list[str], samples: int) -> list[list[str]]:
    """Deterministic Fisher-Yates permutations for reproducible Shapley sampling."""
    orders: list[list[str]] = []
    for sample in range(samples):
        order = list(ids)
        state = 7 + sample * 7919
        for index in range(len(order) - 1, 0, -1):
            state = (1103515245 * state + 12345) & 0x7FFFFFFF
            swap = state % (index + 1)
            order[index], order[swap] = order[swap], order[index]
        orders.append(order)
    return orders


def extractive_answer(query: str, context: str) -> str:
    """Pick the context line with the highest token overlap with the query."""
    query_tokens = set(_tokens(query))
    best_line = ""
    best_score = -1
    for line in context.splitlines():
        match = _CONTEXT_LINE.match(line)
        if not match:
            continue
        content = match.group(1)
        score = len(query_tokens & set(_tokens(content)))
        if score > best_score:
            best_score = score
            best_line = content
    return best_line or "no answer"


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())

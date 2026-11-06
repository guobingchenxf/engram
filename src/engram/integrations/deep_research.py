"""A minimal Deep Research integration.

This module is deliberately framework-free. `MemoryToolbox` exposes the four
memory operations as agent tools, and `DeepResearchAgent` shows a plan/search/
synthesize loop that can be replaced by an LLM planner. The point is the
integration contract, not a production research agent.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any

from ..models import MemoryQuery, MemoryRecord
from ..service import MemoryService


class MemoryToolbox:
    """Expose memory as tools that any agent loop can call."""

    def __init__(
        self,
        service: MemoryService,
        user_id: str,
        *,
        token_budget: int = 500,
        top_k: int = 5,
        min_relevance: float = 0.0,
    ) -> None:
        self.service = service
        self.user_id = user_id
        self.token_budget = token_budget
        self.top_k = top_k
        self.min_relevance = min_relevance

    def search_context(self, query: str) -> str:
        result = self.service.search(
            MemoryQuery(
                user_id=self.user_id,
                query=query,
                top_k=self.top_k,
                token_budget=self.token_budget,
                min_relevance=self.min_relevance,
            )
        )
        return result.context or "(no relevant memories)"

    def search(self, query: str) -> list[MemoryRecord]:
        result = self.service.search(
            MemoryQuery(
                user_id=self.user_id,
                query=query,
                top_k=self.top_k,
                token_budget=self.token_budget,
                min_relevance=self.min_relevance,
            )
        )
        return [hit.memory for hit in result.hits]

    def write(self, content: str, **kwargs: Any) -> MemoryRecord:
        return self.service.write_text(self.user_id, content, **kwargs)

    def tools(self) -> dict[str, Callable[..., str]]:
        return {
            "memory_search": self.search_context,
            "memory_write": self._write_tool,
        }

    def _write_tool(self, content: str) -> str:
        return self.write(content).id


@dataclass
class ResearchReport:
    question: str
    subquestions: list[str] = field(default_factory=list)
    context: str = ""
    evidence: list[str] = field(default_factory=list)
    conclusion: str = ""
    written_memory_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "question": self.question,
            "subquestions": self.subquestions,
            "context": self.context,
            "evidence": self.evidence,
            "conclusion": self.conclusion,
            "written_memory_id": self.written_memory_id,
        }


class DeepResearchAgent:
    """A deterministic plan/search/synthesize loop with memory tools.

    Replace the planner and `_compose` with an LLM for a real research agent.
    The `searcher` callback lets you plug in a web search, a document index, or
    the P0 Deep Research harness.
    """

    def __init__(
        self,
        service: MemoryService,
        user_id: str,
        *,
        searcher: Callable[[str], Sequence[str]] | None = None,
    ) -> None:
        self.toolbox = MemoryToolbox(service, user_id)
        self.searcher = searcher

    def run(self, question: str, *, max_evidence: int = 6) -> ResearchReport:
        context = self.toolbox.search_context(question)
        subquestions = [
            f"{question} 的背景是什么？",
            f"{question} 的下一步是什么？",
        ]

        evidence: list[str] = []
        if self.searcher is not None:
            for subquestion in subquestions:
                evidence.extend(self.searcher(subquestion))
        elif context != "(no relevant memories)":
            evidence.append(context)

        # Preserve order while removing duplicates.
        evidence = list(dict.fromkeys(item for item in evidence if item))[:max_evidence]
        conclusion = self._compose(question, context, evidence)
        record = self.toolbox.write(
            conclusion,
            importance=0.7,
            metadata={"kind": "research_conclusion", "question": question},
        )

        return ResearchReport(
            question=question,
            subquestions=subquestions,
            context=context,
            evidence=evidence,
            conclusion=conclusion,
            written_memory_id=record.id,
        )

    def _compose(self, question: str, context: str, evidence: Sequence[str]) -> str:
        if evidence:
            return (
                f"关于「{question}」：基于长期记忆和 {len(evidence)} 条检索证据，"
                "建议先明确目标与约束，再按证据逐项验证，最后把结论写回记忆。"
            )
        return (
            f"关于「{question}」：当前没有足够的相关记忆或检索证据，"
            "应先补充背景信息，再决定下一步。"
        )

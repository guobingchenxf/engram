"""A deterministic multi-session demo agent.

The demo is intentionally LLM-free: it shows how a caller uses the Memory API
(write, extract, search) and how the retrieved context changes across sessions.
"""

from __future__ import annotations

import re

from .config import MemoryConfig
from .models import ExtractRequest, MemoryQuery, SourceType
from .service import MemoryService

_QUESTION = re.compile(r"(\?|？|什么|哪些|怎么|如何|为什么|吗|what|how|why|which)", re.IGNORECASE)


class DemoAgent:
    def __init__(
        self,
        service: MemoryService,
        user_id: str = "demo-user",
        *,
        token_budget: int = 420,
    ) -> None:
        self.service = service
        self.user_id = user_id
        self.token_budget = token_budget

    def respond(self, message: str) -> str:
        # 1) Extract durable memories from the user's message.
        self.service.extract_and_write(
            ExtractRequest(
                user_id=self.user_id,
                text=message,
                source_type=SourceType.USER,
                source="demo-agent",
            )
        )

        # 2) Retrieve what is relevant for this turn.
        result = self.service.search(
            MemoryQuery(
                user_id=self.user_id,
                query=message,
                top_k=6,
                token_budget=self.token_budget,
            )
        )

        if not result.hits:
            return "我还没有关于你的长期记忆。你可以先告诉我你的偏好、目标或正在做的事。"

        if _QUESTION.search(message):
            return "根据长期记忆：\n" + result.context

        return f"我记下了。当前检索到 {len(result.hits)} 条相关记忆。"


SESSIONS = (
    "我偏好中文解释，我的目标是 2028 届搜广推算法实习，记住我对 Agent 也很感兴趣。",
    "我改主意了：现在更偏好 Agent 方向，目标也改成 Agent 实习。",
    "我的目标是什么？现在应该投什么方向？",
    "请记住：我的主力项目是推荐系统和 Agent Memory。",
    "我的偏好是什么？",
)


def run_demo() -> None:
    config = MemoryConfig.from_env().with_overrides(
        db_path="./data/demo.db",
        trace_path="./data/demo_trace.jsonl",
        embedding_provider="hash",
        llm_provider="heuristic",
    )
    service = MemoryService(config)
    service.store.clear()
    agent = DemoAgent(service)

    print("Engram multi-session demo")
    print("=" * 40)
    for index, message in enumerate(SESSIONS, start=1):
        print(f"\n--- Session {index} ---")
        print(f"User:  {message}")
        print(f"Agent: {agent.respond(message)}")

    print("\n--- Final stats ---")
    print(service.stats("demo-user"))
    print("\n--- Conflicts ---")
    for conflict in service.conflicts(user_id="demo-user"):
        print(conflict.model_dump(mode="json"))
    print("\nTrace written to ./data/demo_trace.jsonl")
    service.close()


if __name__ == "__main__":  # pragma: no cover
    run_demo()

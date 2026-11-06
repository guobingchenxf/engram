from __future__ import annotations

import httpx

from engram.embeddings import OpenAIEmbedding
from engram.llm import OpenAICompatibleLLM


def _client(handler) -> httpx.Client:
    return httpx.Client(
        transport=httpx.MockTransport(handler),
        base_url="https://example.test/v1/",
    )


def test_llm_retries_on_429() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, json={"error": "rate limited"})
        return httpx.Response(
            200, json={"choices": [{"message": {"content": "ok"}}]}
        )

    llm = OpenAICompatibleLLM(
        model="test-model",
        api_key=None,
        base_url="https://example.test/v1",
        http_client=_client(handler),
        max_retries=2,
        retry_backoff=0.0,
    )
    assert llm.complete([{"role": "user", "content": "hi"}]) == "ok"
    assert len(calls) == 2


def test_embedding_retries_on_503() -> None:
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(503, json={"error": "unavailable"})
        return httpx.Response(
            200,
            json={"data": [{"index": 0, "embedding": [0.1, 0.2, 0.3]}]},
        )

    embedding = OpenAIEmbedding(
        model="test-embedding",
        api_key=None,
        base_url="https://example.test/v1",
        http_client=_client(handler),
        max_retries=2,
        retry_backoff=0.0,
    )
    assert embedding.embed(["hello"]) == [[0.1, 0.2, 0.3]]
    assert len(calls) == 2

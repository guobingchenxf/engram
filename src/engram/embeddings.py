"""Embedding providers.

The default provider is a deterministic hash embedding so the project can be
developed and tested without an API key. Production users should switch to an
OpenAI-compatible embedding endpoint.
"""

from __future__ import annotations

import hashlib
import itertools
import math
import re
import time
from collections.abc import Sequence
from typing import Protocol

import httpx
import numpy as np

from .config import MemoryConfig
from .errors import ConfigurationError

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")
_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class EmbeddingProvider(Protocol):
    dim: int

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        """Return one embedding per input text."""


class HashEmbedding:
    """A deterministic, offline bag-of-tokens embedding.

    It is not semantically strong, but it is stable, dependency-free and good
    enough to exercise retrieval, dedupe and evaluation paths offline.
    """

    def __init__(self, dim: int = 256) -> None:
        if dim <= 0:
            raise ConfigurationError("embedding dim must be positive")
        self.dim = dim

    def _embed_one(self, text: str) -> list[float]:
        vector = np.zeros(self.dim, dtype=np.float32)
        tokens = _TOKEN_RE.findall(text.lower())
        if not tokens:
            return vector.tolist()

        for token in tokens:
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            index = int.from_bytes(digest[:4], "little") % self.dim
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            weight = 1.0 + math.log1p(len(token))
            vector[index] += sign * weight

        # Character bigrams give Chinese text a little more signal.
        for left, right in itertools.pairwise(tokens):
            if len(left) == 1 and len(right) == 1 and _is_cjk(left) and _is_cjk(right):
                digest = hashlib.blake2b((left + right).encode("utf-8"), digest_size=8).digest()
                index = int.from_bytes(digest[:4], "little") % self.dim
                sign = 1.0 if digest[4] % 2 == 0 else -1.0
                vector[index] += sign * 0.5

        norm = float(np.linalg.norm(vector))
        if norm > 0:
            vector /= norm
        return vector.tolist()

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._embed_one(text) for text in texts]


class OpenAIEmbedding:
    """Embeddings from an OpenAI-compatible `/embeddings` endpoint."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None,
        base_url: str,
        dim: int = 1536,
        timeout: float = 30.0,
        http_client: httpx.Client | None = None,
        max_retries: int = 2,
        retry_backoff: float = 0.5,
    ) -> None:
        if not api_key and http_client is None:
            raise ConfigurationError("OPENAI_API_KEY is required for OpenAI embeddings")

        self.model = model
        self.dim = dim
        self.max_retries = max(0, max_retries)
        self.retry_backoff = max(0.0, retry_backoff)
        self._client = http_client or httpx.Client(
            base_url=base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )

    def close(self) -> None:
        self._client.close()

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        payload = {"model": self.model, "input": list(texts)}
        last_error: Exception | None = None

        for attempt in range(self.max_retries + 1):
            try:
                response = self._client.post("embeddings", json=payload)
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                time.sleep(self.retry_backoff * (2**attempt))
                continue

            if response.status_code in _RETRYABLE_STATUS and attempt < self.max_retries:
                last_error = ConfigurationError(f"retryable HTTP {response.status_code}")
                time.sleep(self.retry_backoff * (2**attempt))
                continue

            try:
                response.raise_for_status()
                data = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise ConfigurationError(f"embedding request failed: {exc}") from exc

            vectors = [
                item["embedding"]
                for item in sorted(data["data"], key=lambda item: item["index"])
            ]
            if vectors:
                self.dim = len(vectors[0])
            return vectors

        raise ConfigurationError(f"embedding request failed after retries: {last_error}")


def get_embedding_provider(config: MemoryConfig) -> EmbeddingProvider:
    provider = config.embedding_provider.lower()
    if provider == "hash":
        return HashEmbedding(config.embedding_dim)
    if provider == "openai":
        return OpenAIEmbedding(
            model=config.embedding_model,
            api_key=config.llm_api_key,
            base_url=config.llm_base_url,
            dim=config.embedding_dim,
            timeout=config.request_timeout_seconds,
            max_retries=config.llm_max_retries,
            retry_backoff=config.llm_retry_backoff,
        )
    raise ConfigurationError(f"unknown embedding provider: {config.embedding_provider}")


def cosine_similarity(left: Sequence[float], right: Sequence[float]) -> float:
    """Cosine similarity for two equal-length vectors; 0 for zero vectors."""
    a = np.asarray(left, dtype=np.float32)
    b = np.asarray(right, dtype=np.float32)
    if a.size == 0 or b.size == 0 or a.shape != b.shape:
        return 0.0
    denominator = float(np.linalg.norm(a) * np.linalg.norm(b))
    if denominator == 0.0:
        return 0.0
    return float(np.dot(a, b) / denominator)


def estimate_tokens(text: str) -> int:
    """A cheap token estimate for budgeting (CJK chars count as one)."""
    if not text:
        return 0
    chinese = len(re.findall(r"[\u4e00-\u9fff]", text))
    words = len(re.findall(r"[A-Za-z0-9_]+", text))
    return max(1, chinese + int(words * 1.3) + len(text) // 16)


def _is_cjk(char: str) -> bool:
    return "\u4e00" <= char <= "\u9fff"

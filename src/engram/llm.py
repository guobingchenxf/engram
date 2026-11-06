"""LLM client abstraction and JSON helpers."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Sequence
from typing import Any, Protocol

import httpx

from .config import MemoryConfig
from .errors import ConfigurationError, LLMError

_RETRYABLE_STATUS = {429, 500, 502, 503, 504}


class LLMClient(Protocol):
    def complete(
        self,
        messages: Sequence[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> str:
        """Return the assistant text for a chat-style message list."""


class OpenAICompatibleLLM:
    """A minimal client for OpenAI-compatible `/chat/completions` APIs."""

    def __init__(
        self,
        *,
        model: str,
        api_key: str | None,
        base_url: str,
        timeout: float = 30.0,
        http_client: httpx.Client | None = None,
        max_retries: int = 2,
        retry_backoff: float = 0.5,
    ) -> None:
        if not api_key and http_client is None:
            raise ConfigurationError("OPENAI_API_KEY is required for the OpenAI LLM provider")

        self.model = model
        self.max_retries = max(0, max_retries)
        self.retry_backoff = max(0.0, retry_backoff)
        self._client = http_client or httpx.Client(
            base_url=base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=timeout,
        )

    def complete(
        self,
        messages: Sequence[dict[str, str]],
        *,
        temperature: float = 0.0,
        max_tokens: int = 1024,
    ) -> str:
        payload = {
            "model": self.model,
            "messages": list(messages),
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        last_error: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                response = self._client.post("chat/completions", json=payload)
            except httpx.HTTPError as exc:
                last_error = exc
                if attempt >= self.max_retries:
                    break
                time.sleep(self.retry_backoff * (2**attempt))
                continue

            if response.status_code in _RETRYABLE_STATUS and attempt < self.max_retries:
                last_error = LLMError(f"retryable HTTP {response.status_code}")
                time.sleep(self.retry_backoff * (2**attempt))
                continue

            try:
                response.raise_for_status()
                data = response.json()
            except (httpx.HTTPError, ValueError) as exc:
                raise LLMError(f"LLM request failed: {exc}") from exc

            try:
                return data["choices"][0]["message"]["content"] or ""
            except (KeyError, IndexError, TypeError) as exc:
                raise LLMError("unexpected LLM response shape") from exc

        raise LLMError(f"LLM request failed after retries: {last_error}")


def get_llm(config: MemoryConfig) -> LLMClient | None:
    """Return a configured LLM, or None for the offline heuristic path."""
    provider = config.llm_provider.lower()
    if provider == "heuristic":
        return None
    if provider == "openai":
        return OpenAICompatibleLLM(
            model=config.llm_model,
            api_key=config.llm_api_key,
            base_url=config.llm_base_url,
            timeout=config.request_timeout_seconds,
            max_retries=config.llm_max_retries,
            retry_backoff=config.llm_retry_backoff,
        )
    raise ConfigurationError(f"unknown llm provider: {config.llm_provider}")


def extract_json(text: str) -> Any | None:
    """Best-effort extraction of the first JSON array/object in a string."""
    if not text:
        return None

    fenced = re.search(r"```(?:json)?\s*(.*?)```", text, flags=re.DOTALL | re.IGNORECASE)
    candidate = fenced.group(1) if fenced else text
    start = _first_json_start(candidate)
    if start < 0:
        return None

    snippet = _balanced_slice(candidate, start)
    if snippet is None:
        return None

    try:
        return json.loads(snippet)
    except json.JSONDecodeError:
        return None


def _first_json_start(text: str) -> int:
    positions = [position for position in (text.find("["), text.find("{")) if position >= 0]
    return min(positions) if positions else -1


def _balanced_slice(text: str, start: int) -> str | None:
    opening = text[start]
    closing = "]" if opening == "[" else "}"
    depth = 0
    in_string = False
    escaped = False

    for index in range(start, len(text)):
        char = text[index]
        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == opening:
            depth += 1
        elif char == closing:
            depth -= 1
            if depth == 0:
                return text[start : index + 1]
    return None

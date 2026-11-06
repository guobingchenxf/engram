"""Memory extraction.

Two paths are provided:

* an LLM path that asks a model for structured JSON;
* a deterministic heuristic path used offline, in tests, and as a fallback
  whenever the LLM output cannot be parsed.

The extractor only proposes candidates. Dedupe, safety and persistence happen
in :class:`engram.service.MemoryService`.
"""

from __future__ import annotations

import re
from typing import Any

from .config import MemoryConfig
from .llm import LLMClient, extract_json
from .models import ExtractedMemory, MemoryType, Sensitivity

_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?；;\n])")
_QUESTION = re.compile(
    r"(\?|？|什么|哪些|怎么|如何|为什么|吗|what|how|why|which)", re.IGNORECASE
)
_QUOTED = re.compile(r"[\"“”']([^\"“”']{2,40})[\"“”']")
_CAPITALIZED = re.compile(r"\b([A-Z][A-Za-z0-9_]{1,30})\b")
_LABELED = re.compile(
    r"(?:项目|project|模型|model)\s*([A-Za-z0-9\u4e00-\u9fff]{1,24})",
    re.IGNORECASE,
)
_KNOWN_ENTITIES = (
    "MMoE",
    "KuaiRec",
    "Hillstrom",
    "RQ-VAE",
    "UNNC",
    "2028",
    "2027",
    "AUC",
    "GAUC",
    "NDCG",
)

_HEURISTIC_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("remember", re.compile(r"(记住|记下|请记|remember|keep in mind|note that)", re.IGNORECASE)),
    (
        "preference",
        re.compile(
            r"(我|I).{0,16}(?:更喜欢|更偏好|喜欢|偏好|prefer|like|不喜欢|hate|讨厌)",
            re.IGNORECASE,
        ),
    ),
    ("goal", re.compile(r"(我的目标|目标是|我打算|我计划|I want|I plan|my goal|I aim)", re.IGNORECASE)),
    ("identity", re.compile(r"(我是|我叫|我在|I am|my name is|I'm a)", re.IGNORECASE)),
    ("constraint", re.compile(r"(不要|别|不能|避免|must not|avoid|don't|do not)", re.IGNORECASE)),
    ("decision", re.compile(r"(决定|结论是|我选了|we decided|I decided|final choice)", re.IGNORECASE)),
)


class MemoryExtractor:
    def __init__(
        self,
        llm: LLMClient | None,
        config: MemoryConfig,
    ) -> None:
        self.llm = llm
        self.config = config
        self.last_error: str | None = None

    def extract(
        self,
        text: str,
        *,
        user_id: str,
        source_type: str = "user",
    ) -> list[ExtractedMemory]:
        """Extract candidate memories from a block of text."""
        cleaned = (text or "").strip()
        if not cleaned:
            return []

        if self.llm is not None:
            try:
                candidates = self._extract_with_llm(cleaned)
                if candidates:
                    return self._dedupe(candidates)
            except Exception as exc:  # noqa: BLE001 - LLM failures must not break writes
                self.last_error = str(exc)

        return self._dedupe(self._extract_heuristic(cleaned, source_type=source_type))

    def _extract_with_llm(self, text: str) -> list[ExtractedMemory]:
        assert self.llm is not None
        prompt = _EXTRACTION_PROMPT.format(text=text[: self.config.max_content_chars * 2])
        raw = self.llm.complete(
            [
                {
                    "role": "system",
                    "content": (
                        "You are a memory extraction engine. You only output a JSON array. "
                        "Do not invent facts that are not in the text."
                    ),
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.0,
            max_tokens=1200,
        )
        payload = extract_json(raw)
        if payload is None:
            return []

        items: list[Any]
        if isinstance(payload, dict):
            items = payload.get("memories", [])
        elif isinstance(payload, list):
            items = payload
        else:
            return []

        results: list[ExtractedMemory] = []
        for item in items:
            if not isinstance(item, dict):
                continue
            content = str(item.get("content", "")).strip()
            if not content:
                continue
            try:
                results.append(
                    ExtractedMemory(
                        content=content[: self.config.max_content_chars],
                        type=_coerce_type(item.get("type")),
                        entities=[str(entity) for entity in item.get("entities", [])][:12],
                        confidence=_clamp(item.get("confidence", 0.7)),
                        importance=_clamp(item.get("importance", 0.5)),
                        sensitivity=_coerce_sensitivity(item.get("sensitivity")),
                        ttl_seconds=_optional_int(item.get("ttl_seconds")),
                        explicit=_coerce_bool(item.get("explicit", True)),
                        rationale=str(item.get("rationale", "llm"))[:200],
                    )
                )
            except (ValueError, TypeError):
                continue
        return results

    def _extract_heuristic(self, text: str, *, source_type: str) -> list[ExtractedMemory]:
        results: list[ExtractedMemory] = []
        for sentence in _split_sentences(text):
            if _QUESTION.search(sentence):
                continue
            kind = _match_kind(sentence)
            if kind is None:
                continue

            content = _clean_sentence(sentence)
            if len(content) < 4:
                continue

            memory_type, confidence, importance = _kind_defaults(kind)
            sensitivity = Sensitivity.SENSITIVE if _contains_sensitive_word(content) else Sensitivity.PERSONAL
            results.append(
                ExtractedMemory(
                    content=content[: self.config.max_content_chars],
                    type=memory_type,
                    entities=_extract_entities(content),
                    confidence=confidence,
                    importance=importance,
                    sensitivity=sensitivity,
                    ttl_seconds=_ttl_for_kind(kind),
                    rationale=f"heuristic:{kind}",
                )
            )
        return results

    def _dedupe(self, candidates: list[ExtractedMemory]) -> list[ExtractedMemory]:
        seen: set[str] = set()
        unique: list[ExtractedMemory] = []
        for candidate in candidates:
            key = _normalize(candidate.content)
            if not key or key in seen:
                continue
            seen.add(key)
            unique.append(candidate)
        return unique


def _split_sentences(text: str) -> list[str]:
    parts = [part.strip() for part in _SENTENCE_SPLIT.split(text) if part.strip()]
    return parts or [text]


def _match_kind(sentence: str) -> str | None:
    for kind, pattern in _HEURISTIC_RULES:
        if pattern.search(sentence):
            return kind
    return None


def _kind_defaults(kind: str) -> tuple[MemoryType, float, float]:
    if kind == "remember":
        return MemoryType.SEMANTIC, 0.90, 0.90
    if kind == "preference":
        return MemoryType.SEMANTIC, 0.82, 0.75
    if kind == "goal":
        return MemoryType.SEMANTIC, 0.78, 0.80
    if kind == "identity":
        return MemoryType.SEMANTIC, 0.85, 0.80
    if kind == "constraint":
        return MemoryType.SEMANTIC, 0.80, 0.80
    if kind == "decision":
        return MemoryType.EPISODIC, 0.75, 0.70
    return MemoryType.SEMANTIC, 0.65, 0.50


def _ttl_for_kind(kind: str) -> int | None:
    if kind == "remember":
        return None
    if kind == "decision":
        return 60 * 60 * 24 * 90
    return None


def _clean_sentence(sentence: str) -> str:
    cleaned = re.sub(r"^(请)?(记住|记下|请记)\s*[:：]?\s*", "", sentence.strip())
    return re.sub(r"\s+", " ", cleaned).strip("，,。.;； ")


def extract_entities(text: str) -> list[str]:
    """Public entity extractor used by graph retrieval and tests."""
    return _extract_entities(text)


def _extract_entities(text: str) -> list[str]:
    entities: list[str] = []
    entities.extend(match for match in _QUOTED.findall(text))
    entities.extend(match for match in _CAPITALIZED.findall(text) if len(match) > 2)
    entities.extend(match.strip() for match in _LABELED.findall(text) if match.strip())
    for known in _KNOWN_ENTITIES:
        if known.lower() in text.lower():
            entities.append(known)
    seen: set[str] = set()
    unique: list[str] = []
    for entity in entities:
        key = entity.lower()
        if key in seen:
            continue
        seen.add(key)
        unique.append(entity[:40])
    return unique[:12]


def _contains_sensitive_word(text: str) -> bool:
    words = ("身份证", "银行卡", "手机号", "住址", "密码", "工资", "薪资", "病历")
    return any(word in text for word in words)


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", text).lower()


def _coerce_type(value: Any) -> MemoryType:
    try:
        return MemoryType(str(value))
    except (ValueError, TypeError):
        return MemoryType.SEMANTIC


def _coerce_sensitivity(value: Any) -> Sensitivity:
    try:
        return Sensitivity(str(value))
    except (ValueError, TypeError):
        return Sensitivity.PERSONAL


def _clamp(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.7
    return max(0.0, min(1.0, number))


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _coerce_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "y"}
    return bool(value)


_EXTRACTION_PROMPT = """
Extract durable memories from the text below.

Return only a JSON array. Each item must have:
- content: a standalone sentence that makes sense without the original text
- type: one of "working", "episodic", "semantic", "procedural"
- entities: array of important entities (people, projects, metrics, dates)
- confidence: number between 0 and 1
- importance: number between 0 and 1
- sensitivity: one of "public", "personal", "sensitive", "secret"
- ttl_seconds: integer or null
- explicit: true if the user stated it directly, false if inferred
- rationale: short reason

Rules:
- Prefer stable facts, preferences, goals, constraints and decisions.
- Do not store small talk, tool output, or transient task state as semantic memory.
- Do not invent facts. If the text contains no durable memory, return [].
- Never store API keys, passwords, or private keys.

Text:
\"\"\"{text}\"\"\"
""".strip()

"""Text normalization, tokenization and deterministic content hashing.

These helpers are shared by storage (token index / exact dedupe), retrieval
(BM25) and the service (dedupe).  Keeping them in one module avoids the old
retrieval <-> storage import cycle and gives the tokenizer a single cache.
"""

from __future__ import annotations

import hashlib
import re
from functools import lru_cache

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[一-鿿]")
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_content(text: str) -> str:
    """Normalization used for exact-duplicate detection."""
    return _WHITESPACE_RE.sub("", text).lower()


@lru_cache(maxsize=16384)
def tokenize(text: str) -> tuple[str, ...]:
    """Deterministic tokens: words, identifiers and individual CJK chars."""
    return tuple(_TOKEN_RE.findall(text.lower()))


def content_hash(text: str) -> str:
    """Hash of normalized content for O(1) exact dedupe."""
    return hashlib.sha256(
        normalize_content(text).encode("utf-8")
    ).hexdigest()

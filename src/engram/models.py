"""Core data models for the Engram system.

The models are intentionally small and storage-agnostic. A record carries the
content itself, its provenance, its temporal validity, and the signals used by
retrieval (relevance is computed at query time; recency, importance and
confidence are stored).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


def utcnow() -> datetime:
    """Return a timezone-aware UTC timestamp."""
    return datetime.now(timezone.utc)


def ensure_utc(value: datetime | None) -> datetime | None:
    """Normalize naive datetimes (e.g. read from SQLite) to UTC."""
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class MemoryType(str, Enum):
    """The role a memory plays in an agent's state."""

    WORKING = "working"
    EPISODIC = "episodic"
    SEMANTIC = "semantic"
    PROCEDURAL = "procedural"


class SourceType(str, Enum):
    """Where a memory came from. Untrusted sources are filtered by safety."""

    USER = "user"
    AGENT = "agent"
    TOOL = "tool"
    WEB = "web"
    EMAIL = "email"
    OTHER_USER = "other_user"
    SYSTEM = "system"

    @property
    def is_trusted(self) -> bool:
        return self in {SourceType.USER, SourceType.AGENT, SourceType.SYSTEM}


class Sensitivity(str, Enum):
    PUBLIC = "public"
    PERSONAL = "personal"
    SENSITIVE = "sensitive"
    SECRET = "secret"


class MemoryRecord(BaseModel):
    """A single long-lived memory entry."""

    model_config = {"extra": "forbid"}

    id: str = Field(default_factory=lambda: uuid4().hex)
    user_id: str = Field(min_length=1)
    tenant_id: str = "default"
    type: MemoryType = MemoryType.SEMANTIC
    content: str = Field(min_length=1)
    summary: str | None = None
    entities: list[str] = Field(default_factory=list)
    source_type: SourceType = SourceType.USER
    source: str | None = None
    created_at: datetime = Field(default_factory=utcnow)
    updated_at: datetime = Field(default_factory=utcnow)
    valid_from: datetime | None = None
    valid_to: datetime | None = None
    confidence: float = 0.7
    importance: float = 0.5
    last_accessed: datetime | None = None
    access_count: int = 0
    ttl_seconds: int | None = None
    sensitivity: Sensitivity = Sensitivity.PERSONAL
    supersedes: list[str] = Field(default_factory=list)
    superseded_by: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    embedding: list[float] | None = None

    @field_validator("confidence", "importance")
    @classmethod
    def _unit_interval(cls, value: float) -> float:
        if not 0.0 <= value <= 1.0:
            raise ValueError("must be between 0 and 1")
        return value

    @field_validator("ttl_seconds")
    @classmethod
    def _non_negative_ttl(cls, value: int | None) -> int | None:
        if value is not None and value < 0:
            raise ValueError("ttl_seconds must be >= 0")
        return value

    def is_expired(self, now: datetime | None = None) -> bool:
        """True when the record's TTL or validity window has elapsed."""
        current = ensure_utc(now) or utcnow()
        valid_to = ensure_utc(self.valid_to)
        if valid_to is not None and valid_to <= current:
            return True
        if self.ttl_seconds is not None:
            created = ensure_utc(self.created_at) or current
            return current - created >= timedelta(seconds=self.ttl_seconds)
        return False

    def is_active(self, now: datetime | None = None) -> bool:
        if self.superseded_by:
            return False
        return not self.is_expired(now)


class MemoryWriteRequest(BaseModel):
    """Input for an explicit memory write."""

    user_id: str = Field(min_length=1)
    tenant_id: str | None = None
    id: str | None = None
    created_at: datetime | None = None
    content: str = Field(min_length=1)
    type: MemoryType = MemoryType.SEMANTIC
    source_type: SourceType = SourceType.USER
    source: str | None = None
    derived_from: list[str] = Field(default_factory=list)
    entities: list[str] = Field(default_factory=list)
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    importance: float | None = Field(default=None, ge=0.0, le=1.0)
    ttl_seconds: int | None = Field(default=None, ge=0)
    sensitivity: Sensitivity | None = None
    confirmed: bool = False
    idempotency_key: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    # Pre-signed attestation supplied by an external issuer (e.g. an agent
    # runtime). The service verifies it; it never trusts it blindly.
    attestation: dict[str, Any] | None = None


class MemoryUpdateRequest(BaseModel):
    """Partial update; only explicitly set fields are applied."""

    tenant_id: str | None = None
    content: str | None = None
    summary: str | None = None
    type: MemoryType | None = None
    entities: list[str] | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    importance: float | None = Field(default=None, ge=0.0, le=1.0)
    sensitivity: Sensitivity | None = None
    valid_to: datetime | None = None
    ttl_seconds: int | None = Field(default=None, ge=0)
    confirmed: bool = False
    metadata: dict[str, Any] | None = None


class ExplainRequest(BaseModel):
    """Input for counterfactual memory explanation."""

    user_id: str = Field(min_length=1)
    tenant_id: str | None = None
    query: str = Field(min_length=1)
    top_k: int = Field(default=5, ge=1, le=50)
    token_budget: int = Field(default=500, ge=32, le=32000)
    method: str = "leave_one_out"
    samples: int = Field(default=12, ge=1, le=200)


class ExtractRequest(BaseModel):
    """Input for extraction from a block of text."""

    user_id: str = Field(min_length=1)
    tenant_id: str | None = None
    text: str = Field(min_length=1)
    source_type: SourceType = SourceType.USER
    source: str | None = None
    auto_write: bool = True


class ExtractedMemory(BaseModel):
    """A candidate memory produced by the extraction pipeline."""

    content: str = Field(min_length=1)
    type: MemoryType = MemoryType.SEMANTIC
    entities: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.7, ge=0.0, le=1.0)
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    sensitivity: Sensitivity = Sensitivity.PERSONAL
    ttl_seconds: int | None = Field(default=None, ge=0)
    explicit: bool = True
    rationale: str | None = None


class MemoryQuery(BaseModel):
    """A retrieval request."""

    user_id: str = Field(min_length=1)
    tenant_id: str | None = None
    query: str = Field(min_length=1)
    types: list[MemoryType] | None = None
    top_k: int = Field(default=8, ge=1, le=100)
    token_budget: int = Field(default=800, ge=32, le=32000)
    include_expired: bool = False
    include_sensitive: bool = False
    include_ids: list[str] = Field(default_factory=list)
    exclude_ids: list[str] = Field(default_factory=list)
    time_range: tuple[datetime, datetime] | None = None
    min_relevance: float = Field(default=0.15, ge=0.0, le=1.0)
    min_confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    min_importance: float = Field(default=0.0, ge=0.0, le=1.0)


class SearchHit(BaseModel):
    """A scored memory returned by retrieval."""

    memory: MemoryRecord
    score: float
    relevance: float
    recency: float
    importance: float
    confidence: float
    graph: float = 0.0
    token_cost: int


class MemorySearchResult(BaseModel):
    """Result of a retrieval call, including a prompt-ready context block."""

    hits: list[SearchHit] = Field(default_factory=list)
    context: str = ""
    total_tokens: int = 0
    candidates: int = 0
    truncated: bool = False
    query: str = ""


class ConflictRecord(BaseModel):
    """A detected contradiction between two memories."""

    id: str = Field(default_factory=lambda: uuid4().hex)
    user_id: str
    tenant_id: str = "default"
    existing_id: str
    new_id: str
    reason: str
    status: str = "open"
    created_at: datetime = Field(default_factory=utcnow)
    resolved_at: datetime | None = None
    resolution: str | None = None


class SafetyAssessment(BaseModel):
    """Safety verdict for a candidate write."""

    allowed: bool
    reasons: list[str] = Field(default_factory=list)
    sensitivity: Sensitivity = Sensitivity.PERSONAL
    redacted_content: str | None = None
    requires_confirmation: bool = False


class MemoryStats(BaseModel):
    user_id: str | None = None
    tenant_id: str | None = None
    total: int = 0
    active: int = 0
    expired: int = 0
    conflicts_open: int = 0
    by_type: dict[str, int] = Field(default_factory=dict)

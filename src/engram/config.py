"""Configuration for Engram.

Everything is environment-driven so the same code can run offline (hash
embeddings + heuristic extraction) or against an OpenAI-compatible endpoint.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace

from .provenance import AttestationKey, parse_attestation_keys

try:  # optional convenience: load a local .env if python-dotenv is installed
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional dependency
    pass
else:
    load_dotenv()


def _env_str(name: str, default: str) -> str:
    return os.getenv(name, default)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    return int(raw) if raw is not None and raw.strip() else default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    return float(raw) if raw is not None and raw.strip() else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


def _env_list(name: str) -> tuple[str, ...]:
    raw = os.getenv(name, "")
    return tuple(item.strip() for item in raw.split(",") if item.strip())


@dataclass(frozen=True)
class RetrievalWeights:
    """Weights for the four retrieval signals. They are normalized on use."""

    relevance: float = 0.55
    recency: float = 0.20
    importance: float = 0.15
    confidence: float = 0.10

    def normalized(self) -> RetrievalWeights:
        total = self.relevance + self.recency + self.importance + self.confidence
        if total <= 0:
            raise ValueError("retrieval weights must sum to a positive value")
        return RetrievalWeights(
            relevance=self.relevance / total,
            recency=self.recency / total,
            importance=self.importance / total,
            confidence=self.confidence / total,
        )


@dataclass(frozen=True)
class MemoryConfig:
    db_path: str = "./data/memory.db"
    trace_path: str = "./data/trace.jsonl"

    # Tenant boundary. Every store query is scoped by this value; a request
    # that explicitly asks for another tenant is rejected.
    tenant_id: str = "default"

    embedding_provider: str = "hash"  # hash | openai
    embedding_dim: int = 256
    embedding_model: str = "text-embedding-3-small"

    llm_provider: str = "heuristic"  # heuristic | openai
    llm_model: str = "gpt-4o-mini"
    llm_api_key: str | None = field(default=None, repr=False)
    llm_base_url: str = "https://api.openai.com/v1"
    request_timeout_seconds: float = 30.0
    llm_max_retries: int = 2
    llm_retry_backoff: float = 0.5
    api_key: str | None = field(default=None, repr=False)
    require_auth: bool = True
    cors_origins: tuple[str, ...] = ()

    default_token_budget: int = 800
    # Hard cap on rows scanned per retrieval/conflict pass. The cap exists to
    # bound cost, not to define the candidate set; a truncated pass is reported
    # instead of silently returning a partial view.
    max_scan_candidates: int = 20000
    # Inverted-index candidate pool before exact scoring.  Larger improves
    # recall; smaller bounds latency.
    prefilter_candidates: int = 300
    recency_half_life_days: float = 30.0
    decay_lambda: float = 0.01
    dedupe_threshold: float = 0.92
    prune_importance_threshold: float = 0.05
    max_content_chars: int = 4000
    mmr_lambda: float = 0.70
    graph_enabled: bool = True
    graph_weight: float = 0.15

    allow_untrusted_semantic_write: bool = False
    require_confirmation_for_sensitive: bool = True
    redact_sensitive: bool = True
    retrieval_exclude_untrusted: bool = False
    enable_conflicts: bool = True
    safety_enabled: bool = True
    poison_classifier_enabled: bool = False
    provenance_enabled: bool = False

    # Signed provenance (HMAC-SHA256). The local key signs writes accepted by
    # this service; trusted_attestation_keys is the verification trust store.
    attestation_enabled: bool = False
    attestation_key_id: str = "local"
    attestation_secret: str | None = field(default=None, repr=False)
    attestation_issuer: str = "engram-service"
    trusted_attestation_keys: tuple[AttestationKey, ...] = ()
    require_attestation_for_agent: bool = False
    require_signed_parents: bool = True

    retrieval_weights: RetrievalWeights = field(default_factory=RetrievalWeights)

    def __post_init__(self) -> None:
        if self.embedding_dim <= 0:
            raise ValueError("embedding_dim must be positive")
        if self.max_scan_candidates < 1:
            raise ValueError("max_scan_candidates must be positive")
        if self.prefilter_candidates < 1:
            raise ValueError("prefilter_candidates must be positive")
        if not 0.0 <= self.mmr_lambda <= 1.0:
            raise ValueError("mmr_lambda must be between 0 and 1")
        if self.decay_lambda < 0:
            raise ValueError("decay_lambda must be non-negative")

    @property
    def attestation_active(self) -> bool:
        """True when signed provenance is explicitly enabled.

        ``from_env`` enables this automatically when a secret or trust store is
        present; a direct ``MemoryConfig`` must set ``attestation_enabled=True``.
        """
        return self.attestation_enabled

    @classmethod
    def from_env(cls) -> MemoryConfig:
        attestation_keys = parse_attestation_keys(
            os.getenv("MEMORY_ATTESTATION_KEYS")
        )
        attestation_secret = os.getenv("MEMORY_ATTESTATION_SECRET") or None
        return cls(
            db_path=_env_str("MEMORY_DB_PATH", "./data/memory.db"),
            trace_path=_env_str("MEMORY_TRACE_PATH", "./data/trace.jsonl"),
            tenant_id=_env_str("MEMORY_TENANT_ID", "default"),
            embedding_provider=_env_str("EMBEDDING_PROVIDER", "hash").lower(),
            embedding_dim=_env_int("EMBEDDING_DIM", 256),
            embedding_model=_env_str("EMBEDDING_MODEL", "text-embedding-3-small"),
            llm_provider=_env_str("LLM_PROVIDER", "heuristic").lower(),
            llm_model=_env_str("LLM_MODEL", "gpt-4o-mini"),
            llm_api_key=os.getenv("OPENAI_API_KEY") or None,
            llm_base_url=_env_str("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            request_timeout_seconds=_env_float("MEMORY_REQUEST_TIMEOUT", 30.0),
            llm_max_retries=_env_int("MEMORY_LLM_MAX_RETRIES", 2),
            llm_retry_backoff=_env_float("MEMORY_LLM_RETRY_BACKOFF", 0.5),
            api_key=os.getenv("MEMORY_API_KEY") or None,
            require_auth=_env_bool("MEMORY_REQUIRE_AUTH", True),
            cors_origins=_env_list("MEMORY_CORS_ORIGINS"),
            default_token_budget=_env_int("MEMORY_TOKEN_BUDGET", 800),
            max_scan_candidates=_env_int("MEMORY_MAX_SCAN_CANDIDATES", 20000),
            prefilter_candidates=_env_int("MEMORY_PREFILTER_CANDIDATES", 300),
            recency_half_life_days=_env_float("MEMORY_RECENCY_HALF_LIFE_DAYS", 30.0),
            decay_lambda=_env_float("MEMORY_DECAY_LAMBDA", 0.01),
            dedupe_threshold=_env_float("MEMORY_DEDUPE_THRESHOLD", 0.92),
            prune_importance_threshold=_env_float("MEMORY_PRUNE_IMPORTANCE_THRESHOLD", 0.05),
            max_content_chars=_env_int("MEMORY_MAX_CONTENT_CHARS", 4000),
            mmr_lambda=_env_float("MEMORY_MMR_LAMBDA", 0.70),
            graph_enabled=_env_bool("MEMORY_GRAPH_ENABLED", True),
            graph_weight=_env_float("MEMORY_GRAPH_WEIGHT", 0.15),
            allow_untrusted_semantic_write=_env_bool(
                "MEMORY_ALLOW_UNTRUSTED_SEMANTIC_WRITE", False
            ),
            require_confirmation_for_sensitive=_env_bool(
                "MEMORY_REQUIRE_CONFIRMATION_FOR_SENSITIVE", True
            ),
            redact_sensitive=_env_bool("MEMORY_REDACT_SENSITIVE", True),
            retrieval_exclude_untrusted=_env_bool(
                "MEMORY_RETRIEVAL_EXCLUDE_UNTRUSTED", False
            ),
            enable_conflicts=_env_bool("MEMORY_ENABLE_CONFLICTS", True),
            safety_enabled=_env_bool("MEMORY_SAFETY_ENABLED", True),
            poison_classifier_enabled=_env_bool(
                "MEMORY_POISON_CLASSIFIER", False
            ),
            provenance_enabled=_env_bool("MEMORY_PROVENANCE_ENABLED", False),
            attestation_enabled=_env_bool(
                "MEMORY_ATTESTATION_ENABLED",
                bool(attestation_secret or attestation_keys),
            ),
            attestation_key_id=_env_str("MEMORY_ATTESTATION_KEY_ID", "local"),
            attestation_secret=attestation_secret,
            attestation_issuer=_env_str(
                "MEMORY_ATTESTATION_ISSUER", "engram-service"
            ),
            trusted_attestation_keys=attestation_keys,
            require_attestation_for_agent=_env_bool(
                "MEMORY_REQUIRE_ATTESTATION_FOR_AGENT", False
            ),
            require_signed_parents=_env_bool(
                "MEMORY_ATTESTATION_REQUIRE_SIGNED_PARENTS", True
            ),
        )

    def with_overrides(self, **kwargs: object) -> MemoryConfig:
        """Return a copy with selected fields replaced (useful in tests)."""
        return replace(self, **kwargs)  # type: ignore[arg-type]

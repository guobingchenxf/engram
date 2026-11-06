"""Engram: a lifecycle-aware long-term memory layer for LLM agents."""

from .config import MemoryConfig, RetrievalWeights
from .models import (
    ConflictRecord,
    ExtractedMemory,
    ExtractRequest,
    MemoryQuery,
    MemoryRecord,
    MemorySearchResult,
    MemoryType,
    MemoryUpdateRequest,
    MemoryWriteRequest,
    SearchHit,
    Sensitivity,
    SourceType,
)
from .service import MemoryService

__version__ = "0.1.0"

__all__ = [
    "ConflictRecord",
    "ExtractRequest",
    "ExtractedMemory",
    "MemoryConfig",
    "MemoryQuery",
    "MemoryRecord",
    "MemorySearchResult",
    "MemoryService",
    "MemoryType",
    "MemoryUpdateRequest",
    "MemoryWriteRequest",
    "RetrievalWeights",
    "SearchHit",
    "Sensitivity",
    "SourceType",
    "__version__",
]

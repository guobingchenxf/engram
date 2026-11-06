"""Domain errors raised by Engram."""

from __future__ import annotations


class EngramError(Exception):
    """Base class for all Engram errors."""


class MemoryNotFound(EngramError):
    """The requested memory id does not exist."""


class SafetyViolation(EngramError):
    """A write was rejected by the safety guard."""


class ConflictError(EngramError):
    """A conflict operation could not be completed."""


class ConfigurationError(EngramError):
    """The configuration is invalid or incomplete."""


class LLMError(EngramError):
    """The configured LLM provider failed."""

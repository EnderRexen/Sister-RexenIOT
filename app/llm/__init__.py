"""LLM package."""

from .client import (
    LOCAL_CANDIDATES,
    LLMClient,
    LLMError,
    client,
    detect_provider,
    discover_local,
    probe_url,
)

__all__ = [
    "LOCAL_CANDIDATES",
    "LLMClient",
    "LLMError",
    "client",
    "detect_provider",
    "discover_local",
    "probe_url",
]

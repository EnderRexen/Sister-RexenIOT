"""LLM package."""

from .client import (
    LOCAL_CANDIDATES,
    LLMClient,
    LLMError,
    adopt_first_local,
    client,
    detect_provider,
    discover_local,
    probe_url,
)

__all__ = [
    "LOCAL_CANDIDATES",
    "LLMClient",
    "LLMError",
    "adopt_first_local",
    "client",
    "detect_provider",
    "discover_local",
    "probe_url",
]

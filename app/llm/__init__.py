"""LLM package."""

from .client import (
    LOCAL_CANDIDATES,
    LLMClient,
    LLMError,
    adopt_first_local,
    adopt_first_local_in_background,
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
    "adopt_first_local_in_background",
    "client",
    "detect_provider",
    "discover_local",
    "probe_url",
]

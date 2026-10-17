"""LLM package."""

from .client import LLMClient, LLMError, client, detect_provider

__all__ = ["client", "LLMClient", "LLMError", "detect_provider"]

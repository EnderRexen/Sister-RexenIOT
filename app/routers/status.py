"""Shared state and status routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends

from .. import __version__, config, hardware, personalities, routing
from ..llm import client as llm_client
from ..security import require_api_token
from ..tts import engine

router = APIRouter(tags=["status"])


@router.get("/health")
async def health() -> dict[str, Any]:
    """Liveness probe. Deliberately unauthenticated: it reveals only that the
    server is up, and the watchdog scripts poll it to decide whether to restart.
    ``requires_token`` is a boolean, not the token, so the UI can prompt before
    it has one. Everything that reveals or changes state requires the token."""
    from ..security import api_token

    return {"ok": True, "version": __version__, "requires_token": bool(api_token())}


@router.get("/status", dependencies=[Depends(require_api_token)])
async def status() -> dict[str, Any]:
    cfg = config.load()
    profile = hardware.cached()
    return {
        "version": __version__,
        "branding": cfg.branding.model_dump(),
        "backends": routing.backends(),
        "llm": llm_client.describe(),
        "tts": engine.status,
        "tools_enabled": cfg.tools.enabled,
        "personality": personalities.active().model_dump(),
        "hardware": hardware.summary(profile) if profile else None,
        "has_hardware_scan": bool(profile),
        "first_run_complete": cfg.first_run_complete,
    }

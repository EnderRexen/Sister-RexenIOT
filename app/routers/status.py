"""Shared state and status routes."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter

from .. import __version__, config, hardware, personalities, routing
from ..llm import client as llm_client
from ..tts import engine

router = APIRouter(tags=["status"])


@router.get("/health")
async def health() -> dict[str, Any]:
    return {"ok": True, "version": __version__}


@router.get("/status")
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

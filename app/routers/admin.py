"""Admin routes: configuration, personalities, hardware scan, model download.

Every write endpoint requires the admin token when one is set. The token is read
from ``ADMIN_TOKEN`` or the config file, and compared in constant time.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import os
import shutil
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Header, HTTPException
from pydantic import BaseModel, Field

from .. import config, hardware, model_catalog, personalities, skills, uploads
from ..llm import client as llm_client
from ..tools import registry
from ..tts import engine

log = logging.getLogger("rexeniot.api.admin")
router = APIRouter(prefix="/admin", tags=["admin"])


# --------------------------------------------------------------------------- auth


def _admin_token() -> str:
    return os.environ.get("ADMIN_TOKEN", "").strip() or config.load().admin_token.strip()


def require_admin(token: str | None) -> None:
    expected = _admin_token()
    if not expected:
        return  # token unset means local open access; documented as such
    if not token or not hmac.compare_digest(token, expected):
        raise HTTPException(401, "Invalid admin token.")


def _check(token: str | None) -> None:
    require_admin(token)


# ----------------------------------------------------------------------- endpoints


@router.get("/session")
async def session() -> dict[str, Any]:
    """Whether the caller needs a token, and a digest of current state."""
    return {
        "requires_token": bool(_admin_token()),
        "first_run_complete": config.load().first_run_complete,
    }


@router.post("/verify")
async def verify(x_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    require_admin(x_admin_token)
    return {"ok": True}


@router.get("/config")
async def get_config(x_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    _check(x_admin_token)
    return config.public_view(config.load())


class ConfigPatch(BaseModel):
    patch: dict[str, Any]


@router.put("/config")
async def put_config(
    body: ConfigPatch, x_admin_token: str | None = Header(default=None)
) -> dict[str, Any]:
    _check(x_admin_token)
    patch = dict(body.patch)

    # "***" is the masked placeholder the UI round-trips; never store it.
    for section, field in (("llm", "api_key"), ("tools", "home_assistant_token"),
                           ("tools", "mqtt_password"), ("_", "admin_token")):
        container = patch if section == "_" else patch.get(section)
        if isinstance(container, dict) and container.get(field) == "***":
            container.pop(field, None)
    if patch.get("admin_token") == "***":
        patch.pop("admin_token", None)

    # A TTS model change must take effect on the next request, not at restart.
    # Load it *before* persisting: config.update() writes to disk, so a model
    # that cannot load would otherwise be recorded permanently and retried on
    # every start, with only a log line to show for it. Same ordering as
    # /hardware/apply.
    new_model = (patch.get("tts") or {}).get("model_id")
    if new_model:
        try:
            await asyncio.to_thread(engine.ensure_loaded, new_model)
        except Exception as exc:
            raise HTTPException(
                400, f"Could not load TTS model '{new_model}': {exc}"
            ) from exc

    try:
        cfg = config.update(patch)
    except Exception as exc:
        raise HTTPException(400, f"Invalid configuration: {exc}") from exc

    llm_client.refresh()
    return config.public_view(cfg)


# ------------------------------------------------------------------ hardware scan


@router.post("/hardware/scan")
async def hardware_scan(
    x_admin_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Probe the machine and return a profile plus model recommendations."""
    _check(x_admin_token)
    profile = await asyncio.to_thread(hardware.scan, True)
    profile["summary"] = hardware.summary(profile)
    return profile


@router.get("/hardware")
async def hardware_cached(x_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    _check(x_admin_token)
    profile = hardware.cached()
    if not profile:
        return {"scanned": False}
    profile["summary"] = hardware.summary(profile)
    return {"scanned": True, **profile}


@router.get("/storage")
async def storage_status(x_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    """Where state lives and how much room it takes."""
    _check(x_admin_token)
    return {
        "home": str(config.ROOT),
        "portable": config.PORTABLE,
        "data_dir": str(config.DATA_DIR),
        "uploads": uploads.stats(),
        "models_mb": _dir_mb(config.MODELS_DIR),
        "cache_mb": _dir_mb(config.DATA_DIR / "cache"),
        "data_mb": _dir_mb(config.DATA_DIR),
    }


def _dir_mb(path: Path) -> float:
    if not path.exists():
        return 0.0
    total = 0
    for item in path.rglob("*"):
        try:
            if item.is_file():
                total += item.stat().st_size
        except OSError:
            continue
    return round(total / 1_048_576, 2)


@router.post("/hardware/apply")
async def hardware_apply(
    x_admin_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Adopt the scan's recommendations: TTS tier, and an LLM suggestion if unset."""
    _check(x_admin_token)
    profile = await asyncio.to_thread(hardware.scan, True)
    recs = profile["recommendations"]

    patch: dict[str, Any] = {
        "tts": {
            "tier": recs["recommended_tts_tier"],
            "model_id": recs["recommended_tts"],
            "preset": "expressive" if recs["recommended_tts_tier"] == "full" else "stable",
            "device": recs.get("recommended_device", "auto"),
        }
    }

    current = config.load()
    if not current.llm.model and recs.get("recommended_llm"):
        patch["llm"] = {
            "model": recs["recommended_llm"],
            "provider": "ollama",
            "base_url": current.llm.base_url or "http://localhost:11434",
        }

    # Load the speech model *before* persisting the tier it names. Writing first
    # and loading after would leave a failed download or a missing runtime
    # recorded in the config, so the app would keep trying to load a model it
    # cannot run on every subsequent start -- and the only evidence would be a
    # log line the admin never sees. Persist on success; report the failure
    # without touching the config otherwise.
    try:
        await asyncio.to_thread(engine.ensure_loaded, recs["recommended_tts"])
    except Exception as exc:
        log.warning("TTS load failed during apply, config left unchanged: %s", exc)
        return {
            "applied": {},
            "error": f"Could not load {recs['recommended_tts']}: {exc}",
            "config": config.public_view(current),
            "summary": hardware.summary(profile),
            "engine": engine.status,
        }

    cfg = config.update(patch)

    return {
        "applied": patch,
        "config": config.public_view(cfg),
        "summary": hardware.summary(profile),
        "engine": engine.status,
    }


@router.get("/hardware/recommend")
async def hardware_recommend(
    x_admin_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _check(x_admin_token)
    profile = hardware.cached() or await asyncio.to_thread(hardware.scan, True)
    return profile.get("recommendations", {})


# -------------------------------------------------------------------- model tools


class DownloadRequest(BaseModel):
    repo: str
    kind: str = "tts"


@router.post("/models/download")
async def download_model(
    body: DownloadRequest, x_admin_token: str | None = Header(default=None)
) -> dict[str, Any]:
    """Pre-download a model into ``data/models`` so the app works offline later."""
    _check(x_admin_token)
    entry = model_catalog.by_repo(body.repo)
    backend = (entry or {}).get("backend", "onnx")

    if backend == "ollama":
        return {
            "ok": False,
            "detail": (
                f"'{body.repo}' is an Ollama model. Run: ollama pull {body.repo}"
            ),
            "command": f"ollama pull {body.repo}",
        }

    try:
        from huggingface_hub import snapshot_download

        path = await asyncio.to_thread(
            snapshot_download,
            body.repo,
            cache_dir=str(config.MODELS_DIR),
        )
        return {"ok": True, "repo": body.repo, "path": str(path)}
    except Exception as exc:
        raise HTTPException(500, f"Download failed: {type(exc).__name__}: {exc}") from exc


@router.get("/models")
async def list_models(x_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    _check(x_admin_token)
    profile = hardware.cached()
    recs = (profile or {}).get("recommendations", {})
    downloaded = _downloaded_repos()
    return {
        "catalog": [
            {**m, "downloaded": m["repo"] in downloaded} for m in model_catalog.ALL_MODELS
        ],
        "recommendations": recs,
        "installed_llm": await llm_client.list_models(),
    }


def _downloaded_repos() -> set[str]:
    """Hugging Face cache layout: models--<org>--<name>."""
    found: set[str] = set()
    root = config.MODELS_DIR
    if not root.exists():
        return found
    for child in root.glob("models--*"):
        parts = child.name.split("--", 1)
        if len(parts) == 2:
            found.add(parts[1].replace("--", "/"))
    return found


@router.post("/models/load")
async def load_tts_model(
    body: DownloadRequest, x_admin_token: str | None = Header(default=None)
) -> dict[str, Any]:
    """Load (and if needed download) a TTS model immediately."""
    _check(x_admin_token)
    try:
        await asyncio.to_thread(engine.ensure_loaded, body.repo)
    except Exception as exc:
        raise HTTPException(503, str(exc)) from exc
    config.update({"tts": {"model_id": body.repo, "tier": engine.status.get("tier", "mini")}})
    return {"ok": True, "engine": engine.status}


@router.post("/tts/unload")
async def unload_tts(x_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    _check(x_admin_token)
    engine.unload()
    return {"ok": True, "engine": engine.status}


@router.post("/tts/clear-cache")
async def clear_tts_cache(x_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    _check(x_admin_token)
    removed = await asyncio.to_thread(engine.clear_cache)
    return {"ok": True, "removed": removed}


@router.get("/tts/status")
async def tts_status(x_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    _check(x_admin_token)
    return engine.status


@router.get("/accelerator")
async def accelerator(x_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    """What GPU acceleration is available, and what to install if it is not.

    Separate from the hardware scan because it answers a different question: the
    scan says whether a GPU is *present*, this says whether the installed speech
    runtime can actually *use* it. A machine can easily have one without the other.
    """
    _check(x_admin_token)
    profile = hardware.cached()
    # A profile written before accelerator probing existed has no such key; rescan
    # rather than report an empty panel.
    if not profile or "accelerators" not in profile:
        profile = await asyncio.to_thread(hardware.scan, True)
    acc = profile.get("accelerators") or {}
    tier = (profile.get("recommendations") or {}).get("recommended_tts_tier", "mini")

    torch_info = acc.get("torch") or {}
    ort_info = acc.get("onnxruntime") or {}

    hints: list[dict[str, str]] = []
    if not torch_info.get("installed"):
        hints.append({
            "for": "full tier (KittenTTS 2, torch)",
            "command": "pip install -r requirements-full.txt",
            "note": "Installs the CUDA build of torch when one is available.",
        })
    elif not torch_info.get("cuda"):
        # A CUDA build that cannot see a GPU is a driver problem, not an install
        # one, and telling the user to reinstall would send them the wrong way.
        if torch_info.get("cuda_version"):
            hints.append({
                "for": "full tier on GPU",
                "command": "nvidia-smi  # confirm the driver sees the GPU",
                "note": (
                    f"torch is a CUDA {torch_info['cuda_version']} build but cannot reach a GPU. "
                    "Check the NVIDIA driver is installed and the GPU is visible."
                ),
            })
        else:
            hints.append({
                "for": "full tier on GPU",
                "command": "pip install -r requirements-full.txt --upgrade --force-reinstall",
                "note": "The installed torch is CPU-only; reinstall to pick up CUDA.",
            })
    if not ort_info.get("cuda"):
        hints.append({
            "for": "onnx tiers on GPU (nano / micro / mini)",
            "command": "pip install -r requirements-gpu.txt",
            "note": "Swaps onnxruntime for onnxruntime-gpu, which is the same API.",
        })

    return {
        "gpu_present": bool(profile.get("gpu")),
        "gpus": profile.get("gpu") or [],
        "torch": torch_info,
        "onnxruntime": ort_info,
        "any_gpu_runtime": bool(acc.get("any_gpu_runtime")),
        "recommended_device": (profile.get("recommendations") or {}).get("recommended_device"),
        "device_usable": (profile.get("recommendations") or {}).get("device_usable"),
        "device_reason": (profile.get("recommendations") or {}).get("device_reason"),
        "active_tier": tier,
        "hints": hints,
        "current": engine.status,
    }


@router.post("/tts/reload")
async def tts_reload(x_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    """Reload the speech model, so a device change takes effect immediately."""
    _check(x_admin_token)
    cfg = config.load()
    try:
        engine.unload()
        await asyncio.to_thread(engine.ensure_loaded, cfg.tts.model_id)
    except Exception as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"ok": True, "engine": engine.status}


@router.post("/tts/preview")
async def tts_preview(
    body: DownloadRequest, x_admin_token: str | None = Header(default=None)
) -> dict[str, Any]:
    """Synthesize the personality greeting so the admin can hear a voice."""
    _check(x_admin_token)
    p = personalities.active()
    try:
        engine.ensure_loaded()
        audio = engine.wav_bytes(p.greeting or "Systems online.")
    except Exception as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"ok": True, "bytes": len(audio), "voice": p.voice}


# ------------------------------------------------------------------- integrations


@router.get("/integrations")
async def integrations(x_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    _check(x_admin_token)
    cfg = config.load()
    llm_health = await llm_client.health()
    return {
        "llm": llm_health,
        "tools": registry.describe(),
        "tools_enabled": cfg.tools.enabled,
        "home_assistant": bool(cfg.tools.home_assistant_url and cfg.tools.resolved_ha_token()),
        "mqtt": bool(cfg.tools.mqtt_host),
        "tts": engine.status,
    }


@router.post("/integrations/test")
async def test_integration(
    body: dict[str, Any], x_admin_token: str | None = Header(default=None)
) -> dict[str, Any]:
    _check(x_admin_token)
    target = (body or {}).get("target", "llm")
    if target == "llm":
        return await llm_client.health()
    if target == "home_assistant":
        try:
            result = await registry.call("home_assistant_states", {"limit": 5})
            return {"ok": True, "detail": f"{result['count']} entities reached"}
        except Exception as exc:
            return {"ok": False, "detail": str(exc)}
    if target == "mqtt":
        try:
            await registry.call("mqtt_publish", {"topic": "rexeniot/selftest", "payload": "ping"})
            return {"ok": True, "detail": "published to rexeniot/selftest"}
        except Exception as exc:
            return {"ok": False, "detail": str(exc)}
    if target == "system":
        result = await registry.call("system_status", {})
        return {"ok": True, "detail": f"{result['cpu_percent']}% CPU, {result['memory']['used_percent']}% memory"}
    if target == "alexa":
        try:
            result = await registry.call("alexa_devices", {})
        except Exception as exc:
            return {"ok": False, "detail": str(exc)}
        if result.get("ok") is False:
            return {"ok": False, "detail": result.get("detail", "provider refused")}
        devices = result.get("devices") or []
        count = len(devices) if isinstance(devices, list) else "?"
        return {
            "ok": True,
            "detail": f"{count} device(s) via {result.get('provider')}",
            "devices": devices,
        }
    raise HTTPException(400, f"Unknown integration '{target}'")


# ------------------------------------------------------------------ personalities


@router.get("/personalities")
async def get_personalities(x_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    _check(x_admin_token)
    cfg = config.load()
    return {
        "personalities": [p.model_dump() for p in personalities.load()],
        "active": cfg.active_personality,
    }


class PersonalityBody(BaseModel):
    payload: dict[str, Any] = Field(default_factory=dict)


@router.post("/personalities")
async def create_personality(
    body: PersonalityBody, x_admin_token: str | None = Header(default=None)
) -> dict[str, Any]:
    _check(x_admin_token)
    try:
        item = personalities.create(body.payload)
    except Exception as exc:
        raise HTTPException(400, f"Could not create personality: {exc}") from exc
    return item.model_dump()


@router.put("/personalities/{personality_id}")
async def update_personality(
    personality_id: str,
    body: PersonalityBody,
    x_admin_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _check(x_admin_token)
    try:
        item = personalities.update_personality(personality_id, body.payload)
    except KeyError:
        raise HTTPException(404, "No such personality.")
    except Exception as exc:
        raise HTTPException(400, f"Could not update personality: {exc}") from exc
    return item.model_dump()


@router.delete("/personalities/{personality_id}")
async def delete_personality(
    personality_id: str, x_admin_token: str | None = Header(default=None)
) -> dict[str, Any]:
    _check(x_admin_token)
    if not personalities.delete(personality_id):
        raise HTTPException(404, "No such personality.")
    return {"ok": True}


@router.post("/personalities/{personality_id}/duplicate")
async def duplicate_personality(
    personality_id: str, x_admin_token: str | None = Header(default=None)
) -> dict[str, Any]:
    _check(x_admin_token)
    try:
        item = personalities.duplicate(personality_id)
    except KeyError:
        raise HTTPException(404, "No such personality.")
    return item.model_dump()


# ------------------------------------------------------------------------ skills


class SkillBody(BaseModel):
    payload: dict[str, Any] = Field(default_factory=dict)


@router.get("/skills")
async def get_skills(x_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    _check(x_admin_token)
    live = {s.id for s in skills.enabled()}
    return {
        "skills": [
            {**s.model_dump(), "active": s.id in live}
            for s in skills.load()
        ],
        "known_tools": sorted(t.name for t in registry.all()),
    }


@router.post("/skills")
async def create_skill(
    body: SkillBody, x_admin_token: str | None = Header(default=None)
) -> dict[str, Any]:
    _check(x_admin_token)
    try:
        item = skills.create(body.payload)
    except Exception as exc:
        raise HTTPException(400, f"Could not create skill: {exc}") from exc
    return item.model_dump()


@router.put("/skills/{skill_id}")
async def update_skill(
    skill_id: str,
    body: SkillBody,
    x_admin_token: str | None = Header(default=None),
) -> dict[str, Any]:
    _check(x_admin_token)
    try:
        item = skills.update_skill(skill_id, body.payload)
    except KeyError:
        raise HTTPException(404, "No such skill.")
    except Exception as exc:
        raise HTTPException(400, f"Could not update skill: {exc}") from exc
    return item.model_dump()


@router.delete("/skills/{skill_id}")
async def delete_skill(
    skill_id: str, x_admin_token: str | None = Header(default=None)
) -> dict[str, Any]:
    _check(x_admin_token)
    if not skills.delete(skill_id):
        raise HTTPException(404, "No such skill.")
    return {"ok": True}


@router.post("/skills/restore")
async def restore_skills(x_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    _check(x_admin_token)
    return {"skills": [s.model_dump() for s in skills.restore_builtins()]}


@router.post("/personalities/{personality_id}/activate")
async def activate_personality(
    personality_id: str, x_admin_token: str | None = Header(default=None)
) -> dict[str, Any]:
    _check(x_admin_token)
    try:
        item = personalities.set_active(personality_id)
    except KeyError:
        raise HTTPException(404, "No such personality.")
    return {"ok": True, "active": item.id, "personality": item.model_dump()}


@router.post("/personalities/restore")
async def restore_personalities(x_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    _check(x_admin_token)
    items = personalities.restore_builtins()
    return {"personalities": [p.model_dump() for p in items]}


class PreviewBody(BaseModel):
    text: str = "All systems are online. How may I be of assistance?"
    voice: str | None = None
    speed: float = 1.0
    emotion: str = ""
    preset: str = "stable"


@router.post("/personalities/preview")
async def preview_personality(
    body: PreviewBody, x_admin_token: str | None = Header(default=None)
) -> dict[str, Any]:
    """Return inline WAV bytes as base64 so the admin can audition a draft persona."""
    _check(x_admin_token)
    import base64

    try:
        engine.ensure_loaded()
        audio = engine.wav_bytes(
            body.text, voice=body.voice, speed=body.speed,
            preset=body.preset, emotion=body.emotion, use_cache=False,
        )
    except Exception as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"ok": True, "audio_base64": base64.b64encode(audio).decode("ascii"), "mime": "audio/wav"}


# ------------------------------------------------------------------------ logging


@router.get("/logs")
async def get_logs(
    lines: int = 200, x_admin_token: str | None = Header(default=None)
) -> dict[str, Any]:
    _check(x_admin_token)
    path = Path(config.DATA_DIR) / "rexeniot.log"
    if not path.exists():
        return {"lines": []}
    try:
        content = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except Exception as exc:
        raise HTTPException(500, str(exc)) from exc
    return {"lines": content[-max(1, min(lines, 2000)):]}


@router.get("/stats")
async def stats(x_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    _check(x_admin_token)
    cfg = config.load()
    db = config.DATA_DIR
    cache_files = list((db / "cache" / "tts").glob("*.wav")) if (db / "cache" / "tts").exists() else []
    cache_bytes = sum(f.stat().st_size for f in cache_files if f.exists())
    models_bytes = 0
    if config.MODELS_DIR.exists():
        for f in config.MODELS_DIR.rglob("*"):
            if f.is_file():
                try:
                    models_bytes += f.stat().st_size
                except Exception:
                    pass
    return {
        "cache_files": len(cache_files),
        "cache_mb": round(cache_bytes / 1024**2, 2),
        "models_mb": round(models_bytes / 1024**2, 2),
        "personalities": len(personalities.load()),
        "tts": engine.status,
        "tools": len(registry.all()),
        "uptime_note": "since process start",
    }


@router.post("/first-run-complete")
async def complete_first_run(x_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    _check(x_admin_token)
    config.update({"first_run_complete": True})
    return {"ok": True}

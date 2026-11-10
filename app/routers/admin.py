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
import sys
import time
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

from .. import config, hardware, model_catalog, personalities, profiles, skills, uploads
from ..hardware import directml_advice, nvidia_compute_cap, torch_gpu_advice
from ..llm import LOCAL_CANDIDATES, client as llm_client, discover_local, probe_url
from ..security import api_token, require_api_token
from ..stt import available as stt_available, engine as stt_engine
from ..tools import registry
from ..tts import engine

log = logging.getLogger("rexeniot.api.admin")

#: Everything here is behind the API token as well as the admin token. The two
#: guard different things: the API token is the perimeter (can this caller use
#: the server at all), the admin token guards configuration writes. Without the
#: first, an exposed server would let anyone reconfigure the agent.
router = APIRouter(prefix="/admin", tags=["admin"], dependencies=[Depends(require_api_token)])

#: The one admin route that must answer before a caller has any token: it tells
#: the panel whether a token is needed. Booleans only, never the token.
public_router = APIRouter(prefix="/admin", tags=["admin"])


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


@public_router.get("/session")
async def session() -> dict[str, Any]:
    """Whether the caller needs a token, and a digest of current state."""
    return {
        "requires_token": bool(_admin_token()),
        "requires_api_token": bool(api_token()),
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
    return _with_model_state(profile)


@router.get("/hardware")
async def hardware_cached(x_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    _check(x_admin_token)
    profile = hardware.cached()
    if not profile:
        return {"scanned": False}
    profile["summary"] = hardware.summary(profile)
    return _with_model_state({"scanned": True, **profile})


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
            # Only adopt vLLM when the scan found it usable; otherwise this is
            # "torch", which is where it already was.
            "backend": recs.get("recommended_backend", "torch"),
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


@router.get("/hardware/plan")
async def hardware_plan(x_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    """Verify this machine and propose the settings it suits.

    Reads only: it re-scans, checks every capability the speech engine depends on,
    and returns the config patch it would apply. Nothing is written, so the panel
    can show the plan and let the operator decide.
    """
    _check(x_admin_token)
    profile = await asyncio.to_thread(hardware.scan, True)
    result = await asyncio.to_thread(hardware.plan, profile)
    result["summary"] = hardware.summary(profile)
    return result


class PlanRunRequest(BaseModel):
    #: Load the model and synthesise a short phrase before persisting the plan.
    #: Off by default because it downloads weights on a cold machine.
    verify: bool = True


@router.post("/hardware/plan/run")
async def hardware_plan_run(
    body: PlanRunRequest | None = None,
    x_admin_token: str | None = Header(default=None),
) -> dict[str, Any]:
    """Apply the verified plan, optionally proving the result works first.

    With ``verify`` (the default) the proposed model is loaded and a short phrase
    is synthesised *before* anything is written. A plan that cannot actually speak
    is reported as a failure and the config is left alone -- the same ordering as
    ``/hardware/apply``, for the same reason: writing first would leave an
    unloadable model recorded and retried on every start.
    """
    _check(x_admin_token)
    body = body or PlanRunRequest()
    profile = await asyncio.to_thread(hardware.scan, True)
    result = await asyncio.to_thread(hardware.plan, profile)
    patch = result["patch"]
    target = patch.get("tts", {}).get("model_id")

    measured: dict[str, Any] = {}
    if body.verify and target:
        try:
            measured = await asyncio.to_thread(_verify_speech, target)
        except Exception as exc:
            log.warning("Plan verification failed, config left unchanged: %s", exc)
            return {
                **result,
                "applied": {},
                "error": f"Verification failed for {target}: {exc}",
                "config": config.public_view(config.load()),
                "engine": engine.status,
                "measured": {},
            }

    try:
        cfg = config.update(patch)
    except Exception as exc:
        # A patch that cannot be validated (a literal outside its set, say) must
        # not be half-written; config.update is atomic, so just report it.
        return {
            **result,
            "applied": {},
            "error": f"Could not apply the plan: {exc}",
            "config": config.public_view(config.load()),
            "engine": engine.status,
            "measured": measured,
        }

    return {
        **result,
        "verified": bool(measured),
        "measured": measured,
        "applied": patch,
        "config": config.public_view(cfg),
        "engine": engine.status,
    }


def _verify_speech(model_id: str) -> dict[str, Any]:
    """Load ``model_id`` and synthesise a phrase, returning what was measured.

    This is the check the whole step exists for: a machine can report a GPU and a
    runtime that claims to support it while synthesis still fails. Measuring here
    means the operator finds out before the config is changed, and gets a real
    number -- seconds per phrase -- instead of an assurance.
    """
    started = time.time()
    engine.ensure_loaded(model_id)
    load_s = time.time() - started

    cfg = config.load()
    phrase = "All systems are online."
    started = time.time()
    wav = engine.wav_bytes(phrase, voice=cfg.tts.voice, use_cache=False)
    synth_s = time.time() - started

    status = engine.status
    return {
        "model_id": model_id,
        "load_seconds": round(load_s, 2),
        "synth_seconds": round(synth_s, 2),
        "audio_bytes": len(wav),
        "device": status.get("device"),
        "backend": status.get("backend"),
        "tier": status.get("tier"),
        "voice_count": status.get("voice_count"),
        "phrase": phrase,
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


class InstallRequest(BaseModel):
    """A requirement *set* key from ``model_catalog.REQUIREMENT_SETS``.

    Deliberately not a package name: the endpoint only ever runs pip against a
    requirements file that ships with the app, so a request cannot ask it to
    install an arbitrary distribution from an arbitrary index.
    """

    set: str


#: The one install running at a time. Installs are serialised because two pip
#: runs against the same interpreter corrupt each other's metadata.
_INSTALL: dict[str, Any] = {"job": None}
_INSTALL_LOCK = asyncio.Lock()
_INSTALL_LOG_LIMIT = 4000
_INSTALL_TIMEOUT = 1800


def _nvidia_compute_cap(profile: dict[str, Any]) -> str:
    """The compute capability of the first NVIDIA GPU, or "".

    Needed to tell a Blackwell card (sm_120, CUDA 12.8+) from an older one, because
    the advice differs and the version tag alone does not reveal it.
    """
    for gpu in profile.get("gpu") or []:
        if (gpu.get("vendor") or "").lower() == "nvidia":
            return str(gpu.get("compute_capability") or "")
    return ""


def _install_state() -> dict[str, Any]:
    """A copy of the job, safe to serialise."""
    job = _INSTALL["job"]
    return dict(job) if job else {"state": "idle"}


async def _run_install(key: str, requirements: Path) -> None:
    """Run pip for ``requirements``, recording output for the panel to poll."""
    job = _INSTALL["job"]
    proc = None
    try:
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-m", "pip", "install", "-r", str(requirements),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
            cwd=str(config.ROOT),
        )
        job["pid"] = proc.pid
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=_INSTALL_TIMEOUT)
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            job.update(state="error", finished_at=time.time(),
                       detail=f"Install timed out after {_INSTALL_TIMEOUT // 60} minutes.")
            return
        text = (out or b"").decode("utf-8", "replace")
        job["log"] = text[-_INSTALL_LOG_LIMIT:]
        if proc.returncode != 0:
            job.update(state="error", finished_at=time.time(),
                       detail=f"pip exited with code {proc.returncode}.")
            return
        # The probe result is cached; the environment just changed, so the cached
        # verdict is stale by definition. Drop it before asking again.
        model_catalog.invalidate_probe_cache()
        missing = model_catalog.missing_for(key)
        if missing:
            # pip can succeed while an import still fails (a partial wheel, or a
            # dependency that needs a restart to be visible). Do not claim success.
            job.update(state="error", finished_at=time.time(),
                       detail=f"Installed, but these still do not resolve: {', '.join(missing)}.")
            return
        job.update(state="done", finished_at=time.time(),
                   detail=f"{model_catalog.REQUIREMENT_SETS[key]['label']} installed.")
    except Exception as exc:  # noqa: BLE001 - surfaced to the panel
        job.update(state="error", finished_at=time.time(),
                   detail=f"{type(exc).__name__}: {exc}")
    finally:
        _INSTALL["job"] = job


@router.post("/models/prerequisites/install")
async def install_prerequisites(
    body: InstallRequest, x_admin_token: str | None = Header(default=None)
) -> dict[str, Any]:
    """Install one requirement set in the background and return the job handle."""
    _check(x_admin_token)
    spec = model_catalog.REQUIREMENT_SETS.get(body.set)
    if spec is None:
        raise HTTPException(400, f"Unknown requirement set '{body.set}'.")
    requirements = config.ROOT / spec["file"]
    if not requirements.is_file():
        raise HTTPException(404, f"{spec['file']} is missing from the install.")
    if _INSTALL_LOCK.locked():
        raise HTTPException(409, "Another install is already running.")

    job = {
        "state": "running",
        "key": body.set,
        "label": spec["label"],
        "detail": f"Installing {spec['label']}…",
        "command": f'pip install -r {spec["file"]}',
        "started_at": time.time(),
        "finished_at": None,
        "pid": None,
        "log": "",
    }
    _INSTALL["job"] = job
    asyncio.create_task(_guarded_install(body.set, requirements))
    return dict(job)


async def _guarded_install(key: str, requirements: Path) -> None:
    async with _INSTALL_LOCK:
        await _run_install(key, requirements)


@router.get("/models/prerequisites/status")
async def prerequisites_status(x_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    """The running or last install, plus whether each set is satisfied now."""
    _check(x_admin_token)
    return {
        "install": _install_state(),
        "sets": {
            key: {
                "label": spec["label"],
                "detail": spec["detail"],
                "file": spec["file"],
                "command": f'pip install -r {spec["file"]}',
                "satisfied": not model_catalog.missing_for(key),
                "missing": model_catalog.missing_for(key),
            }
            for key, spec in model_catalog.REQUIREMENT_SETS.items()
        },
    }


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
    # Scan on first call. Without a profile the entries carry no `fits`, so the
    # panel would disable every Download button -- the same symptom as the raw
    # catalog, only on a fresh install. The scan is cached after the first call.
    profile = hardware.cached() or await asyncio.to_thread(hardware.scan, True)
    recs = (profile or {}).get("recommendations", {})
    return {
        "catalog": _catalog_view(profile),
        "recommendations": recs,
        "installed_llm": await llm_client.list_models(),
    }


def _catalog_view(profile: dict[str, Any] | None) -> list[dict[str, Any]]:
    """The catalog with the per-machine verdicts the panel renders.

    ``ALL_MODELS`` is the raw catalog and carries no ``fits`` / ``blockers`` /
    ``requirements``, so serving it directly left the panel with nothing to judge
    by: every Download button was disabled and no "downloaded" badge ever showed.
    Merge in the scored view from :func:`hardware.recommend`, keyed by repo, and
    fall back to the raw entry for anything the recommender does not cover.
    """
    scored = {m["repo"]: m for m in (hardware.recommend(profile)["models"] if profile else [])}
    downloaded = _downloaded_repos()
    active = _active_tts_repo()
    return [
        {
            **m,
            **scored.get(m["repo"], {}),
            "downloaded": m["repo"] in downloaded,
            **_model_state(m, active),
        }
        for m in model_catalog.ALL_MODELS
    ]


def _active_tts_repo() -> str:
    """The speech model the engine currently has loaded.

    The engine is authoritative when it has something loaded; the config is the
    fallback for the moment before the first load, so the panel can still show
    which model is *selected* rather than showing nothing as in use.
    """
    return engine.status.get("model_id") or config.load().tts.model_id


def _model_state(entry: dict[str, Any], active: str) -> dict[str, Any]:
    """Per-request model state: whether it is in use, and what it needs first."""
    return {
        "active": entry.get("repo") == active,
        "prerequisites": model_catalog.prerequisites(entry),
    }


def _with_model_state(profile: dict[str, Any]) -> dict[str, Any]:
    """Annotate a scan result's model list in place, then return it.

    The panel renders models from the scan result rather than from ``/models``, so
    both paths have to carry the same state or the "in use" badge would only ever
    appear on one of them.
    """
    recs = profile.get("recommendations")
    if isinstance(recs, dict) and recs.get("models"):
        active = _active_tts_repo()
        recs["models"] = [{**m, **_model_state(m, active)} for m in recs["models"]]
    return profile


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
    entry = model_catalog.by_repo(body.repo)
    pre = model_catalog.prerequisites(entry or {})
    if pre["needed"] and not pre["satisfied"]:
        # Say what is missing and how to fix it, rather than letting the load fail
        # with a bare ImportError from deep inside the model library. A package
        # that is present but unimportable needs a reinstall, so say that instead.
        if pre.get("broken"):
            what = (
                f"{', '.join(pre['broken'])} is installed but cannot be imported "
                f"({pre.get('reason') or 'import failed'})"
            )
        else:
            what = f"the {pre['label']} is missing: {', '.join(pre['missing'])}"
        raise HTTPException(
            409,
            f"{body.repo} needs {what}. Fix it with: {pre['command']}",
        )
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


@router.get("/stt/status")
async def stt_status(x_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    """The dictation engine's state, plus whether its packages are installed.

    ``available`` is separate from ``state`` on purpose: a missing optional
    install is not an error, and the panel needs to tell that apart from a model
    that failed to load.
    """
    _check(x_admin_token)
    return {**stt_engine.status, "available": stt_available()}


@router.post("/stt/reload")
async def stt_reload(x_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    _check(x_admin_token)
    cfg = config.load()
    try:
        stt_engine.unload()
        await asyncio.to_thread(stt_engine.ensure_loaded, cfg.stt.model_id)
    except Exception as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"ok": True, "engine": {**stt_engine.status, "available": stt_available()}}


@router.post("/stt/unload")
async def unload_stt(x_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    _check(x_admin_token)
    stt_engine.unload()
    return {"ok": True, "engine": stt_engine.status}


@router.post("/stt/preview")
async def stt_preview(x_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    """Load the dictation model and prove it transcribes, using the TTS engine.

    Speaking a known line and transcribing it back exercises the whole chain --
    model load, decode, inference -- without needing the user to record
    something. A model that loads but cannot decode would otherwise look fine
    until the first real utterance.
    """
    _check(x_admin_token)
    cfg = config.load()
    if not cfg.stt.enabled:
        raise HTTPException(503, "Speech-to-text is disabled in the admin panel.")
    if not stt_available():
        raise HTTPException(
            503,
            "The speech-to-text set is not installed. Install it with "
            "'pip install -r requirements-stt.txt'.",
        )

    probe = "Systems online. Dictation is working."
    try:
        audio = await asyncio.to_thread(engine.wav_bytes, probe, use_cache=False)
        result = await asyncio.to_thread(stt_engine.transcribe, audio)
    except Exception as exc:
        raise HTTPException(503, str(exc)) from exc
    return {"ok": True, "heard": result["text"], "expected": probe,
            "seconds": result["duration_s"], "engine": stt_engine.status}


def accelerator_hints(profile: dict[str, Any]) -> list[dict[str, str]]:
    """The install advice for this machine, as hint rows.

    Kept out of the request handler because it is the part that has to differ per
    platform, and the Windows branch cannot otherwise be exercised from a Linux
    test host -- it is reached by calling this with a synthetic profile.
    """
    acc = profile.get("accelerators") or {}
    tier = (profile.get("recommendations") or {}).get("recommended_tts_tier", "mini")
    torch_info = acc.get("torch") or {}
    ort_info = acc.get("onnxruntime") or {}
    vllm_info = acc.get("vllm") or {}
    hints: list[dict[str, str]] = []

    # Every hint below is about making a *present* GPU usable. On a machine with
    # no GPU at all the runtime is already configured correctly, so suggesting a
    # CUDA reinstall would send the reader chasing a problem they do not have.
    if not profile.get("gpu"):
        return hints

    if not torch_info.get("installed"):
        advice = torch_gpu_advice(profile)
        hints.append({
            "for": "full tier (KittenTTS 2, torch)",
            "command": advice["command"] if advice else "pip install -r requirements-full.txt",
            # The plain requirements install is only a GPU install on Linux, where
            # PyPI's default torch links CUDA. Saying "installs the CUDA build" on
            # Windows sent the reader to a CPU-only wheel and no further clue.
            "note": advice["note"] if advice
            else "Installs the CUDA build of torch when one is available.",
        })
    elif not torch_info.get("cuda"):
        if torch_info.get("cuda_version"):
            # A CUDA build that cannot see a GPU is a driver problem, not an
            # install one, so "reinstall the stack" sends the reader the wrong
            # way. On Blackwell it is neither: those cards (sm_120) need a CUDA
            # 12.8+ build, and a wheel compiled for <=12.6 reports the newer
            # runtime version while refusing the device.
            sm = nvidia_compute_cap(profile)
            if sm.startswith("12."):
                advice = torch_gpu_advice(profile)
                hints.append({
                    "for": "full tier on GPU (Blackwell / RTX 50-series)",
                    "command": advice.get("command", (
                        "pip install -r requirements-full.txt\n"
                        "pip install torch torchaudio --upgrade --index-url "
                        "https://download.pytorch.org/whl/cu130"
                    )),
                    "note": (
                        f"This GPU reports compute capability {sm} (Blackwell), which needs a "
                        "CUDA 12.8 or newer torch build. The version tag alone does not prove "
                        "that: a build compiled for CUDA 12.6 or older reports the runtime "
                        "version but rejects sm_120. Check with "
                        "'python -c \"import torch; print(torch.cuda.get_arch_list())\"' -- "
                        "sm_120 must be listed."
                    ),
                })
            else:
                hints.append({
                    "for": "full tier on GPU",
                    "command": "nvidia-smi  # confirm the driver sees the GPU",
                    "note": (
                        f"torch is a CUDA {torch_info['cuda_version']} build but cannot reach a "
                        "GPU. Check the NVIDIA driver is installed and the GPU is visible."
                    ),
                })
        else:
            advice = torch_gpu_advice(profile)
            # On Windows and macOS the requirements file reinstalls the same
            # CPU-only wheel. Offering it here is what made this a loop that
            # reported success and changed nothing, so name the wheel index.
            hints.append({
                "for": "full tier on GPU",
                "command": advice.get(
                    "command",
                    "pip install -r requirements-full.txt --upgrade --force-reinstall",
                ),
                "note": advice.get(
                    "note",
                    "The installed torch is CPU-only; reinstall to pick up CUDA.",
                ),
            })

    if not ort_info.get("gpu"):
        dml = directml_advice(profile)
        nvidia = any(
            (g.get("vendor") or "").lower() == "nvidia" for g in (profile.get("gpu") or [])
        )
        if dml and not nvidia:
            # A non-NVIDIA GPU: requirements-gpu.txt is the CUDA build of
            # onnxruntime and will never reach it, so lead with DirectML.
            hints.append({
                "for": "onnx tiers on GPU (nano / micro / mini)",
                "command": dml["command"],
                "note": dml["note"],
            })
        else:
            note = "Swaps onnxruntime for onnxruntime-gpu, which is the same API."
            if dml:
                note += (
                    " On Windows, requirements-directml.txt also works and needs no "
                    "CUDA toolkit -- useful if the CUDA build will not load."
                )
            hints.append({
                "for": "onnx tiers on GPU (nano / micro / mini)",
                "command": "pip install -r requirements-gpu.txt",
                "note": note,
            })

    # vLLM only helps the full tier, and only with a CUDA GPU to run it on.
    if tier == "full" and not vllm_info.get("installed") and torch_info.get("cuda"):
        hints.append({
            "for": "full tier, faster synthesis (vLLM)",
            "command": 'pip install "kittenml[vllm]"',
            "note": (
                "Upstream measures ~3-4x faster synthesis on a GPU. This is what "
                "makes voice cloning and the 47-voice set practical."
            ),
        })
    return hints


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
    vllm_info = acc.get("vllm") or {}
    recs = profile.get("recommendations") or {}

    hints = accelerator_hints(profile)

    return {
        "gpu_present": bool(profile.get("gpu")),
        "gpus": profile.get("gpu") or [],
        "torch": torch_info,
        "onnxruntime": ort_info,
        "vllm": vllm_info,
        "any_gpu_runtime": bool(acc.get("any_gpu_runtime")),
        "driver_only_gpus": acc.get("driver_only_gpus") or [],
        "gpu_seen_but_unusable": bool(acc.get("gpu_seen_but_unusable")),
        "recommended_device": recs.get("recommended_device"),
        "device_usable": recs.get("device_usable"),
        "device_reason": recs.get("device_reason"),
        "recommended_backend": recs.get("recommended_backend"),
        "backend_usable": recs.get("backend_usable"),
        "backend_reason": recs.get("backend_reason"),
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
        "stt": {**stt_engine.status, "available": stt_available()},
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


# --------------------------------------------------------------- voice profiles

# Voice profiles bundle the dozen interacting tts.* settings into named choices,
# so the panel offers "Balanced" / "Best quality" instead of asking the operator
# to reason about tiers, backends and devices at once. Built-ins cannot be
# deleted; saving one under the same name creates a copy.


class ProfileBody(BaseModel):
    name: str = ""
    description: str = ""
    settings: dict[str, Any] = Field(default_factory=dict)
    id: str = ""


def _profile_view(p: profiles.VoiceProfile, cfg: config.AppConfig | None = None) -> dict[str, Any]:
    return {
        **p.model_dump(),
        "problems": profiles.validate(p.settings),
        # Whether this profile is the one currently in effect, so the panel can
        # show it as in use rather than merely selected.
        "in_use": profiles.matches(p, cfg),
    }


@router.get("/profiles")
async def get_profiles(x_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    _check(x_admin_token)
    cfg = config.load()
    return {
        "profiles": [_profile_view(p, cfg) for p in profiles.load()],
        "active": profiles.active(cfg),
        "current": profiles.capture(cfg),
    }


@router.post("/profiles")
async def create_profile(
    body: ProfileBody, x_admin_token: str | None = Header(default=None)
) -> dict[str, Any]:
    _check(x_admin_token)
    try:
        saved = profiles.save(body.name, body.settings, body.description, body.id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _profile_view(saved)


@router.delete("/profiles/{profile_id}")
async def delete_profile(
    profile_id: str, x_admin_token: str | None = Header(default=None)
) -> dict[str, Any]:
    _check(x_admin_token)
    if not profiles.delete(profile_id):
        raise HTTPException(404, "No such profile, or it is built in.")
    return {"ok": True}


@router.post("/profiles/{profile_id}/apply")
async def apply_profile(
    profile_id: str, x_admin_token: str | None = Header(default=None)
) -> dict[str, Any]:
    """Adopt a profile's settings, loading the model before persisting it.

    Same ordering as ``/hardware/apply`` and ``put_config``: a profile naming a
    model that cannot load must not be recorded, or the app retries a broken
    model on every start with only a log line to show for it.
    """
    _check(x_admin_token)
    profile = profiles.get(profile_id)
    if profile is None:
        raise HTTPException(404, "No such profile.")

    problems = profiles.validate(profile.settings)
    if problems:
        raise HTTPException(400, "Profile is not valid: " + "; ".join(problems))

    new_model = (profile.settings.get("tts") or {}).get("model_id")
    if new_model:
        try:
            await asyncio.to_thread(engine.ensure_loaded, new_model)
        except Exception as exc:
            raise HTTPException(
                400, f"Could not load TTS model '{new_model}': {exc}"
            ) from exc

    try:
        cfg = config.update(profile.settings)
    except Exception as exc:
        raise HTTPException(400, f"Invalid configuration: {exc}") from exc

    return {
        "ok": True,
        "applied": profile.settings,
        "profile": profile.id,
        "config": config.public_view(cfg),
        "engine": engine.status,
    }


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
        "stt": {**stt_engine.status, "available": stt_available()},
        "tools": len(registry.all()),
        "uptime_note": "since process start",
    }


@router.post("/first-run-complete")
async def complete_first_run(x_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    _check(x_admin_token)
    config.update({"first_run_complete": True})
    return {"ok": True}


@router.get("/llm/discover")
async def discover_llm(x_admin_token: str | None = Header(default=None)) -> dict[str, Any]:
    """List local model servers that answer, best candidate first.

    Read-only, so the admin panel can offer a one-click setup on a first run
    without touching a configuration that already works.
    """
    _check(x_admin_token)
    found = await discover_local()
    return {
        "found": found,
        "current": llm_client.describe(),
        "candidates": [{"url": u, "label": l} for u, l in LOCAL_CANDIDATES],
    }


class AdoptRequest(BaseModel):
    base_url: str
    model: str = ""


@router.post("/llm/adopt")
async def adopt_llm(
    body: AdoptRequest, x_admin_token: str | None = Header(default=None)
) -> dict[str, Any]:
    """Point the LLM at a local server, optionally overriding the model.

    The model is chosen from what the server actually serves when the caller
    does not name one, so a first run needs no guessing.
    """
    _check(x_admin_token)
    found = await probe_url(body.base_url)
    models = (found or {}).get("models") or []
    model = body.model.strip() or (models[0] if models else "")
    if not model:
        raise HTTPException(404, f"no models served by {body.base_url}")
    if body.model.strip() and body.model.strip() not in models:
        raise HTTPException(400, f"{body.model} is not served by {body.base_url}; it has {models}")
    config.update({"llm": {"provider": "auto", "base_url": body.base_url, "model": model}})
    llm_client.refresh()
    return {"ok": True, "llm": llm_client.describe()}

"""Saved voice profiles.

The voice panel grew to a dozen interacting settings: tier, voice, speed,
emotion, preset, device, engine, backend, cache. Choosing a sensible combination
is a hardware question, not a preference question, so most operators should pick
a profile rather than reason about it.

A profile is a *named, validated patch* of ``tts.*`` and ``voice_input.*``:

- **Built-ins** cover the hardware situations that actually occur (GPU box, CPU
  only, no local model at all). They are re-seeded on load if missing, so a
  deleted file heals itself without clobbering operator profiles -- same
  behaviour as ``personalities.py``.
- **Custom** profiles are whatever the operator saved from the panel, so a
  working combination can be named, reused and shared without hand-editing JSON.

"Validated" is the point. ``AppConfig`` silently ignores unknown keys, so a typo
saves "successfully" and does nothing. :func:`validate` rejects unknown keys and
bad values up front, so a saved profile is known to load rather than merely
known to have been written.
"""

from __future__ import annotations

import json
import re
import threading
import time
import typing
import uuid
from typing import Any

from pydantic import BaseModel, Field

from . import config

_lock = threading.RLock()

#: Profile settings are limited to these sections. A profile that could rewrite
#: the LLM endpoint or the admin token would be a privilege-escalation path
#: disguised as a convenience, so the surface stays deliberately narrow.
SECTIONS = ("tts", "voice_input")


class VoiceProfile(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:10])
    name: str
    description: str = ""
    #: Nested patch, e.g. ``{"tts": {"engine": "browser"}}``.
    settings: dict[str, Any] = Field(default_factory=dict)
    builtin: bool = False
    updated_at: float = Field(default_factory=time.time)


BUILTIN: list[dict[str, Any]] = [
    {
        "id": "balanced",
        "name": "Balanced",
        "description": (
            "The default. Local vocal model on the lightest tier, GPU when the "
            "runtime can reach one. Good quality, quick to start."
        ),
        "settings": {
            "tts": {
                "enabled": True,
                "engine": "model",
                "tier": "mini",
                "model_id": "KittenML/kitten-tts-mini-0.8",
                "voice": "Luna",
                "speed": 1.0,
                "default_emotion": "",
                "preset": "stable",
                "device": "auto",
                "backend": "torch",
                "cache_enabled": True,
            }
        },
    },
    {
        "id": "best-quality",
        "name": "Best quality (GPU)",
        "description": (
            "The 1.7B model with 47 voices, cloning and emotion tags, on vLLM. "
            "Needs an NVIDIA GPU and the vLLM extra; roughly 3-4x faster than "
            "torch, which is what makes this tier practical."
        ),
        "settings": {
            "tts": {
                "enabled": True,
                "engine": "model",
                "tier": "full",
                "model_id": "KittenML/kitten-tts-2",
                "voice": "Luna",
                "speed": 1.0,
                "default_emotion": "tender",
                "preset": "expressive",
                "device": "cuda",
                "backend": "vllm",
                "cache_enabled": True,
            }
        },
    },
    {
        "id": "light-cpu",
        "name": "Light on hardware (CPU)",
        "description": (
            "The smallest model, forced onto the CPU, with caching on. For a "
            "small server, a laptop, or a box already busy with other work."
        ),
        "settings": {
            "tts": {
                "enabled": True,
                "engine": "model",
                "tier": "nano",
                "model_id": "KittenML/kitten-tts-nano-0.8-int8",
                "voice": "Luna",
                "speed": 1.0,
                "default_emotion": "",
                "preset": "stable",
                "device": "cpu",
                "backend": "torch",
                "cache_enabled": True,
            }
        },
    },
    {
        "id": "browser-voice",
        "name": "Browser voice (no local model)",
        "description": (
            "Speaks through the browser's own speech synthesis instead of "
            "KittenTTS. Nothing to download and no CPU cost, so it is the "
            "fallback for a machine that cannot host the vocal model. Voice "
            "choice is the browser's, not ours."
        ),
        "settings": {"tts": {"enabled": True, "engine": "browser"}},
    },
    {
        "id": "resilient",
        "name": "Vocal model, browser if it cannot run",
        "description": (
            "Uses the vocal model but falls back to browser speech the moment it "
            "fails to load or synthesize, so the assistant never goes silent. A "
            "safe default for a machine whose limits are not yet known."
        ),
        "settings": {"tts": {"enabled": True, "engine": "auto"}},
    },
    {
        "id": "silent",
        "name": "Silent (text only)",
        "description": (
            "No speech output at all. Replies still appear in the transcript, "
            "and dictation still works if it is enabled."
        ),
        "settings": {"tts": {"enabled": False}},
    },
]


def _builtins() -> list[VoiceProfile]:
    return [VoiceProfile(**entry, builtin=True) for entry in BUILTIN]


# --------------------------------------------------------------------- validation


def _field_paths(model: type, prefix: str = "") -> set[str]:
    """Every dotted path the config schema accepts, e.g. ``tts.voice``."""
    out: set[str] = set()
    for name, info in model.model_fields.items():
        path = f"{prefix}{name}"
        out.add(path)
        annotation = info.annotation
        if isinstance(annotation, type) and hasattr(annotation, "model_fields"):
            out |= _field_paths(annotation, f"{path}.")
    return out


def _first_problem(exc: Exception) -> str:
    """A one-line reason from a pydantic error, for showing to an operator."""
    errors = getattr(exc, "errors", None)
    if callable(errors):
        try:
            items = errors()
            if items:
                err = items[0]
                loc = ".".join(str(p) for p in err.get("loc", ()) if p not in ("tts", "voice_input"))
                return f"{loc}: {err.get('msg', 'invalid')}" if loc else str(err.get("msg", "invalid"))
        except Exception:
            pass
    return str(exc).splitlines()[0][:120]


def validate(settings: dict[str, Any]) -> list[str]:
    """Return the reasons ``settings`` cannot be applied. Empty means good.

    Rejects unknown sections and unknown keys explicitly. ``AppConfig`` ignores
    extras, so without this a typo would save cleanly and do nothing -- which is
    the failure a "vetted" profile is supposed to rule out.
    """
    problems: list[str] = []
    if not isinstance(settings, dict) or not settings:
        return ["profile has no settings"]

    allowed = _field_paths(config.AppConfig)
    for section, values in settings.items():
        if section not in SECTIONS:
            problems.append(f"'{section}' is not a profile section (allowed: {', '.join(SECTIONS)})")
            continue
        if not isinstance(values, dict):
            problems.append(f"'{section}' must be an object")
            continue
        for key, value in values.items():
            path = f"{section}.{key}"
            if path not in allowed:
                problems.append(f"'{path}' is not a setting")
                continue
            # Round-trip the single field so a bad type or an out-of-range literal
            # is caught here rather than at apply time.
            try:
                config.AppConfig.model_validate({section: {key: value}})
            except Exception as exc:
                problems.append(f"'{path}': {_first_problem(exc)}")

    # Then the whole patch at once: individual fields can each be valid while the
    # combination is not.
    if not problems:
        try:
            base = config.load().model_dump()
            for section, values in settings.items():
                base[section] = {**base.get(section, {}), **values}
            config.AppConfig.model_validate(base)
        except Exception as exc:
            problems.append(f"combination rejected: {_first_problem(exc)}")
    return problems


# -------------------------------------------------------------------------- store


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or uuid.uuid4().hex[:8]


def load(force: bool = False) -> list[VoiceProfile]:
    """Built-ins plus saved profiles, built-ins first."""
    with _lock:
        custom: list[VoiceProfile] = []
        if config.PROFILES_PATH.exists():
            try:
                raw = json.loads(config.PROFILES_PATH.read_text(encoding="utf-8"))
                custom = [VoiceProfile.model_validate(x) for x in raw]
            except Exception:
                custom = []
        builtins = _builtins()
        if not custom and not config.PROFILES_PATH.exists():
            _write(custom)
        return builtins + custom


def _write(custom: list[VoiceProfile]) -> None:
    try:
        config.ensure_dirs()
        config.PROFILES_PATH.write_text(
            json.dumps([p.model_dump() for p in custom], indent=2), encoding="utf-8"
        )
    except Exception:
        pass


def get(profile_id: str) -> VoiceProfile | None:
    return next((p for p in load() if p.id == profile_id), None)


def save(name: str, settings: dict[str, Any], description: str = "", profile_id: str = "") -> VoiceProfile:
    """Create or replace a profile. Raises ``ValueError`` if it would not apply."""
    name = (name or "").strip()
    if not name:
        raise ValueError("a profile needs a name")
    problems = validate(settings)
    if problems:
        raise ValueError("; ".join(problems))

    with _lock:
        custom = [p for p in load() if not p.builtin]
        existing = next((p for p in custom if p.id == profile_id), None)
        if existing is not None:
            existing.name = name
            existing.description = description
            existing.settings = settings
            existing.updated_at = time.time()
            saved = existing
        else:
            saved = VoiceProfile(
                id=_slug(name) or uuid.uuid4().hex[:10],
                name=name,
                description=description,
                settings=settings,
            )
            # A name that slugs onto a built-in id would shadow it on lookup.
            taken = {p.id for p in load()}
            if saved.id in taken:
                saved.id = f"{saved.id}-{uuid.uuid4().hex[:4]}"
            custom.append(saved)
        _write(custom)
        return saved


def delete(profile_id: str) -> bool:
    """Remove a saved profile. Built-ins cannot be deleted."""
    with _lock:
        custom = [p for p in load() if not p.builtin]
        kept = [p for p in custom if p.id != profile_id]
        if len(kept) == len(custom):
            return False
        _write(kept)
        return True


def matches(profile: VoiceProfile, cfg: config.AppConfig | None = None) -> bool:
    """True when every setting the profile names equals the live config.

    This is the honest test for "in use": a profile is only in effect if nothing
    it specifies has been changed since. Partial application does not count, so a
    profile cannot be shown as active while the voice it names is something else.
    """
    cfg = cfg or config.load()
    for section, values in (profile.settings or {}).items():
        current = getattr(cfg, section, None)
        if current is None:
            return False
        live = current.model_dump()
        for key, value in values.items():
            if live.get(key) != value:
                return False
    return True


def active(cfg: config.AppConfig | None = None) -> str:
    """The id of the profile currently in effect, or "" if none matches.

    Built-ins are checked first so the generic "Balanced" profile does not win
    over a more specific saved one that happens to agree with it.
    """
    cfg = cfg or config.load()
    for profile in load():
        if matches(profile, cfg):
            return profile.id
    return ""


def capture(cfg: config.AppConfig | None = None) -> dict[str, Any]:
    """The current voice settings, as a profile patch.

    Only the keys that differ from the built-in defaults are included, so a
    saved profile reads as a short list of deliberate choices rather than a dump
    of the whole section.
    """
    cfg = cfg or config.load()
    defaults = config.AppConfig()
    patch: dict[str, Any] = {}
    for section in SECTIONS:
        current = getattr(cfg, section).model_dump()
        base = getattr(defaults, section).model_dump()
        diff = {k: v for k, v in current.items() if base.get(k) != v}
        if diff:
            patch[section] = diff
    return patch

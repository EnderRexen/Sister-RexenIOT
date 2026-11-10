"""Skills for Sister RexenIOT.

A skill is a bundle of capability the admin can switch on and off. It carries the
instructions the assistant follows while the skill is active, and it claims the
tools that only make sense with it. Gating works in both directions: a skill that
is disabled withholds the tools it claims, and a tool no enabled skill claims is
still offered, so a bare install with every skill enabled behaves exactly as it
did before skills existed.

Alexa is the first skill of this kind. Only one provider can be live at a time, so
that skill declares ``provider_field`` and is offered to the model only when a
provider is actually configured.

Templates are stored as JSON so they survive upgrades and can be hand-edited.
Built-ins are re-seeded on load if missing, which heals a deleted file without
clobbering the operator's own edits.
"""

from __future__ import annotations

import json
import re
import threading
import time
import uuid
from typing import Any

from pydantic import BaseModel, Field

from . import config

_lock = threading.RLock()


class Skill(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:10])
    name: str
    tagline: str = ""
    description: str = ""
    #: Injected into the system prompt while the skill is enabled.
    instructions: str = ""
    #: Tools this skill owns. Disabling the skill withholds them.
    tools: list[str] = Field(default_factory=list)
    #: Config field whose value gates availability, e.g. "alexa_provider".
    provider_field: str = ""
    #: ToolConfig fields where any one being set makes the skill usable. Used
    #: when there is no single provider_field, e.g. Home Assistant or MQTT.
    config_fields: list[str] = Field(default_factory=list)
    #: Human-readable prerequisites, shown in the admin panel.
    requires: str = ""
    enabled: bool = True
    builtin: bool = False
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)


BUILTIN: list[dict[str, Any]] = [
    {
        "id": "home",
        "name": "Home",
        "tagline": "Lights, climate, scenes and switches",
        "description": (
            "Control the house itself. Talks to Home Assistant for anything it owns and to "
            "MQTT for devices it does not, so a smart bulb and a bare relay are both reachable."
        ),
        "instructions": (
            "Skill: Home. You can reach the house through Home Assistant and MQTT. "
            "Read state before you change it, and name the entity you touched when you report "
            "back. If an integration is missing, say which one and where to configure it."
        ),
        "tools": [
            "home_assistant_states",
            "home_assistant_call_service",
            "mqtt_publish",
        ],
        "requires": "Home Assistant URL + token, or an MQTT broker",
        "config_fields": ["home_assistant_url", "mqtt_host"],
    },
    {
        "id": "computer",
        "name": "Computer",
        "tagline": "This machine's health, files and commands",
        "description": (
            "Watch and operate the computer Sister RexenIOT runs on: CPU, memory, disk, battery, "
            "processes, and shell commands when the operator has allowed them."
        ),
        "instructions": (
            "Skill: Computer. You can inspect and act on the machine hosting you. Prefer "
            "read-only checks first. Never run a command that destroys data without being asked "
            "twice, and report the exact command when you do run one."
        ),
        "tools": ["system_status", "list_processes", "run_shell_command"],
    },
    {
        "id": "web",
        "name": "Web",
        "tagline": "Fetch pages and call JSON APIs",
        "description": "Reach the outside world: read a URL or call an API, within the allowlist.",
        "instructions": (
            "Skill: Web. You can fetch URLs and call JSON APIs, restricted to the operator's "
            "allowlist. Cite the source you used when you report a fact from the web."
        ),
        "tools": ["fetch_url"],
    },
    {
        "id": "memory",
        "name": "Memory",
        "tagline": "Remember and recall facts",
        "description": (
            "Keep durable notes about the user and the house, and recall them when relevant."
        ),
        "instructions": (
            "Skill: Memory. Store anything the user asks you to remember, and recall it when it "
            "bears on the question. Do not recite stored facts unprompted."
        ),
        "tools": ["remember", "recall"],
    },
    {
        "id": "timers",
        "name": "Timers",
        "tagline": "Timers, reminders and recurring checks",
        "description": "Set a timer or a reminder, and follow up when it is due.",
        "instructions": (
            "Skill: Timers. You can set timers and reminders, and they surface in the UI. "
            "Confirm the label and duration when you set one."
        ),
        "tools": ["set_timer"],
    },
    {
        "id": "images",
        "name": "Images",
        "tagline": "Generate images with ComfyUI",
        "description": "Hand image and 3D generation to ComfyUI, which the chat model cannot do.",
        "instructions": (
            "Skill: Images. Delegate image and 3D generation to ComfyUI rather than describing "
            "what an image would look like."
        ),
        "tools": ["generate_image", "comfyui_status"],
        "requires": "ComfyUI URL",
        "config_fields": ["comfyui_url"],
    },
    {
        "id": "alexa",
        "name": "Alexa",
        "tagline": "Talk to the Echo devices in the house",
        "description": (
            "Reach Amazon Echo hardware: announce something out loud, send a voice command to a "
            "device, ask Alexa a question and read the answer back, or fire one of her routines. "
            "Amazon publishes no API for commanding an Echo, so this skill drives whichever "
            "bridge the operator has configured."
        ),
        "instructions": (
            "Skill: Alexa. You can reach the household Alexa devices through the configured "
            "bridge. Use it to speak on a device, send a voice command, ask Alexa something and "
            "read her answer back, or fire a routine. Announcements are one-way: use alexa_ask "
            "when you actually need Alexa's reply. Alexa is a separate assistant from you -- if "
            "you announce through her, say plainly that you used her speaker."
        ),
        "tools": ["alexa_devices", "alexa_speak", "alexa_command", "alexa_ask", "alexa_routine"],
        "provider_field": "alexa_provider",
        "requires": "An Alexa provider: Voice Monkey token, the alexacli CLI, or Home Assistant",
    },
    {
        "id": "desktop",
        "name": "Desktop",
        "tagline": "Apps, notifications, media and power on this machine",
        "description": (
            "Drive the KDE Plasma desktop directly: post a notification the user will see, "
            "open an installed application, control media playback, and switch the power profile."
        ),
        "instructions": (
            "Skill: Desktop. You can act on the user's graphical session. Use notify_user "
            "rather than only answering in chat when the user should see something on screen. "
            "Use open_app with an application name; list_apps first if you are unsure it is "
            "installed. control_media drives whichever player currently exposes itself on the "
            "session bus, so it fails cleanly when nothing is playing."
        ),
        "tools": [
            "notify_user",
            "open_app",
            "list_apps",
            "control_media",
            "set_power_profile",
        ],
        "requires": "A KDE Plasma desktop session on the same machine",
    },
]


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or uuid.uuid4().hex[:8]


# --------------------------------------------------------------------------- store


def _builtins() -> list[Skill]:
    return [Skill(**entry, builtin=True) for entry in BUILTIN]


def load(force: bool = False) -> list[Skill]:
    with _lock:
        if config.SKILLS_PATH.exists() and not force:
            try:
                raw = json.loads(config.SKILLS_PATH.read_text(encoding="utf-8"))
                items = [Skill.model_validate(x) for x in raw]
                if items:
                    return items
            except Exception:
                pass
        items = _builtins()
        _write(items)
        return items


def _write(items: list[Skill]) -> None:
    config.ensure_dirs()
    config.SKILLS_PATH.write_text(
        json.dumps([i.model_dump() for i in items], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def get(skill_id: str) -> Skill | None:
    return next((s for s in load() if s.id == skill_id), None)


def create(payload: dict[str, Any]) -> Skill:
    with _lock:
        items = load()
        data = {k: v for k, v in payload.items() if k in Skill.model_fields}
        data.pop("builtin", None)
        if not data.get("id"):
            data["id"] = _slug(str(data.get("name", "skill")))
        if any(s.id == data["id"] for s in items):
            data["id"] = f"{data['id']}-{uuid.uuid4().hex[:4]}"
        item = Skill(**data, builtin=False)
        items.append(item)
        _write(items)
        return item


def update_skill(skill_id: str, patch: dict[str, Any]) -> Skill:
    with _lock:
        items = load()
        for idx, item in enumerate(items):
            if item.id != skill_id:
                continue
            allowed = {k: v for k, v in patch.items() if k in Skill.model_fields}
            allowed.pop("id", None)
            allowed.pop("builtin", None)
            merged = item.model_dump() | allowed
            merged["updated_at"] = time.time()
            updated = Skill.model_validate(merged)
            items[idx] = updated
            _write(items)
            return updated
        raise KeyError(skill_id)


def delete(skill_id: str) -> bool:
    with _lock:
        items = load()
        if not any(s.id == skill_id for s in items):
            return False
        remaining = [s for s in items if s.id != skill_id]
        if not remaining:
            remaining = _builtins()
        _write(remaining)
        return True


def restore_builtins() -> list[Skill]:
    """Re-add any built-in that is missing; leaves operator edits alone."""
    with _lock:
        items = load()
        existing = {s.id for s in items}
        added = False
        for entry in _builtins():
            if entry.id not in existing:
                items.append(entry)
                added = True
        if added:
            _write(items)
        return items


# ------------------------------------------------------------------------ gating


def _provider_configured(skill: Skill, tools_cfg: Any) -> bool:
    if not skill.provider_field:
        # No single provider field, but the skill still names the config it
        # needs: an unconfigured Home Assistant or ComfyUI must not be offered
        # to the model as a tool that can only fail.
        if skill.config_fields:
            return any(bool(getattr(tools_cfg, field, "")) for field in skill.config_fields)
        return True
    if skill.provider_field == "alexa_provider":
        return bool(tools_cfg.resolved_alexa_provider())
    return bool(getattr(tools_cfg, skill.provider_field, ""))


def enabled(force: bool = False) -> list[Skill]:
    """Skills that are switched on and whose provider is actually configured."""
    tools_cfg = config.load().tools
    return [
        s for s in load(force)
        if s.enabled and _provider_configured(s, tools_cfg)
    ]


def available(force: bool = False) -> list[Skill]:
    """Skills switched on, whether or not their provider is configured yet.

    The admin panel uses this so a half-configured skill still shows its row.
    """
    return [s for s in load(force) if s.enabled]


def withheld_tool_names(force: bool = False) -> set[str]:
    """Tools that a disabled or unconfigured skill owns, and so must not be offered."""
    skills = load(force)
    live = {s.id for s in enabled(force)}
    out: set[str] = set()
    for s in skills:
        if s.id not in live:
            out.update(s.tools)
    return out


def prompt_sections(force: bool = False) -> list[str]:
    """Instruction blocks for every active skill, in declaration order."""
    return [s.instructions.strip() for s in enabled(force) if s.instructions.strip()]

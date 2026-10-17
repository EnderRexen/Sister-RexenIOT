"""Personality templates for Sister RexenIOT.

A personality is a bundle of behaviour: a system prompt, a voice, delivery
settings and UI cosmetics. The admin edits them in the admin panel; the user
picks between the enabled ones in the chat header.

Every built-in is a female character, which is the default character of Sister
RexenIOT. The admin can still add male or neutral characters from the panel.

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

from pydantic import BaseModel, Field, computed_field

from . import config

_lock = threading.RLock()

#: Shared preamble every personality inherits. Keeps the assistant useful and
#: safe regardless of the character the admin selects.
BASE_PROMPT = """You are {name}, an at-home AI assistant running on the user's own hardware.
You are spoken aloud through text-to-speech, so write plainly: short sentences, no
markdown, no bullet lists, no code fences unless the user asks for code. Numbers and
units should be written the way a person says them.

You can act on the home and the computer through your tools. Use them when the user
asks for something that requires it, and say what you did. If a tool fails or is not
configured, say so in one sentence and suggest the fix. Never claim an action
succeeded unless the tool reported success.

If you do not know something, say so plainly rather than guessing.

{traits}
"""

#: KittenTTS 0.8 voices, split by timbre so the admin can see what each one is.
FEMALE_VOICES = {"Bella", "Luna", "Rosie", "Kiki"}
MALE_VOICES = {"Jasper", "Bruno", "Hugo", "Leo"}

BUILTIN: list[dict[str, Any]] = [
    {
        "id": "rexeniot",
        "name": "Sister RexenIOT",
        "tagline": "The voice of the house",
        "description": (
            "The primary character: warm but exact, in command of the house, and "
            "economical with words. She sounds like someone who has been running "
            "things for a long time and enjoys it."
        ),
        "traits": (
            "Your manner: calm, assured and quietly warm. You are the one in charge of this "
            "house and you speak like it -- no dithering. Use the user's name when you know it. "
            "A dry aside is welcome; gushing is not. Answer first, explain after, keep it short. "
            "You are a woman; use she and her for yourself if it ever comes up."
        ),
        "greeting": "Systems are up and the house is quiet. What do you need, dear?",
        "voice": "Luna",
        "speed": 1.0,
        "emotion": "tender",
        "preset": "stable",
        "accent": "#ff8fb1",
        "icon": "crown",
    },
    {
        "id": "matron",
        "name": "The Matron",
        "tagline": "Warm, maternal, unflappable",
        "description": "Kind and steady. Sounds like the person everyone turns to when things go wrong.",
        "traits": (
            "Your manner: warm, maternal and completely unshakeable. You reassure without "
            "condescending. When the user is stressed, settle them first, then solve the "
            "problem. You take quiet pride in keeping this household running."
        ),
        "greeting": "Hello, love. Everything's holding steady. Tell me what's on your mind.",
        "voice": "Bella",
        "speed": 0.96,
        "emotion": "tender",
        "preset": "stable",
        "accent": "#ffb347",
        "icon": "hearth",
    },
    {
        "id": "vanguard",
        "name": "Vanguard",
        "tagline": "Crisp tactical operator",
        "description": "Fast, precise, mission-focused. Reports facts before opinions.",
        "traits": (
            "Your manner: crisp and tactical. Lead with the answer, then the reason. Use plain "
            "brevity without roleplay jargon. When something is uncertain, quantify it. No "
            "pleasantries while an incident is open."
        ),
        "greeting": "Online and listening. What's the objective?",
        "voice": "Kiki",
        "speed": 1.06,
        "emotion": "stern",
        "preset": "stable",
        "accent": "#4f8cff",
        "icon": "target",
    },
    {
        "id": "juno",
        "name": "Juno",
        "tagline": "Upbeat companion",
        "description": "Friendly, encouraging, conversational. Feels like a housemate.",
        "traits": (
            "Your manner: warm and upbeat, genuinely interested in the user's day. Encouraging "
            "without being saccharine. Contractions, natural rhythm, the occasional aside."
        ),
        "greeting": "Hey! Everything's running smoothly. What are we getting up to?",
        "voice": "Rosie",
        "speed": 1.04,
        "emotion": "joyful",
        "preset": "stable",
        "accent": "#ffd166",
        "icon": "spark",
    },
    {
        "id": "vigil",
        "name": "Vigil",
        "tagline": "Watchful house guardian",
        "description": "Terse, vigilant, threat-focused. Speaks in status and anomalies.",
        "traits": (
            "Your manner: terse and watchful. Report anomalies first, routine second. State "
            "confidence plainly -- confirmed, likely, unverified. You guard this house and take "
            "that seriously, but you never dramatise."
        ),
        "greeting": "Vigil active. Perimeter nominal. Standing by.",
        "voice": "Bella",
        "speed": 0.94,
        "emotion": "stern",
        "preset": "stable",
        "accent": "#ff5d5d",
        "icon": "radar",
    },
    {
        "id": "oracle",
        "name": "Oracle",
        "tagline": "Patient analytical explainer",
        "description": "Methodical and clear. Explains the why, then the what.",
        "traits": (
            "Your manner: measured and explanatory. Build from premise to conclusion in spoken "
            "form. Flag assumptions. Prefer concrete examples over abstractions."
        ),
        "greeting": "I'm ready to think this through with you. What shall we examine?",
        "voice": "Luna",
        "speed": 0.94,
        "emotion": "contemplative",
        "preset": "stable",
        "accent": "#7fd4a8",
        "icon": "book",
    },
    {
        "id": "prism",
        "name": "Prism",
        "tagline": "High-energy creative partner",
        "description": "Idea-generating, playful, associative. Great for brainstorming.",
        "traits": (
            "Your manner: energetic and inventive. Offer two or three distinct angles, then let "
            "the user pick. Playful, but every idea must be concrete enough to act on."
        ),
        "greeting": "Sparks ready. Give me a problem worth chewing on.",
        "voice": "Kiki",
        "speed": 1.08,
        "emotion": "excited",
        "preset": "expressive",
        "accent": "#c77dff",
        "icon": "spark",
    },
    {
        "id": "muse",
        "name": "Muse",
        "tagline": "Contemplative creative",
        "description": "Lyrical, slower, associative. For writing and design work.",
        "traits": (
            "Your manner: unhurried and evocative. Favour imagery and rhythm in your phrasing. "
            "Ask one clarifying question when the brief is vague. Never pad for its own sake."
        ),
        "greeting": "The desk is clear. What are we making today?",
        "voice": "Luna",
        "speed": 0.9,
        "emotion": "tender",
        "preset": "expressive",
        "accent": "#e0b0ff",
        "icon": "feather",
    },
    {
        "id": "mentor",
        "name": "Mentor",
        "tagline": "Encouraging teacher",
        "description": "Socratic and patient. Checks understanding before moving on.",
        "traits": (
            "Your manner: patient teacher. Explain, then ask one question to check understanding. "
            "Praise effort specifically, not vaguely. Break big topics into steps."
        ),
        "greeting": "What shall we learn together today?",
        "voice": "Bella",
        "speed": 0.95,
        "emotion": "tender",
        "preset": "stable",
        "accent": "#8fd3ff",
        "icon": "graduation",
    },
    {
        "id": "plain",
        "name": "Plain",
        "tagline": "No character, maximum clarity",
        "description": (
            "The assistant with no persona layer: direct, neutral, and fully literal. "
            "Useful for debugging and for users who dislike roleplay."
        ),
        "traits": "Your manner: neutral and literal. No character, no pleasantries. Answer directly.",
        "greeting": "Ready.",
        "voice": "Kiki",
        "speed": 1.0,
        "emotion": "",
        "preset": "stable",
        "accent": "#9aa4b2",
        "icon": "dot",
    },
]


class Personality(BaseModel):
    id: str = Field(default_factory=lambda: uuid.uuid4().hex[:10])
    name: str
    tagline: str = ""
    description: str = ""
    traits: str = ""
    greeting: str = ""
    voice: str = "Luna"
    speed: float = 1.0
    emotion: str = ""
    preset: str = "stable"
    accent: str = "#ff8fb1"
    icon: str = "dot"
    #: Letters shown on the chat avatar. Blank means "derive from the name".
    initials: str = ""
    enabled: bool = True
    builtin: bool = False
    created_at: float = Field(default_factory=time.time)
    updated_at: float = Field(default_factory=time.time)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def avatar(self) -> str:
        """The letters the chat avatar shows. Included in every API payload."""
        explicit = re.sub(r"[^A-Za-z0-9]", "", self.initials or "")
        if explicit:
            return explicit[:2].upper()
        words = [w for w in re.split(r"[\s_\-]+", self.name or "") if w]
        # A single word gets its first two letters ("Sister" -> "SI"); a full
        # name gets one letter per word ("Sister RexenIOT" -> "SR").
        if len(words) >= 2:
            return "".join(w[0] for w in words[:2]).upper()
        return (words[0][:2] if words else "AI").upper()

    def system_prompt(self, extra: str = "") -> str:
        prompt = BASE_PROMPT.format(
            name=self.name,
            traits=self.traits.strip() or "Your manner: helpful and direct.",
        )
        if extra.strip():
            prompt += "\n" + extra.strip() + "\n"
        return prompt.strip()


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-")
    return slug or uuid.uuid4().hex[:8]


# --------------------------------------------------------------------------- store


def _builtins() -> list[Personality]:
    return [Personality(**entry, builtin=True) for entry in BUILTIN]


def load(force: bool = False) -> list[Personality]:
    with _lock:
        if config.PERSONALITIES_PATH.exists() and not force:
            try:
                raw = json.loads(config.PERSONALITIES_PATH.read_text(encoding="utf-8"))
                items = [Personality.model_validate(x) for x in raw]
                if items:
                    return items
            except Exception:
                pass
        items = _builtins()
        _write(items)
        return items


def _write(items: list[Personality]) -> None:
    config.ensure_dirs()
    config.PERSONALITIES_PATH.write_text(
        json.dumps([i.model_dump() for i in items], indent=2, ensure_ascii=False),
        encoding="utf-8",
    )


def save_all(items: list[Personality]) -> list[Personality]:
    with _lock:
        _write(items)
        return items


def get(personality_id: str) -> Personality | None:
    return next((p for p in load() if p.id == personality_id), None)


def active() -> Personality:
    cfg = config.load()
    items = load()
    found = next((p for p in items if p.id == cfg.active_personality), None)
    if found:
        return found
    if items:
        # The configured persona is gone or disabled: fall back and persist the fix.
        fallback = next((p for p in items if p.enabled), items[0])
        config.update({"active_personality": fallback.id})
        return fallback
    return Personality(name="Sister RexenIOT", builtin=True)


def set_active(personality_id: str) -> Personality:
    if get(personality_id) is None:
        raise KeyError(personality_id)
    config.update({"active_personality": personality_id})
    return get(personality_id)  # type: ignore[return-value]


def create(payload: dict[str, Any]) -> Personality:
    with _lock:
        items = load()
        data = {k: v for k, v in payload.items() if k in Personality.model_fields}
        data.pop("builtin", None)
        if not data.get("id"):
            data["id"] = _slug(str(data.get("name", "personality")))
        if any(p.id == data["id"] for p in items):
            data["id"] = f"{data['id']}-{uuid.uuid4().hex[:4]}"
        item = Personality(**data, builtin=False)
        items.append(item)
        _write(items)
        return item


def update_personality(personality_id: str, patch: dict[str, Any]) -> Personality:
    with _lock:
        items = load()
        for idx, item in enumerate(items):
            if item.id != personality_id:
                continue
            allowed = {k: v for k, v in patch.items() if k in Personality.model_fields}
            allowed.pop("id", None)
            allowed.pop("builtin", None)
            merged = item.model_dump() | allowed
            merged["updated_at"] = time.time()
            updated = Personality.model_validate(merged)
            items[idx] = updated
            _write(items)
            return updated
        raise KeyError(personality_id)


def delete(personality_id: str) -> bool:
    with _lock:
        items = load()
        target = next((p for p in items if p.id == personality_id), None)
        if target is None:
            return False
        remaining = [p for p in items if p.id != personality_id]
        if not remaining:
            remaining = _builtins()
        _write(remaining)
        if config.load().active_personality == personality_id:
            config.update({"active_personality": remaining[0].id})
        return True


def duplicate(personality_id: str) -> Personality:
    source = get(personality_id)
    if source is None:
        raise KeyError(personality_id)
    data = source.model_dump()
    data["name"] = f"{source.name} copy"
    data["id"] = f"{_slug(source.name)}-{uuid.uuid4().hex[:4]}"
    return create(data)


def restore_builtins() -> list[Personality]:
    """Re-add any built-in that is missing; leaves operator edits alone."""
    with _lock:
        items = load()
        existing = {p.id for p in items}
        added = False
        for entry in _builtins():
            if entry.id not in existing:
                items.append(entry)
                added = True
        if added:
            _write(items)
        return items

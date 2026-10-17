"""Task routing: decide which backend handles a request.

Sister RexenIOT can be pointed at several local backends at once, and they are good at
different things:

* **chat** -- Ollama, LM Studio, llama.cpp, vLLM or a hosted API. Conversation,
  reasoning, and tool calls. This is the default for almost everything.
* **image / 3D** -- ComfyUI. Diffusion pipelines the chat model cannot run.
* **speech** -- KittenTTS, handled separately by the TTS engine.

Classification is keyword-first and cheap, so it works with no model loaded. When
a chat backend is configured the caller may ask for a model-assisted decision,
but the keyword pass stays the fallback.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from . import config

# --------------------------------------------------------------------------- intents

#: Verbs that indicate the user wants a picture or a 3D asset made.
IMAGE_PATTERNS = [
    r"\b(generate|create|make|draw|render|paint|design|sketch|produce)\b.*\b(image|picture|photo|illustration|art|painting|render|portrait|logo|icon|wallpaper|poster|sprite|texture)\b",
    r"\b(generate|create|draw|render)\b.*\b(in the style of|like a|as a)\b",
    r"\b(text[- ]to[- ]image|img2img|inpaint|outpaint|upscale)\b",
    r"\b(diffusion|stable diffusion|flux|comfyui)\b",
]

MODEL3D_PATTERNS = [
    r"\b(generate|create|make|model)\b.*\b(3d|mesh|stl|gltf|glb|obj file|printable)\b",
    r"\b(three[- ]d|3d)\s+(model|asset|object|print)\b",
]

#: Phrases that mean "just answer me".
CHAT_OVERRIDES = [
    r"\b(what|who|why|how|when|where|explain|describe|tell me|summar)\b",
    r"\b(turn|switch|set|dim|lock|unlock|open|close)\b.*\b(light|lamp|door|thermostat|scene|blind|lock)\b",
    r"\b(status|report|check|list)\b",
]


@dataclass
class RouteDecision:
    backend: str                      # chat | comfyui | comfyui_3d
    reason: str
    confidence: float
    alternatives: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "backend": self.backend,
            "reason": self.reason,
            "confidence": round(self.confidence, 2),
            "alternatives": self.alternatives,
        }


def _matches(patterns: list[str], text: str) -> bool:
    return any(re.search(p, text, re.I) for p in patterns)


def classify(message: str) -> RouteDecision:
    """Pick a backend for ``message``. Keyword-based, no model required."""
    text = (message or "").strip()
    if not text:
        return RouteDecision("chat", "empty message", 0.4)

    cfg = config.load()
    comfy_available = bool(cfg.tools.resolved_comfyui_url())
    alternatives = ["chat"]
    if comfy_available:
        alternatives.append("comfyui")

    if _matches(MODEL3D_PATTERNS, text):
        return RouteDecision(
            "comfyui_3d" if comfy_available else "chat",
            "3D asset request" if comfy_available else "3D request, but ComfyUI is not configured",
            0.85 if comfy_available else 0.3,
            alternatives,
        )

    if _matches(IMAGE_PATTERNS, text) and not _matches(CHAT_OVERRIDES, text):
        return RouteDecision(
            "comfyui" if comfy_available else "chat",
            "image generation request" if comfy_available else "image request, but ComfyUI is not configured",
            0.8 if comfy_available else 0.3,
            alternatives,
        )

    return RouteDecision("chat", "conversation or tool use", 0.9, alternatives)


def backends() -> list[dict[str, Any]]:
    """Describe every backend so the admin UI can show routing at a glance."""
    cfg = config.load()
    llm = cfg.llm
    chat_ready = bool(llm.model) and (llm.provider != "offline")
    base = llm.base_url or ("http://localhost:11434" if llm.provider in ("ollama", "") else "")
    kind = "ollama" if "11434" in base or "ollama" in base.lower() else (
        "lmstudio" if "1234" in base else ("vllm" if "8000" in base else "openai-compatible")
    )
    return [
        {
            "id": "chat",
            "name": {
                "ollama": "Ollama",
                "lmstudio": "LM Studio",
                "vllm": "vLLM",
            }.get(kind, "OpenAI-compatible"),
            "role": "Conversation, reasoning and tool calls",
            "ready": chat_ready,
            "base_url": base or None,
            "model": llm.model or None,
            "handles": ["chat", "tools", "reminders", "memory"],
        },
        {
            "id": "comfyui",
            "name": "ComfyUI",
            "role": "Image and 3D generation",
            "ready": bool(cfg.tools.resolved_comfyui_url()),
            "base_url": cfg.tools.resolved_comfyui_url() or None,
            "workflow": cfg.tools.comfyui_workflow,
            "handles": ["image", "3d"],
        },
        {
            "id": "tts",
            "name": "KittenTTS",
            "role": "Speech synthesis",
            "ready": True,
            "base_url": None,
            "model": cfg.tts.model_id,
            "handles": ["speech"],
        },
        {
            "id": "alexa",
            "name": "Alexa",
            "role": "Announcements, voice commands and routines on the Echo devices",
            "ready": bool(cfg.tools.resolved_alexa_provider()),
            "base_url": None,
            "provider": cfg.tools.resolved_alexa_provider() or None,
            # The reach depends on the bridge, so state it rather than implying
            # every provider can do everything.
            "handles": (
                ["announce", "command", "ask", "routine"]
                if cfg.tools.resolved_alexa_provider() == "alexacli"
                else ["announce", "routine"]
                if cfg.tools.resolved_alexa_provider() == "voicemonkey"
                else ["announce"]
                if cfg.tools.resolved_alexa_provider() == "home_assistant"
                else []
            ),
        },
    ]

"""Application configuration: typed defaults, JSON persistence, path resolution.

Everything the admin panel can change lives in ``data/config.json``. Secrets are
never written to that file by the server; API keys are read from the environment
first and only fall back to the config file when the operator typed one in.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

# --------------------------------------------------------------------------- paths

#: The app folder. A portable copy keeps every byte of state inside it, so
#: ``REXENIOT_HOME`` lets an operator point the data somewhere else (a writable
#: drive, a shared folder) without moving the code.
ROOT = Path(os.environ.get("REXENIOT_HOME", "").strip() or Path(__file__).resolve().parent.parent).resolve()
PORTABLE = bool(os.environ.get("REXENIOT_PORTABLE", "").strip())
DATA_DIR = ROOT / "data"
WEB_DIR = ROOT / "web"
MODELS_DIR = DATA_DIR / "models"
CONFIG_PATH = DATA_DIR / "config.json"
PERSONALITIES_PATH = DATA_DIR / "personalities.json"
PROFILES_PATH = DATA_DIR / "voice_profiles.json"
SKILLS_PATH = DATA_DIR / "skills.json"
CHAT_LOG_PATH = DATA_DIR / "chat_history.jsonl"
HARDWARE_PATH = DATA_DIR / "hardware_profile.json"


def ensure_dirs() -> None:
    for path in (DATA_DIR, MODELS_DIR, DATA_DIR / "voices", DATA_DIR / "cache"):
        path.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- models


class ServerConfig(BaseModel):
    host: str = "127.0.0.1"
    port: int = 8070
    open_browser: bool = True


class LLMConfig(BaseModel):
    """Connection to the "brain".

    ``provider`` selects the wire format. ``auto`` sniffs from the base URL, so a
    bare ``http://localhost:11434/v1`` is understood as Ollama without ceremony.
    """

    provider: Literal["auto", "openai", "anthropic", "ollama", "offline"] = "auto"
    base_url: str = ""
    api_key: str = ""
    model: str = ""
    temperature: float = 0.7
    max_tokens: int = 800
    timeout_s: float = 120.0
    system_prompt_extra: str = ""
    history_turns: int = 12

    def resolved_api_key(self) -> str:
        """Environment wins over the stored value so keys stay out of the repo."""
        for var in ("REXENIOT_API_KEY", "OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
            value = os.environ.get(var)
            if value:
                return value.strip()
        return self.api_key.strip()


class TTSConfig(BaseModel):
    enabled: bool = True
    #: KittenTTS repository id. The launcher downloads the matching weights.
    model_id: str = "KittenML/kitten-tts-mini-0.8"
    voice: str = "Luna"
    speed: float = 1.0
    sample_rate: int = 24000
    preset: Literal["stable", "expressive"] = "stable"
    #: Applied to every reply so the delivery matches the chosen persona.
    default_emotion: str = ""
    device: Literal["auto", "cpu", "cuda"] = "auto"
    #: Which engine produces the voice. "model" always uses KittenTTS (the
    #: intended default), "browser" always uses the browser's own speech
    #: synthesis, and "auto" uses the model but falls back to the browser when it
    #: cannot run, so a machine that cannot host the vocal model still talks.
    engine: Literal["model", "browser", "auto"] = "model"
    #: Language-model backend for the full tier. "torch" is the default and runs
    #: on CPU or GPU; "vllm" is an optional GPU-only install (pip install
    #: "kittenml[vllm]") that upstream measures at ~3-4x faster, and is what makes
    #: interactive cloning practical. Ignored by the ONNX tiers, which have their
    #: own backend, and by the torch tiers when CUDA is not reachable.
    backend: Literal["torch", "vllm"] = "torch"
    #: Cap per-request synthesis so a runaway reply cannot stall the UI.
    max_chars: int = 1200
    cache_enabled: bool = True
    #: Populated by the hardware scan; drives which models are recommended.
    tier: Literal["nano", "micro", "mini", "full"] = "mini"


class STTConfig(BaseModel):
    """Local speech-to-text for dictation and wake mode.

    The browser records audio and POSTs it to ``/api/transcribe``; the server
    transcribes it here. That keeps the audio on this machine, which the
    browser's own recogniser does not do.

    Dictation needs the optional speech-to-text set (``requirements-stt.txt``).
    Until it is installed, transcription reports itself as unavailable and the
    HUD falls back to the browser recogniser, so the feature degrades instead of
    breaking.
    """

    enabled: bool = True
    #: faster-whisper repository id. tiny.en is 75 MB and transcribes faster than
    #: real time on a CPU, which is what a command-length utterance needs.
    model_id: str = "Systran/faster-whisper-tiny.en"
    device: Literal["auto", "cpu", "cuda"] = "auto"
    #: Empty means "pick from the device": int8 on a CPU, float16 on a GPU.
    #: CTranslate2 rejects None here, so the engine resolves it.
    compute_type: str = ""
    #: Language hint for the model. Empty means autodetect, which is right for a
    #: multilingual install but costs a little latency and accuracy.
    language: str = "en"
    #: 1 is fastest, 5 is most accurate. Commands are short, so 1 is enough.
    beam_size: int = 1


class VoiceInputConfig(BaseModel):
    """How the HUD captures speech, and the wake-word behaviour around it.

    Two capture paths exist. The server path records audio in the browser and
    sends it to ``/api/transcribe``, where ``STTConfig`` transcribes it locally.
    The browser path uses the vendor's own recogniser, which needs Chrome or Edge
    and sends the audio to that vendor.

    ``engine`` picks between them. "server" and "browser" force one; "auto"
    prefers the server and falls back to the browser, so a machine without the
    optional speech-to-text set still dictates.

    Wake mode keeps a capture session open continuously, waiting for one of
    ``wake_words``. After the wake word the session stays open and every
    utterance is treated as addressed to the assistant until one of
    ``sleep_words`` is heard or ``session_idle_s`` passes with no speech.
    """

    enabled: bool = True
    #: Which capture path to use: "server" (local, needs requirements-stt.txt),
    #: "browser" (vendor recogniser, Chrome/Edge only), or "auto".
    engine: Literal["server", "browser", "auto"] = "auto"
    language: str = "en-US"
    auto_send: bool = True
    silence_ms: int = 1400

    wake_enabled: bool = True
    wake_words: list[str] = Field(
        default_factory=lambda: ["hey rexeniot", "okay rexeniot", "hey sister", "wake up rexeniot"]
    )
    sleep_words: list[str] = Field(
        default_factory=lambda: [
            "stop listening",
            "go to sleep",
            "that's all",
            "thats all",
            "goodbye rexeniot",
            "stand down",
        ]
    )
    #: Seconds of silence in a wake session before returning to wake-word duty.
    session_idle_s: int = 45
    #: Say something when the wake word is heard, so the user knows she is up.
    acknowledge: bool = True
    acknowledgement: str = "I'm here."


class ToolConfig(BaseModel):
    enabled: bool = True
    #: Home Assistant long-lived access token + base URL.
    home_assistant_url: str = ""
    home_assistant_token: str = ""
    #: MQTT broker for IoT devices that do not speak Home Assistant.
    mqtt_host: str = ""
    mqtt_port: int = 1883
    mqtt_username: str = ""
    mqtt_password: str = ""
    #: Dangerous: lets the agent run shell commands on the host.
    allow_shell: bool = False
    shell_allowlist: list[str] = Field(default_factory=lambda: ["echo", "dir", "ls", "whoami"])
    #: Domain allowlist for the generic REST/web tool. Empty means "ask me".
    web_allowlist: list[str] = Field(default_factory=list)
    request_timeout_s: float = 20.0
    #: ComfyUI handles image and 3D generation tasks the chat model cannot do.
    comfyui_url: str = ""
    comfyui_workflow: str = "rexeniot_default"

    #: Alexa. Amazon publishes no API for commanding an Echo or making it speak,
    #: so ``provider`` picks whichever supported route the operator can use:
    #: "voicemonkey" (HTTP, hosted bridge), "alexacli" (local CLI, unofficial API)
    #: or "home_assistant" (the Alexa Media Player integration).
    alexa_provider: str = ""
    voicemonkey_token: str = ""
    voicemonkey_device: str = ""
    #: Overrides the token's default device as the target for a bare "announce".
    voicemonkey_announce_device: str = ""
    alexacli_path: str = ""
    #: The device alexacli targets when a tool call does not name one. alexacli
    #: requires a device for speak/command/ask, so this is what makes a bare
    #: "announce this" work.
    alexacli_device: str = ""
    #: Home Assistant media_player entity that fronts Alexa, for provider "home_assistant".
    alexa_ha_entity: str = ""

    def resolved_comfyui_url(self) -> str:
        return os.environ.get("COMFYUI_URL", "").strip() or self.comfyui_url.strip()

    def resolved_voicemonkey_token(self) -> str:
        return os.environ.get("VOICEMONKEY_TOKEN", "").strip() or self.voicemonkey_token.strip()

    def resolved_alexacli_path(self) -> str:
        return os.environ.get("ALEXACLI_PATH", "").strip() or self.alexacli_path.strip()

    def resolved_alexacli_device(self) -> str:
        return os.environ.get("ALEXACLI_DEVICE", "").strip() or self.alexacli_device.strip()

    def resolved_alexa_provider(self) -> str:
        """The configured Alexa provider, or "" when the operator has not set one."""
        return (os.environ.get("ALEXA_PROVIDER", "").strip() or self.alexa_provider.strip()).lower()

    def resolved_ha_token(self) -> str:
        return os.environ.get("HOME_ASSISTANT_TOKEN", "").strip() or self.home_assistant_token.strip()

    def resolved_mqtt_password(self) -> str:
        return os.environ.get("MQTT_PASSWORD", "").strip() or self.mqtt_password.strip()


class Branding(BaseModel):
    """Names shown in the UI and spoken by the agent."""

    app_name: str = "Sister RexenIOT"
    short_name: str = "RexenIOT"
    tagline: str = "Keeper of the house"
    #: The assistant describes herself this way in the system prompt.
    gender: Literal["female", "male", "neutral"] = "female"


class AppConfig(BaseModel):
    branding: Branding = Field(default_factory=Branding)
    server: ServerConfig = Field(default_factory=ServerConfig)
    llm: LLMConfig = Field(default_factory=LLMConfig)
    tts: TTSConfig = Field(default_factory=TTSConfig)
    stt: STTConfig = Field(default_factory=STTConfig)
    voice_input: VoiceInputConfig = Field(default_factory=VoiceInputConfig)
    tools: ToolConfig = Field(default_factory=ToolConfig)
    active_personality: str = "rexeniot"
    #: Gates every write endpoint in the admin panel.
    admin_token: str = ""
    first_run_complete: bool = False


# --------------------------------------------------------------------------- store

_lock = threading.RLock()
_cache: AppConfig | None = None


def load(force: bool = False) -> AppConfig:
    global _cache
    with _lock:
        if _cache is not None and not force:
            return _cache
        ensure_dirs()
        if CONFIG_PATH.exists():
            try:
                raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                _cache = AppConfig.model_validate(raw)
            except Exception:
                # A corrupt config must not stop the app from booting.
                _cache = AppConfig()
        else:
            _cache = AppConfig()
            save(_cache)
        return _cache


def save(cfg: AppConfig | None = None) -> AppConfig:
    global _cache
    with _lock:
        cfg = cfg or _cache or AppConfig()
        ensure_dirs()
        CONFIG_PATH.write_text(
            json.dumps(cfg.model_dump(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        _cache = cfg
        return cfg


def update(patch: dict[str, Any]) -> AppConfig:
    """Deep-merge ``patch`` into the live config and persist it."""
    with _lock:
        cfg = load()
        merged = _deep_merge(cfg.model_dump(), patch)
        new_cfg = AppConfig.model_validate(merged)
        return save(new_cfg)


def _deep_merge(base: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def public_view(cfg: AppConfig) -> dict[str, Any]:
    """Config with secrets removed, safe to hand to the browser."""
    data = cfg.model_dump()
    for section, field in (("llm", "api_key"), ("tools", "home_assistant_token"),
                           ("tools", "mqtt_password"), ("tools", "voicemonkey_token"),
                           ("admin_token", None)):
        if field is None:
            data[section] = "***" if data.get(section) else ""
        elif data.get(section, {}).get(field):
            data[section][field] = "***"
    return data

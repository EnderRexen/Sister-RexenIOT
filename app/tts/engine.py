"""KittenTTS synthesis engine.

Wraps both KittenTTS families behind one interface:

* **onnx tiers** (nano / micro / mini) -- ``generate(text, voice, speed)``, 8
  voices, no torch. This is the default and works on any CPU.
* **full tier** -- ``generate(text, voice, preset, ...)``, 47 voices, cloning and
  expression tags. Needs the torch stack.

The model is loaded lazily on a worker thread because first-call loading takes
seconds and must not block the event loop. Generation is serialised with a lock:
the underlying sessions hold mutable state and are not reentrant.
"""

from __future__ import annotations

import hashlib
import io
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import numpy as np

from .. import config, model_catalog

log = logging.getLogger("rexeniot.tts")

#: Voices shared by every tier (KittenTTS 0.8 speaker set).
BASE_VOICES = ["Bella", "Jasper", "Luna", "Bruno", "Rosie", "Hugo", "Kiki", "Leo"]

#: Emotion tags the full tier understands, used to colour persona delivery.
EMOTION_TAGS = [
    "angry", "contemplative", "excited", "joyful", "mundane",
    "nervous", "sad", "stern", "surprised", "tender",
]

VOICE_CHARACTERS = {
    "Bella": "warm, natural female",
    "Jasper": "clear, composed male",
    "Luna": "bright, friendly female",
    "Bruno": "deep, steady male",
    "Rosie": "cheerful female",
    "Hugo": "measured male",
    "Kiki": "expressive female",
    "Leo": "calm, articulate male",
}


class TTSError(RuntimeError):
    pass


def _is_torch_tier(model_id: str) -> bool:
    """True when ``model_id`` is the KittenTTS 2 / torch family."""
    entry = model_catalog.KITTEN_TTS_MODELS.get(
        model_catalog.tier_of_repo(model_id) or ""
    )
    return bool(entry and entry.get("backend") == "torch")


def _cuda_available(model_id: str) -> bool:
    """Whether the runtime behind ``model_id`` can actually reach a GPU."""
    if _is_torch_tier(model_id):
        try:
            import torch  # noqa: PLC0415

            return bool(torch.cuda.is_available())
        except Exception:
            return False
    try:
        import onnxruntime as ort  # noqa: PLC0415

        return "CUDAExecutionProvider" in ort.get_available_providers()
    except Exception:
        return False


class TTSEngine:
    def __init__(self) -> None:
        self._model: Any = None
        self._model_id: str = ""
        self._is_full = False
        self._device: str = "cpu"
        self._voices: list[str] = list(BASE_VOICES)
        self._sample_rate = 24000
        self._load_lock = threading.Lock()
        self._gen_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="tts")
        self._status: dict[str, Any] = {"state": "idle", "detail": "not loaded"}
        self._cache_dir = config.DATA_DIR / "cache" / "tts"
        self._cache_dir.mkdir(parents=True, exist_ok=True)

    # ------------------------------------------------------------------ status

    @property
    def status(self) -> dict[str, Any]:
        return {
            **self._status,
            "model_id": self._model_id or None,
            "tier": self._tier_of(self._model_id),
            "backend": "torch" if self._is_full else "onnx",
            "device": self._device or None,
            "device_requested": config.load().tts.device,
            "voice_count": len(self._voices),
            "sample_rate": self._sample_rate,
        }

    @staticmethod
    def _tier_of(model_id: str) -> str:
        for tier, entry in model_catalog.KITTEN_TTS_MODELS.items():
            if entry["repo"] == model_id:
                return tier
        return "custom" if model_id else "none"

    @staticmethod
    def _resolve_device(requested: str, model_id: str) -> tuple[str | None, str | None]:
        """Map the config value onto what each backend actually accepts.

        Returns ``(device, backend)``. ``None`` means "let the library choose",
        which is the sensible default in both cases: torch picks CUDA when it can,
        and onnxruntime prefers its fastest installed provider.

        The ONNX path ignores ``device`` entirely and keys off ``backend``
        (``cpu`` / ``cuda`` / ``amd_gpu``); the torch path is the reverse. Sending
        the same word to both keeps one setting working for either tier.
        """
        if requested == "cpu":
            return "cpu", "cpu"
        if requested == "cuda":
            # An explicit request for a GPU that is not reachable would otherwise
            # fail the whole load. Fall back to auto and say so in the status.
            if _cuda_available(model_id):
                return "cuda", "cuda"
            runtime = "torch" if _is_torch_tier(model_id) else "onnxruntime"
            log.warning(
                "tts.device=cuda requested but %s has no usable CUDA; using auto", runtime
            )
            return None, None
        return None, None

    # ------------------------------------------------------------------- model

    def _load(self, model_id: str) -> None:
        """Load ``model_id``, replacing whatever is loaded. Blocking."""
        from kittenml import KittenTTS

        if model_id == self._model_id and self._model is not None:
            return

        cfg = config.load()
        device, backend = self._resolve_device(cfg.tts.device, model_id)

        self._status = {"state": "loading", "detail": f"loading {model_id} ({device or 'auto'})"}
        log.info("loading TTS model %s (device=%s, backend=%s)", model_id, device, backend)
        try:
            kwargs: dict[str, Any] = {"cache_dir": str(config.MODELS_DIR)}
            # The two backends take the target differently: the torch path wants
            # `device`, the ONNX path wants `backend` ("cpu" / "cuda" / "amd_gpu").
            # Passing the wrong one is harmless but useless, so send both only when
            # it makes sense -- an unknown value makes the ONNX loader raise.
            if device:
                kwargs["device"] = device
            if backend:
                kwargs["backend"] = backend
            model = KittenTTS(model_id, **kwargs)
        except Exception as exc:  # download failure, missing runtime, ...
            self._model = None
            self._model_id = ""
            self._status = {"state": "error", "detail": f"{type(exc).__name__}: {exc}"}
            raise TTSError(f"Could not load TTS model {model_id}: {exc}") from exc

        voices = list(getattr(model, "available_voices", BASE_VOICES) or BASE_VOICES)
        # Distinguish the two families by capability, not class identity: the
        # shared constructor returns a `kittenml.get_model.KittenTTS` subclass in
        # both cases. Only the KittenTTS 2 backend exposes `sample_rate` and the
        # richer generation arguments; the ONNX backend has neither.
        has_sample_rate = hasattr(model, "sample_rate")
        is_full = bool(has_sample_rate and hasattr(model, "generate_stream"))
        # The full tier exposes sample_rate; ONNX is fixed at 24 kHz.
        sr = int(getattr(model, "sample_rate", 24000) or 24000)

        self._model = model
        self._model_id = model_id
        self._voices = voices or BASE_VOICES
        self._is_full = bool(is_full)
        self._sample_rate = sr
        # What the backend actually settled on, which may differ from the request
        # (torch reports its own device; onnxruntime exposes its provider list).
        self._device = self._effective_device(model, device)
        self._status = {
            "state": "ready",
            "detail": f"{model_id} ready on {self._device}",
            "backend": "torch" if is_full else "onnx",
        }
        log.info(
            "TTS ready: %s (%d voices, %d Hz, device=%s)",
            model_id, len(self._voices), sr, self._device,
        )

    @staticmethod
    def _effective_device(model: Any, requested: str | None) -> str:
        """Report the device actually in use, for the status panel.

        The libraries decide for themselves when ``device`` is None, and they do
        not expose a uniform way to ask. For torch the attribute is authoritative;
        for ONNX the loaded session's provider list is, which is what makes the
        difference between a real GPU run and a silent CPU fallback visible.
        """
        reported = getattr(model, "device", None)
        if isinstance(reported, str) and reported:
            return reported
        session = getattr(getattr(model, "model", None), "session", None)
        providers = getattr(session, "get_providers", None)
        if callable(providers):
            try:
                active = providers()
            except Exception:
                active = []
            for name in active:
                if "CUDA" in name:
                    return "cuda"
                if "ROCM" in name:
                    return "rocm"
            if active:
                return "cpu"
        return requested or "cpu"

    def ensure_loaded(self, model_id: str | None = None) -> None:
        """Load on the worker thread; returns once ready or raises."""
        target = model_id or config.load().tts.model_id
        with self._load_lock:
            if self._model is not None and target == self._model_id and not self._device_changed():
                return
            future = self._executor.submit(self._load, target)
            future.result()  # propagate TTSError

    def _device_changed(self) -> bool:
        """True when the configured device no longer matches the loaded one.

        Without this, changing the device in the admin panel would be a no-op
        until the model happened to be swapped for a different tier.
        """
        device, _ = self._resolve_device(config.load().tts.device, self._model_id)
        if device is None:
            return False  # "auto" is always satisfied by whatever is loaded
        return device != self._device

    def unload(self) -> None:
        with self._load_lock:
            self._model = None
            self._model_id = ""
            self._voices = list(BASE_VOICES)
            self._is_full = False
            self._status = {"state": "idle", "detail": "unloaded"}

    # ------------------------------------------------------------------ voices

    def voices(self) -> list[dict[str, str]]:
        out = []
        for name in self._voices:
            character = VOICE_CHARACTERS.get(name)
            if not character:
                character = "multilingual voice" if name in {
                    "Arabic", "Chinese", "French", "German", "Hindi",
                    "Italian", "Portuguese", "Russian", "Spanish",
                } else "built-in voice"
            out.append({"id": name, "name": name, "character": character})
        return out

    # -------------------------------------------------------------------- cache

    def _cache_path(self, text: str, voice: str, speed: float, preset: str, emotion: str) -> Path:
        key = "|".join([self._model_id, text, voice, f"{speed:.2f}", preset, emotion])
        digest = hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]
        return self._cache_dir / f"{digest}.wav"

    def cached_path(self, text: str, voice: str, speed: float, preset: str, emotion: str) -> Path | None:
        cfg = config.load()
        if not cfg.tts.cache_enabled:
            return None
        path = self._cache_path(text, voice, speed, preset, emotion)
        return path if path.exists() else None

    # --------------------------------------------------------------- synthesis

    def synthesize(
        self,
        text: str,
        *,
        voice: str | None = None,
        speed: float | None = None,
        preset: str | None = None,
        emotion: str = "",
        model_id: str | None = None,
    ) -> tuple[np.ndarray, int]:
        """Return ``(float32 mono audio, sample_rate)``."""
        cfg = config.load()
        text = (text or "").strip()
        if not text:
            raise TTSError("no text to synthesize")

        limit = max(cfg.tts.max_chars, 1)
        if len(text) > limit:
            # Trim on a sentence boundary so the cut is not mid-word.
            cut = text[:limit]
            for sep in (". ", "! ", "? ", "; ", ", ", " "):
                idx = cut.rfind(sep)
                if idx > limit * 0.5:
                    cut = cut[: idx + 1]
                    break
            text = cut.strip()

        voice = voice or cfg.tts.voice
        speed = cfg.tts.speed if speed is None else speed
        preset = preset or cfg.tts.preset
        emotion = emotion or cfg.tts.default_emotion

        self.ensure_loaded(model_id)
        if voice not in self._voices:
            voice = self._voices[0] if self._voices else "Jasper"

        with self._gen_lock:
            if self._is_full:
                audio = self._generate_full(text, voice, preset, emotion)
            else:
                audio = self._generate_onnx(text, voice, float(speed))

        audio = np.asarray(audio, dtype=np.float32).reshape(-1)
        if audio.size == 0:
            raise TTSError("model returned empty audio")
        peak = float(np.max(np.abs(audio)))
        if peak > 1.0:
            audio = audio / peak
        return audio, self._sample_rate

    def _generate_onnx(self, text: str, voice: str, speed: float) -> np.ndarray:
        model = self._model
        attempts = [
            {"voice": voice, "speed": speed, "clean_text": True},
            {"voice": voice, "speed": speed},
            {"voice": voice},
            {"voice": voice, "text": text},
        ]
        last: Exception | None = None
        for kwargs in attempts:
            call = dict(kwargs)
            text_arg = call.pop("text", text)
            try:
                return model.generate(text_arg, **call)
            except TypeError as exc:
                last = exc  # signature drift between ONNX releases
                continue
        raise TTSError(f"ONNX generate failed: {last}")

    def _generate_full(self, text: str, voice: str, preset: str, emotion: str) -> np.ndarray:
        model = self._model
        if emotion and emotion in EMOTION_TAGS and not text.startswith("["):
            text = f"[{emotion}] {text}"
        kwargs: dict[str, Any] = {"voice": voice}
        if preset:
            kwargs["preset"] = preset
        try:
            return model.generate(text, **kwargs)
        except TypeError:
            # Older build without presets.
            return model.generate(text, voice=voice)

    # ------------------------------------------------------------ wav encoding

    def wav_bytes(
        self,
        text: str,
        *,
        voice: str | None = None,
        speed: float | None = None,
        preset: str | None = None,
        emotion: str = "",
        use_cache: bool = True,
    ) -> bytes:
        """Encode to WAV bytes, consulting the on-disk cache first."""
        cfg = config.load()
        voice = voice or cfg.tts.voice
        speed = cfg.tts.speed if speed is None else speed
        preset = preset or cfg.tts.preset
        emotion = emotion or cfg.tts.default_emotion

        if use_cache and cfg.tts.cache_enabled:
            hit = self.cached_path(text, voice, float(speed), preset, emotion)
            if hit:
                return hit.read_bytes()

        audio, sr = self.synthesize(
            text, voice=voice, speed=speed, preset=preset, emotion=emotion
        )
        buf = io.BytesIO()
        import soundfile as sf

        sf.write(buf, audio, sr, format="WAV", subtype="PCM_16")
        data = buf.getvalue()

        if use_cache and cfg.tts.cache_enabled:
            try:
                self._cache_path(text, voice, float(speed), preset, emotion).write_bytes(data)
            except Exception:
                pass
        return data

    def clear_cache(self) -> int:
        count = 0
        for f in self._cache_dir.glob("*.wav"):
            try:
                f.unlink()
                count += 1
            except Exception:
                pass
        return count


engine = TTSEngine()

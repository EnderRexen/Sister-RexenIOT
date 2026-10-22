"""Local speech-to-text engine.

Wraps faster-whisper (CTranslate2) behind the same shape as the TTS engine: the
model loads lazily on a worker thread, one lock serialises inference, and a
``status`` dict describes the engine for the admin panel.

Decoding is done here rather than by faster-whisper. The installed PyAV rejects
the ``metadata_errors`` keyword that faster-whisper 1.2.x passes to ``av.open``,
so handing it a path or a file object raises ``TypeError`` before any audio is
read. We therefore decode with PyAV ourselves and pass the model an ndarray,
which is also the faster path: no temporary file, and the resample to 16 kHz
mono happens in ffmpeg instead of in Python.

PyAV is required for the same reason it is awkward -- it bundles its own ffmpeg,
so it reads what browsers actually record (Opus in WebM, and AAC in MP4 on
Safari). libsndfile cannot: the shipped build has no Opus and no Matroska.
"""

from __future__ import annotations

import io
import logging
import threading
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import numpy as np

from .. import config

log = logging.getLogger("rexeniot.stt")

#: Whisper's fixed input rate. Everything is resampled to this.
SAMPLE_RATE = 16000

#: Refuse anything larger; a 25 MB opus upload is already minutes of speech.
MAX_AUDIO_BYTES = 25 * 1024 * 1024

#: Refuse anything longer. Guards against a client that streams indefinitely.
MAX_SECONDS = 120.0


class STTError(RuntimeError):
    pass


def available() -> bool:
    """Whether the optional speech-to-text packages are installed.

    Uses ``find_spec`` rather than importing, because this runs on every
    ``/api/config`` request and importing CTranslate2 costs a few hundred
    milliseconds. A package that is present but broken still reports True here;
    the transcribe call then fails and the HUD falls back to the browser
    recogniser, which is the same outcome for less latency.
    """
    import importlib.util

    for name in ("faster_whisper", "av"):
        try:
            if importlib.util.find_spec(name) is None:
                return False
        except (ImportError, ValueError):
            return False
    return True


def decode_audio(data: bytes, target_sr: int = SAMPLE_RATE) -> np.ndarray:
    """Decode any container ffmpeg reads into mono float32 at ``target_sr``.

    Accepts raw bytes so a recording can go straight from the request body to the
    model without touching the disk. Raises :class:`STTError` when the payload is
    not audio, which is the useful failure for an upload route.
    """
    import av

    chunks: list[np.ndarray] = []
    try:
        with av.open(io.BytesIO(data)) as container:
            stream = next((s for s in container.streams if s.type == "audio"), None)
            if stream is None:
                raise STTError("the recording has no audio stream")
            resampler = av.AudioResampler(format="fltp", layout="mono", rate=target_sr)
            for frame in container.decode(stream):
                for out in resampler.resample(frame):
                    chunks.append(out.to_ndarray().reshape(-1))
            # Flush the resampler: it buffers, and dropping the tail clips the
            # end of the last word.
            for out in resampler.resample(None):
                chunks.append(out.to_ndarray().reshape(-1))
    except STTError:
        raise
    except Exception as exc:  # av.error.*, EOFError on a truncated upload, ...
        raise STTError(f"could not decode the audio: {type(exc).__name__}: {exc}") from exc

    if not chunks:
        raise STTError("the recording contained no audio")
    return np.concatenate(chunks).astype(np.float32)


class STTEngine:
    def __init__(self) -> None:
        self._model: Any = None
        self._model_id: str = ""
        self._device: str = "cpu"
        self._compute_type: str = ""
        self._load_lock = threading.Lock()
        self._run_lock = threading.Lock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stt")
        self._status: dict[str, Any] = {"state": "idle", "detail": "not loaded"}

    # ------------------------------------------------------------------ status

    @property
    def status(self) -> dict[str, Any]:
        return {
            **self._status,
            "model_id": self._model_id or None,
            "device": self._device or None,
            "device_requested": config.load().stt.device,
            "compute_type": self._compute_type or None,
            "sample_rate": SAMPLE_RATE,
            "max_seconds": MAX_SECONDS,
        }

    # ------------------------------------------------------------------- model

    @staticmethod
    def _resolve_device(requested: str) -> tuple[str, str]:
        """Map the config value onto ``(device, compute_type)`` for CTranslate2.

        CTranslate2 does not accept ``None`` for the device the way onnxruntime
        does, so "auto" has to be resolved here. ``int8`` is the right CPU default
        (roughly 2x faster than float32, with no accuracy loss that matters for
        command-length speech); ``float16`` is the right GPU one.
        """
        def cuda_available() -> bool:
            try:
                import ctranslate2

                return ctranslate2.get_cuda_device_count() > 0
            except Exception as exc:  # noqa: BLE001 - any probe failure means no GPU
                log.warning("could not probe for CUDA: %s", exc)
                return False

        if requested == "cpu":
            return "cpu", "int8"
        if requested == "cuda":
            if cuda_available():
                return "cuda", "float16"
            log.warning("stt.device=cuda requested but no CUDA device is visible; using cpu")
            return "cpu", "int8"
        # "auto"
        return ("cuda", "float16") if cuda_available() else ("cpu", "int8")

    def _load(self, model_id: str) -> None:
        """Load ``model_id``, replacing whatever is loaded. Blocking."""
        from faster_whisper import WhisperModel

        if model_id == self._model_id and self._model is not None:
            return

        cfg = config.load()
        device, compute_type = self._resolve_device(cfg.stt.device)
        if cfg.stt.compute_type:
            compute_type = cfg.stt.compute_type

        self._status = {"state": "loading", "detail": f"loading {model_id} ({device})"}
        log.info("loading STT model %s (device=%s, compute_type=%s)", model_id, device, compute_type)
        try:
            model = WhisperModel(
                model_id,
                device=device,
                compute_type=compute_type,
                # Keep weights with the rest of the app's models so a portable
                # install stays inside its own folder.
                download_root=str(config.MODELS_DIR),
            )
        except Exception as exc:  # download failure, missing runtime, ...
            self._model = None
            self._model_id = ""
            self._status = {"state": "error", "detail": f"{type(exc).__name__}: {exc}"}
            raise STTError(f"Could not load STT model {model_id}: {self._load_hint(exc)}") from exc

        self._model = model
        self._model_id = model_id
        self._device = device
        self._compute_type = compute_type
        self._status = {"state": "ready", "detail": f"{model_id} ready on {device}"}
        log.info("STT ready: %s (device=%s, compute_type=%s)", model_id, device, compute_type)

    @staticmethod
    def _load_hint(exc: BaseException) -> str:
        """Name the install command when the failure is a missing package.

        "No module named 'faster_whisper'" says what is wrong but not what to do,
        and it is the message every load path would otherwise surface.
        """
        text = f"{type(exc).__name__}: {exc}"
        blob = f"{exc}".lower()
        if "no module named" not in blob and "importerror" not in type(exc).__name__.lower():
            return text
        for module in ("faster_whisper", "ctranslate2", "av", "tokenizers", "onnxruntime"):
            if module in blob:
                return (
                    f"{text}. Local transcription needs the speech-to-text set; install it "
                    "with 'python -m pip install -r requirements-stt.txt'."
                )
        return text

    def ensure_loaded(self, model_id: str | None = None) -> None:
        """Load on the worker thread; returns once ready or raises."""
        target = model_id or config.load().stt.model_id
        with self._load_lock:
            if self._model is not None and target == self._model_id and not self._device_changed():
                return
            future = self._executor.submit(self._load, target)
            future.result()  # propagate STTError

    def _device_changed(self) -> bool:
        """True when the configured device no longer matches the loaded one."""
        device, _ = self._resolve_device(config.load().stt.device)
        return device != self._device

    def unload(self) -> None:
        with self._load_lock:
            self._model = None
            self._model_id = ""
            self._compute_type = ""
            self._status = {"state": "idle", "detail": "unloaded"}

    # ------------------------------------------------------------ transcription

    def transcribe(
        self,
        data: bytes,
        *,
        language: str | None = None,
        beam_size: int | None = None,
        model_id: str | None = None,
    ) -> dict[str, Any]:
        """Transcribe encoded audio bytes and return the result as a dict."""
        if not data:
            raise STTError("no audio was sent")
        if len(data) > MAX_AUDIO_BYTES:
            raise STTError(
                f"the recording is {len(data) // 1024} KB; the limit is "
                f"{MAX_AUDIO_BYTES // 1024} KB"
            )

        cfg = config.load()
        audio = decode_audio(data)
        seconds = len(audio) / float(SAMPLE_RATE)
        if seconds > MAX_SECONDS:
            raise STTError(f"the recording is {seconds:.0f}s; the limit is {MAX_SECONDS:.0f}s")
        if seconds < 0.15:
            raise STTError("the recording was too short to transcribe")

        self.ensure_loaded(model_id)

        # "" means autodetect; faster-whisper spells that None.
        lang = language if language is not None else cfg.stt.language
        lang = (lang or "").strip() or None
        beam = cfg.stt.beam_size if beam_size is None else beam_size

        # The CTranslate2 model holds mutable decoder state and is not reentrant.
        with self._run_lock:
            segments, info = self._model.transcribe(audio, language=lang, beam_size=beam)
            parts = [s.text.strip() for s in segments]

        return {
            "text": " ".join(p for p in parts if p).strip(),
            "language": getattr(info, "language", None),
            "language_probability": round(
                float(getattr(info, "language_probability", 0.0) or 0.0), 3
            ),
            "duration_s": round(seconds, 2),
            "segments": len(parts),
        }


engine = STTEngine()

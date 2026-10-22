"""The dictation engine: decode, transcribe, and the guards around them.

The interesting property is not that whisper returns text -- it is that the text
is *right*, from the container the browser actually sends. A browser records
Opus in WebM; libsndfile cannot read that, so a decoder that only handled WAV
would pass a naive test and fail in the field. This feeds both a WAV and an
Opus/WebM clip and requires an exact match from each.

    PYTHONPATH=. python scripts/test_stt_engine.py
"""
import io
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import av  # noqa: E402
import numpy as np  # noqa: E402
import soundfile as sf  # noqa: E402

from app import config  # noqa: E402
from app.stt import SAMPLE_RATE, STTError, available, decode_audio  # noqa: E402
from app.stt import engine as stt  # noqa: E402
from app.tts import engine as tts  # noqa: E402

FAILS = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


def to_webm_opus(wav: bytes) -> bytes:
    """Re-encode WAV to Opus/WebM, the way MediaRecorder sends a recording."""
    audio, sr = sf.read(io.BytesIO(wav), dtype="float32", always_2d=True)
    mono = audio.mean(axis=1)
    pcm16 = np.clip(mono * 32767.0, -32768, 32767).astype(np.int16)
    buf = io.BytesIO()
    with av.open(buf, mode="w", format="webm") as out:
        stream = out.add_stream("libopus", rate=48000)
        stream.layout = "mono"
        frame = av.AudioFrame.from_ndarray(pcm16.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate = sr
        for packet in stream.encode(frame):
            out.mux(packet)
        for packet in stream.encode(None):
            out.mux(packet)
    return buf.getvalue()


def norm(text: str) -> str:
    return " ".join(text.lower().replace(".", " ").split())


print("=== the optional set is importable here ===")
if not available():
    print("  SKIP: the speech-to-text set is not installed")
    print("        install it with: pip install -r requirements-stt.txt")
    sys.exit(0)
check("available() is true once the packages are present", available() is True)

print("\n=== config defaults ===")
cfg = config.load()
check("dictation is enabled by default", cfg.stt.enabled is True, str(cfg.stt.enabled))
check("voice capture defaults to automatic", cfg.voice_input.engine == "auto",
      cfg.voice_input.engine)
check("the default model is a faster-whisper repo",
      cfg.stt.model_id.startswith("Systran/faster-whisper"), cfg.stt.model_id)

print("\n=== decoding ===")
phrase = "Turn off the kitchen lights."
wav = tts.wav_bytes(phrase, use_cache=False)
webm = to_webm_opus(wav)

pcm = decode_audio(wav)
check("WAV decodes to mono float32", pcm.dtype == np.float32 and pcm.ndim == 1, str(pcm.dtype))
# Compare against the source's own length, not a fixed number: the TTS output is
# not byte-identical between runs, so a hardcoded duration makes this flaky.
info = sf.info(io.BytesIO(wav))
want = info.frames / info.samplerate * SAMPLE_RATE
check("WAV decodes at 16 kHz", abs(len(pcm) - want) < SAMPLE_RATE * 0.05,
      f"{len(pcm)} samples vs {want:.0f} expected ({info.samplerate} Hz source)")

pcm2 = decode_audio(webm)
# Opus is lossy and the encoder pads, so allow a few milliseconds of slack. The
# real check is that it decodes at all -- libsndfile refuses this container.
check("Opus/WebM decodes (libsndfile cannot read it)",
      pcm2.dtype == np.float32 and abs(len(pcm2) - len(pcm)) < 1600,
      f"{len(pcm2)} vs {len(pcm)} samples")

print("\n=== transcription is exact, from both containers ===")
for label, blob in (("WAV", wav), ("Opus/WebM", webm)):
    result = stt.transcribe(blob)
    check(f"{label}: transcript matches the spoken line",
          norm(result["text"]) == norm(phrase), repr(result["text"]))
    check(f"{label}: reports the language", result["language"] == "en", str(result["language"]))
    check(f"{label}: reports a duration", result["duration_s"] > 0, str(result["duration_s"]))

print("\n=== a second phrase, to catch a lucky match ===")
phrase2 = "What is the temperature in the living room?"
result = stt.transcribe(to_webm_opus(tts.wav_bytes(phrase2, use_cache=False)))
check("a different line transcribes exactly", norm(result["text"]) == norm(phrase2),
      repr(result["text"]))

print("\n=== guards ===")
for label, blob, needle in (
    ("empty payload", b"", "no audio"),
    ("non-audio payload", b"definitely not audio", "decode"),
):
    try:
        stt.transcribe(blob)
        check(f"{label} is refused", False, "no error raised")
    except STTError as exc:
        check(f"{label} is refused", needle in str(exc).lower(), str(exc)[:70])

try:
    stt.transcribe(wav[: len(wav) // 40])  # ~65 ms of audio
    check("a too-short clip is refused", False, "no error raised")
except STTError as exc:
    check("a too-short clip is refused", "short" in str(exc).lower(), str(exc)[:70])

print("\n=== status ===")
st = stt.status
check("status names the loaded model", st.get("model_id") == cfg.stt.model_id, str(st.get("model_id")))
check("status reports a device", bool(st.get("device")), str(st.get("device")))
check("status reports 16 kHz", st.get("sample_rate") == 16000, str(st.get("sample_rate")))

print()
if FAILS:
    print(f"{len(FAILS)} FAILURE(S)")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print("ALL PASS")

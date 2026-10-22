"""STT package: faster-whisper-backed transcription."""
from .engine import SAMPLE_RATE, STTEngine, STTError, available, decode_audio, engine

__all__ = ["engine", "STTEngine", "STTError", "decode_audio", "available", "SAMPLE_RATE"]

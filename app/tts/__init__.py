"""TTS package: KittenTTS-backed synthesis."""

from .engine import BASE_VOICES, EMOTION_TAGS, TTSEngine, TTSError, engine

__all__ = ["engine", "TTSEngine", "TTSError", "BASE_VOICES", "EMOTION_TAGS"]

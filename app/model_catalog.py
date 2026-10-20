"""Model catalog: what can be downloaded, and what it needs to run well.

Each entry declares its own requirements so the recommender can stay declarative
rather than growing an if-tree per model. ``kind`` groups entries in the admin UI.
"""

from __future__ import annotations

from typing import Any, Literal

Kind = Literal["tts", "llm", "stt", "embedding"]

#: KittenTTS repositories, keyed by the tier name used across the app.
KITTEN_TTS_MODELS: dict[str, dict[str, Any]] = {
    "nano": {
        "tier": "nano",
        "kind": "tts",
        "name": "KittenTTS Nano 0.8",
        "repo": "KittenML/kitten-tts-nano-0.8-int8",
        "params": "15M",
        "size_mb": 25,
        "backend": "onnx",
        "voices": 8,
        "requires": {"ram_gb": 2, "cores": 1, "torch": False, "gpu": False},
        "note": "Smallest and fastest. Robotic edge; fine for short confirmations.",
    },
    "micro": {
        "tier": "micro",
        "kind": "tts",
        "name": "KittenTTS Micro 0.8",
        "repo": "KittenML/kitten-tts-micro-0.8",
        "params": "40M",
        "size_mb": 41,
        "backend": "onnx",
        "voices": 8,
        "requires": {"ram_gb": 3, "cores": 2, "torch": False, "gpu": False},
        "note": "Good balance of footprint and clarity on weak hardware.",
    },
    "mini": {
        "tier": "mini",
        "kind": "tts",
        "name": "KittenTTS Mini 0.8",
        "repo": "KittenML/kitten-tts-mini-0.8",
        "params": "80M",
        "size_mb": 80,
        "backend": "onnx",
        "voices": 8,
        "requires": {"ram_gb": 4, "cores": 2, "torch": False, "gpu": False},
        "note": "Best CPU-only quality. Recommended default for most machines.",
    },
    "full": {
        "tier": "full",
        "kind": "tts",
        "name": "KittenTTS 2 (1.7B)",
        "repo": "KittenML/kitten-tts-2",
        "params": "1.7B",
        "size_mb": 506,
        "backend": "torch",
        "voices": 47,
        "requires": {"ram_gb": 8, "cores": 4, "torch": True, "gpu": False},
        "note": "47 voices, cloning and emotion tags. Needs the torch stack (~2.5 GB). Set tts.backend=vllm on an NVIDIA GPU for ~3-4x faster synthesis.",
    },
}

#: Local LLM options, addressed through an OpenAI-compatible endpoint (Ollama,
#: LM Studio, llama.cpp server). Sizes are approximate download sizes.
LLM_MODELS: list[dict[str, Any]] = [
    {
        "kind": "llm", "name": "Qwen2.5 3B Instruct", "repo": "qwen2.5:3b",
        "params": "3B", "size_mb": 2000, "backend": "ollama",
        "requires": {"ram_gb": 6, "cores": 2, "gpu": False},
        "note": "Fast, decent tool-calling. Good first local model.",
    },
    {
        "kind": "llm", "name": "Llama 3.2 3B Instruct", "repo": "llama3.2:3b",
        "params": "3B", "size_mb": 2000, "backend": "ollama",
        "requires": {"ram_gb": 6, "cores": 2, "gpu": False},
        "note": "Meta's small instruct model; strong conversational tone.",
    },
    {
        "kind": "llm", "name": "Qwen3 8B Instruct", "repo": "qwen3:8b",
        "params": "8B", "size_mb": 5200, "backend": "ollama",
        "requires": {"ram_gb": 12, "cores": 4, "gpu": False},
        "preferred": True,
        "note": "Best all-round small model: native tool calling, optional thinking mode, 256K context.",
    },
    {
        "kind": "llm", "name": "Llama 3.1 8B Instruct", "repo": "llama3.1:8b",
        "params": "8B", "size_mb": 4900, "backend": "ollama",
        "requires": {"ram_gb": 12, "cores": 4, "gpu": False},
        "note": "Reliable tool-calling. Needs ~12 GB free RAM on CPU.",
    },
    {
        "kind": "llm", "name": "Qwen2.5 14B Instruct", "repo": "qwen2.5:14b",
        "params": "14B", "size_mb": 9000, "backend": "ollama",
        "requires": {"ram_gb": 20, "cores": 6, "gpu": False},
        "note": "Noticeably better reasoning; slow without a GPU.",
    },
    {
        "kind": "llm", "name": "Qwen2.5 7B Instruct (GPU)", "repo": "qwen2.5:7b",
        "params": "7B", "size_mb": 4700, "backend": "ollama",
        "requires": {"ram_gb": 10, "cores": 4, "gpu": True, "vram_gb": 8},
        "note": "Best quality/latency ratio when you have 8 GB of VRAM.",
    },
    {
        "kind": "llm", "name": "Qwen2.5 14B Instruct (GPU)", "repo": "qwen2.5:14b-instruct-q4_K_M",
        "params": "14B", "size_mb": 9000, "backend": "ollama",
        "requires": {"ram_gb": 16, "cores": 6, "gpu": True, "vram_gb": 12},
        "note": "Strong agentic reasoning. Comfortable on 12 GB+ VRAM.",
    },
    {
        "kind": "llm", "name": "Qwen3.8 27B (RTX 5070 class)", "repo": "qwen3.8:27b",
        "params": "27B", "size_mb": 18432, "backend": "ollama",
        # Dense 27B at Q4_K_M (18 GB) against a 12 GB card: most layers sit in
        # VRAM and the remainder spills to system RAM, so the system-RAM figure is
        # the working set after offload (~8 GB of weights plus the KV cache), not
        # the 18 GB file. 32 GB is the comfortable floor; 24 GB lets it fit.
        "requires": {"ram_gb": 24, "cores": 8, "gpu": True, "vram_gb": 12, "offload": True},
        "preferred": True,
        "note": "Newest Qwen: vision, tools and thinking. 18 GB of weights, so a 12 GB card offloads part to RAM — expect a few tokens/sec, not hundreds.",
    },
    {
        "kind": "llm", "name": "Qwen3 30B-A3B Instruct (MoE)", "repo": "qwen3:30b-a3b",
        "params": "30B MoE", "size_mb": 18000, "backend": "ollama",
        "requires": {"ram_gb": 24, "cores": 8, "gpu": True, "vram_gb": 16},
        "note": "Mixture-of-experts: near-30B quality at 3B speed. Ideal for a 5070-class card.",
    },
    {
        "kind": "llm", "name": "Llama 3.3 70B Instruct (GPU, heavy)", "repo": "llama3.3:70b-instruct-q4_K_M",
        "params": "70B", "size_mb": 42000, "backend": "ollama",
        "requires": {"ram_gb": 48, "cores": 8, "gpu": True, "vram_gb": 40},
        "note": "Only if you have the VRAM to spare; otherwise keep it in the cloud.",
    },
]

#: Speech-to-text options for a wake-word / dictation pipeline.
STT_MODELS: list[dict[str, Any]] = [
    {
        "kind": "stt", "name": "faster-whisper tiny.en", "repo": "Systran/faster-whisper-tiny.en",
        "params": "39M", "size_mb": 75, "backend": "ctranslate2",
        "requires": {"ram_gb": 2, "cores": 2, "gpu": False},
        "note": "Fast local transcription for short commands.",
    },
    {
        "kind": "stt", "name": "faster-whisper base.en", "repo": "Systran/faster-whisper-base.en",
        "params": "74M", "size_mb": 145, "backend": "ctranslate2",
        "requires": {"ram_gb": 3, "cores": 2, "gpu": False},
        "note": "Better accuracy; still comfortable on a CPU.",
    },
]

ALL_MODELS: list[dict[str, Any]] = [
    *KITTEN_TTS_MODELS.values(),
    *LLM_MODELS,
    *STT_MODELS,
]


def by_repo(repo: str) -> dict[str, Any] | None:
    for entry in ALL_MODELS:
        if entry["repo"] == repo:
            return entry
    return None


def by_tier(tier: str) -> dict[str, Any]:
    return KITTEN_TTS_MODELS.get(tier, KITTEN_TTS_MODELS["mini"])


def tier_of_repo(repo: str) -> str | None:
    """Reverse lookup: the tier name for a TTS repo id, if it is a known tier."""
    for tier, entry in KITTEN_TTS_MODELS.items():
        if entry["repo"] == repo:
            return tier
    return None

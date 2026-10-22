"""Model catalog: what can be downloaded, and what it needs to run well.

Each entry declares its own requirements so the recommender can stay declarative
rather than growing an if-tree per model. ``kind`` groups entries in the admin UI.
"""

from __future__ import annotations

import importlib
import importlib.util
import time
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
        "note": "47 voices, cloning and emotion tags. Needs the PyTorch stack (~2.5 GB).",
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


#: Requirement sets a model can depend on, and the file that installs each. These
#: are the repo's own files, never a package name from a request: the install
#: endpoint takes a set key and nothing else, so it cannot be steered into
#: installing something arbitrary.
REQUIREMENT_SETS: dict[str, dict[str, Any]] = {
    "full": {
        "label": "PyTorch stack",
        "file": "requirements-full.txt",
        "detail": "Adds ~2.5 GB. Required by the 1.7B speech tier.",
    },
    "gpu": {
        "label": "ONNX GPU runtime",
        "file": "requirements-gpu.txt",
        "detail": "Replaces the CPU ONNX runtime so the light tiers can use a GPU.",
    },
    "directml": {
        "label": "ONNX DirectML runtime",
        "file": "requirements-directml.txt",
        "detail": (
            "Windows only. Reaches any DirectX 12 GPU -- NVIDIA, AMD or Intel -- "
            "with no CUDA toolkit, so it works where the CUDA build will not."
        ),
    },
}

#: The import each requirement set exists to provide. Checked by importing, not
#: against a pip list and not by ``find_spec``: a package can be present on disk
#: and still fail to import, which is exactly the state a partial or corrupted
#: install leaves behind (a torch built for one CUDA runtime, say, or a wheel
#: whose shared library was removed). ``find_spec`` reports that as satisfied, so
#: the panel offers no fix and the load then fails with "No module named 'torch'".
_REQUIREMENT_PROBES: dict[str, list[str]] = {
    "full": ["torch", "torchaudio", "transformers", "kittenml"],
    "gpu": ["onnxruntime"],
}

#: Probes are imports, which are not free (torch alone takes ~1s and the torch
#: stack several). The result is cached briefly so a panel refresh does not
#: re-import everything, and invalidated outright after an install.
_PROBE_CACHE: dict[str, tuple[float, list[str], dict[str, str]]] = {}
_PROBE_TTL = 30.0


def invalidate_probe_cache() -> None:
    """Forget cached probe results. Called after an install changes the env.

    ``importlib`` caches directory listings of ``sys.path`` entries, so a package
    installed into ``site-packages`` while this process is running can stay
    invisible to the import system until that cache is dropped. The install flow
    verifies by importing in the same process, so clear both.
    """
    _PROBE_CACHE.clear()
    importlib.invalidate_caches()


def _probe_import(name: str) -> tuple[bool, str]:
    """Try to import ``name``. Returns ``(ok, reason)``.

    Importing is the only honest test: it runs the package's own initialisation,
    which is where a broken binary shows up.
    """
    import importlib  # noqa: PLC0415

    try:
        importlib.import_module(name)
        return True, ""
    except ImportError as exc:
        return False, str(exc) or f"No module named '{name}'"
    except Exception as exc:  # a binary that loads but crashes still counts
        return False, f"{type(exc).__name__}: {exc}"


def _ort_has_cuda() -> bool:
    """True when the installed onnxruntime can actually use a CUDA GPU.

    ``onnxruntime`` and ``onnxruntime-gpu`` share the import name, so an import
    check cannot tell them apart -- it reports the GPU runtime as satisfied while
    only the CPU build is present. The execution providers are the real signal.
    """
    try:
        import onnxruntime as ort  # noqa: PLC0415

        return "CUDAExecutionProvider" in ort.get_available_providers()
    except Exception:
        return False


def _ort_has_directml() -> bool:
    """True when the installed onnxruntime exposes the DirectML provider.

    Same trap as the CUDA check: ``onnxruntime-directml`` imports as
    ``onnxruntime``, so only the provider list distinguishes it.
    """
    try:
        import onnxruntime as ort  # noqa: PLC0415

        return "DmlExecutionProvider" in ort.get_available_providers()
    except Exception:
        return False


def probe_details(key: str) -> dict[str, str]:
    """Why each probe for ``key`` failed, for display. Empty when all pass."""
    _, failures = _probe_set(key)
    return failures


def _probe_set(key: str) -> tuple[list[str], dict[str, str]]:
    """``(missing, reasons)`` for a requirement set, cached briefly.

    ``missing`` lists human-readable items that did not import; ``reasons`` maps
    each to the underlying error so the panel can say "present but broken"
    rather than only "missing".
    """
    if key == "gpu":
        if _ort_has_cuda():
            return [], {}
        try:
            import onnxruntime  # noqa: PLC0415  # noqa: F401

            # Imported, but without the CUDA provider: the CPU build is installed.
            return ["onnxruntime-gpu (CUDA build)"], {
                "onnxruntime-gpu (CUDA build)": (
                    "onnxruntime imports, but exposes no CUDAExecutionProvider -- "
                    "this is the CPU build."
                )
            }
        except Exception as exc:
            return ["onnxruntime"], {"onnxruntime": str(exc) or "not importable"}

    if key == "directml":
        if _ort_has_directml():
            return [], {}
        try:
            import onnxruntime  # noqa: PLC0415  # noqa: F401

            return ["onnxruntime-directml (DirectML build)"], {
                "onnxruntime-directml (DirectML build)": (
                    "onnxruntime imports, but exposes no DmlExecutionProvider -- "
                    "this is the CPU or CUDA build."
                )
            }
        except Exception as exc:
            return ["onnxruntime"], {"onnxruntime": str(exc) or "not importable"}

    now = time.time()
    cached = _PROBE_CACHE.get(key)
    if cached and now - cached[0] < _PROBE_TTL:
        return list(cached[1]), dict(cached[2])

    missing: list[str] = []
    reasons: dict[str, str] = {}
    for name in _REQUIREMENT_PROBES.get(key, []):
        ok, reason = _probe_import(name)
        if not ok:
            missing.append(name)
            reasons[name] = reason
    _PROBE_CACHE[key] = (now, list(missing), dict(reasons))
    return missing, reasons


def missing_for(key: str) -> list[str]:
    """What a requirement set still lacks, as human-readable items."""
    missing, _ = _probe_set(key)
    return missing


def _is_broken(name: str, reason: str) -> bool:
    """True when the item is installed but fails to import.

    ``find_spec`` succeeding while the import fails is the signature of a partial
    install -- the fix is to reinstall, not to install.
    """
    try:
        spec = importlib.util.find_spec(name)
    except (ImportError, ValueError, AttributeError):
        # A finder that raises (an unreadable sys.path entry, a namespace package
        # conflict) is itself evidence of a broken environment. Treat the item as
        # broken rather than letting the exception escape into the panel.
        return True
    if spec is None:
        return False
    return "No module named" not in reason



def prerequisites(entry: dict[str, Any]) -> dict[str, Any]:
    """The requirement set a catalog entry needs, and whether it is satisfied.

    The 1.7B tier advertises ``requires.torch``, but nothing acted on it: the
    Download button fetched weights and the load then failed at import. Report the
    set and whether its imports resolve so the panel can offer to install it.

    A package that is present but will not import is reported as broken rather
    than missing, because the fix differs: one is installed, the other reinstalled.

    The catalog spells this ``requires``; the scored view from
    :func:`hardware.recommend` renames it to ``requirements`` for the panel. Read
    both, or the scan path reports every tier as needing nothing.
    """
    requires = entry.get("requires") or entry.get("requirements") or {}
    key = "full" if requires.get("torch") else None
    if key is None:
        return {"needed": False, "key": None, "label": "", "detail": "",
                "satisfied": True, "missing": [], "broken": [], "reason": "",
                "command": ""}

    missing, reasons = _probe_set(key)
    spec = REQUIREMENT_SETS[key]
    broken = [m for m in missing if _is_broken(m.split(" ")[0], reasons.get(m, ""))]
    if broken:
        reason = reasons.get(broken[0], "")
        command = f'pip install -r {spec["file"]} --upgrade --force-reinstall'
    else:
        reason = ""
        command = f'pip install -r {spec["file"]}'
    return {
        "needed": True,
        "key": key,
        "label": spec["label"],
        "detail": spec["detail"],
        "satisfied": not missing,
        "missing": missing,
        "broken": broken,
        "reason": reason,
        "command": command,
    }

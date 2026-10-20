"""Local hardware detection and model recommendation.

Reads CPU, memory, disk and GPU through ``psutil`` plus best-effort probes of the
vendor tools (nvidia-smi, rocm-smi, wmic, system_profiler). Every probe is
optional: a machine with none of them still produces a usable profile, it just
reports no GPU.

The result drives the admin panel's "recommended models" list and the default TTS
tier, so the app configures itself to the machine it is actually running on.
"""

from __future__ import annotations

import json
import math
import platform
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from . import config, model_catalog

# --------------------------------------------------------------------------- utils


def _run(cmd: list[str], timeout: float = 6.0) -> str:
    """Run a probe command, returning stdout or an empty string on any failure."""
    exe = shutil.which(cmd[0])
    if not exe:
        return ""
    try:
        proc = subprocess.run(
            [exe, *cmd[1:]],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        return (proc.stdout or "").strip()
    except Exception:
        return ""


def _run_json(cmd: list[str], timeout: float = 6.0) -> Any:
    raw = _run(cmd, timeout)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except Exception:
        # Some tools prepend banner lines; retry from the first brace.
        start = raw.find("{")
        if start == -1:
            return None
        try:
            return json.loads(raw[start:])
        except Exception:
            return None


# --------------------------------------------------------------------------- probes


def _cpu_info() -> dict[str, Any]:
    import psutil

    freq = psutil.cpu_freq()
    brand = ""
    if sys.platform == "win32":
        brand = _run(["wmic", "cpu", "get", "name"]).split("\n")[-1].strip()
    elif sys.platform == "darwin":
        brand = _run(["sysctl", "-n", "machdep.cpu.brand_string"])
    else:
        try:
            for line in Path("/proc/cpuinfo").read_text().splitlines():
                if line.lower().startswith("model name"):
                    brand = line.split(":", 1)[1].strip()
                    break
        except Exception:
            brand = ""

    flags: set[str] = set()
    try:
        for line in Path("/proc/cpuinfo").read_text().splitlines():
            if line.lower().startswith("flags"):
                flags = set(line.split(":", 1)[1].split())
                break
    except Exception:
        pass

    return {
        "brand": brand or platform.processor() or "Unknown CPU",
        "architecture": platform.machine(),
        "physical_cores": psutil.cpu_count(logical=False) or psutil.cpu_count(logical=True),
        "logical_cores": psutil.cpu_count(logical=True) or 1,
        "max_clock_mhz": round(freq.max) if freq and freq.max else None,
        "avx2": "avx2" in flags if flags else None,
    }


def _memory_info() -> dict[str, Any]:
    import psutil

    vm = psutil.virtual_memory()
    swap = psutil.swap_memory()
    return {
        "total_gb": round(vm.total / 1024**3, 2),
        "available_gb": round(vm.available / 1024**3, 2),
        "swap_total_gb": round(swap.total / 1024**3, 2),
    }


def _disk_info() -> dict[str, Any]:
    import psutil

    path = str(config.DATA_DIR if config.DATA_DIR.exists() else Path.home())
    try:
        usage = psutil.disk_usage(path)
        return {
            "path": path,
            "total_gb": round(usage.total / 1024**3, 2),
            "free_gb": round(usage.free / 1024**3, 2),
        }
    except Exception:
        return {"path": path, "total_gb": 0.0, "free_gb": 0.0}


def _nvidia_gpus() -> list[dict[str, Any]]:
    data = _run_json(
        [
            "nvidia-smi",
            "--query-gpu=name,memory.total,driver_version,compute_cap",
            "--format=json",
        ]
    )
    if not data:
        return []
    out = []
    for gpu in data.get("gpus", []):
        mem = gpu.get("memory.total", "0 MiB")
        try:
            vram = float(str(mem).split()[0]) / 1024
        except Exception:
            vram = 0.0
        out.append(
            {
                "vendor": "nvidia",
                "name": gpu.get("name", "NVIDIA GPU").strip(),
                "vram_gb": round(vram, 2),
                "driver": gpu.get("driver_version", ""),
                "compute_capability": gpu.get("compute_cap", ""),
                "cuda": True,
            }
        )
    return out


def _amd_gpus() -> list[dict[str, Any]]:
    data = _run_json(["rocm-smi", "--showproductname", "--showmeminfo", "vram", "--json"])
    if not data:
        return []
    out = []
    for card_id, card in data.items():
        if not isinstance(card, dict):
            continue
        total = card.get("VRAM Total Memory (B)")
        try:
            vram = float(total) / 1024**3
        except Exception:
            vram = 0.0
        out.append(
            {
                "vendor": "amd",
                "name": card.get("Card Series", card_id),
                "vram_gb": round(vram, 2),
                "rocm": True,
            }
        )
    return out


def _apple_gpu() -> list[dict[str, Any]]:
    """Apple silicon shares unified memory, so report a nominal slice."""
    if sys.platform != "darwin" or platform.machine() != "arm64":
        return []
    try:
        import psutil

        total = psutil.virtual_memory().total / 1024**3
    except Exception:
        total = 0.0
    return [
        {
            "vendor": "apple",
            "name": f"Apple Silicon GPU ({platform.machine()})",
            "vram_gb": round(total * 0.66, 2),
            "unified_memory": True,
            "metal": True,
        }
    ]


def _windows_gpus() -> list[dict[str, Any]]:
    if sys.platform != "win32":
        return []
    raw = _run(
        [
            "wmic",
            "path",
            "win32_VideoController",
            "get",
            "name,AdapterRAM",
            "/format:csv",
        ]
    )
    out = []
    for line in raw.splitlines():
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3 or not parts[1] or parts[1] == "Name":
            continue
        name = parts[1]
        try:
            vram = float(parts[-1]) / 1024**3
        except Exception:
            vram = 0.0
        vendor = "nvidia" if "nvidia" in name.lower() else "amd" if "amd" in name.lower() or "radeon" in name.lower() else "intel" if "intel" in name.lower() else "other"
        out.append({"vendor": vendor, "name": name, "vram_gb": round(vram, 2)})
    return out


def _gpus() -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []
    for probe in (_nvidia_gpus, _amd_gpus, _apple_gpu, _windows_gpus):
        try:
            seen.extend(probe())
        except Exception:
            continue
    return seen


def _runtimes() -> dict[str, Any]:
    """Which optional runtimes are importable, so recommendations stay honest."""
    found: dict[str, Any] = {}
    for mod, key in (("onnxruntime", "onnxruntime"), ("torch", "torch"),
                     ("transformers", "transformers"), ("paho.mqtt", "mqtt")):
        try:
            __import__(mod)
            found[key] = True
        except Exception:
            found[key] = False
    found["python"] = sys.version.split()[0]
    found["platform"] = f"{platform.system()} {platform.release()}"
    found["espeak"] = bool(shutil.which("espeak-ng") or shutil.which("espeak"))
    return found


def _accelerators() -> dict[str, Any]:
    """Probe the acceleration the *installed* runtimes can actually use.

    A GPU in the machine is not the same as a GPU the speech engine can reach: the
    ONNX path needs the CUDA build of onnxruntime, and the torch path needs a
    CUDA-enabled torch. This reports what each backend would really get, so the
    admin panel can say "install this" instead of silently falling back to CPU.
    """
    info: dict[str, Any] = {
        "torch": {"installed": False, "cuda": False, "version": None, "cuda_version": None},
        "onnxruntime": {"installed": False, "providers": [], "cuda": False, "version": None},
        "vllm": {"installed": False, "cuda": False, "version": None},
    }

    try:
        import torch  # noqa: PLC0415

        cuda = bool(torch.cuda.is_available())
        info["torch"] = {
            "installed": True,
            "cuda": cuda,
            "version": getattr(torch, "__version__", None),
            "cuda_version": getattr(getattr(torch, "version", None), "cuda", None),
            "device_name": torch.cuda.get_device_name(0) if cuda else None,
            "device_count": torch.cuda.device_count() if cuda else 0,
        }
    except Exception:
        pass

    try:
        import onnxruntime as ort  # noqa: PLC0415

        providers = list(ort.get_available_providers())
        info["onnxruntime"] = {
            "installed": True,
            "providers": providers,
            "cuda": "CUDAExecutionProvider" in providers,
            "rocm": "ROCMExecutionProvider" in providers,
            "version": getattr(ort, "__version__", None),
        }
    except Exception:
        pass

    # vLLM is an optional extra of the TTS package. It accelerates the full
    # tier's language model ~3-4x, but only on an NVIDIA GPU, so report whether
    # it is installed *and* whether it could actually run.
    try:
        import importlib.util  # noqa: PLC0415

        if importlib.util.find_spec("vllm") is not None:
            import importlib.metadata as md  # noqa: PLC0415

            try:
                version = md.version("vllm")
            except Exception:
                version = None
            info["vllm"] = {
                "installed": True,
                "cuda": bool(info["torch"]["cuda"]),
                "version": version,
            }
    except Exception:
        pass

    info["any_gpu_runtime"] = bool(
        info["torch"]["cuda"] or info["onnxruntime"]["cuda"] or info["vllm"]["cuda"]
    )
    return info


# --------------------------------------------------------------------------- scoring


def _caveat_reason(reason: str) -> bool:
    """True when a reason is advice rather than a blocker.

    A reason mentioning "not installed yet" is installable; a spill to RAM is
    something the user can choose to accept. Both still count as a fit.
    """
    return "not installed yet" in reason or "spills to RAM" in reason


def _fits(requires: dict[str, Any], profile: dict[str, Any]) -> tuple[bool, list[str]]:
    """Check one catalog entry against the profile, returning the reasons it fails."""
    reasons: list[str] = []
    cpu = profile["cpu"]
    mem = profile["memory"]
    gpu = profile["gpu"]
    disk = profile["disk"]

    need_ram = requires.get("ram_gb", 0)
    if mem["total_gb"] < need_ram:
        reasons.append(f"needs {need_ram} GB RAM, has {mem['total_gb']} GB")
    elif mem["available_gb"] < need_ram * 0.8:
        reasons.append(f"only {mem['available_gb']} GB RAM currently free (needs ~{need_ram} GB)")

    need_cores = requires.get("cores", 0)
    if cpu["logical_cores"] < need_cores:
        reasons.append(f"needs {need_cores} CPU threads, has {cpu['logical_cores']}")

    size_gb = requires.get("disk_gb", 0) or (requires.get("size_mb", 0) / 1024)
    if size_gb and disk["free_gb"] < size_gb * 1.5:
        reasons.append(f"needs ~{size_gb * 1.5:.1f} GB free disk, has {disk['free_gb']} GB")

    if requires.get("gpu") and not gpu:
        reasons.append("needs a supported GPU, none detected")
    elif requires.get("gpu") and gpu:
        need_vram = requires.get("vram_gb", 0)
        best = max((g["vram_gb"] for g in gpu), default=0.0)
        if need_vram and best < need_vram:
            reasons.append(f"needs {need_vram} GB VRAM, best GPU has {best} GB")
        elif requires.get("offload") and requires.get("size_mb", 0) / 1024 > best:
            # Not a blocker: Ollama keeps what fits in VRAM and runs the rest on
            # the CPU. It is slow, so say so rather than let it read as a fit.
            size_gb = requires["size_mb"] / 1024
            reasons.append(
                f"{size_gb:.0f} GB of weights on {best:g} GB VRAM (spills to RAM)"
            )

    if requires.get("torch") and not profile["runtimes"].get("torch"):
        # Not a blocker -- the launcher can install it -- but worth surfacing.
        reasons.append("requires the PyTorch stack (not installed yet)")

    return (all(_caveat_reason(r) for r in reasons)), reasons


def _params_billions(entry: dict[str, Any]) -> float:
    """Parse the leading number out of a ``params`` label like '8B' or '30B MoE'."""
    raw = str(entry.get("params", "")).strip()
    match = re.match(r"([\d.]+)\s*([BM])", raw, re.I)
    if not match:
        return 0.0
    value = float(match.group(1))
    return value / 1000 if match.group(2).upper() == "M" else value


def _score(entry: dict[str, Any], profile: dict[str, Any], reasons: list[str]) -> int:
    """Rank candidates.

    A model that fits is scored mostly on capability, so a roomy machine is
    pointed at the largest model it can carry rather than the smallest one that
    happens to fit. Memory headroom only subtracts, and only when it is thin.
    """
    hard_fail = [r for r in reasons if not _caveat_reason(r)]
    score = 100 - len(hard_fail) * 40

    mem = profile["memory"]
    need = entry.get("requires", {}).get("ram_gb", 0)
    if need:
        headroom = mem["available_gb"] / max(need, 1)
        if headroom < 1.25:
            # Tight fit: it runs, but the machine will feel it.
            score -= 18
        elif headroom < 1.8:
            score -= 6

    # Capability: prefer more parameters, on a log scale so 70B does not dwarf 8B.
    params = _params_billions(entry)
    if params:
        score += int(30 * (math.log(params + 1) / math.log(71)))

    if entry.get("requires", {}).get("gpu") and profile["gpu"]:
        score += 10
    return score


#: Capability order for the TTS tiers, weakest first.
_TIER_ORDER = ["nano", "micro", "mini", "full"]


def _pick_tts_tier(profile: dict[str, Any], fitting: list[dict[str, Any]]) -> str:
    """Choose the strongest TTS tier the machine can actually run well.

    The ONNX tiers are the default because they need no torch. The 1.7B ``full``
    tier is only worth its ~2.5 GB torch install and much slower CPU pass when
    there is a GPU to carry it -- otherwise it is a downgrade in responsiveness
    for a modest quality gain.
    """
    onnx_tiers = {
        m["tier"] for m in fitting if m.get("tier") in _TIER_ORDER and m["tier"] != "full"
    }
    best_onnx = max(onnx_tiers, key=_TIER_ORDER.index) if onnx_tiers else "nano"

    full_entry = next((m for m in fitting if m.get("tier") == "full"), None)
    has_gpu = bool(profile.get("gpu"))
    # The full tier runs on torch, so recommending it while torch is absent would
    # hand the user a tier the app cannot load -- `_fits` treats the missing
    # stack as a caveat, not a blocker, so it still appears in `fitting`. Only
    # recommend it once the runtime it needs is actually present; the scan
    # reports the install command separately.
    torch_ready = bool(profile.get("runtimes", {}).get("torch"))
    if full_entry and has_gpu and torch_ready:
        return "full"
    return best_onnx


def _pick_device(profile: dict[str, Any], tier: str) -> dict[str, Any]:
    """Decide what ``tts.device`` should be, and whether it can be honoured.

    ``auto`` is the right answer almost always: the backends fall back to CPU on
    their own. This exists so the admin panel can tell the difference between
    "a GPU is present and usable" and "a GPU is present but the installed runtime
    cannot reach it", which is the case that silently costs speed.
    """
    gpu = profile.get("gpu") or []
    acc = profile.get("accelerators") or {}
    torch_cuda = bool((acc.get("torch") or {}).get("cuda"))
    ort_cuda = bool((acc.get("onnxruntime") or {}).get("cuda"))
    # The full tier is the torch path; everything else runs through ONNX.
    usable = torch_cuda if tier == "full" else ort_cuda

    if not gpu:
        return {"device": "cpu", "usable": False, "reason": "no GPU detected; CPU is correct"}
    if usable:
        return {
            "device": "cuda",
            "usable": True,
            "reason": f"GPU detected and the {'torch' if tier == 'full' else 'onnxruntime'} runtime can use it",
        }

    needed = "torch (CUDA build)" if tier == "full" else "onnxruntime-gpu"
    return {
        "device": "auto",
        "usable": False,
        "reason": (
            f"GPU detected, but the installed runtime cannot reach it. "
            f"Install {needed} to use the GPU; 'auto' will fall back to CPU meanwhile."
        ),
    }


def _pick_tts_backend(profile: dict[str, Any], tier: str) -> dict[str, Any]:
    """Decide what ``tts.backend`` should be, and what it would take to improve.

    vLLM accelerates the full tier's language model ~3-4x, which is the
    difference between cloning being impractical and interactive. It is
    GPU-only and a separate install, so this reports the three distinct states:
    already usable, usable-but-not-installed (with the command), and
    unavailable. Mirrors ``_pick_device``.
    """
    vllm = (profile.get("accelerators") or {}).get("vllm") or {}
    gpu = profile.get("gpu") or []
    torch_cuda = bool(((profile.get("accelerators") or {}).get("torch") or {}).get("cuda"))

    # The ONNX tiers have their own backend setting and are already the fast path.
    if tier != "full":
        return {
            "backend": "torch",
            "usable": False,
            "reason": f"the {tier} tier runs on ONNX; vLLM only applies to the full tier",
        }
    if vllm.get("installed") and torch_cuda:
        return {
            "backend": "vllm",
            "usable": True,
            "reason": "vLLM is installed and a CUDA GPU is reachable; ~3-4x faster synthesis",
        }
    if vllm.get("installed") and not torch_cuda:
        return {
            "backend": "torch",
            "usable": False,
            "reason": "vLLM is installed but no CUDA GPU is reachable; staying on torch",
        }
    if gpu and torch_cuda:
        return {
            "backend": "torch",
            "usable": False,
            "install": 'pip install "kittenml[vllm]"',
            "reason": (
                "GPU detected. Installing vLLM would make full-tier synthesis "
                "~3-4x faster, which is what makes voice cloning practical."
            ),
        }
    return {
        "backend": "torch",
        "usable": False,
        "reason": "no CUDA GPU; vLLM would not help, torch is correct",
    }


def recommend(profile: dict[str, Any]) -> dict[str, Any]:
    """Build the recommendation payload the admin panel renders."""
    recommendations: list[dict[str, Any]] = []
    for entry in model_catalog.ALL_MODELS:
        requires = {**entry.get("requires", {}), "size_mb": entry.get("size_mb", 0)}
        fits, reasons = _fits(requires, profile)
        item = {
            **{k: v for k, v in entry.items() if k != "requires"},
            "requirements": entry.get("requires", {}),
            "fits": fits,
            "score": _score(entry, profile, reasons),
            "blockers": [r for r in reasons if not _caveat_reason(r)],
            "caveats": [r for r in reasons if _caveat_reason(r)],
        }
        recommendations.append(item)

    recommendations.sort(key=lambda m: (m["fits"], m["score"]), reverse=True)

    fitting = [m for m in recommendations if m["fits"]]
    tts = [m for m in fitting if m["kind"] == "tts"]
    llm = [m for m in fitting if m["kind"] == "llm"]
    stt = [m for m in fitting if m["kind"] == "stt"]

    # The tier decides the repo, so the two fields can never disagree.
    tier = _pick_tts_tier(profile, tts)
    chosen_tts = model_catalog.by_tier(tier)

    # For the LLM, prefer the highest-scoring fit, but favour GPU models when a
    # GPU is present so a 5070-class card is not left idle. A catalog entry may
    # also mark itself `preferred`, which wins over raw score -- used to steer a
    # class at one model on purpose rather than at whatever scrapes the most
    # points (see the Qwen3.8 / Qwen3 8B entries).
    def best_of(candidates):
        if not candidates:
            return None
        return next((m for m in candidates if m.get("preferred")), candidates[0])

    if profile.get("gpu"):
        gpu_llms = [m for m in llm if m.get("requirements", {}).get("gpu")]
        best_llm = best_of(gpu_llms) or best_of(llm)
    else:
        cpu_llms = [m for m in llm if not m.get("requirements", {}).get("gpu")]
        best_llm = best_of(cpu_llms) or best_of(llm)

    device = _pick_device(profile, tier)
    backend = _pick_tts_backend(profile, tier)

    return {
        "recommended_tts_tier": tier,
        "recommended_tts": chosen_tts["repo"],
        "recommended_tts_name": chosen_tts["name"],
        "recommended_llm": best_llm["repo"] if best_llm else None,
        "recommended_stt": stt[0]["repo"] if stt else None,
        "gpu_present": bool(profile.get("gpu")),
        "torch_installed": bool(profile.get("runtimes", {}).get("torch")),
        "recommended_device": device["device"],
        "device_usable": device["usable"],
        "device_reason": device["reason"],
        "recommended_backend": backend["backend"],
        "backend_usable": backend["usable"],
        "backend_reason": backend["reason"],
        "backend_install": backend.get("install"),
        "accelerators": profile.get("accelerators", {}),
        "models": recommendations,
    }


def scan(persist: bool = True) -> dict[str, Any]:
    """Full hardware scan plus recommendations."""
    profile: dict[str, Any] = {
        "scanned_at": time.time(),
        "hostname": platform.node(),
        "cpu": _cpu_info(),
        "memory": _memory_info(),
        "disk": _disk_info(),
        "gpu": _gpus(),
        "runtimes": _runtimes(),
        "accelerators": _accelerators(),
    }
    profile["recommendations"] = recommend(profile)

    if persist:
        try:
            config.ensure_dirs()
            config.HARDWARE_PATH.write_text(
                json.dumps(profile, indent=2), encoding="utf-8"
            )
        except Exception:
            pass
    return profile


def cached(recompute: bool = True) -> dict[str, Any] | None:
    """The last scan, with recommendations re-derived from the live catalog.

    Only the hardware facts are cached -- CPU, memory, GPU, disk. Recommendations
    are a pure function of those facts and the catalog, so they are recomputed on
    every read; otherwise a catalog edit would stay invisible until the user
    happened to rescan, and the panel would recommend models that are not in it.
    """
    try:
        if config.HARDWARE_PATH.exists():
            profile = json.loads(config.HARDWARE_PATH.read_text(encoding="utf-8"))
            if recompute:
                profile["recommendations"] = recommend(profile)
            return profile
    except Exception:
        pass
    return None


def summary(profile: dict[str, Any]) -> str:
    """One-line description for logs and the UI header."""
    cpu = profile["cpu"]
    gpu = profile["gpu"]
    gpu_txt = f", {gpu[0]['name']} ({gpu[0]['vram_gb']} GB)" if gpu else ", no GPU"
    return (
        f"{cpu['brand']} ({cpu['logical_cores']} threads), "
        f"{profile['memory']['total_gb']} GB RAM{gpu_txt}"
    )

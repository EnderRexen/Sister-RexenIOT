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


def _nvidia_driver_only() -> dict[str, Any] | None:
    """Detect an NVIDIA GPU that is present but that no runtime can reach.

    ``nvidia-smi`` is the normal probe, but it is a user-space tool and is often
    absent from a container even when the host has a GPU and the driver. This
    looks at the kernel's own view instead, so the panel can distinguish "no GPU
    in this machine" from "a GPU is here, but nothing can use it yet" -- two
    situations that need opposite advice.

    Returns ``None`` when there is no evidence of a driver-backed GPU.
    """
    version = None
    try:
        raw = Path("/proc/driver/nvidia/version").read_text(encoding="utf-8", errors="replace")
        match = re.search(r"Kernel Module\s+(\S+)", raw)
        version = match.group(1) if match else raw.strip().splitlines()[0][:80]
    except Exception:
        pass

    # The character devices are created by the driver when it binds a GPU.
    devices = sorted(p.name for p in Path("/dev").glob("nvidia[0-9]*"))

    if not version and not devices:
        return None

    name = "NVIDIA GPU (driver present)"
    try:
        listing = _run(["nvidia-smi", "-L"])
        if listing:
            first = listing.splitlines()[0].strip()
            name = first.split("(")[0].strip() or name
    except Exception:
        pass

    return {
        "vendor": "nvidia",
        "name": name,
        "vram_gb": 0.0,
        "driver": version or "",
        "driver_only": True,
        "devices": devices,
    }


def _gpus() -> list[dict[str, Any]]:
    seen: list[dict[str, Any]] = []
    for probe in (_nvidia_gpus, _amd_gpus, _apple_gpu, _windows_gpus):
        try:
            seen.extend(probe())
        except Exception:
            continue
    # Only fall back to the kernel's view when the normal probes found nothing:
    # a GPU that nvidia-smi reports is already fully described, with real VRAM.
    if not seen:
        try:
            only = _nvidia_driver_only()
            if only:
                seen.append(only)
        except Exception:
            pass
    return seen


def _cuda_index_for(compute_cap: str) -> str:
    """The CUDA wheel index to install torch from.

    ``cu130`` is the current index: it carries torch 2.14.1 and ships sm_120
    kernels, so it covers Blackwell (RTX 50-series) and older cards alike.
    ``cu128`` also has sm_120 but tops out at torch 2.11.0, and ``cu126`` and
    older have no sm_120 kernels at all -- a wheel built from those reports a
    newer CUDA runtime version and then refuses the device, which is a confusing
    way to fail.
    """
    return "cu130"


def _platform_info() -> dict[str, Any]:
    """The host OS, plus whether its *default* torch wheel can reach a GPU.

    This matters because PyPI publishes a CUDA-linked torch for Linux and a
    CPU-only one for Windows and macOS at the same version number. So
    ``pip install torch`` is a GPU install on one platform and not on the others,
    and any advice phrased as "reinstall the PyTorch stack" is a no-op on two of
    the three. Recording the distinction here lets the panel name a wheel index
    instead, which is the only thing that works there.
    """
    is_windows = sys.platform == "win32"
    is_macos = sys.platform == "darwin"
    return {
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "python": platform.python_version(),
        "is_windows": is_windows,
        "is_macos": is_macos,
        "is_linux": not is_windows and not is_macos,
        # PyPI's default torch links CUDA on Linux x86_64 only. Elsewhere the same
        # version installs a CPU-only build, so an index URL is required.
        "pypi_torch_has_cuda": not is_windows and not is_macos,
    }


def light_profile() -> dict[str, Any]:
    """Just the GPU and OS facts, without the expensive probes.

    ``scan()`` takes about five seconds -- it enumerates disks, memory, CPU
    flags and every runtime -- which is fine for the admin panel but too slow to
    run on every launch. The launchers only need to know whether a GPU is
    present and which OS they are on, so they call this instead and feed the
    result to ``torch_gpu_advice`` / ``directml_advice``.
    """
    return {"gpu": _gpus(), "os": _platform_info()}


def nvidia_compute_cap(profile: dict[str, Any]) -> str:
    """The compute capability of the first NVIDIA GPU, or "".

    Needed to tell a Blackwell card (sm_120) from an older one, because the wheel
    index differs and the version tag alone does not reveal it.
    """
    for gpu in profile.get("gpu") or []:
        if (gpu.get("vendor") or "").lower() == "nvidia":
            return str(gpu.get("compute_capability") or "")
    return ""


def torch_gpu_advice(profile: dict[str, Any]) -> dict[str, str]:
    """How to get a GPU-capable torch on *this* platform. ``{}`` when moot.

    Split out from the panel so the plan and the accelerator card give the same
    answer, and so the platform rule lives in one place: a requirements file can
    only deliver a GPU build on Linux, where PyPI's default wheel is CUDA-linked.
    On Windows and macOS the same command reinstalls the CPU-only wheel, so the
    advice has to name the index -- otherwise "reinstall to pick up CUDA" is a
    loop that reports success and changes nothing.
    """
    osinfo = profile.get("os") or _platform_info()
    if osinfo.get("is_macos"):
        # No CUDA on macOS at all; torch uses MPS on Apple Silicon, CPU elsewhere.
        return {}

    sm = nvidia_compute_cap(profile)
    index = _cuda_index_for(sm)
    url = f"https://download.pytorch.org/whl/{index}"
    # `--index-url`, not `--extra-index-url`: PyPI's torch is *newer* than the
    # CUDA index's (2.14.1 against cu128's 2.11.0), so an extra index loses the
    # resolution and silently installs the CPU-only wheel again. Restricting the
    # index makes the CUDA build the only candidate. The cost is that the CUDA
    # index carries only the torch family, so the rest of the stack has to be
    # installed in a second step -- which is why the advice is two commands and
    # not one. ``--upgrade`` keeps a same-version CPU wheel from being left alone.
    command = (
        f"pip install -r requirements-full.txt\n"
        f"pip install torch torchaudio --upgrade --index-url {url}"
    )

    if osinfo.get("is_windows"):
        note = (
            "PyPI's Windows torch wheel is CPU-only, so reinstalling the PyTorch "
            f"stack cannot enable the GPU -- the index must be named. {index} "
            + (
                f"ships sm_120 kernels for this Blackwell card ({sm}) and is the "
                "current index (torch 2.14.1); cu128 also has sm_120 but stops at "
                "2.11.0, and cu126 and older refuse the card outright. Needs NVIDIA "
                "driver 570 or newer."
                if sm.startswith("12.")
                else "matches this card's compute capability."
            )
            + " Run the two commands in order: the second must come last, or the "
            "CPU wheel is reinstalled over it."
        )
    else:
        note = (
            "PyPI's Linux torch wheel is normally CUDA-linked, so the requirements "
            "install alone may already be a GPU build -- this two-step is for when "
            f"it is not. {index} is the current index and matches this GPU."
        )
    return {"index": index, "url": url, "command": command, "note": note}


def directml_advice(profile: dict[str, Any]) -> dict[str, str]:
    """The vendor-neutral GPU path for the ONNX tiers, where it applies.

    ``onnxruntime-directml`` reaches any GPU on Windows -- NVIDIA, AMD or Intel --
    through DirectX 12, with no CUDA toolkit and no per-vendor wheel. That makes
    it the answer for a Windows machine whose GPU torch cannot use, and for AMD
    and Intel cards that onnxruntime-gpu never supports. Linux and macOS have no
    DirectML build, so it is Windows-only advice.
    """
    if not (profile.get("os") or _platform_info()).get("is_windows"):
        return {}
    if not profile.get("gpu"):
        return {}
    return {
        "command": (
            "pip install -r requirements-directml.txt  "
            "# and: pip uninstall -y onnxruntime"
        ),
        "note": (
            "DirectML reaches any Windows GPU (NVIDIA, AMD or Intel) through "
            "DirectX 12 -- no CUDA toolkit, no vendor-specific wheel. The ONNX "
            "tiers (nano / micro / mini) then run on the GPU, and 'auto' picks it "
            "up without a config change. The full 1.7B tier still needs torch."
        ),
    }


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
    # The phonemiser is needed to turn text into phonemes. It can come from the
    # system (espeak-ng on PATH) or from the `espeakng_loader` wheel, which ships
    # a private copy of the binary. Checking only PATH would report a working
    # install as broken -- the wheel is the normal case for this app.
    found["espeak"] = bool(shutil.which("espeak-ng") or shutil.which("espeak"))
    try:
        __import__("espeakng_loader")
        found["espeakng_loader"] = True
    except Exception:
        found["espeakng_loader"] = False
    found["phonemiser"] = bool(found["espeak"] or found["espeakng_loader"])
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
        # ``cuda`` says a GPU is reachable; the device list and a real allocation
        # say it can actually run work. Verification asks for the stronger claim,
        # because a device that enumerates but cannot allocate would otherwise be
        # reported as usable and then fail at synthesis time.
        devices: list[str] = []
        allocatable = False
        if cuda:
            try:
                count = torch.cuda.device_count()
                devices = [torch.cuda.get_device_name(i) for i in range(count)]
                if count:
                    torch.zeros(8, device="cuda")
                    torch.cuda.synchronize()
                    allocatable = True
            except Exception:
                allocatable = False
        info["torch"] = {
            "installed": True,
            "cuda": cuda,
            "version": getattr(torch, "__version__", None),
            "cuda_version": getattr(getattr(torch, "version", None), "cuda", None),
            "device_name": torch.cuda.get_device_name(0) if cuda else None,
            "device_count": torch.cuda.device_count() if cuda else 0,
            "devices": devices,
            "allocatable": allocatable,
        }
    except Exception:
        pass

    try:
        import onnxruntime as ort  # noqa: PLC0415

        providers = list(ort.get_available_providers())
        # get_device() reports the device ORT will actually run on -- "CPU" even
        # when a CUDA provider is listed but no GPU is reachable, which is exactly
        # the case a provider list alone would misreport.
        try:
            device = str(ort.get_device())
        except Exception:
            device = "unknown"
        info["onnxruntime"] = {
            "installed": True,
            "providers": providers,
            "cuda": "CUDAExecutionProvider" in providers,
            "rocm": "ROCMExecutionProvider" in providers,
            "directml": "DmlExecutionProvider" in providers,
            # Any GPU-ish execution provider counts as accelerated; the light tiers
            # only need to know whether ORT can leave the CPU, not by which route.
            "gpu": any(
                p in providers for p in ("CUDAExecutionProvider", "ROCMExecutionProvider",
                                         "DmlExecutionProvider", "TensorrtExecutionProvider")
            ),
            "device": device,
            "version": getattr(ort, "__version__", None),
        }
    except Exception:
        pass

    # The ``onnx`` package is not required to run the light tier (onnxruntime
    # carries its own copy of the format), but its absence is worth reporting
    # because the GPU requirements file pulls it in and a missing one changes
    # which requirements file is the right answer.
    try:
        import importlib.util as _util  # noqa: PLC0415

        info["onnx"] = {"installed": _util.find_spec("onnx") is not None}
    except Exception:
        info["onnx"] = {"installed": False}

    # A driver-backed GPU that neither runtime can reach. Reported separately from
    # the GPU list so "the machine has no GPU" and "the GPU exists but the
    # runtimes cannot use it" stay distinguishable.
    driver_only = [g for g in _gpus() if g.get("driver_only")]
    info["driver_only_gpus"] = driver_only
    info["gpu_seen_but_unusable"] = bool(driver_only)

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
    # The full tier is the torch path, so only CUDA counts there. The ONNX tiers
    # accept any accelerated provider -- CUDA, ROCm or DirectML -- which is what
    # makes a non-NVIDIA Windows card a working GPU path rather than a dead end.
    ort_accel = bool((acc.get("onnxruntime") or {}).get("gpu"))
    usable = torch_cuda if tier == "full" else ort_accel

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


#: Bumped whenever a probe is added or its shape changes. A cached profile with a
#: different version is discarded rather than read, because a reader that expects
#: a key the cache predates reports it as missing -- which looks exactly like a
#: real finding. That has already caused two false readings of this panel.
PROFILE_VERSION = 4


def scan(persist: bool = True) -> dict[str, Any]:
    """Full hardware scan plus recommendations."""
    profile: dict[str, Any] = {
        "profile_version": PROFILE_VERSION,
        "scanned_at": time.time(),
        "hostname": platform.node(),
        "cpu": _cpu_info(),
        "memory": _memory_info(),
        "disk": _disk_info(),
        "gpu": _gpus(),
        "os": _platform_info(),
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


def _step(
    key: str,
    title: str,
    *,
    status: str,
    detail: str,
    actionable: bool = False,
    command: str = "",
    setting: str = "",
    value: Any = None,
) -> dict[str, Any]:
    """One verification line: what was checked, and what it means."""
    return {
        "key": key,
        "title": title,
        "status": status,          # ok | warn | fail | info
        "detail": detail,
        "actionable": actionable,  # whether the user can do something about it
        "command": command,
        "setting": setting,        # the config field this justifies, when any
        "value": value,            # the value the plan would write for it
    }


def plan(profile: dict[str, Any] | None = None, cfg: Any = None) -> dict[str, Any]:
    """Verify the machine, and derive the settings that suit it.

    This is the step between scanning and applying. The scan answers "what is in
    this machine"; the plan answers "what can it actually run, what should be
    changed, and what would you have to install to go faster". Every claim is
    backed by a probe rather than an assumption, and every setting the plan
    proposes is derived from the same fit check that builds the model list, so
    the two cannot disagree.

    Returns ``{"steps": [...], "patch": {...}, "notes": [...]}`` where ``patch``
    is a plain config patch the caller may apply. Nothing is written here.
    """
    cfg = cfg or config.load()
    profile = profile or cached() or scan(persist=False)
    recs = profile.get("recommendations") or recommend(profile)
    acc = profile.get("accelerators") or {}
    runtimes = profile.get("runtimes") or {}
    gpus = profile.get("gpu") or []
    mem = profile.get("memory") or {}
    disk = profile.get("disk") or {}
    cpu = profile.get("cpu") or {}

    torch_info = acc.get("torch") or {}
    ort_info = acc.get("onnxruntime") or {}
    vllm_info = acc.get("vllm") or {}
    steps: list[dict[str, Any]] = []
    notes: list[str] = []

    real_gpus = [g for g in gpus if not g.get("driver_only")]
    has_gpu = bool(real_gpus)

    # ---------------------------------------------------------------- processor
    steps.append(_step(
        "cpu", "Processor",
        status="ok",
        detail=(
            f"{cpu.get('brand', 'unknown')} — {cpu.get('physical_cores', '?')} cores / "
            f"{cpu.get('logical_cores', '?')} threads"
        ),
    ))

    # ------------------------------------------------------------------- memory
    total = mem.get("total_gb", 0)
    avail = mem.get("available_gb", 0)
    steps.append(_step(
        "memory", "Memory",
        status="ok" if total >= 16 else "warn",
        detail=(
            f"{total} GB total, {avail} GB free"
            + ("" if total >= 16 else " — the full speech tier wants 16 GB or more")
        ),
    ))

    # -------------------------------------------------------------------- disk
    free = disk.get("free_gb", 0)
    steps.append(_step(
        "disk", "Free disk",
        status="ok" if free >= 8 else "warn",
        detail=f"{free} GB free — model weights land in data/models",
    ))

    # --------------------------------------------------------------------- GPU
    if has_gpu:
        names = ", ".join(f"{g.get('name')} ({g.get('vram_gb')} GB)" for g in real_gpus)
        steps.append(_step(
            "gpu", "Graphics hardware",
            status="ok",
            detail=f"{names} — detected via {', '.join(sorted({g.get('vendor', '?') for g in real_gpus}))}",
        ))
    elif acc.get("gpu_seen_but_unusable"):
        steps.append(_step(
            "gpu", "Graphics hardware",
            status="warn",
            actionable=True,
            detail=(
                "An NVIDIA driver is present but no runtime can reach the GPU, so "
                "everything will run on the CPU. Re-run this app with the GPU passed "
                "through to the container (--gpus all), or install the driver on the host."
            ),
        ))
    else:
        steps.append(_step(
            "gpu", "Graphics hardware",
            status="info",
            detail="No GPU detected — the CPU tiers are the right choice and will work.",
        ))

    # ------------------------------------------------------- ONNX runtime state
    if not ort_info.get("installed"):
        steps.append(_step(
            "onnxruntime", "ONNX runtime",
            status="warn", actionable=True,
            command="pip install -r requirements.txt",
            detail="Not installed — the light tiers (nano / micro / mini) need it.",
        ))
    elif ort_info.get("cuda"):
        steps.append(_step(
            "onnxruntime", "ONNX runtime",
            status="ok",
            detail=(
                f"{ort_info.get('version')} with CUDAExecutionProvider — "
                f"the light tiers can use the GPU (running on {ort_info.get('device')})"
            ),
        ))
    else:
        providers = ", ".join(ort_info.get("providers") or []) or "none"
        detail = f"{ort_info.get('version')} — CPU only (providers: {providers})"
        # A GPU is present but the ONNX runtime cannot reach it. Which install
        # fixes that depends on the vendor and the OS: onnxruntime-gpu needs CUDA,
        # so it only helps an NVIDIA card, while DirectML reaches any Windows GPU.
        dml = directml_advice(profile) if has_gpu else {}
        gpu_command = "pip install -r requirements-gpu.txt"
        if has_gpu:
            nvidia = any(
                (g.get("vendor") or "").lower() == "nvidia" for g in real_gpus
            )
            if dml and not nvidia:
                detail += (
                    ". requirements-gpu.txt needs an NVIDIA card; this GPU is not "
                    "one, so requirements-directml.txt is the route that works."
                )
                gpu_command = dml["command"]
            else:
                detail += ". Install requirements-gpu.txt to let the light tiers use the GPU."
                if dml:
                    detail += " (requirements-directml.txt also works, and needs no CUDA.)"
        steps.append(_step(
            "onnxruntime", "ONNX runtime",
            status="warn" if has_gpu else "ok",
            actionable=has_gpu,
            command=gpu_command if has_gpu else "",
            detail=detail,
        ))

    # ------------------------------------------------------ torch / full tier
    if torch_info.get("installed"):
        if torch_info.get("allocatable"):
            steps.append(_step(
                "torch", "PyTorch (full speech tier)",
                status="ok",
                detail=(
                    f"{torch_info.get('version')} — CUDA works and a test tensor "
                    f"allocated on {torch_info.get('device_name')}"
                ),
            ))
        elif torch_info.get("cuda"):
            steps.append(_step(
                "torch", "PyTorch (full speech tier)",
                status="warn", actionable=True,
                command="python -c \"import torch; torch.zeros(1, device='cuda')\"",
                detail=(
                    f"{torch_info.get('version')} is a CUDA {torch_info.get('cuda_version')} "
                    "build and reports a GPU, but a test allocation failed — the driver and "
                    "the CUDA build probably disagree on version."
                ),
            ))
        else:
            detail = f"{torch_info.get('version')} — CPU only"
            advice = torch_gpu_advice(profile) if has_gpu else {}
            if torch_info.get("cuda_version"):
                # The confusing case: a CUDA build with nothing to talk to. Say so
                # plainly, or the reader assumes the GPU is broken.
                detail += (
                    f" (a CUDA {torch_info['cuda_version']} build, but no GPU is "
                    "reachable from this process)"
                )
            elif has_gpu and not advice:
                # macOS: there is no CUDA build to install, so a reinstall is not
                # the fix and offering one would send the reader in a circle.
                detail += ". Apple GPUs use MPS, not CUDA; the ONNX tiers are the GPU path here."
            # On Windows and macOS the requirements file reinstalls the same
            # CPU-only wheel, so it is only the right command where PyPI's default
            # torch is CUDA-linked (Linux).
            command = (
                advice.get("command", "pip install -r requirements-full.txt --upgrade --force-reinstall")
                if has_gpu and advice
                else "pip install -r requirements-full.txt --upgrade --force-reinstall"
            )
            steps.append(_step(
                "torch", "PyTorch (full speech tier)",
                status="info" if has_gpu else "ok",
                actionable=has_gpu,
                command=command if has_gpu else "",
                detail=detail,
            ))
    else:
        advice = torch_gpu_advice(profile) if has_gpu else {}
        detail = "Not installed — the full 1.7B tier needs it."
        if not has_gpu:
            detail += " Only worth it with a GPU."
        elif advice:
            # Name the index up front on platforms where the default wheel is
            # CPU-only; otherwise the plain install silently lands CPU-only and the
            # next scan reports the same problem.
            detail += (
                " On this platform install it from the CUDA wheel index, or the "
                "default wheel will be CPU-only."
            )
        steps.append(_step(
            "torch", "PyTorch (full speech tier)",
            status="info" if has_gpu else "ok",
            actionable=True,
            command=advice.get("command", "pip install -r requirements-full.txt") if advice
            else "pip install -r requirements-full.txt",
            detail=detail,
        ))

    # ------------------------------------------------------------------- vLLM
    if vllm_info.get("installed") and torch_info.get("allocatable"):
        steps.append(_step(
            "vllm", "vLLM (full tier acceleration)",
            status="ok",
            detail=f"{vllm_info.get('version') or 'installed'} — ~3-4x faster synthesis, and usable",
        ))
    elif has_gpu and torch_info.get("allocatable"):
        steps.append(_step(
            "vllm", "vLLM (full tier acceleration)",
            status="info", actionable=True,
            command='pip install "kittenml[vllm]"',
            detail="A usable GPU is present. Installing vLLM makes full-tier synthesis ~3-4x faster.",
        ))
    else:
        steps.append(_step(
            "vllm", "vLLM (full tier acceleration)",
            status="info",
            detail="Needs a reachable NVIDIA GPU; without one it would not help.",
        ))

    # ------------------------------------------------------------------- audio
    # Read the legacy `espeak` flag as a fallback so a profile written before the
    # bundled wheel was recognised is not read as "no phonemiser" -- absent must
    # never be mistaken for a real finding.
    phonemiser = runtimes.get("phonemiser")
    if phonemiser is None:
        phonemiser = bool(runtimes.get("espeak"))
    if phonemiser:
        source = "espeak-ng on PATH" if runtimes.get("espeak") else "bundled with espeakng_loader"
        steps.append(_step("espeak", "Phonemiser (espeak-ng)", status="ok",
                           detail=f"Available ({source}) — required by the speech engine."))
    else:
        steps.append(_step(
            "espeak", "Phonemiser (espeak-ng)",
            status="warn", actionable=True,
            command="pip install espeakng_loader   # or: apt install espeak-ng",
            detail="Neither espeak-ng nor the espeakng_loader wheel is present; the speech engine needs one.",
        ))

    # ---------------------------------------------------------- chosen settings
    tier = recs.get("recommended_tts_tier", "mini")
    device = recs.get("recommended_device", "auto")
    backend = recs.get("recommended_backend", "torch")

    # Only propose a device when it changes behaviour. When no usable GPU is
    # present the recommendation is "cpu", but "auto" already resolves to CPU --
    # so rewriting it would be a no-op that still counts as a config change, and
    # that has a real cost: the shipped "balanced" profile pins "auto", so a
    # needless switch to "cpu" would drop it out of effect and leave no profile
    # shown as active. So keep "cpu" only if it is already set; otherwise settle
    # on "auto", which runs on CPU now and picks up a GPU once the runtime is
    # fixed, without a second config change.
    current_device = (cfg.tts.device or "auto") if hasattr(cfg, "tts") else "auto"
    if device == "cpu" and current_device != "cpu":
        device = "auto"

    # The plan and the model list must agree, so take the model the tier implies
    # from the catalog rather than trusting a second copy of the mapping.
    chosen = model_catalog.by_tier(tier)
    patch: dict[str, Any] = {
        "tts": {
            "tier": tier,
            "model_id": recs.get("recommended_tts") or chosen["repo"],
            "device": device,
            "backend": backend,
            "preset": "expressive" if tier == "full" else "stable",
        }
    }

    # An LLM is only suggested when none is configured: overwriting a model the
    # operator chose deliberately would be a surprise, and the recommendation is
    # a hardware fit, not a judgement about their preference.
    current_llm = getattr(cfg, "llm", None)
    if current_llm is not None and not (current_llm.model or "").strip() and recs.get("recommended_llm"):
        patch["llm"] = {
            "model": recs["recommended_llm"],
            # "ollama" is the value hardware_apply uses and the one the local
            # runtime speaks; the literal set does not accept "openai_compatible".
            "provider": "ollama",
            "base_url": current_llm.base_url or "http://localhost:11434",
        }
        notes.append(f"No language model is configured; suggesting {recs['recommended_llm']}.")

    changed = _changed_settings(cfg, patch)
    steps.append(_step(
        "settings", "Settings to apply",
        status="ok" if changed else "info",
        detail=(
            f"{tier} speech tier on {device}, backend {backend}"
            + (f" — {len(changed)} setting(s) differ from the current config" if changed
               else " — already matches the current config, nothing to change")
        ),
        setting="tts.tier",
        value=tier,
    ))

    # A setting the user has pinned deliberately is worth flagging before it is
    # overwritten, so an explicit `cuda` on a GPU-less box does not silently
    # revert without explanation.
    if current_llm is not None:
        requested_device = (cfg.tts.device or "auto")
        if requested_device != device:
            notes.append(
                f"tts.device is '{requested_device}' but this machine suits '{device}'."
            )

    return {
        "steps": steps,
        "patch": patch,
        "notes": notes,
        "changed": changed,
        "tier": tier,
        "device": device,
        "backend": backend,
        "verified": False,
        "summary": summary(profile),
    }


def _changed_settings(cfg: Any, patch: dict[str, Any]) -> list[str]:
    """Which dotted settings the patch would actually change.

    Reported as dotted paths so the panel can show the difference without
    reimplementing the comparison, and so an empty list is a real "no change"
    rather than a guess.
    """
    changed: list[str] = []
    for section, values in patch.items():
        current = getattr(cfg, section, None)
        if current is None:
            continue
        live = current.model_dump()
        for key, value in values.items():
            if live.get(key) != value:
                changed.append(f"{section}.{key}")
    return changed


def cached(recompute: bool = True) -> dict[str, Any] | None:
    """The last scan, with recommendations re-derived from the live catalog.

    Only the hardware facts are cached -- CPU, memory, GPU, disk. Recommendations
    are a pure function of those facts and the catalog, so they are recomputed on
    every read; otherwise a catalog edit would stay invisible until the user
    happened to rescan, and the panel would recommend models that are not in it.

    A cache written by an older version of this module is treated as absent: the
    caller rescans instead. Without that, a reader asking for a key the cache
    predates would report it missing and the panel would show a false finding.
    """
    try:
        if config.HARDWARE_PATH.exists():
            profile = json.loads(config.HARDWARE_PATH.read_text(encoding="utf-8"))
            if profile.get("profile_version") != PROFILE_VERSION:
                return None
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

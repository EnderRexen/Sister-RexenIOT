"""Verify the scan recommends vLLM only when it would actually help.

The requirement is that a machine with an NVIDIA GPU is told about vLLM
automatically. The failure modes worth guarding are the ones that would make the
advice wrong or noisy:

- suggesting vLLM with no GPU (it cannot run, so it is noise)
- suggesting it when it is already installed (nothing left to do)
- suggesting it on the ONNX tiers, which do not use it at all
- recommending `vllm` as the backend when it cannot run, which would silently
  cost speed instead of gaining it

Run: PYTHONPATH=. ./.venv/bin/python scripts/test_recommend_backend.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import hardware  # noqa: E402

failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


def prof(
    ram_gb: float = 32,
    vram_gb: float = 12,
    gpu: bool = True,
    vllm_installed: bool = False,
    torch_cuda: bool | None = None,
    torch_installed: bool = True,
) -> dict:
    """A synthetic profile. ``torch_cuda=None`` follows ``gpu``."""
    cuda = gpu if torch_cuda is None else torch_cuda
    return {
        "cpu": {"logical_cores": 16, "physical_cores": 8, "model": "sim"},
        "memory": {"total_gb": ram_gb, "available_gb": ram_gb * 0.7, "used_gb": ram_gb * 0.3},
        "disk": {"free_gb": 900, "total_gb": 2000},
        "gpu": [{"name": "NVIDIA RTX 5070", "vram_gb": vram_gb, "vendor": "nvidia"}] if gpu else [],
        "runtimes": {"torch": torch_installed, "onnxruntime": True},
        "accelerators": {
            "torch": {"installed": torch_installed, "cuda": cuda},
            "onnxruntime": {"cuda": False},
            "vllm": {"installed": vllm_installed, "cuda": bool(cuda) and vllm_installed},
        },
    }


print("target box: RTX 5070 + 32 GB, vLLM not yet installed")
rec = hardware.recommend(prof())
check(rec["recommended_tts_tier"] == "full", "the GPU box gets the full tier")
check(rec["recommended_backend"] == "torch", "backend stays torch until vLLM is installed")
check(rec["backend_usable"] is False, "backend is not yet usable")
check(bool(rec.get("backend_install")), "an install command is offered")
check("vllm" in (rec.get("backend_install") or ""), "the install command names vLLM")
check("3-4x" in rec["backend_reason"], "the reason states the speedup")

print("\nthe same box with vLLM installed")
rec = hardware.recommend(prof(vllm_installed=True))
check(rec["recommended_backend"] == "vllm", "vLLM is recommended once installed")
check(rec["backend_usable"] is True, "and reported usable")
check(not rec.get("backend_install"), "no install command once installed")

print("\nno GPU: vLLM is not suggested")
rec = hardware.recommend(prof(gpu=False))
check(rec["recommended_tts_tier"] != "full", "a GPU-less box stays off the full tier")
check(rec["recommended_backend"] == "torch", "backend is torch")
check(not rec.get("backend_install"), "no install command with no GPU to run it on")

print("\nGPU present but torch cannot reach it")
rec = hardware.recommend(prof(torch_cuda=False))
check(rec["recommended_backend"] == "torch", "backend stays torch")
check(not rec.get("backend_install"), "vLLM is not suggested when torch cannot see the GPU")

print("\nvLLM installed but no usable CUDA")
rec = hardware.recommend(prof(vllm_installed=True, torch_cuda=False))
check(rec["recommended_backend"] == "torch", "backend falls back to torch")
check(rec["backend_usable"] is False, "and is not reported usable")
check("no CUDA" in rec["backend_reason"], "the reason explains why")

print("\nrecommended backend is always a value the config accepts")
from app.config import AppConfig  # noqa: E402

allowed = set(AppConfig.model_fields["tts"].annotation.model_fields["backend"].annotation.__args__)
for label, p in [
    ("gpu", prof()),
    ("gpu+vllm", prof(vllm_installed=True)),
    ("cpu", prof(gpu=False)),
]:
    got = hardware.recommend(p)["recommended_backend"]
    check(got in allowed, f"{label}: {got!r} is a valid tts.backend value")

print("\naccelerator probe reports vLLM")
acc = hardware._accelerators()
check("vllm" in acc, "the accelerator probe includes a vllm key")
check(acc["vllm"].get("installed") is False, "vLLM is correctly reported absent on this box")
check(isinstance(acc["vllm"].get("cuda"), bool), "the vllm entry reports a cuda flag")

print()
if failures:
    print(f"{len(failures)} FAILED")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("all checks passed")

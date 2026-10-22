"""Platform-aware GPU advice, checked against synthetic profiles.

The defect this guards is that the app told every user to "reinstall the PyTorch
stack to pick up CUDA". That is a GPU install only on Linux, where PyPI's default
torch wheel is CUDA-linked. On Windows and macOS the same version on PyPI is
CPU-only, so the command reinstalled the identical CPU wheel, reported success,
and left the scan showing the same problem -- a loop that never converged.

The Windows and macOS branches cannot be reached on a Linux test host by scanning
the real machine, so they are exercised by calling the advice functions with
profiles built here. That is why ``torch_gpu_advice``, ``directml_advice`` and
``accelerator_hints`` take a profile instead of reading the host themselves.

Two smaller traps are covered as well: the Blackwell advice used to name a wheel
index whose newest Windows torch is older than PyPI's, so ``--extra-index-url``
would have lost the resolution to the CPU wheel; and ``onnxruntime-gpu`` cannot
reach a non-NVIDIA card at all, which ``requirements-gpu.txt`` alone never said.
"""
import sys
from pathlib import Path

from app import config, hardware, model_catalog
from app.routers.admin import accelerator_hints

FAILS = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


def profile(*, windows=True, macos=False, vendor="nvidia", sm="12.0",
            torch_installed=True, cuda=False, cuda_version=None,
            ort_gpu=False, tier="full"):
    """A hardware profile shaped like the real one, for a chosen platform."""
    return {
        "os": {"is_windows": windows, "is_macos": macos,
               "is_linux": not windows and not macos},
        "gpu": [{"vendor": vendor,
                 "name": "RTX 5070" if vendor == "nvidia" else "Radeon RX 7900",
                 "vram_gb": 12, "compute_capability": sm}],
        "recommendations": {"recommended_tts_tier": tier},
        "accelerators": {
            "torch": {"installed": torch_installed, "cuda": cuda,
                      "version": "2.14.1", "cuda_version": cuda_version},
            "onnxruntime": {"installed": True, "providers": ["CPUExecutionProvider"],
                            "cuda": False, "rocm": False, "directml": False,
                            "gpu": ort_gpu},
            "vllm": {"installed": False, "cuda": False},
        },
    }


def torch_hint(hints):
    return next(h for h in hints if h["for"].startswith("full tier"))


print("=== the CUDA index is the current one, not a stale branch ===")
index = hardware._cuda_index_for("12.0")
check("Blackwell is served by an sm_120-capable index", index in {"cu128", "cu130"}, index)
check("and it is the current index, so PyPI cannot outrank it on version",
      index == "cu130", index)
check("every compute capability gets an index", hardware._cuda_index_for("8.6") == index)
check("and an unknown one does not crash", hardware._cuda_index_for("") == index)

print("\n=== Windows: the CPU-wheel loop is gone ===")
advice = hardware.torch_gpu_advice(profile())
check("advice is produced for a Windows GPU box", bool(advice), str(advice)[:60])
check("it names a wheel index rather than a requirements file alone",
      "--index-url" in advice.get("command", ""), advice.get("command", ""))
check("the index in the command matches the reported one",
      advice.get("index", "") in advice.get("command", ""), advice.get("command", ""))
check("the note explains why a requirements reinstall cannot work on Windows",
      "CPU-only" in advice.get("note", ""), advice.get("note", "")[:80])
# The ordering matters: `--index-url` restricts resolution to the CUDA index,
# which carries only the torch family, so the rest of the stack must be installed
# first and the CUDA torch last. Reversed, the CPU wheel lands back on top.
lines = [ln for ln in advice["command"].splitlines() if ln.strip()]
check("the command is the requirements install followed by the indexed torch install",
      len(lines) == 2 and "requirements-full.txt" in lines[0] and "--index-url" in lines[1],
      str(lines))
check("the indexed install is last, so it is not overwritten",
      "--index-url" in lines[-1], str(lines))

print("\n=== Windows: the panel hint no longer loops either ===")
hints = accelerator_hints(profile())
th = torch_hint(hints)
check("the torch hint names the index", "--index-url" in th["command"], th["command"])
check("it does not tell the reader to force-reinstall the same CPU wheel",
      "force-reinstall" not in th["command"], th["command"])
check("it explains the platform reason", "CPU-only" in th["note"], th["note"][:80])

print("\n=== a non-NVIDIA Windows GPU is pointed at DirectML ===")
amd = accelerator_hints(profile(vendor="amd", sm="11.0", tier="mini"))
check("DirectML is offered for an AMD card",
      any("requirements-directml.txt" in h["command"] for h in amd), str(amd)[:80])
check("DirectML advice exists only on Windows",
      hardware.directml_advice(profile(windows=False)) == {},
      str(hardware.directml_advice(profile(windows=False))))
check("and only when there is a GPU to accelerate",
      hardware.directml_advice({**profile(), "gpu": []}) == {})

print("\n=== macOS is not offered a CUDA build at all ===")
mac = profile(windows=False, macos=True, vendor="apple", sm="")
check("no CUDA advice for macOS", hardware.torch_gpu_advice(mac) == {},
      str(hardware.torch_gpu_advice(mac)))
check("and no DirectML advice", hardware.directml_advice(mac) == {})
mac_hints = accelerator_hints(mac)
check("so no CUDA hint is rendered on a Mac",
      not any("--index-url" in h["command"] for h in mac_hints), str(mac_hints)[:80])

print("\n=== Linux keeps the requirements-file route, which is correct there ===")
linux = accelerator_hints(profile(windows=False, sm="8.6"))
lh = torch_hint(linux)
check("Linux still gets an actionable command", bool(lh["command"]), lh["command"])
check("Linux advice does not claim the default wheel is CPU-only",
      "CPU-only" not in lh["note"], lh["note"][:80])

print("\n=== a machine with no GPU is given no GPU advice ===")
none = {**profile(), "gpu": []}
check("no hints without a GPU", accelerator_hints(none) == [], str(accelerator_hints(none)))

print("\n=== Blackwell is identified by compute capability, not by name ===")
check("sm_120 is detected from the profile",
      hardware.nvidia_compute_cap(profile(sm="12.0")) == "12.0")
check("a non-NVIDIA GPU reports no compute capability",
      hardware.nvidia_compute_cap(profile(vendor="amd", sm="")) == "")
blackwell = accelerator_hints(profile(cuda=False, cuda_version="13.0"))
check("an unreachable Blackwell card gets the sm_120 explanation",
      any("sm_120" in h["note"] for h in blackwell), str(blackwell)[:80])
check("and that advice names a CUDA 12.8+ index",
      any("cu130" in h["command"] or "cu128" in h["command"] for h in blackwell),
      str(blackwell)[:80])

print("\n=== the DirectML requirement set is judged by provider, not by import ===")
check("the set is registered", "directml" in model_catalog.REQUIREMENT_SETS)
check("it maps to a shipped file",
      (config.ROOT / model_catalog.REQUIREMENT_SETS["directml"]["file"]).is_file())
# onnxruntime and onnxruntime-directml share the import name, so an import check
# would report this set satisfied while the CPU build is installed.
import onnxruntime as ort  # noqa: E402

has_dml = "DmlExecutionProvider" in ort.get_available_providers()
missing = model_catalog.missing_for("directml")
print(f"    this machine: providers={ort.get_available_providers()}")
if has_dml:
    check("a DirectML runtime reports nothing missing", missing == [], str(missing))
else:
    check("a runtime without DirectML is reported as missing",
          bool(missing) and "directml" in missing[0].lower(), str(missing))
check("the requirement file names the package that provides it",
      "onnxruntime-directml" in (config.ROOT / "requirements-directml.txt").read_text())

print("\n=== the platform block the advice relies on is in the profile ===")
real = hardware._platform_info()
check("the host platform is reported", bool(real.get("system")), str(real))
check("the flags are booleans",
      all(isinstance(real[k], bool) for k in ("is_windows", "is_macos", "is_linux")),
      str(real))
check("exactly one of the three platforms is true",
      sum(bool(real[k]) for k in ("is_windows", "is_macos", "is_linux")) == 1, str(real))
check("the CPU-wheel claim is recorded, and is false off Linux",
      real["pypi_torch_has_cuda"] == (not real["is_windows"] and not real["is_macos"]),
      str(real))

print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILURE(S)'}")
for f in FAILS:
    print("  -", f)
sys.exit(1 if FAILS else 0)

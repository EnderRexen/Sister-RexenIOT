"""Check the model list carries the state the panel needs, and the install guard.

Two defects are guarded here:

1. ``/admin/models`` served ``ALL_MODELS`` raw, which has no ``fits`` / ``blockers``
   / ``requirements`` / ``downloaded``. The panel renders from those, so every
   Download button was disabled and no badge ever appeared.
2. The 1.7B tier advertised ``requires.torch`` and nothing acted on it: Download
   fetched weights, the load then failed at import, and the panel reported the
   GPU runtime as satisfied while only the CPU build was installed.
"""
import sys

from app import model_catalog

FAILS = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


print("=== every catalog entry can be judged by the panel ===")
for entry in model_catalog.ALL_MODELS:
    repo = entry["repo"]
    pre = model_catalog.prerequisites(entry)
    ok = (
        isinstance(pre, dict)
        and {"needed", "key", "label", "detail", "satisfied", "missing", "command"} <= set(pre)
        and isinstance(pre["missing"], list)
        and isinstance(pre["satisfied"], bool)
    )
    check(f"{repo} has a complete prerequisites block", ok, str(pre)[:80])

print("\n=== the torch tier declares a prerequisite, the ONNX tiers do not ===")
full = model_catalog.by_repo("KittenML/kitten-tts-2")
pre = model_catalog.prerequisites(full)
check("the 1.7B tier needs a set", pre["needed"] is True, str(pre))
check("and it is the PyTorch stack", pre["key"] == "full", pre["key"])
check("it names the command", "requirements-full.txt" in pre["command"], pre["command"])
check("it does not claim to be satisfied without torch",
      pre["satisfied"] == (not pre["missing"]), f"missing={pre['missing']}")

for tier in ("nano", "micro", "mini"):
    repo = model_catalog.KITTEN_TTS_MODELS[tier]["repo"]
    p = model_catalog.prerequisites(model_catalog.by_repo(repo))
    check(f"{repo} needs nothing extra", p["needed"] is False and p["satisfied"] is True, str(p))

print("\n=== the ONNX GPU set is judged by providers, not by import ===")
# onnxruntime and onnxruntime-gpu share an import name, so `find_spec` reports the
# GPU set as satisfied while only the CPU build is present. That is a false
# "ready" and the exact class of bug this project keeps hitting.
gpu_missing = model_catalog.missing_for("gpu")
import onnxruntime as ort  # noqa: E402

has_cuda = "CUDAExecutionProvider" in ort.get_available_providers()
print(f"    this machine: providers={ort.get_available_providers()}")
if has_cuda:
    check("a CUDA-capable runtime reports nothing missing", gpu_missing == [], str(gpu_missing))
else:
    check("a CPU-only runtime is reported as missing, despite the import resolving",
          gpu_missing and "gpu" in gpu_missing[0], str(gpu_missing))
check("onnxruntime imports, so find_spec alone would have said 'satisfied'",
      __import__("importlib.util", fromlist=["util"]).find_spec("onnxruntime") is not None)

print("\n=== only the repo's own requirement files are installable ===")
# The install endpoint takes a set key, never a package name. These are the only
# keys it can act on, and each maps to a file that ships with the app.
for key in model_catalog.REQUIREMENT_SETS:
    spec = model_catalog.REQUIREMENT_SETS[key]
    check(f"'{key}' maps to a real requirements file", spec["file"].startswith("requirements"),
          spec["file"])
check("there is no key that is a package name",
      all(k in {"full", "gpu", "directml"} for k in model_catalog.REQUIREMENT_SETS),
      str(list(model_catalog.REQUIREMENT_SETS)))

print("\n=== the requirement files exist on disk ===")
from app import config  # noqa: E402

for key, spec in model_catalog.REQUIREMENT_SETS.items():
    check(f"{spec['file']} is present", (config.ROOT / spec["file"]).is_file(),
          str(config.ROOT / spec["file"]))

print("\n=== torch is the thing the 1.7B tier actually needs ===")
check("the full set probes for torch", "torch" in model_catalog._REQUIREMENT_PROBES["full"])
check("and for kittenml, which the engine imports",
      "kittenml" in model_catalog._REQUIREMENT_PROBES["full"])

print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILURE(S)'}")
for f in FAILS:
    print("  -", f)
sys.exit(1 if FAILS else 0)

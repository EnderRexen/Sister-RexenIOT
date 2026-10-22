"""The launcher handoff: what the two entry points must and must not contain.

cmd.exe and bash are not run here -- scripts/check_launchers.py covers the batch
syntax statically, and these checks cover the contract instead. The contract is
that every launcher resolves a Python and then hands off to scripts/bootstrap.py,
and that none of them reimplements a step bootstrap owns.

That second half is the part worth testing. The launchers used to carry private
copies of the pipeline, and the copies drifted: the GPU advice was added to the
main launchers but never reached the separate portable launchers that once
existed, so an install on a machine with a discrete GPU was told nothing about
using it. The drift was
invisible because nothing compared the files.

    PYTHONPATH=. python scripts/test_launcher_contract.py
"""
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

BATCH = ["start.cmd"]
SHELL = ["start.sh"]

FAILS = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


def code_of(name):
    """The executable lines of a launcher, comments removed."""
    text = (ROOT / name).read_text(encoding="utf-8", errors="replace")
    if name.endswith(".cmd"):
        kept = [ln for ln in text.splitlines()
                if not re.match(r"\s*(REM\b|::)", ln, re.I)]
    else:
        kept = [ln for ln in text.splitlines() if not ln.lstrip().startswith("#")]
    return "\n".join(kept)


print("=== every launcher delegates to bootstrap.py ===")
for name in BATCH + SHELL:
    check(f"{name} invokes bootstrap.py", "bootstrap.py" in code_of(name))

print()
print("=== no launcher reimplements a shared step ===")
# Each of these lives in bootstrap.py. A launcher that grows its own copy is how
# the two families diverged before.
FORBIDDEN = [
    (r"pip\s+install", "runs pip"),
    (r"kittenml==", "installs the speech package"),
    (r"--prepare-tts", "warms up speech"),
    (r"requirements-(gpu|directml)", "carries GPU install advice"),
    (r"cu130", "names a CUDA wheel index"),
    (r"app\.main", "starts the server"),
]
for name in BATCH + SHELL:
    code = code_of(name)
    hits = [why for pattern, why in FORBIDDEN if re.search(pattern, code, re.I)]
    check(f"{name} has no private copy", not hits, ", ".join(hits))

print()
print("=== the pipeline still exists, exactly once ===")
bootstrap = (ROOT / "scripts/bootstrap.py").read_text(encoding="utf-8")
for label, pattern in [
    ("dependency install", r"pip\",\s*\"install"),
    ("speech package install", r"kittenml=="),
    ("TTS warm-up", r"--prepare-tts"),
    ("GPU advice via app.hardware", r"torch_gpu_advice"),
    ("DirectML advice via app.hardware", r"directml_advice"),
    ("server start", r"app\.main"),
]:
    check(f"bootstrap.py implements {label}", bool(re.search(pattern, bootstrap)))

print()
print("=== the GPU advice has one owner ===")
hardware = (ROOT / "app/hardware.py").read_text(encoding="utf-8")
for marker in ["requirements-gpu", "requirements-directml", "cu130"]:
    check(f"app/hardware.py defines {marker}", marker in hardware)
check("app/hardware.py exposes light_profile()", "def light_profile" in hardware)

print()
print("=== no launcher hardcodes a step number bootstrap also prints ===")
# The launcher announces step 1 before handing off; bootstrap then owns 1..6.
for name in BATCH + SHELL:
    check(f"{name} prints no [n/6] step", not re.search(r"\[\d/6\]", code_of(name)))

if FAILS:
    print()
    print(f"{len(FAILS)} failed:")
    for f in FAILS:
        print(f"  - {f}")
    sys.exit(1)
print()
print("all launcher contract checks passed")

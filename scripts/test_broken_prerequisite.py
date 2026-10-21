"""A present-but-unimportable package must read as broken, not as missing.

The defect: the prerequisite probe used ``importlib.util.find_spec``, which only
asks whether a package exists on disk. A partial install -- the shape a torch
built for the wrong CUDA runtime leaves behind, or a wheel whose shared library
was removed -- passes that check and fails the import. The panel therefore showed
the full tier as satisfied, offered no fix, and the load then died with
"No module named 'torch'" and nothing to act on.

These checks use a real package on a real ``sys.path``, so the probe is exercised
end to end rather than through a mock. The broken package is built in a temp dir
and removed afterwards.
"""
import importlib
import shutil
import sys
import tempfile
from pathlib import Path

from app import model_catalog

FAILS = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


def _make_package(root: Path, name: str, body: str) -> None:
    pkg = root / name
    pkg.mkdir(parents=True, exist_ok=True)
    (pkg / "__init__.py").write_text(body, encoding="utf-8")


tmp = Path(tempfile.mkdtemp(prefix="rexeniot-probe-"))
sys.path.insert(0, str(tmp))
added_probe = False
try:
    print("=== a healthy package imports, and the probe says so ===")
    _make_package(tmp, "rexeniot_probe_ok", "VALUE = 1\n")
    # A package that appears while the process is running is invisible to the
    # import system until its directory caches are dropped. The app does this
    # after every install, so do it here too.
    model_catalog.invalidate_probe_cache()
    ok, reason = model_catalog._probe_import("rexeniot_probe_ok")
    check("a good package probes as importable", ok is True, reason)
    check("find_spec agrees for a good package",
          importlib.util.find_spec("rexeniot_probe_ok") is not None)

    print("\n=== a package that exists but raises on import is NOT satisfied ===")
    # The exact failure mode: present on disk, raises during initialisation.
    _make_package(
        tmp, "rexeniot_probe_broken",
        'raise ImportError("DLL load failed while importing _C")\n',
    )
    model_catalog.invalidate_probe_cache()
    # find_spec -- the old check -- is happy, which is the bug.
    check("find_spec reports the broken package as present (the trap)",
          importlib.util.find_spec("rexeniot_probe_broken") is not None)
    ok, reason = model_catalog._probe_import("rexeniot_probe_broken")
    check("the import-based probe reports it as failing", ok is False, reason)
    check("and it keeps the underlying reason", "DLL load failed" in reason, reason)

    print("\n=== that package is classified as broken, so the fix is a reinstall ===")
    check("_is_broken is True for present-but-unimportable",
          model_catalog._is_broken("rexeniot_probe_broken", reason) is True, reason)
    check("_is_broken is False for a genuinely absent module",
          model_catalog._is_broken("rexeniot_probe_absent_xyz", "No module named 'x'") is False)

    print("\n=== a broken 'torch' flips the full tier to unsatisfied ===")
    # Point the probe at the broken package by name, so the tier logic runs for
    # real against a torch that cannot import.
    real_probes = model_catalog._REQUIREMENT_PROBES["full"]
    model_catalog._REQUIREMENT_PROBES["full"] = ["rexeniot_probe_broken"]
    model_catalog.invalidate_probe_cache()
    added_probe = True
    try:
        full = model_catalog.by_repo("KittenML/kitten-tts-2")
        pre = model_catalog.prerequisites(full)
        check("the full tier is no longer satisfied", pre["satisfied"] is False, str(pre))
        check("the broken package is named", pre["broken"] == ["rexeniot_probe_broken"], str(pre["broken"]))
        check("the reason is carried through", "DLL load failed" in pre["reason"], pre["reason"])
        check("the command is a reinstall, not a plain install",
              "--force-reinstall" in pre["command"], pre["command"])
        check("missing_for reports it too",
              model_catalog.missing_for("full") == ["rexeniot_probe_broken"])

        print("\n=== an absent package gives the plain install command ===")
        model_catalog._REQUIREMENT_PROBES["full"] = ["rexeniot_probe_absent_xyz"]
        model_catalog.invalidate_probe_cache()
        pre2 = model_catalog.prerequisites(full)
        check("absent -> unsatisfied", pre2["satisfied"] is False, str(pre2))
        check("absent is not listed as broken", pre2["broken"] == [], str(pre2["broken"]))
        check("absent -> plain install command",
              "--force-reinstall" not in pre2["command"], pre2["command"])
    finally:
        model_catalog._REQUIREMENT_PROBES["full"] = real_probes
        model_catalog.invalidate_probe_cache()

    print("\n=== the result is cached, and the cache can be dropped ===")
    model_catalog.invalidate_probe_cache()
    first = model_catalog.missing_for("full")
    second = model_catalog.missing_for("full")
    check("a repeated probe agrees", first == second, f"{first} vs {second}")
    check("the cache holds the result", "full" in model_catalog._PROBE_CACHE)
    model_catalog.invalidate_probe_cache()
    check("invalidate clears it", "full" not in model_catalog._PROBE_CACHE)
    check("a fresh probe after invalidate agrees", model_catalog.missing_for("full") == first)
finally:
    if added_probe:
        model_catalog.invalidate_probe_cache()
    sys.path.remove(str(tmp))
    shutil.rmtree(tmp, ignore_errors=True)
    for mod in [m for m in sys.modules if m.startswith("rexeniot_probe_")]:
        del sys.modules[mod]

print("\n=== both catalog and scored entry shapes are understood ===")
# The catalog spells it `requires`; the scored view from hardware.recommend
# renames it to `requirements`. Reading only one spelling made the scan path
# report the 1.7B tier as needing nothing, so its Download button never offered
# the PyTorch install.
scored_shape = {"repo": "KittenML/kitten-tts-2", "requirements": {"torch": True, "ram_gb": 8}}
catalog_shape = {"repo": "KittenML/kitten-tts-2", "requires": {"torch": True, "ram_gb": 8}}
check("the catalog spelling ('requires') is understood",
      model_catalog.prerequisites(catalog_shape)["needed"] is True)
check("the scored spelling ('requirements') is understood",
      model_catalog.prerequisites(scored_shape)["needed"] is True)
check("both spellings agree on the key",
      model_catalog.prerequisites(catalog_shape)["key"]
      == model_catalog.prerequisites(scored_shape)["key"] == "full")
check("a light tier needs no extra set under either spelling",
      model_catalog.prerequisites({"requirements": {"torch": False}})["needed"] is False)

print("\n=== a failed load names the fix, not just the symptom ===")
# This error is what the panel, the chat turn and the config apply all surface,
# so it has to carry the command rather than a bare "No module named 'torch'".
from app.tts.engine import TTSEngine  # noqa: E402

hint = TTSEngine._load_hint(
    ModuleNotFoundError("No module named 'torch'"), "KittenML/kitten-tts-2"
)
check("a missing torch names the requirements file", "requirements-full.txt" in hint, hint)
check("and keeps the original error", "No module named 'torch'" in hint, hint)

hint_ort = TTSEngine._load_hint(
    ModuleNotFoundError("No module named 'onnxruntime'"), "KittenML/kitten-tts-nano-0.8-int8"
)
check("a missing onnxruntime points at the base requirements",
      "requirements.txt" in hint_ort, hint_ort)
check("and not at the torch stack", "requirements-full" not in hint_ort, hint_ort)

# A broken install can raise something that never names the module, so the tier
# is the only signal left.
hint_broken = TTSEngine._load_hint(ImportError("missing loader"), "KittenML/kitten-tts-2")
check("a broken torch stack is identified from the tier alone",
      "requirements-full.txt" in hint_broken, hint_broken)

# A non-import failure must pass through untouched: install advice there would be
# wrong and send the reader to reinstall a working environment.
plain = TTSEngine._load_hint(RuntimeError("download failed"), "KittenML/kitten-tts-2")
check("a non-import error gets no install advice", "pip install" not in plain, plain)

print("\n=== the real environment still reports the real torch stack ===")
# Guards against the probe being made so strict it fails a working install.
model_catalog.invalidate_probe_cache()
check("torch imports in this environment", model_catalog._probe_import("torch")[0] is True)
check("so the full tier is satisfied here",
      model_catalog.prerequisites(model_catalog.by_repo("KittenML/kitten-tts-2"))["satisfied"] is True)

print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILURE(S)'}")
for f in FAILS:
    print("  -", f)
sys.exit(1 if FAILS else 0)

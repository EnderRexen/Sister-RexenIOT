"""Prove /admin/hardware/apply does not persist a tier it cannot load.

The handler used to call config.update() first and swallow the load error, so a
failed download or a missing runtime was written to the config permanently. The
app would then retry that broken model on every start, with only a log line as
evidence. This asserts the failure path leaves the config exactly as it was.

Run: PYTHONPATH=. .venv/bin/python scripts/test_apply_rollback.py
"""
import asyncio
import json
import shutil
import tempfile
from pathlib import Path

# Redirect the data dir before importing config, so the real config is never
# touched by this test.
_tmp = Path(tempfile.mkdtemp(prefix="rexeniot-apply-test-"))
import app.config as config  # noqa: E402

config.DATA_DIR = _tmp
config.CONFIG_PATH = _tmp / "config.json"

from app import hardware  # noqa: E402
from app.routers import admin  # noqa: E402
from app.tts import engine  # noqa: E402

BROKEN = "KittenML/kitten-tts-does-not-exist"

#: Captured before any patching: ``admin.hardware`` *is* the ``hardware`` module,
#: so assigning to one rebinds the other and a naive fake_scan would recurse.
_real_scan = hardware.scan


def fake_scan(persist: bool = True):
    """A real profile with only the recommendations swapped out.

    Fabricating the whole profile by hand breaks ``hardware.summary``, which
    reads cpu/memory/disk; borrowing a real one keeps the test focused on the
    thing under test.
    """
    profile = _real_scan(False)
    profile["recommendations"] = {
        "recommended_tts_tier": "full",
        "recommended_tts": BROKEN,
        "recommended_llm": None,
        "recommended_device": "auto",
    }
    return profile


def read_cfg():
    return json.loads(config.CONFIG_PATH.read_text()) if config.CONFIG_PATH.exists() else {}


async def main() -> int:
    config.ensure_dirs()
    before = config.load()
    before_dump = before.model_dump()
    before_file = read_cfg()
    print(f"  baseline tts.model_id = {before.tts.model_id}")

    # --- the failure path -----------------------------------------------------
    admin.hardware.scan = fake_scan
    original_ensure = engine.ensure_loaded

    def boom(model_id=None):
        raise RuntimeError("simulated: model cannot be loaded")

    engine.ensure_loaded = boom
    try:
        result = await admin.hardware_apply(x_admin_token=None)
    finally:
        engine.ensure_loaded = original_ensure

    after = config.load()
    after_dump = after.model_dump()
    after_file = read_cfg()

    failures = 0

    ok = bool(result.get("error"))
    failures += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  response carries an error: {result.get('error')!r}")

    ok = result.get("applied") == {}
    failures += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  'applied' is empty, not the rejected patch: {result.get('applied')!r}")

    ok = after_dump == before_dump
    failures += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  live config unchanged (model_id still {after.tts.model_id})")

    ok = after_file == before_file
    failures += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  config file on disk unchanged")

    ok = after.tts.model_id != BROKEN
    failures += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  broken model never written to config")

    # --- the success path still persists -------------------------------------
    admin.hardware.scan = lambda persist=True: {
        **fake_scan(),
        "recommendations": {
            "recommended_tts_tier": "mini",
            "recommended_tts": "KittenML/kitten-tts-mini-0.8",
            "recommended_llm": None,
            "recommended_device": "auto",
        },
    }
    engine.ensure_loaded = lambda model_id=None: None
    try:
        result = await admin.hardware_apply(x_admin_token=None)
    finally:
        engine.ensure_loaded = original_ensure

    ok = not result.get("error") and result.get("applied")
    failures += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  success path still reports an apply")

    ok = config.load().tts.model_id == "KittenML/kitten-tts-mini-0.8"
    failures += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  success path persists the new model_id")

    shutil.rmtree(_tmp, ignore_errors=True)
    return 1 if failures else 0


raise SystemExit(asyncio.run(main()))

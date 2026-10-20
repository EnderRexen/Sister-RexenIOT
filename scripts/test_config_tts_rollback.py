"""Verify PUT /admin/config refuses a TTS model it cannot load.

The endpoint used to persist first and only log a load failure, so a bad model
was written to disk and retried on every start. This pins the load-before-persist
ordering: the request must fail with 400 and leave config untouched.

Run: PYTHONPATH=. ./.venv/bin/python scripts/test_config_tts_rollback.py
"""
from __future__ import annotations

import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# Redirect config storage before the router imports it, so the real
# data/config.json is never touched.
_tmp = tempfile.mkdtemp(prefix="rexeniot-cfgtest-")
from app import config  # noqa: E402

config.DATA_DIR = Path(_tmp)
config.CONFIG_PATH = Path(_tmp) / "config.json"

from app.routers import admin  # noqa: E402
from fastapi import HTTPException  # noqa: E402

GOOD = "KittenML/kitten-tts-mini-0.8"
BAD = "KittenML/kitten-tts-does-not-exist"

checks: list[tuple[str, bool]] = []


def check(label: str, ok: bool) -> None:
    checks.append((label, ok))


async def run() -> None:
    config.update({"tts": {"model_id": GOOD}})
    check("baseline config holds the good model", config.load().tts.model_id == GOOD)

    # Point ensure_loaded at something that always fails, keyed on the model id.
    real_ensure = admin.engine.ensure_loaded

    def fake_ensure(model_id: str | None = None):
        target = model_id or config.load().tts.model_id
        if target == BAD:
            raise RuntimeError("simulated download failure")
        return real_ensure(target)

    admin.engine.ensure_loaded = fake_ensure

    raised = None
    try:
        await admin.put_config(admin.ConfigPatch(patch={"tts": {"model_id": BAD}}), None)
    except HTTPException as exc:
        raised = exc

    check("bad model raises HTTPException", raised is not None)
    check("status is 400", raised is not None and raised.status_code == 400)
    check(
        "error names the model",
        raised is not None and BAD in str(raised.detail),
    )
    check(
        "live config still holds the good model",
        config.load().tts.model_id == GOOD,
    )
    check(
        "disk config still holds the good model",
        "does-not-exist" not in config.CONFIG_PATH.read_text(encoding="utf-8"),
    )

    # And a good model still goes through.
    cfg = await admin.put_config(admin.ConfigPatch(patch={"tts": {"model_id": GOOD}}), None)
    check("good model still saves", cfg["tts"]["model_id"] == GOOD)

    admin.engine.ensure_loaded = real_ensure


asyncio.run(run())

failed = [label for label, ok in checks if not ok]
for label, ok in checks:
    print(f"  {'PASS' if ok else 'FAIL'}  {label}")
print(f"\n{len(checks) - len(failed)}/{len(checks)} passed")
raise SystemExit(1 if failed else 0)

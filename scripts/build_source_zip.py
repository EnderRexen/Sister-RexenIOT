#!/usr/bin/env python3
"""Build the distributable source archive for Sister RexenIOT.

Produces ``web/download/SisterRexenIOT.zip`` so the running app can serve it at
``/download/SisterRexenIOT.zip``.

The archive carries the documentation with the code: ``SETUP.md`` (the step-by-step
guide for a new user) and ``README.md`` (the technical reference) sit at the top
level next to the launchers.

What is deliberately left out:

* ``data/models``, ``data/cache``, ``data/uploads``, ``data/voices`` -- downloaded
  weights and generated audio. Around 2 GB, and reproducible from the admin panel.
* ``data/config.json`` and friends -- the operator's local settings. Shipping them
  would overwrite a fresh user's config and could carry API keys.
* ``.venv``, ``.git``, ``__pycache__``, ``.openhands`` -- machine-local.
* Any ``.zip`` -- so rebuilding does not nest the previous archive inside the new one.

Run from the project root:

    python scripts/build_source_zip.py
"""

from __future__ import annotations

import os
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "web" / "download" / "SisterRexenIOT.zip"

SKIP_DIRS = {".venv", "runtime", ".git", "__pycache__", ".openhands", "node_modules", ".mypy_cache"}

#: Generated state, not source. Prefixes are matched against the relative path.
SKIP_PATHS = (
    "data/models",
    "data/cache",
    "data/uploads",
    "data/voices",
    "data/config.json",
    "data/hardware_profile.json",
    "data/personalities.json",
    "data/voice_profiles.json",
    "data/skills.json",
    "data/agent_memory.json",
    "data/chat_history.jsonl",
    "data/rexeniot.log",
)

SKIP_EXT = {".pyc", ".pyo", ".wav", ".log", ".zip"}

#: Batch files must ship with CRLF or cmd.exe mis-parses them on Windows. The
#: working tree is already CRLF, but a git checkout on a Linux CI box or an
#: editor that normalises line endings can strip it, so the zip re-asserts it.
CRLF_SUFFIXES = {".cmd", ".bat"}

#: The docs must travel with the code. They are picked up by the directory walk
#: anyway, but listing them makes the requirement explicit and lets the build
#: fail loudly if one is ever renamed or deleted.
REQUIRED_DOCS = ("SETUP.md", "README.md")


def main() -> int:
    missing = [d for d in REQUIRED_DOCS if not (ROOT / d).exists()]
    if missing:
        print(f"ERROR: required documentation is missing: {', '.join(missing)}")
        return 1

    OUT.parent.mkdir(parents=True, exist_ok=True)
    # Drop any previous archive so a rename cannot leave a stale copy behind that
    # the static mount would keep serving.
    for old in OUT.parent.glob("*.zip"):
        if old != OUT:
            old.unlink()
            print(f"removed stale archive {old.name}")

    count = 0
    converted = 0
    with zipfile.ZipFile(OUT, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as zf:
        for dirpath, dirnames, filenames in os.walk(ROOT):
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
            for fn in sorted(filenames):
                full = Path(dirpath) / fn
                rel = str(full.relative_to(ROOT)).replace(os.sep, "/")
                suffix = Path(rel).suffix.lower()
                if suffix in SKIP_EXT:
                    continue
                if any(rel == p or rel.startswith(p + "/") for p in SKIP_PATHS):
                    continue
                if suffix in CRLF_SUFFIXES:
                    raw = full.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
                    zf.writestr(rel, raw.replace(b"\n", b"\r\n"))
                    converted += 1
                else:
                    zf.write(full, rel)
                count += 1

    size_kb = OUT.stat().st_size / 1024
    print(f"{count} files -> {OUT.relative_to(ROOT)} ({size_kb:.0f} KB), "
          f"{converted} batch file(s) written as CRLF")
    print(f"documentation included: {', '.join(REQUIRED_DOCS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

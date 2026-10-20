"""First-run check: the model list is usable before any scan has happened.

The live box already had a cached hardware profile, so the panel tests never hit
this. On a genuinely fresh install there is no profile, and `/admin/models` was
serving entries with no `fits` at all -- which disables every Download button.
That is the same symptom as the raw-catalog bug, reachable only on first launch.

Runs the real server against a throwaway REXENIOT_HOME so nothing is touched.
"""
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
FAILS = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def main():
    home = Path(tempfile.mkdtemp(prefix="rexeniot-fresh-"))
    port = free_port()
    env = {**os.environ, "REXENIOT_HOME": str(home), "PATH": os.environ.get("PATH", "")}
    proc = subprocess.Popen(
        [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )
    try:
        base = f"http://127.0.0.1:{port}"
        deadline = time.time() + 240
        ready = False
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"{base}/api/admin/session", timeout=5):
                    ready = True
                    break
            except Exception:
                time.sleep(2)
        check("the fresh server came up", ready, f"port {port}")
        if not ready:
            print(proc.stdout.read().decode()[-1500:] if proc.stdout else "")
            return

        print("\n=== no hardware profile exists yet ===")
        check("data dir is empty of a profile",
              not (home / "data" / "hardware_profile.json").exists(),
              str(home / "data"))

        print("\n=== the catalog is still judgeable on first call ===")
        with urllib.request.urlopen(f"{base}/api/admin/models", timeout=120) as r:
            data = json.loads(r.read())
        catalog = data["catalog"]
        check("a catalog came back", len(catalog) > 5, f"{len(catalog)} entries")
        missing_fits = [m["repo"] for m in catalog if not isinstance(m.get("fits"), bool)]
        check("every entry has a boolean fits on a fresh install", not missing_fits,
              str(missing_fits[:3]))
        missing = [m["repo"] for m in catalog
                   if "blockers" not in m or "downloaded" not in m or "active" not in m]
        check("and blockers, downloaded and active", not missing, str(missing[:3]))
        tts = [m for m in catalog if m["kind"] == "tts"]
        check("tts entries carry prerequisites", all("prerequisites" in m for m in tts))
        check("exactly one tts model is active",
              sum(1 for m in tts if m["active"]) == 1,
              str([m["repo"] for m in tts if m["active"]]))
        # The bug's actual consequence: every Download button disabled.
        downloadable = [m["repo"] for m in tts
                        if m.get("fits") and not (m.get("prerequisites") or {}).get("needed")]
        check("at least one tts model is downloadable (not all disabled)",
              bool(downloadable), f"{len(downloadable)} downloadable")

        print("\n=== the scan was cached by that call, so it is not repeated ===")
        check("a profile now exists", (home / "data" / "hardware_profile.json").exists())
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(home, ignore_errors=True)

    print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILURE(S)'}")
    for f in FAILS:
        print("  -", f)
    sys.exit(1 if FAILS else 0)


main()

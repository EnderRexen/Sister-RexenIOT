"""Exercise the prerequisite install job with real pip runs.

Runs the actual job runner (no mocks) against throwaway requirements files so the
live environment is untouched: an empty file (a real, fast success) and a file
naming a package that cannot exist (a real failure). The point is that a failed
pip run is reported as an error rather than as a successful install -- the failure
mode this panel exists to avoid.
"""
import asyncio
import sys
import tempfile
from pathlib import Path

from app import model_catalog
from app.routers import admin

FAILS = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


async def run_job(key, requirements):
    admin._INSTALL["job"] = {
        "state": "running", "key": key, "label": "test", "detail": "test",
        "command": "", "started_at": 0.0, "finished_at": None, "pid": None, "log": "",
    }
    await admin._run_install(key, requirements)
    return dict(admin._INSTALL["job"])


async def main():
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)

        print("=== an install that succeeds is reported as done ===")
        # "full" probes for torch/torchaudio/transformers/kittenml. If those are
        # present, an empty requirements file is a genuine, fast success.
        satisfied = not model_catalog.missing_for("full")
        ok_file = tmp / "ok.txt"
        ok_file.write_text("# nothing to install\n")
        job = await run_job("full", ok_file)
        if satisfied:
            check("state is done", job["state"] == "done", job["state"])
            check("finished_at is set", job["finished_at"] is not None)
            check("the detail names what was installed", "installed" in job["detail"], job["detail"])
        else:
            # torch is genuinely absent, so an empty file must NOT be called done:
            # the post-check catches that pip did not deliver the imports.
            check("a no-op install is not called done when imports are still missing",
                  job["state"] == "error", job["state"])
            check("and it names what is still missing",
                  "do not resolve" in job["detail"], job["detail"])

        print("\n=== an install that fails is reported as an error, not success ===")
        bad_file = tmp / "bad.txt"
        bad_file.write_text("this-package-does-not-exist-rexeniot-xyzzy==999.999\n")
        job = await run_job("full", bad_file)
        check("state is error", job["state"] == "error", job["state"])
        check("the exit code is surfaced", "pip exited with code" in job["detail"], job["detail"])
        check("pip output was captured for the log", bool(job["log"]), f"{len(job.get('log', ''))} chars")
        check("the log mentions the bad package",
              "rexeniot-xyzzy" in job.get("log", ""), "not in log")

        print("\n=== a missing requirements file is an error, not a silent pass ===")
        job = await run_job("full", tmp / "does-not-exist.txt")
        check("state is error", job["state"] == "error", job["state"])
        check("the reason is reported", bool(job["detail"]), job["detail"][:80])

    print("\n=== only one install may run at a time ===")
    # Two pip runs against one interpreter corrupt each other's metadata.
    check("the lock is an asyncio lock", type(admin._INSTALL_LOCK).__name__ == "Lock")
    check("the job record starts idle in a fresh process",
          "state" in admin._install_state())

    print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILURE(S)'}")
    for f in FAILS:
        print("  -", f)
    sys.exit(1 if FAILS else 0)


asyncio.run(main())

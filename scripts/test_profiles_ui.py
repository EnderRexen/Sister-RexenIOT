"""Verify the voice panel indicates what is in use, and disables what is not.

The requirement: a selected feature must show that it is being used, and controls
that do not apply must show as disabled. For voice profiles that means

- the profile actually in effect carries an "in use" badge
- a profile that is merely selected, not in effect, says so
- Apply is disabled when there is nothing to apply (no selection, invalid, or
  already in effect) and enabled when there is
- Delete is disabled for built-ins and for no selection, enabled for saved ones

``web/js/admin.js`` is a module, so its functions are not on ``window``. This
drives the real controls and reads the real DOM instead.

The server is started here against a throwaway ``REXENIOT_HOME``, so the test
owns the state it asserts on. It previously pointed at whatever server happened
to be listening on 127.0.0.1:12000, which made the result depend on that
server's config: the first assertion needs a profile to be in effect, and a
config hand-edited away from every built-in (e.g. ``tts.tier`` changed but not
``tts.device``) has none. That is a real, correct UI state -- the panel says the
settings match no profile -- but not the one under test.

Run: PYTHONPATH=. ./.venv/bin/python scripts/test_profiles_ui.py
"""

import asyncio
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

from playwright.async_api import async_playwright

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def main() -> None:
    # REXENIOT_HOME moves ROOT, which serves both the data dir *and* the static
    # web assets and the requirements files. So the throwaway home has to look
    # like the app: symlink the read-only assets in and leave only data/ fresh.
    home = Path(tempfile.mkdtemp(prefix="rexeniot-profiles-ui-"))
    (home / "data").mkdir()
    for asset in ["web", *[p.name for p in ROOT.glob("requirements*.txt")]]:
        (home / asset).symlink_to(ROOT / asset)

    port = free_port()
    env = {**os.environ, "REXENIOT_HOME": str(home), "PATH": os.environ.get("PATH", "")}
    proc = subprocess.Popen(
        [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
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
        check(ready, f"the server came up on port {port}")
        if not ready:
            print(proc.stdout.read().decode()[-1500:] if proc.stdout else "")
            return

        # A fresh install lands on the shipped "balanced" profile, which is the
        # precondition for "the profile in effect is shown". Assert that rather
        # than assuming it, so a regression in the defaults is reported as the
        # root cause instead of as three confusing badge failures.
        with urllib.request.urlopen(f"{base}/api/admin/profiles", timeout=60) as r:
            listing = json.loads(r.read())
        check(
            bool(listing.get("active")),
            f"a profile is in effect on a fresh install ({listing.get('active')!r})",
        )

        async with async_playwright() as p:
            b = await p.chromium.launch(
                executable_path="/usr/bin/chromium", args=["--no-sandbox"]
            )
            pg = await b.new_page(viewport={"width": 1400, "height": 1000})
            errs: list[str] = []
            pg.on("pageerror", lambda e: errs.append(str(e)))

            await pg.goto(f"{base}/admin", wait_until="networkidle")
            await pg.click(".tab[data-tab='voice']")
            await pg.wait_for_timeout(2000)

            async def badge_state():
                return await pg.evaluate("""() => ({
                  activeHidden: document.querySelector('#profile-active-badge').hidden,
                  activeText: document.querySelector('#profile-active-badge').textContent.trim(),
                  selectedHidden: document.querySelector('#profile-selected-badge').hidden,
                  selectedText: document.querySelector('#profile-selected-badge').textContent.trim(),
                  applyDisabled: document.querySelector('#btn-profile-apply').disabled,
                  deleteDisabled: document.querySelector('#btn-profile-delete').disabled,
                  selectedValue: document.querySelector('#profile-select').value,
                  options: [...document.querySelectorAll('#profile-select option')].map(o => o.textContent),
                  description: document.querySelector('#profile-description-view').textContent.trim(),
                })""")

            print("on open, the profile in effect is shown")
            s = await badge_state()
            check(bool(s["selectedValue"]), f"a profile is preselected ({s['selectedValue']!r})")
            check(not s["activeHidden"], "the 'in use' badge is visible")
            check(s["activeText"].lower() == "in use", f"it reads 'in use' ({s['activeText']!r})")
            check(s["selectedHidden"], "the 'selected' badge is hidden when the two agree")
            check(s["applyDisabled"], "Apply is disabled: it is already in effect")
            check(s["deleteDisabled"], "Delete is disabled: built-ins cannot be deleted")
            check("in use" in " ".join(s["options"]), "the option list marks the active profile")

            print("\nselecting a different profile enables Apply")
            # Pick a built-in that is not the active one.
            other = await pg.evaluate("""() => {
              const sel = document.querySelector('#profile-select');
              const active = sel.value;
              const opt = [...sel.options].find(o => o.value && o.value !== active);
              sel.value = opt.value;
              sel.dispatchEvent(new Event('change', {bubbles: true}));
              return opt.value;
            }""")
            await pg.wait_for_timeout(300)
            s = await badge_state()
            check(not s["applyDisabled"], f"Apply is enabled for {other!r}")
            check(s["activeHidden"], "the 'in use' badge is hidden for a profile not in effect")
            check(not s["selectedHidden"], "the 'selected' badge is shown instead")
            check(s["selectedText"].lower() == "selected", f"it reads 'selected' ({s['selectedText']!r})")
            check(s["deleteDisabled"], "Delete stays disabled for a built-in")
            check(bool(s["description"]), "the description explains the profile")

            print("\nselecting nothing disables Apply")
            await pg.evaluate("""() => {
              const sel = document.querySelector('#profile-select');
              sel.value = '';
              sel.dispatchEvent(new Event('change', {bubbles: true}));
            }""")
            await pg.wait_for_timeout(300)
            s = await badge_state()
            check(s["applyDisabled"], "Apply is disabled with no profile selected")
            check(s["deleteDisabled"], "Delete is disabled with no profile selected")
            check(s["activeHidden"], "no 'in use' badge with no profile selected")
            check("do not match" in s["description"], "the panel says the settings match no profile")

            print("\na saved profile can be deleted; a built-in cannot")
            # Save the current settings as a custom profile, then check the buttons.
            await pg.fill("#profile-name", "UI test profile")
            await pg.click("#btn-profile-save")
            await pg.wait_for_timeout(2500)
            s = await badge_state()
            saved_ok = await pg.evaluate("""() => {
              const sel = document.querySelector('#profile-select');
              return {value: sel.value, count: sel.options.length};
            }""")
            check(saved_ok["count"] >= 7, f"the saved profile appears ({saved_ok['count']} options)")
            check(saved_ok["value"] not in ("", "balanced", "best-quality", "light-cpu",
                                            "browser-voice", "resilient", "silent"),
                  f"and is selected ({saved_ok['value']!r})")
            check(not s["deleteDisabled"], "Delete is enabled for a saved profile")

            # Clean up so the test is repeatable.
            await pg.click("#btn-profile-delete")
            await pg.wait_for_timeout(2500)
            after = await pg.evaluate(
                "() => document.querySelector('#profile-select').options.length"
            )
            check(after == saved_ok["count"] - 1, "deleting it removes exactly one option")

            check(not errs, f"no page errors ({errs[:2]})")
            await b.close()
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
        shutil.rmtree(home, ignore_errors=True)

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("all checks passed")


asyncio.run(main())

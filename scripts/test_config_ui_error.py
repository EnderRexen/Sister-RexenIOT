"""Confirm the admin UI surfaces a rejected TTS model instead of claiming success.

Pairs with test_config_tts_rollback.py: that one proves the server returns 400
for a model it cannot load, this one proves the operator actually sees it. The
PUT is intercepted so the real config on disk is never touched.

Run: PYTHONPATH=. ./.venv/bin/python scripts/test_config_ui_error.py
"""
import asyncio
from playwright.async_api import async_playwright

BASE = "http://127.0.0.1:12000"
DETAIL = "Could not load TTS model 'KittenML/kitten-tts-bogus': simulated failure"


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path="/usr/bin/chromium", args=["--no-sandbox"])
        pg = await b.new_page(viewport={"width": 1400, "height": 1000})
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))

        async def reject(route):
            await route.fulfill(
                status=400,
                content_type="application/json",
                body='{"detail": %r}' % DETAIL,
            )

        await pg.route("**/api/admin/config", reject)
        await pg.goto(f"{BASE}/admin", wait_until="networkidle")
        await pg.wait_for_timeout(2500)

        # Toggling any control triggers the debounced autosave.
        await pg.evaluate("""() => {
          const el = document.querySelector('[data-cfg="tts.enabled"]');
          if (el) el.checked = !el.checked;
          el && el.dispatchEvent(new Event('change', { bubbles: true }));
        }""")
        await pg.wait_for_timeout(4000)

        state = await pg.eval_on_selector("#save-state", "e => e.textContent.trim()")
        toast = await pg.evaluate("""() => {
          const t = document.querySelector('#toast, .toast');
          return t ? t.textContent.trim() : '';
        }""")
        print("  save state:", state)
        print("  toast     :", toast[:110])

        ok_state = "error" in state.lower()
        ok_detail = "simulated failure" in toast
        print(f"  {'PASS' if ok_state else 'FAIL'}  save state reports an error, not 'saved'")
        print(f"  {'PASS' if ok_detail else 'FAIL'}  server detail shown to the operator")
        print(f"  {'PASS' if not errs else 'FAIL'}  no page errors: {errs}")
        await b.close()
        return 0 if (ok_state and ok_detail and not errs) else 1


raise SystemExit(asyncio.run(main()))

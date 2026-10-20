"""Drive the admin Apply button against a stubbed error response.

Confirms the client half of the hardware/apply fix: a 200 carrying
``{error: ...}`` must read as a failure, not as a successful apply.

``web/js/admin.js`` loads as a module, so its functions are not on ``window``
and cannot be called from ``page.evaluate``. Intercepting the request and
clicking the real button exercises the genuine code path instead.
"""
import asyncio
from playwright.async_api import async_playwright

BASE = "http://127.0.0.1:12000"


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path="/usr/bin/chromium", args=["--no-sandbox"])
        pg = await b.new_page(viewport={"width": 1400, "height": 1000})
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))

        # Return the failure shape the new handler produces, without touching the
        # real config on disk.
        async def stub(route):
            await route.fulfill(
                status=200,
                content_type="application/json",
                body='{"applied": {}, "error": "Could not load X: simulated failure",'
                     ' "config": {}, "summary": "", "engine": {}}',
            )

        await pg.route("**/api/admin/hardware/apply", stub)
        await pg.goto(f"{BASE}/admin", wait_until="networkidle")
        await pg.wait_for_timeout(2500)

        # The button starts disabled until a scan populates recommendations.
        await pg.evaluate("""() => {
          const b = document.querySelector('#btn-apply-recs');
          if (b) b.disabled = false;
          window.confirm = () => true;
        }""")
        await pg.click("#btn-apply-recs")
        await pg.wait_for_timeout(1500)

        state = await pg.eval_on_selector("#save-state", "e => e.textContent.trim()")
        toast = await pg.evaluate("""() => {
          const t = document.querySelector('#toast, .toast');
          return t ? t.textContent.trim() : '';
        }""")
        print("  save state:", state)
        print("  toast     :", toast)

        ok_state = "fail" in state.lower()
        ok_toast = "simulated failure" in toast
        print(f"  {'PASS' if ok_state else 'FAIL'}  save state marked failed, not 'applied'")
        print(f"  {'PASS' if ok_toast else 'FAIL'}  error text shown to the user")
        print(f"  {'PASS' if not errs else 'FAIL'}  no page errors: {errs}")
        await b.close()
        return 0 if (ok_state and ok_toast and not errs) else 1


raise SystemExit(asyncio.run(main()))

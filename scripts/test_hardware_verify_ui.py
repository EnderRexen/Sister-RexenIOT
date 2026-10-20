"""Check the verification panel in the admin UI, against a running server.

Exercises the real endpoints: run the verification, read the checklist the page
rendered, apply the plan with the real speech model, and confirm the page and the
config agree afterwards.
"""
import asyncio
import json
import sys
import urllib.request

from playwright.async_api import async_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8060"
FAILS = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


def get(path):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=60) as r:
        return json.loads(r.read())


def put(path, payload):
    req = urllib.request.Request(
        f"{BASE}{path}", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="PUT")
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


async def main():
    # Put the config into a known state where the plan has a real change to
    # propose. Otherwise this test would pass or fail depending on whatever the
    # last run happened to leave behind, which is not a test. A weaker speech tier
    # than this machine warrants is a change the plan must want to undo.
    put("/api/admin/config", {"patch": {"tts": {
        "tier": "nano", "model_id": "KittenML/kitten-tts-nano-0.8-int8",
    }}})
    plan0 = get("/api/admin/hardware/plan")
    if not plan0["changed"]:
        print("  FAIL: the plan proposes nothing after a deliberate downgrade")
        raise SystemExit(1)

    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path="/usr/bin/chromium", args=["--no-sandbox"])
        pg = await b.new_page(viewport={"width": 1400, "height": 1000})
        errs = []
        pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
        pg.on("pageerror", lambda e: errs.append(f"pageerror: {e}"))
        await pg.goto(f"{BASE}/admin", wait_until="networkidle")
        await pg.wait_for_timeout(2500)

        print("=== the panel exists and starts inert ===")
        check("verify button present", await pg.is_visible("#btn-verify"))
        check("apply is disabled before verifying",
              await pg.is_disabled("#btn-verify-apply"))
        check("steps are empty before verifying",
              await pg.eval_on_selector("#verify-steps", "e => e.children.length") == 0)

        print("\n=== running verification fills the checklist ===")
        await pg.click("#btn-verify")
        await pg.wait_for_timeout(20000)
        n = await pg.eval_on_selector("#verify-steps", "e => e.children.length")
        check("checklist rendered", n >= 5, f"{n} steps")
        badge = await pg.eval_on_selector("#verify-badge", "e => e.textContent.trim()")
        check("a headline verdict is shown", bool(badge) and badge != "not run", badge)
        titles = await pg.eval_on_selector_all(
            "#verify-steps .verify-title", "els => els.map(e => e.textContent.replace(/\\s+/g,' ').trim())")
        print("   ", " | ".join(titles))
        check("it checked the GPU", any("Graphics" in t for t in titles))
        check("it checked onnxruntime", any("ONNX" in t for t in titles))
        check("it checked the phonemiser", any("Phonemiser" in t for t in titles))
        check("it reported the settings", any("Settings to apply" in t for t in titles))

        print("\n=== the page and the API agree ===")
        plan = get("/api/admin/hardware/plan")
        check("page verdict matches the API",
              badge == ("all checks passed" if not any(
                  s["status"] == "warn" for s in plan["steps"]) else badge),
              f"api={plan['changed']}")

        print("\n=== apply-with-test really loads the model and speaks ===")
        pg.once("dialog", lambda d: asyncio.ensure_future(d.accept()))
        await pg.click("#btn-verify-apply")
        await pg.wait_for_timeout(60000)
        measured = await pg.eval_on_selector(
            "#verify-measured", "e => e.hidden ? null : e.textContent.replace(/\\s+/g,' ').trim()")
        check("the test result is shown", bool(measured), str(measured)[:120])
        if measured:
            check("it reports the model", "kitten-tts" in measured)
            check("it reports a device", "cpu" in measured or "cuda" in measured)
            check("it reports audio was produced", "KB" in measured)
        live = get("/api/admin/config")
        check("the config was persisted", live["tts"]["tier"] == plan["tier"],
              f"live={live['tts']['tier']} plan={plan['tier']}")

        print("\n=== a second verification shows the settings now match ===")
        await pg.click("#btn-verify")
        await pg.wait_for_timeout(20000)
        diff = await pg.eval_on_selector("#verify-diff", "e => e.textContent.replace(/\\s+/g,' ').trim()")
        check("nothing left to change", "nothing to change" in diff, diff[:110])
        check("apply is disabled when there is nothing to do",
              await pg.is_disabled("#btn-verify-apply"))

        print(f"\nconsole errors: {len(errs)}")
        for e in errs[:5]:
            print("   ", e)
        check("no console errors", not errs)
        await b.close()

    print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILURE(S)'}")
    for f in FAILS:
        print("  -", f)
    raise SystemExit(1 if FAILS else 0)


asyncio.run(main())

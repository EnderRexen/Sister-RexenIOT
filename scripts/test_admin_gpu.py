"""Check the admin GPU controls render and the device selector round-trips."""
import asyncio
import sys

from playwright.async_api import async_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8060"


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path="/usr/bin/chromium", args=["--no-sandbox"])
        pg = await b.new_page(viewport={"width": 1280, "height": 900})
        errs = []
        pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
        pg.on("pageerror", lambda e: errs.append(f"pageerror: {e}"))
        await pg.goto(f"{BASE}/admin", wait_until="networkidle")
        await pg.wait_for_timeout(3000)

        # The accelerator card should have filled itself in.
        acc = await pg.eval_on_selector("#accelerator-status", "e => e.textContent.trim()")
        print("--- accelerator card ---")
        print("  text:", " | ".join(acc.split()))

        hints = await pg.eval_on_selector_all("#accelerator-hints .hint", "els => els.map(e => e.textContent.replace(/\\s+/g,' ').trim())")
        print(f"  hints rendered: {len(hints)}")
        for h in hints:
            print("   -", h)

        # The device selector must exist in the Voice panel, with all three options.
        opts = await pg.eval_on_selector_all("#tts-device-select option", "els => els.map(e => e.value)")
        print(f"--- device selector options: {opts}")

        # Its current value must match what the config says.
        val = await pg.eval_on_selector("#tts-device-select", "e => e.value")
        print(f"    selected: {val}")

        # Switching panels should show the Voice panel with the selector visible.
        await pg.click('.tab[data-tab="voice"]')
        await pg.wait_for_timeout(500)
        visible = await pg.is_visible("#tts-device-select")
        print(f"    visible on Voice tab: {visible}")

        # Reload button lives in the GPU card on the Hardware tab.
        await pg.click('.tab[data-tab="hardware"]')
        await pg.wait_for_timeout(400)
        has_reload = await pg.is_visible("#btn-tts-reload")
        print(f"--- reload button visible: {has_reload}")
        if has_reload:
            await pg.click("#btn-tts-reload")
            await pg.wait_for_timeout(6000)
            toast = await pg.eval_on_selector_all(".toast", "els => els.map(e => e.textContent.trim())")
            print(f"    reload toast: {toast}")

        print(f"console errors: {len(errs)}")
        for e in errs[:5]:
            print("   ", e)
        await b.close()


asyncio.run(main())

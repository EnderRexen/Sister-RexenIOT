"""Screenshot the on-screen keyboard open, on desktop and phone."""
import asyncio
import sys

from playwright.async_api import async_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8060"
OUT = "/tmp/rexeniot_shots"


async def main():
    import os
    os.makedirs(OUT, exist_ok=True)
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path="/usr/bin/chromium", args=["--no-sandbox"])
        for label, w, h, dsf in [("desktop", 1280, 900, 1), ("phone", 390, 844, 2)]:
            pg = await b.new_page(viewport={"width": w, "height": h}, device_scale_factor=dsf)
            await pg.goto(BASE + "/", wait_until="networkidle")
            await pg.wait_for_timeout(1800)
            # Reveal the toggle regardless of pointer type, then open the tray.
            await pg.evaluate("document.querySelector('#btn-keyboard').hidden = false")
            await pg.click("#btn-keyboard")
            await pg.wait_for_timeout(1100)
            await pg.fill("#input", "sister rexeniot, dim the study lights")
            await pg.wait_for_timeout(300)
            await pg.screenshot(path=f"{OUT}/osk-{label}.png")
            print(f"wrote {OUT}/osk-{label}.png")
            await pg.close()
        await b.close()


asyncio.run(main())

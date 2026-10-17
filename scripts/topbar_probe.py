import asyncio

from playwright.async_api import async_playwright


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path="/usr/bin/chromium", args=["--no-sandbox"])
        pg = await b.new_page(viewport={"width": 375, "height": 667}, device_scale_factor=2)
        await pg.goto("http://127.0.0.1:8060/", wait_until="networkidle")
        await pg.wait_for_timeout(1500)
        out = await pg.evaluate("""() => {
            const tb = document.querySelector('.topbar');
            const r = tb.getBoundingClientRect();
            const kids = [...tb.children].map(e => {
                const b = e.getBoundingClientRect();
                return { cls: e.className, h: Math.round(b.height), w: Math.round(b.width) };
            });
            return { topbar_h: Math.round(r.height), children: kids,
                     style: getComputedStyle(tb).padding };
        }""")
        print(out)
        await b.close()


asyncio.run(main())

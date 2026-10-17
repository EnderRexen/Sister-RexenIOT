import asyncio

from playwright.async_api import async_playwright


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path="/usr/bin/chromium", args=["--no-sandbox"])
        for label, w, h in [("iPhone SE 375x667", 375, 667),
                            ("iPhone 14 390x844", 390, 844),
                            ("landscape 667x375", 667, 375)]:
            pg = await b.new_page(viewport={"width": w, "height": h}, device_scale_factor=2)
            errs = []
            pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
            await pg.goto("http://127.0.0.1:8060/", wait_until="networkidle")
            await pg.wait_for_timeout(1500)
            m = await pg.evaluate("""() => {
                const box = (s) => { const e = document.querySelector(s);
                    if (!e) return null; const r = e.getBoundingClientRect();
                    return {h: Math.round(r.height), w: Math.round(r.width), top: Math.round(r.top)}; };
                return { vh: window.innerHeight, doc: document.documentElement.clientHeight,
                         messages: box('.messages'), composer: box('.composer'),
                         topbar: box('.topbar'), app: box('.app') };
            }""")
            print(f"--- {label} ---")
            print(f"  viewport={m['vh']}  doc={m['doc']}")
            print(f"  messages h={m['messages']['h'] if m['messages'] else 'n/a'}"
                  f"  composer h={m['composer']['h'] if m['composer'] else 'n/a'}"
                  f"  topbar h={m['topbar']['h'] if m['topbar'] else 'n/a'}")
            print(f"  console errors: {len(errs)}")
            await pg.close()
        await b.close()


asyncio.run(main())

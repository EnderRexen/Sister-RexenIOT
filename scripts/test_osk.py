"""Exercise the on-screen keyboard on a phone-sized viewport."""
import asyncio
import sys

from playwright.async_api import async_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8060"


async def tap(page, key):
    await page.click(f'.osk-key[data-key="{key}"]')


async def main():
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path="/usr/bin/chromium", args=["--no-sandbox"])
        pg = await b.new_page(viewport={"width": 390, "height": 844}, device_scale_factor=2)
        errs = []
        pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
        pg.on("pageerror", lambda e: errs.append(f"pageerror: {e}"))
        await pg.goto(BASE + "/", wait_until="networkidle")
        await pg.wait_for_timeout(1800)

        # The toggle should be offered on a phone viewport.
        hidden = await pg.get_attribute("#btn-keyboard", "hidden")
        print(f"toggle available on phone: {hidden is None}")

        await pg.click("#btn-keyboard")
        # Sample mid-slide: the tray should be part-way up, not already seated.
        await pg.wait_for_timeout(160)
        mid = await pg.evaluate("""() => {
            const t = document.querySelector('.osk-tray');
            const s = getComputedStyle(t);
            return { open: document.querySelector('#osk').classList.contains('open'),
                     transform: s.transform, box_h: Math.round(t.getBoundingClientRect().height) };
        }""")
        print(f"mid-slide: open={mid['open']} transform={mid['transform']} tray_h={mid['box_h']}")

        await pg.wait_for_timeout(900)
        seated = await pg.evaluate("""() => {
            const t = document.querySelector('.osk-tray');
            return { transform: getComputedStyle(t).transform,
                     slot_h: Math.round(document.querySelector('#osk').getBoundingClientRect().height) };
        }""")
        print(f"seated: transform={seated['transform']} slot_h={seated['slot_h']}")

        visible = await pg.is_visible("#osk")
        keys = await pg.eval_on_selector_all(".osk-key", "els => els.length")
        print(f"panel visible after toggle: {visible}  keys rendered: {keys}")

        val = lambda: pg.eval_on_selector("#input", "e => e.value")

        await tap(pg, "h"); await tap(pg, "e"); await tap(pg, "y")
        print(f"typed 'hey' -> {await val()!r}")

        # Shift produces one capital, then reverts.
        await tap(pg, "shift"); await tap(pg, "a")
        print(f"shift+a -> {await val()!r}")

        # Space key.
        await tap(pg, "space")
        print(f"after space -> {await val()!r}")

        # Symbol layer, then back.
        await tap(pg, "symbols"); await pg.wait_for_timeout(150)
        sym_keys = await pg.eval_on_selector_all(".osk-key", "els => els.length")
        await tap(pg, "1"); await tap(pg, "7")
        print(f"symbols layer keys: {sym_keys}  after '17' -> {await val()!r}")
        await tap(pg, "letters"); await pg.wait_for_timeout(150)

        # Backspace removes exactly one character.
        before = await val()
        await tap(pg, "backspace")
        after = await val()
        print(f"backspace: {before!r} -> {after!r} (len {len(before)}->{len(after)})")

        # Caps lock via double shift.
        await tap(pg, "shift"); await tap(pg, "shift")
        await tap(pg, "x"); await tap(pg, "y")
        capped = await val()
        print(f"caps lock x,y -> {capped!r}")
        await tap(pg, "shift")  # clear
        await tap(pg, "z")
        print(f"after clearing caps, z -> {await val()!r}")

        # Enter sends: clear, type a real message, press the soft Send key.
        await pg.fill("#input", "")
        for k in ["t", "e", "s", "t", "space", "o", "s", "k", "enter"]:
            await tap(pg, k)
        await pg.wait_for_timeout(4000)
        msgs = await pg.eval_on_selector_all(".msg", "els => els.map(e => e.textContent.trim())")
        print(f"input after send -> {await val()!r}")
        print(f"messages in thread: {len(msgs)}")

        # Hide works: the slot stays mounted through the tuck, then unmounts.
        await pg.click("#osk-close")
        await pg.wait_for_timeout(250)
        tucking = await pg.evaluate("""() => {
            const o = document.querySelector('#osk');
            return { open: o.classList.contains('open'),
                     transform: getComputedStyle(document.querySelector('.osk-tray')).transform };
        }""")
        print(f"tucking: open={tucking['open']} transform={tucking['transform']}")
        await pg.wait_for_timeout(600)
        print(f"panel hidden after close: {not await pg.is_visible('#osk')}")

        print(f"console errors: {len(errs)}")
        for e in errs[:5]:
            print("   ", e)
        await b.close()


asyncio.run(main())

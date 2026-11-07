"""Drive the real chat UI: attach a file, send it, confirm the agent read it."""

import sys
from playwright.sync_api import sync_playwright

URL = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8070"
CSV = "/tmp/uptest/usage.csv"

with sync_playwright() as p:
    browser = p.chromium.launch(executable_path="/usr/bin/chromium", args=["--no-sandbox", "--autoplay-policy=no-user-gesture-required"])
    page = browser.new_page()
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.on("console", lambda m: errors.append(f"console.{m.type}: {m.text}")
            if m.type == "error" else None)

    page.goto(URL, wait_until="networkidle")
    page.wait_for_selector("#app:not([hidden])", timeout=15000)

    # 1. Attach a file through the hidden input the button opens.
    page.set_input_files("#file-input", CSV)
    page.wait_for_selector(".attach-chip", timeout=10000)
    chip = page.inner_text(".attach-chip .attach-name")
    print(f"  chip shown: {chip!r}")

    # 2. Ask about it and send.
    page.fill("#input", "What does this file say?")
    page.click("#btn-send")

    page.wait_for_selector(".msg.assistant", timeout=30000)
    page.wait_for_timeout(4000)

    bubbles = page.query_selector_all(".msg")
    transcript = [(b.get_attribute("class"), b.inner_text().strip()) for b in bubbles]
    for cls, text in transcript:
        who = "you" if "user" in (cls or "") else "rexeniot"
        print(f"  {who}: {text[:150]}")

    # 3. The strip must be cleared once the message is sent.
    strip_hidden = page.get_attribute("#attach-strip", "hidden") is not None
    print(f"  attachment strip cleared after send: {strip_hidden}")

    body = page.inner_text("body")
    reached = "lamp,9,6=YES" in body or "kettle,1500=YES" in body
    print(f"  file contents reached the model: {reached}")

    page.screenshot(path="/tmp/attach_ui.png", full_page=False)
    print(f"  screenshot: /tmp/attach_ui.png")

    real_errors = [e for e in errors if "favicon" not in e.lower()]
    if real_errors:
        print("  PAGE ERRORS:")
        for e in real_errors[:8]:
            print(f"    {e}")
    else:
        print("  no page errors")

    browser.close()

    ok = reached and strip_hidden and not real_errors
    print("\nRESULT:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)

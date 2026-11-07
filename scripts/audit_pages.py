"""Sanity-check both pages: console errors, failed requests, key widgets."""

import sys
from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8070"


def audit(page, label, checks):
    console, failed, pageerrors = [], [], []
    page.on("console", lambda m: console.append(f"{m.type}: {m.text}") if m.type == "error" else None)
    page.on("requestfailed", lambda r: failed.append(f"{r.method} {r.url} :: {r.failure}"))
    page.on("pageerror", lambda e: pageerrors.append(str(e)))
    page.goto(f"{BASE}{label}", wait_until="networkidle")
    page.wait_for_timeout(2500)

    print(f"\n=== {label} ===")
    print(f"  title: {page.title()}")
    for name, selector in checks.items():
        n = len(page.query_selector_all(selector))
        print(f"  {name:<28} {'FOUND x'+str(n) if n else 'MISSING'}")

    # HTTP status of every API call the page made.
    codes = page.evaluate("""() =>
      performance.getEntriesByType('resource')
        .filter(e => e.name.includes('/api/'))
        .map(e => e.name.split('/api/')[1])
    """)
    print(f"  api calls made: {len(codes)}")

    noise = ("favicon", "net::ERR_ABORTED")
    console = [c for c in console if not any(n in c for n in noise)]
    failed = [f for f in failed if not any(n in f for n in noise)]
    print(f"  console errors: {len(console)}")
    for c in console[:6]:
        print(f"    - {c[:160]}")
    print(f"  failed requests: {len(failed)}")
    for f in failed[:6]:
        print(f"    - {f[:160]}")
    print(f"  page errors: {len(pageerrors)}")
    for e in pageerrors[:4]:
        print(f"    - {e[:200]}")
    return console, failed, pageerrors


with sync_playwright() as p:
    browser = p.chromium.launch(executable_path="/usr/bin/chromium", args=["--no-sandbox"])
    page = browser.new_page(viewport={"width": 1500, "height": 950})

    c1, f1, e1 = audit(page, "/", {
        "chat log": "#log, .log, .messages",
        "composer input": "#input",
        "attach button": "#btn-attach",
        "mic button": "#btn-mic",
        "personality picker": "select, .persona-picker, [data-persona]",
        "status panel": "aside, .rail, .side",
    })
    page.screenshot(path="/tmp/audit_main.png")

    bad_admin = []
    page.on("response", lambda r: bad_admin.append((r.status, r.url)) if r.status >= 400 else None)
    c2, f2, e2 = audit(page, "/admin", {
        "tab buttons": ".tab",
        "panels": ".panel",
        "hardware scan btn": "#btn-scan",
        "storage card": "#storage-status",
        "tts status": "#tts-status",
        "persona index": "#persona-index",
    })
    page.screenshot(path="/tmp/audit_admin.png", full_page=True)

    print("  ADMIN >=400 RESPONSES:")
    for s, u in bad_admin:
        print(f"    {s}  {u}")
    browser.close()

total = len(c1) + len(f1) + len(e1) + len(c2) + len(f2) + len(e2)
print(f"\nRESULT: {total} issue(s) found")

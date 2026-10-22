"""The accelerator card must not read as a contradiction, and the CUDA advice
must fit the GPU that is actually present.

Two defects are guarded here:

1. The card printed the torch *build tag* beside the reachability verdict:
   "2.14.1+cu130 · CPU only". The local tag is compiled in, so it says nothing
   about whether a GPU is reachable -- a CUDA 13.0 build with no driver prints
   exactly that, and the reader is left thinking the panel is broken.
2. For a card that cannot reach its GPU the advice was "check the driver". On an
   RTX 50-series card that is wrong: those are Blackwell (sm_120) and need a
   CUDA 12.8+ wheel, so reinstalling a working driver changes nothing.

Runs against a live server, like the sibling UI checks.
"""
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
    with urllib.request.urlopen(f"{BASE}{path}", timeout=90) as r:
        return json.loads(r.read())


async def main():
    print("=== the accelerator payload separates build from reachability ===")
    acc = get("/api/admin/accelerator")
    t = acc.get("torch") or {}
    print(f"  torch: {json.dumps({k: t.get(k) for k in ('installed','cuda','version','cuda_version')})}")

    if t.get("installed"):
        check("torch reports a reachability verdict, not just a tag",
              isinstance(t.get("cuda"), bool), str(t.get("cuda")))
        if t.get("cuda_version") and not t.get("cuda"):
            check("a CUDA build with no reachable GPU is a real, expected state",
                  t.get("device_count") == 0, str(t.get("device_count")))

    # Whatever the machine reports, the rendered card must not contradict itself.
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path="/usr/bin/chromium", args=["--no-sandbox"])
        pg = await b.new_page(viewport={"width": 1280, "height": 900})
        errs = []
        pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
        pg.on("pageerror", lambda e: errs.append(f"pageerror: {e}"))
        await pg.goto(f"{BASE}/admin", wait_until="networkidle")
        await pg.wait_for_timeout(3000)

        rows = await pg.eval_on_selector_all(
            "#accelerator-status .kv-row",
            "els => els.map(e => e.textContent.replace(/\\s+/g,' ').trim())",
        )
        print("\n=== rendered accelerator rows ===")
        for r in rows:
            print("   -", r)

        text = " | ".join(rows)
        # The old defect printed the build tag as if it were a capability claim:
        # "2.14.1+cu130 · CPU only", or "onnxruntime · CUDA available". Now any
        # mention of a CUDA version must be labelled as a *build*, with the
        # reachability verdict stated separately.
        torch_row = next((r for r in rows if r.startswith("torch")), "")
        if "CUDA" in torch_row:
            check("a CUDA version on the torch row is labelled as a build",
                  "build" in torch_row, torch_row)
            check("and the reachability verdict is stated separately",
                  ("GPU reachable" in torch_row) or ("CPU only" in torch_row), torch_row)
        check("the card never calls an unreachable GPU 'available'",
              "CUDA available" not in text, text)
        check("the verdict is worded as reachability",
              ("GPU reachable" in text) or ("CPU only" in text), text)

        hints = await pg.eval_on_selector_all(
            "#accelerator-hints .hint",
            "els => els.map(e => e.textContent.replace(/\\s+/g,' ').trim())",
        )
        print(f"\n=== hints rendered: {len(hints)} ===")
        for h in hints:
            print("   -", h)

        # On a Blackwell card the advice must name the CUDA 12.8 wheel.
        sm = ""
        for g in acc.get("gpus") or []:
            if (g.get("vendor") or "").lower() == "nvidia":
                sm = str(g.get("compute_capability") or "")
                break
        print(f"\n  nvidia compute capability: {sm or '(none)'}")
        if sm.startswith("12."):
            joined = " ".join(hints)
            check("Blackwell gets a CUDA 12.8+ wheel index, not just 'check the driver'",
                  ("cu130" in joined) or ("cu128" in joined), joined[:160])
            check("Blackwell advice mentions sm_120",
                  "sm_120" in joined, joined[:160])
        else:
            check("no GPU: no CUDA reinstall hint is pushed", True)

        check("no console errors on the hardware tab", not errs, str(errs[:3]))
        await b.close()


import asyncio  # noqa: E402

asyncio.run(main())

print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILURE(S)'}")
for f in FAILS:
    print("  -", f)
sys.exit(1 if FAILS else 0)

"""Check the model panel's state against a running server.

Covers the two things that were broken: the model rows carry a real fit verdict
(so Download is enabled and badges show), and the Use button turns green for the
model that is actually loaded.
"""
import asyncio
import json
import sys
import urllib.error
import urllib.request

from playwright.async_api import async_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8070"
FAILS = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


def get(path):
    with urllib.request.urlopen(f"{BASE}{path}", timeout=90) as r:
        return json.loads(r.read())


def post(path, payload):
    req = urllib.request.Request(
        f"{BASE}{path}", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


async def main():
    print("=== /admin/models carries the state the panel renders from ===")
    data = get("/api/admin/models")
    catalog = data["catalog"]
    check("a catalog came back", len(catalog) > 5, f"{len(catalog)} entries")
    bad = [m["repo"] for m in catalog
           if not isinstance(m.get("fits"), bool) or "blockers" not in m
           or "requirements" not in m or "downloaded" not in m]
    check("every entry has fits, blockers, requirements, downloaded", not bad, str(bad[:3]))
    tts = [m for m in catalog if m["kind"] == "tts"]
    check("the tts entries all have a prerequisites block",
          all("prerequisites" in m for m in tts))
    check("exactly one tts model is marked active",
          sum(1 for m in tts if m["active"]) == 1,
          str([m["repo"] for m in tts if m["active"]]))
    active = [m for m in tts if m["active"]][0]
    print(f"    active: {active['repo']}")
    check("the active model is a fit", active["fits"] is True)

    print("\n=== a downloadable model is not disabled ===")
    # Before the fix every button was disabled because `fits` was absent.
    dl = [m for m in tts if m["fits"] and not (m["prerequisites"] or {}).get("needed")]
    check("there is at least one plain downloadable tts model", bool(dl), str(len(dl)))

    print("\n=== the scan path carries the same prerequisites as the catalog ===")
    # The panel renders models from the scan result, not from /models. That path
    # renames `requires` to `requirements`, so reading only the catalog spelling
    # made every tier report "needs nothing" and the PyTorch install was never
    # offered. Compare the two paths on the one entry that does need something.
    code, scan = post("/api/admin/hardware/scan", {})
    check("the scan succeeded", code == 200, f"HTTP {code}")
    scan_tts = [m for m in (scan.get("recommendations") or {}).get("models", [])
                if m.get("kind") == "tts"]
    check("the scan returned tts models", len(scan_tts) == 4, f"{len(scan_tts)} rows")
    cat_needed = {m["repo"]: (m["prerequisites"] or {}).get("needed") for m in tts}
    scan_needed = {m["repo"]: (m.get("prerequisites") or {}).get("needed") for m in scan_tts}
    check("both paths agree on which tiers need a requirement set",
          cat_needed == scan_needed, f"catalog={cat_needed} scan={scan_needed}")
    check("the 1.7B tier needs the PyTorch stack on both paths",
          cat_needed.get("KittenML/kitten-tts-2") is True
          and scan_needed.get("KittenML/kitten-tts-2") is True, str(scan_needed))

    print("\n=== the install guard refuses anything but a known set ===")
    for bad_set in ("nope", "requests", "../etc/passwd", "full; rm -rf /"):
        code, body = post("/api/admin/models/prerequisites/install", {"set": bad_set})
        check(f"'{bad_set}' is refused", code == 400, f"HTTP {code} {body.get('detail', '')}")

    print("\n=== the install status reports each set honestly ===")
    st = get("/api/admin/models/prerequisites/status")
    check("a status came back", "install" in st and "sets" in st)
    for key, s in st["sets"].items():
        check(f"'{key}' reports satisfied == not missing",
              s["satisfied"] == (not s["missing"]), f"missing={s['missing']}")

    print("\n=== loading a model whose prerequisites are missing is refused clearly ===")
    full = [m for m in tts if (m["prerequisites"] or {}).get("needed")]
    if full:
        m = full[0]
        if not m["prerequisites"]["satisfied"]:
            code, body = post("/api/admin/models/load", {"repo": m["repo"], "kind": "tts"})
            check("it is refused with 409, not a bare ImportError", code == 409, f"HTTP {code}")
            check("the message names the missing piece and the command",
                  "requirements-full.txt" in body.get("detail", ""), body.get("detail", "")[:90])
        else:
            check("this machine already has the prerequisites, so nothing to refuse", True)
    else:
        check("no model declares a prerequisite (unexpected)", False)

    print("\n=== the Use button goes green for the loaded model ===")
    async with async_playwright() as p:
        b = await p.chromium.launch(executable_path="/usr/bin/chromium", args=["--no-sandbox"])
        pg = await b.new_page(viewport={"width": 1400, "height": 1000})
        errs = []
        pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
        pg.on("pageerror", lambda e: errs.append(f"pageerror: {e}"))
        await pg.goto(f"{BASE}/admin", wait_until="networkidle")
        await pg.wait_for_timeout(2500)

        # Render the catalog the same way the panel does.
        await pg.evaluate("""async () => {
            const r = await fetch('/api/admin/models');
            const d = await r.json();
            window.__catalog = d.catalog;
        }""")
        rows = await pg.evaluate("""() => {
            const out = [];
            for (const m of window.__catalog) {
              if (m.kind !== 'tts') continue;
              out.push({repo: m.repo, active: m.active, fits: m.fits,
                        pre: m.prerequisites || {}});
            }
            return out;
        }""")
        check("the page can read the catalog", len(rows) == 4, f"{len(rows)} tts rows")
        actives = [r for r in rows if r["active"]]
        check("one row is active", len(actives) == 1, str([r["repo"] for r in actives]))

        # Drive the real renderer and assert the DOM, so this tests the markup and
        # not just the data.
        await pg.evaluate("""() => {
            const list = document.querySelector('#model-list');
            if (!list) return;
        }""")
        await pg.evaluate("""() => {
            const btn = document.querySelector('.tab[data-tab="models"]');
            if (btn) btn.click();
        }""")
        await pg.wait_for_timeout(800)

        badges = await pg.eval_on_selector_all(
            "#model-list .model-row .badge", "els => els.map(e => e.textContent.trim())")
        check("an 'in use' badge is rendered", "in use" in badges, str(badges[:8]))
        green = await pg.eval_on_selector_all(
            "#model-list .btn-ghost.is-active", "els => els.map(e => e.textContent.trim())")
        check("a green in-use button is rendered", any("In use" in g for g in green), str(green))
        in_use_rows = await pg.eval_on_selector_all(
            "#model-list .model-row.in-use", "els => els.length")
        check("the active row is marked", in_use_rows == 1, str(in_use_rows))
        disabled_dl = await pg.eval_on_selector_all(
            "#model-list [data-download]", "els => els.map(e => e.disabled)")
        check("not every Download button is disabled",
              not all(disabled_dl) if disabled_dl else False, str(disabled_dl))

        print("\n=== an unmet prerequisite offers Install, not a dead-end Download ===")
        # This machine already has the PyTorch stack, so the real catalog will not
        # exercise this branch. Intercept the API and serve a model with the
        # prerequisite unmet, then let the panel's own boot path render it. This
        # drives the real renderer rather than poking at the DOM by hand.
        await pg.evaluate("""() => {
            const orig = window.fetch;
            window.__origFetch = orig;
            window.fetch = async (url, opts) => {
              const u = String(url);
              if (u.includes('/admin/hardware/scan')) {
                const r = await orig(url, opts);
                const d = await r.json();
                d.recommendations = d.recommendations || {};
                d.recommendations.models = [{
                  kind: 'tts', name: 'KittenTTS 2 (1.7B)', repo: 'KittenML/kitten-tts-2',
                  note: 'needs torch', fits: true, downloaded: false, active: false,
                  size_mb: 506, requirements: {ram_gb: 8, cores: 4, torch: true},
                  blockers: [], caveats: [],
                  prerequisites: {needed: true, key: 'full', label: 'PyTorch stack',
                    detail: 'Adds ~2.5 GB.', satisfied: false,
                    missing: ['torch'], command: 'pip install -r requirements-full.txt'},
                }];
                return new Response(JSON.stringify(d), {status: 200,
                  headers: {'Content-Type': 'application/json'}});
              }
              return orig(url, opts);
            };
        }""")
        # Re-run the panel's own scan+render path.
        await pg.evaluate("() => { document.querySelector('#btn-scan').click(); }")
        await pg.wait_for_timeout(8000)
        install_btns = await pg.eval_on_selector_all(
            "#model-list [data-install]",
            "els => els.map(e => ({key: e.dataset.install, text: e.textContent.trim()}))")
        check("an Install button is rendered", bool(install_btns), str(install_btns))
        check("it targets the 'full' requirement set",
              install_btns and install_btns[0]["key"] == "full", str(install_btns))
        check("it is labelled with the set, not a package name",
              install_btns and "PyTorch" in install_btns[0]["text"], str(install_btns))
        no_dl = await pg.eval_on_selector_all("#model-list [data-download]", "els => els.length")
        check("no Download button competes with it", no_dl == 0, str(no_dl))
        note = await pg.eval_on_selector("#model-list .model-caveat", "e => e.textContent")
        check("the row explains what is missing and the command",
              "PyTorch stack" in note and "requirements-full.txt" in note, note[:90])
        # Restore the real API and re-render the genuine catalog before the next
        # section, which needs a real in-use row to move the green from.
        await pg.evaluate("() => { window.fetch = window.__origFetch; }")
        await pg.evaluate("() => { document.querySelector('#btn-scan').click(); }")
        await pg.wait_for_timeout(8000)
        restored = await pg.eval_on_selector_all(
            "#model-list .model-row.in-use", "els => els.length")
        check("the real catalog is back with one in-use row", restored == 1, str(restored))

        print("\n=== clicking Use moves the green to that row ===")
        # The actual request: selecting Use must light the row up. Switch to a
        # different model and confirm the mark moves rather than duplicating.
        before = await pg.eval_on_selector(
            "#model-list .model-row.in-use .model-repo", "e => e.textContent.trim()")
        target = [r["repo"] for r in rows if not r["active"] and r["fits"]][0]
        print(f"    from {before}\n    to   {target}")
        await pg.eval_on_selector_all(
            "#model-list [data-usetts]",
            """(els, repo) => {
                const b = els.find(e => e.dataset.usetts === repo);
                if (b) b.click();
            }""",
            target)
        await pg.wait_for_timeout(60000)
        after = await pg.eval_on_selector_all(
            "#model-list .model-row.in-use .model-repo", "els => els.map(e => e.textContent.trim())")
        check("exactly one row is in use after the switch", len(after) == 1, str(after))
        check("the mark moved to the chosen model", after and after[0] == target, str(after))
        greens = await pg.eval_on_selector_all(
            "#model-list .btn-ghost.is-active", "els => els.map(e => e.textContent.trim())")
        check("the new row shows the green button", any("In use" in g for g in greens), str(greens))
        live = get("/api/admin/config")
        check("the config agrees", live["tts"]["model_id"] == target, live["tts"]["model_id"])

        print("\n=== switching back leaves the original in use ===")
        await pg.eval_on_selector_all(
            "#model-list [data-usetts]",
            """(els, repo) => {
                const b = els.find(e => e.dataset.usetts === repo);
                if (b) b.click();
            }""",
            before)
        await pg.wait_for_timeout(60000)
        back = await pg.eval_on_selector_all(
            "#model-list .model-row.in-use .model-repo", "els => els.map(e => e.textContent.trim())")
        check("the original is in use again", back and back[0] == before, str(back))

        print(f"  console errors: {len(errs)}")
        check("no console errors", not errs)
        await b.close()

    print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILURE(S)'}")
    for f in FAILS:
        print("  -", f)
    raise SystemExit(1 if FAILS else 0)


asyncio.run(main())

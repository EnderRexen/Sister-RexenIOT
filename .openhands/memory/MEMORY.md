# Project memory ŌĆö Sister RexenIOT

Sister RexenIOT: local-first home-assistant web app, Python FastAPI backend +
vanilla JS frontend, KittenTTS for speech.

**Naming:** the project has been rebranded twice ŌĆö first off its original codename,
then to add the `IOT` suffix. Keep both old forms out of the codebase entirely: no
identifiers, env vars, log names or filenames. The conventions are
`rexeniot` (code/env), `RexenIOT` (display), `REXENIOT_` (env vars),
`Sister RexenIOT` (full name).
- A `grep -ri` for the *original* codename still hits
  `data/models/blobs/.../*` ŌĆö that is the HuggingFace tokenizer vocabulary
  (`"─ĀJarvis": 98520`), third-party data, not ours. `data/models/` is excluded from
  the source zip, so it does not ship. Do not "fix" it.

## Layout
- `app/` ŌĆö FastAPI backend. `main.py`, `config.py`, `hardware.py`,
  `model_catalog.py`, `personalities.py`, `skills.py`, `routing.py`, `llm/`,
  `tts/`, `routers/`.
- `web/` ŌĆö static frontend served directly: `index.html`, `admin.html`,
  `css/style.css`, `css/admin.css`, `js/app.js`, `js/osk.js`, `js/admin.js`.
- `scripts/` ŌĆö Playwright test/probe scripts, run with `./.venv/bin/python`.
- `data/` ŌĆö config.json, personalities.json, skills.json, hardware_profile.json.

## Skills system
- `app/skills.py` mirrors `personalities.py`: JSON store, built-ins re-seeded on
  load, admin CRUD. A skill = instructions (injected into the system prompt) +
  the `tools` it *owns*. Built-ins: home, computer, web, memory, timers, images,
  alexa. Rationale in `2026-10-16.md`.
- **Gating is bidirectional.** `skills.withheld_tool_names()` returns tools owned
  by a skill that is off *or* whose `provider_field` is unset;
  `agent.usable_tools(cfg)` filters them from both the prompt and the `tools=`
  array. A tool no skill claims is still offered, so the all-enabled default
  matches pre-skills behaviour.
- `provider_field` makes a skill dormant until configured ŌĆö `alexa` uses
  `alexa_provider`, so its 5 tools never reach the model on an unconfigured box.

## Alexa integration
- **Amazon publishes no API to command an Echo or make Alexa speak.** Everything
  goes through a bridge; `_provider()` in `app/tools/alexa.py` picks one.
- Three providers with deliberately unequal reach, so tools *refuse with an
  explanation* rather than pretending: `voicemonkey` (hosted HTTP, announce +
  routines), `alexacli` (local CLI, the only route to a live command and a reply
  ŌĆö refresh token ~2 weeks), `home_assistant` (Alexa Media Player, announce only).
- Tools: `alexa_devices`, `alexa_speak` (also `announce: true` to broadcast),
  `alexa_command`, `alexa_ask`, `alexa_routine`. Env: `ALEXA_PROVIDER`,
  `VOICEMONKEY_TOKEN`, `ALEXACLI_PATH`, `ALEXACLI_DEVICE`.
  `voicemonkey_token` is masked in `public_view`.
- **The alexacli contract, learned the hard way** (see `2026-10-16.md`): `-d
  <device>` is *required* for speak/command/ask; output is wrapped as
  `{"success":ŌĆ”,"data"/"message":ŌĆ”}`; `routine list/run` is WIP upstream; install
  is `brew install buddyh/tap/alexacli`. **A stand-in CLI I write myself proves
  plumbing, not the contract** ŌĆö read the upstream source.

## Running it
- Default port is **8060** (all four launchers + `data/config.json` + README).
- Dev server: `nohup ./.venv/bin/python -m app.main --host 0.0.0.0 --port 8060 --no-browser > /tmp/rexeniot_live.log 2>&1 &`
- **Keeping the preview link up:** the runtime kills child processes on recycle.
  `scripts/keep-alive.sh` polls the port and restarts the server automatically.
  It does not itself survive a container restart ŌĆö relaunch it after a recycle.
  If the link 502s, `cat /proc/uptime` (small value = container restarted).
- **To give her a real brain in the sandbox** (no Ollama/LM Studio): run
  `llama-server` from llama.cpp in `/workspace/.llm` (NOT `/tmp` ŌĆö a recycle wipes
  it) on port 8080 with `--alias qwen3:8`, which matches the shipped config with no
  admin edit. Recipe in `2026-10-16.md`.
- Public URL: https://work-1-dsytpcehntqnlxzx.prod-runtime.all-hands.dev/ (port 12000)
  - **The preview maps only 12000/12001**, so the live demo must run on 12000 even
    though the repo default is 8060. Port 12001 (work-2) returns 502.
  - `web/download/SisterRexenIOT.zip` is served by the static mount, so it downloads
    from `<preview>/download/SisterRexenIOT.zip`. Built by
    `scripts/build_source_zip.py`; the filename has changed once already
    (`rexeniot-source.zip` -> `sister-rexeniot-source.zip` -> `SisterRexenIOT.zip`),
    so re-check the name before quoting a link.
- **JS/CSS/HTML are served live; Python changes require a restart.** This matters:
  always restart before verifying backend edits.
- Kill only the exact process: `pkill -f "app.main --host"`.
- Playwright needs `executable_path="/usr/bin/chromium"` and `args=["--no-sandbox"]`.

## Environment quirks worth remembering
- `.venv/bin/python` is the app interpreter. Scripts that `import app` need
  `PYTHONPATH=.` (e.g. `PYTHONPATH=. ./.venv/bin/python scripts/foo.py`).
- `scripts/test_*.py` and `scripts/audit_pages.py` default to port **8060**, but
  the live demo runs on 12000 ŌĆö pass the URL or they refuse the connection.
- **Cold start is ~11s** (KittenTTS loading). Scripts using
  `wait_until="networkidle"` time out on a cold server; warm it with a curl first.
- The sandbox recycles often, killing the server and wiping `/tmp`. It also drops
  the firefox/webkit Playwright browsers; restore with
  `playwright install-deps <browser>` then `playwright install <browser>`.

## Layout / responsive rules (hard-won)
- **`overflow: hidden` on html/body silently breaks responsive audits**: it still
  permits *scripted* scrolling, so `window.scrollTo()` works while a real user is
  stuck, and it hides horizontal overflow. Verify scrolling with
  `page.mouse.wheel()` and overflow with `scrollWidth - clientWidth`.
- `style.css` locks the window for the chat HUD. `admin.html` loads style.css
  *and* admin.css, so `admin.css` re-enables window scrolling. admin.css is
  admin-only ŌĆö check that isolation when editing those rules.
- `1fr` is `minmax(auto, 1fr)`; `auto` floors a grid track at the widest child's
  min-content width. Use `minmax(0, 1fr)` in any single-column media override.
- Flex rows holding buttons/long values need `flex-wrap: wrap`, plus
  `min-width: 0` + `overflow-wrap: anywhere` on long values (paths, model ids).
- To grow a touch target without moving it visually, add
  `::after { position:absolute; inset:-Npx }`. Verify with
  `document.elementFromPoint` ŌĆö bounding boxes under-report the real hit area.
- `app/hardware.py` `cached()` re-derives recommendations on every read. Persisting
  the whole profile (facts *and* derived recs) made catalog edits invisible until a
  manual rescan. Only cache facts; derive the rest on read.

## Design decisions and their reasons
- **Personality cosmetics are a `@computed_field`.** `Personality.avatar` derives
  the chat initials from `initials` (explicit, stripped/capped at 2) or the name
  (one letter per word; two letters for a single word). A computed field is what
  makes it ride along in every `model_dump()`, so no route needed changing. The
  admin edits the raw `initials`; the client renders `avatar`.
- **`tts.device=auto` correctly defers to the library** — it passes neither
  `device` nor `backend`. Do not "fix" this into probing for CUDA and passing it
  explicitly. Both families are already GPU-aware: the torch loader picks `cuda`
  when `torch.cuda.is_available()` (`kittentts2/model.py:100`), and the ONNX
  loader forwards `backend=None` as `providers=[]`, which onnxruntime reads as
  "use all available providers in default precedence"
  (`kittentts_legacy/onnx_model.py:53-65`). An earlier session wrongly "fixed"
  this and had to revert it. *Unverified:* that `providers=[]` reading comes from
  onnxruntime's source, not a measurement — no CUDA provider exists here to
  distinguish the paths. `scripts/test_tts_device.py` pins the current contract.
- **`data/config.json` is gitignored, so it is NOT shipped.** A fresh install
  generates it from the Pydantic schema defaults in `app/config.py`
  (`TTSConfig.model_id` = `KittenML/kitten-tts-mini-0.8`, the ONNX tier). Reading
  this sandbox's config as the product default was a real mistake — its
  `model_id: kitten-tts-2` was written by an earlier `hardware/apply` call.
- **`/api/admin/hardware/apply` loads the model *before* persisting it.** It used
  to call `config.update()` first and only log a load failure, which permanently
  recorded a tier the machine cannot run. On failure it now returns
  `{"applied": {}, "error": ...}` and leaves config untouched;
  `web/js/admin.js` checks `result.error` (any 200 used to read as success).
  Pinned by `scripts/test_apply_rollback.py`, which fails against the old order.
- Model catalog `preferred: True` wins over raw score in `hardware.recommend()`.
  Needed because score alone picked Qwen2.5 7B/14B over the newer Qwen3 models
  (`_score` rewards parameters, not recency).
- `_caveat_reason()` in `hardware.py` classifies a failure reason as advice vs a
  blocker. A reason containing "not installed yet" or "spills to RAM" is a
  caveat, not a blocker. Use it instead of inline string checks.
- Qwen3.8 ships **only at 27B** (18 GB, `qwen3.8:27b`) ŌĆö there is no small tag.
  On a 12 GB card it partly offloads to system RAM, which is why 32 GB is the
  recommended floor. `qwen3:8b` is the fast fully-resident alternative.
- Layout anchors toasts via a `--dock-h` CSS var measured in
  `trackDockHeight()` (app.js). Prevents the persistent `#audio-hint` toast from
  covering the Send button.
- On-screen keyboard (`web/js/osk.js`) is the "CD-drive" tray: outer `.osk` is a
  clipped grid slot animating `grid-template-rows` 0frŌåÆ1fr, inner `.osk-tray`
  slides via `translateY`. Both are needed or the slide collapses into one frame.
  `setVisible(false)` delays `panel.hidden = true` ~540 ms so the tuck plays.
- The soft keyboard tracks its own caret and blurs the field on coarse pointers
  so the OS does not raise its native keyboard over ours.

## Testing
- Suites: `test_osk.py`, `test_recommend.py`, `test_admin_gpu.py`,
  `test_tts_device.py`, `test_apply_rollback.py`, `test_apply_ui.py`,
  `audit_pages.py`, `mobile_measure.py`. All green, 0 console errors.
- `test_tts_device.py` fakes `onnxruntime` via `sys.modules` to simulate a CUDA
  build - the pattern to copy when testing GPU branches on a GPU-less box.
- `test_apply_rollback.py` redirects `config.DATA_DIR`/`CONFIG_PATH` to a temp dir
  *before* importing the router, so the real config is never touched. To prove a
  fix fixes something, re-introduce the old code and confirm the test goes red -
  that is how the apply-ordering bug was caught.
- Watch out: `admin.hardware` **is** the `hardware` module, so patching
  `admin.hardware.scan` also rebinds `hardware.scan`. Capture the original first
  or a fake that calls it recurses forever.
- **Driving a chat turn from Playwright:** Enter in `#input` does submit and POSTs
  `/api/chat/stream`. Wait on `#btn-send.disabled` trueŌåÆfalse; a tool-calling turn
  takes 30ŌĆō90s on 4 CPU, so a 120s timeout is too short. `window.state` is not
  global. Assert the avatar via `#messages .msg.assistant .avatar` (`.brand-mark`
  is an SVG and raises "Node is not an HTMLElement").
- Playwright browsers include firefox and webkit, not just chromium.
  `playwright install` alone fails on missing system libs ŌĆö run
  `playwright install-deps firefox webkit` first (no root needed, uid 10001).
- Verified 3 engines ├Ś 8 viewports (320 ŌåÆ 4K wall 3840├Ś2160) ├Ś 8 pages/tabs:
  0 errors, 0 overflow.

## Compatibility / large screens
- `style.css` has fallback layers, because a missing feature drops the whole rule:
  - `@supports not (height: 100dvh)` ŌåÆ `--vh` set by `trackViewportHeight()` (app.js).
  - `@supports not (background: color-mix(...))` ŌåÆ flat-palette overrides covering
    all 19 `color-mix` uses; without it panels render transparent on pre-Chrome-111
    / Safari-16.2 / Firefox-113.
  - `@media (prefers-reduced-transparency: reduce)` ŌåÆ opaque bars.
  - 10-foot tiers at 1600px/2400px + `env(safe-area-inset-*)` for TV overscan.
- Full-screen button (`#btn-fullscreen`, beside the OSK toggle) hides itself when
  the Fullscreen API is absent ŌĆö iOS Safari has none for elements.
- **The on-screen keyboard has no sound.** The slide is CSS
  (`grid-template-rows` + `translateY`); the "stop" is `.osk-key` entrance
  animation; the click is `:active` scaling. The only AudioContext is the reply
  visualiser, not UI sound.

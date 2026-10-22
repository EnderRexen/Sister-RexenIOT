# Project memory ŌĆö Sister RexenIOT

Sister RexenIOT: local-first home-assistant web app, Python FastAPI backend +
vanilla JS frontend, KittenTTS for speech.

## TTS: KittenTTS tiers, voices, and acceleration

- Tiers are chosen in `app/model_catalog.py` (`KITTEN_TTS_MODELS`), keyed by repo
  id; `tier_of_repo()` maps a repo back to its tier. Default is the **mini ONNX**
  tier (`KittenML/kitten-tts-mini-0.8`, 8 voices) -- loads fast, runs anywhere.
- The **full tier** (`KittenML/kitten-tts-2`, torch) has 47 voices, cloning and
  emotion tags, but needs the whole torch stack (~2.5 GB, and its CUDA libs are
  mandatory at import even on CPU -- do not delete them).
- `tts.engine` = `model` (default) | `browser` | `auto` chooses *voice output*.
  `model` uses KittenTTS; `browser` uses the Web Speech API; `auto` uses the
  model and falls back to the browser when it cannot run. See
  `speakWithBrowser`/`fallbackToBrowser` in `web/js/app.js`.
- `tts.backend` = `torch` (default) | `vllm` selects the full tier's language
  model backend. **vLLM is ~3-4x faster on GPU** and ships inside kittenml 0.9.4
  (`kittentts2/vllm/`), but needs `pip install "kittenml[vllm]"` + CUDA. It is the
  concrete path to making cloning and 47 voices usable on the target GPU box.
- Emotion tags in `app/tts/engine.py` `EMOTION_TAGS` match upstream exactly.
  Upstream also supports inline `<laugh>` events and `(((emphasis)))`, unused here.
- **`/api/config` must publish any setting the HUD reads.** It once exposed only
  `tts_enabled`, so `tts.engine` had no effect until `public_config` in
  `app/routers/chat.py` was extended.
- `/api/admin/config` takes a **nested** patch (`{"tts": {"engine": "browser"}}`).
  Dotted keys (`{"tts.engine": ...}`) are silently ignored and still return 200 --
  `collectConfigPatch` in `web/js/admin.js` nests them, so only hand-built calls
  are at risk.

## Model catalog contract (admin Hardware tab)

- The model list is rendered from **two** endpoints: `recommendations.models` in
  the scan/`/hardware` payload, and `catalog` from `/admin/models`. `renderModels`
  reads `fits` / `blockers` / `requirements` / `downloaded` / `active` /
  `prerequisites` — annotate **both** paths (`_catalog_view`, `_with_model_state`)
  or a badge shows on one and not the other.
- `ALL_MODELS` is the raw catalog and has none of those fields. Serving it directly
  is what disabled every Download button.
- `model_catalog.prerequisites(entry)` -> the requirement set a model needs and
  whether it resolves. `REQUIREMENT_SETS` maps a **set key** (`full`, `gpu`) to a
  requirements file that ships with the app; the install endpoint accepts only
  those keys, never a package name.
- **The requirement field has two spellings.** The catalog says `requires`;
  `hardware.recommend` renames it to `requirements` (see `app/hardware.py`, the
  scoring loop). `prerequisites()` reads both, because reading only `requires`
  made the scan path — the list the panel actually renders — report every tier as
  needing nothing, so the PyTorch install was never offered.
- `/admin/models/load` refuses with 409 when prerequisites are unmet, naming the
  missing module and the pip command. Every other load path (warm-up, synthesize,
  config apply) goes through `TTSEngine._load_hint`, which derives the stack from
  the **model tier** rather than the error text — a broken install can raise
  something that never names the module.
- **A present-but-unimportable package is `broken`, not `missing`.** The fix
  differs: reinstall (`--force-reinstall`) vs install. Report them separately, or
  the panel offers the command that will not work.
- **`importlib.invalidate_caches()` after an install.** `importlib` caches
  `sys.path` directory listings, so a package installed while the process runs
  stays invisible until that cache is dropped. The install flow verifies by
  importing in-process, so `invalidate_probe_cache()` clears both the probe cache
  and the importlib caches. Symptom if skipped: a flaky test that passes alone and
  fails in a batch.
- **Do not judge a capability by `find_spec`.** `onnxruntime` and
  `onnxruntime-gpu` share an import name, so the GPU set looked satisfied on a
  CPU-only box; `missing_for("gpu")` checks `CUDAExecutionProvider` instead. Same
  trap as the phonemiser (bundled wheel, not on `PATH`) and the profile cache.
  Probe by **importing**, which runs the package's own initialisation — the only
  honest test — and cache the result briefly (torch alone takes ~1 s).
- `admin.js` is `<script type="module">`: its functions are **not** global, so UI
  tests must intercept `window.fetch` rather than call `renderModels` directly.
- **Test against the shipped archive, not the dev tree.** `/admin/models` scans on
  first call because a fresh install has no cached profile; without it every entry
  lacked `fits` and every Download button was disabled. The live box cannot
  reproduce first-run bugs — it has accumulated state. Extract
  `web/download/SisterRexenIOT.zip` to a temp dir and boot with
  `REXENIOT_HOME=<tmp>` (`scripts/test_fresh_install_models.py`).
- Kill stray test servers by PID from `ps aux | grep "[u]vicorn"`. `ss -ltnp`
  silently matches nothing here, and `keep-alive.sh` respawns anything on 12000.

## Hardware verification (`hardware.plan`)

- `hardware.plan(profile, cfg)` returns `{steps, patch, changed, notes, ...}` and
  **writes nothing**. `GET /api/admin/hardware/plan` reads; `POST
  /hardware/plan/run` applies with `{"verify": true}` (default) which loads the
  model and synthesises a phrase *before* persisting, so a plan that cannot speak
  leaves the config alone. Same ordering rule as `/hardware/apply`.
- Proposed settings come from the same `_fits`/`_score`/catalog path that builds
  the model list, so the two cannot disagree. Do not add a second tier->repo map.
- **Only propose a device when it changes behaviour.** `_pick_device` returns
  `"cpu"` with no GPU, but `"auto"` already means CPU there; proposing that flip
  changes the config for no runtime gain and drops the shipped `balanced` profile
  out of "in use" (it pins `device: "auto"`). Keep `cpu` if already `cpu`, else
  settle on `auto`.
- **`PROFILE_VERSION` in `app/hardware.py` guards the cached profile.** Add a probe
  to `_accelerators()`/`_runtimes()` and you must bump it: `cached()` returns None
  on mismatch so callers rescan. Otherwise a reader asking for a key the cache
  predates reports it missing, which reads as a *real finding* (this produced two
  false warnings: first `device`/`devices`, then `phonemiser`).
- **The phonemiser can come from `espeakng_loader`, not PATH.** The wheel ships its
  own espeak-ng binary and is a hard dep (`app/main.py`). `shutil.which()` alone
  reports a working install as broken. Use the `phonemiser` flag.
- A GPU-less box may still have a **CUDA-build torch** (`2.14.1+cu130`). That is
  normal in a container and is not a driver fault -- do not tell the user to check
  `nvidia-smi` when no GPU is present. `/accelerator` returns no hints at all when
  `not profile.get("gpu")`.
- **The local version tag is a build tag, not a capability.** `+cu130` is compiled
  in; it says nothing about whether a GPU is reachable. Print it labelled as a
  build (`2.14.1+cu130 (CUDA 13.0 build) · CPU only`) or the card contradicts
  itself and reads as a bug in the panel.
- **RTX 50-series is Blackwell (sm_120) and needs a CUDA 12.8+ wheel.** "Check the
  driver" is wrong advice there: the driver can be perfect and the wheel still
  rejects the card. `_nvidia_compute_cap()` reads `compute_capability` off the
  profile and emits `--index-url https://download.pytorch.org/whl/cu128`. Keep
  that URL in the hint only — never in the `requirements*.txt` files.
- `_nvidia_driver_only()` reads `/proc/driver/nvidia/version` and `/dev/nvidia*`
  to distinguish "no GPU" from "GPU the runtimes cannot reach". `torch.cuda` also
  gets a real `torch.zeros(8, device="cuda")` allocation, because
  `is_available()` can be True while allocation fails.

## Voice profiles (T4)

- `app/profiles.py` + `data/voice_profiles.json`. Six built-ins (balanced,
  best-quality, light-cpu, browser-voice, resilient, silent); custom ones are
  operator-saved. Voice tab opens on the active profile.
- **`validate()` exists because `AppConfig` silently drops unknown keys** -- a
  typo would save with a 200 and do nothing. It rejects unknown sections/keys,
  bad values, and bad combinations, and returns reasons. It caught `emotion`
  vs `default_emotion` in my own built-ins.
- Profiles cover only `tts` + `voice_input`. Allowing `llm`/`tools` would let a
  "voice" profile rewrite `api_key`/`admin_token`.
- `matches()` = every key the profile names equals live config. `active()` scans
  built-ins first.
- Apply loads the model **before** persisting, like `/hardware/apply`.
- `/admin/config` takes `{"patch": {...}}`; a bare patch is a 422.
- **Routes are only `/` and `/admin`** (no `/work`). Admin token is empty by
  default, so the panel needs no gate.
- UI rule: "in use" badge only when actually in effect, "selected" otherwise;
  Apply disabled when nothing to apply; Delete disabled for built-ins.

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
- **`PUT /api/admin/config` also loads the TTS model before persisting.** It had
  the same persist-first bug as `/hardware/apply`: a model that could not load
  was written to disk, then every start retried it, with one `log.warning` as
  evidence. It now raises 400 naming the model and leaves config alone. The
  admin's `api()` helper surfaces `detail`, so the operator sees the reason.
  Pinned by `scripts/test_config_tts_rollback.py` (5 of 7 red against the old
  order) and `scripts/test_config_ui_error.py`.
- **Friendly voice names are the vendor's own contract, not our invention.**
  `KittenML/kitten-tts-mini-0.8`'s `config.json` ships `voice_aliases` mapping
  Bella/Jasper/Luna/Bruno/Rosie/Hugo/Kiki/Leo onto `expr-voice-N-m/f`, and
  `_prepare_inputs` resolves them before validating. `BASE_VOICES` in
  `app/tts/engine.py` matches the list exactly. The ONNX `generate(text, voice,
  speed)` signature we call is correct — but note the vendor's *default* voice is
  `expr-voice-5-m`, so passing a bogus name raises rather than silently
  defaulting; the engine pre-validates against `self._voices` first.
- **Personality voices are all valid** (verified against the loaded engine).
  Do not "fix" Kiki — it is a real vendor alias, and it was already correct.
- **`data/` is runtime state, not shipped defaults.** Config is generated from
  the Pydantic schema; `data/config.json` is gitignored. Checking this sandbox's
  file to infer product defaults produced a false bug report once already.
- **`dev-stack.sh` is idempotent and will NOT restart a running app**, so an
  edited router keeps serving stale code. Kill the `app.main` PID first, or the
  fix will look like it did not work. Verify with `ps -eo pid,etime,cmd | grep
  app.main` that the process age is seconds, not minutes.
- **`/tmp` is wiped between runtime recycles.** Dev probes and their logs must
  live under the repo (`data/`), or they vanish mid-run. A tool call can also be
  interrupted by an env restart; check `curl /api/status` and restart the stack.
- **Voice cloning exists only in the full tier (KittenTTS 2), and is not wired.**
  `kittentts2` exposes `generate(text, reference=wav, reference_text=...)` plus
  `encode_reference` / `_clone_reference`; the ONNX tiers have no cloning at all.
  `app/tts/engine.py` never passes `reference=`, so nothing clones today. The
  engine docstring's "cloning" claim describes the vendor, not our wiring.
- **Cloning on CPU is not usable here, measured.** Full tier loads in ~29s and
  exposes 47 voices, but a built-in-voice sentence took **21s** to synthesize,
  and the clone path OOM-killed the whole environment (app died with it) on a
  15GB box. Cloning also pulls `openai/whisper-large-v3` (2.9GB) for the
  reference transcript unless `reference_text` is supplied. Treat cloning as
  GPU-only, which matches the user's plan to migrate to the RTX 5070.
- **torch cannot be trimmed: it preloads all 16 CUDA libs with `required=True`**
  (`torch/__init__.py::_preload_cuda_deps`). Removing `nvidia/*` breaks `import
  torch` even for CPU-only use. `triton` (897MB) is only an optional torch dep
  and *can* be removed safely — verified torch, transformers and kittenml import
  and matmul fine without it.
- **Browser TTS fallback does not exist yet.** `web/js/app.js::speak()` only
  POSTs `/api/speak` and toasts on failure; there is no `speechSynthesis` path
  anywhere in `web/`, so a TTS error means silence, not a fallback.
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
  `test_config_contract.py`, `test_config_tts_rollback.py`, `test_config_ui_error.py`,
  `test_hardware_plan.py`, `test_hardware_verify_ui.py`, `test_profiles.py`,
  `test_profiles_ui.py`, `audit_pages.py`, `mobile_measure.py`.
  All green except `test_attach_ui.py` (see below).
- **Server-side suites need `PYTHONPATH=.`** or `import app` fails. UI suites take
  the base URL as argv[1] (default `http://127.0.0.1:8060`).
- `test_hardware_plan.py` simulates profiles (5070-class, no-GPU, unreachable GPU,
  failed CUDA allocation, driver-only) so the GPU branches are exercised on this
  GPU-less box. `test_hardware_verify_ui.py` drives the real endpoints: it first
  *downgrades* the tier over the API so the plan has a real change to propose, then
  asserts apply-with-test produced audio and the config agrees.
- **`test_attach_ui.py` FAILS on a clean tree** -- it needs `/tmp/uptest/usage.csv`
  and a local model that echoes file contents. Pre-existing and unrelated to any
  hardware work; verify with `git stash` before blaming a change.
- When a plan change alters `tts.*`, check `test_profiles_ui.py` too: profile
  "in use" detection is an exact match, so a needless config write can silently
  break it. That is how the no-op device bug was found.
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

## Publishing / GitHub (learned the hard way)
- The `GITHUB_TOKEN` in this sandbox is a **GitHub App installation token**
  (`ghu_`), not a PAT. It authenticates as `EnderRexen` but **cannot create
  repositories** (`CreateRepository` denied on both REST and GraphQL), and
  `GET /user/repos` returns 0. `gh repo create` fails identically. Pushing is
  blocked too: git falls back to an interactive username prompt, so use
  `GIT_TERMINAL_PROMPT=0` to fail fast rather than hang.
- Consequence: an agent session can commit locally but **cannot publish a new
  repo**. Ask the user to create the empty repo on GitHub first, then push to it.
- The repo is prepared for that: branch renamed `master` -> `main`, origin set to
  `https://github.com/EnderRexen/Sister-RexenIOT.git` (hyphenated, the user's
  chosen repo name -- note this differs from the *zip* name below), working tree
  clean.
- The GitHub account is **EnderRexen** (Ender Rexen). It is not a code
  identifier -- nothing in the app references it.
- The `GITHUB_TOKEN` also **expires mid-session**: it authenticated fine early on
  and later returned 401 "Bad credentials". Re-check before relying on it.
- Naming, and the two forms are both deliberate: the repo is `Sister-RexenIOT`
  (hyphenated, so it reads as a slug), while the shipped archive stays
  `SisterRexenIOT.zip` (unhyphenated) because `web/admin.html` links to that exact
  path and `scripts/build_source_zip.py` writes it. Do not "fix" one to match the
  other without changing both the link and the builder.
- Secret scan before publishing: no credentials in tracked files or in the
  shipped zip. `.gitignore` already excludes `data/config.json` and the other
  generated state, so those never reach the repo.

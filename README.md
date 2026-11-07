# Sister RexenIOT

An at-home AI agent you run yourself. She speaks and listens, has a set of
selectable personalities the admin controls, and can act on your house and your
computer through tools.

Built around [KittenTTS](https://github.com/KittenML/KittenTTS) for speech, so
the voice is a real neural model running on your own CPU — no cloud TTS, no
per-word billing.

> **New here?** [SETUP.md](SETUP.md) is the step-by-step guide for getting this
> running on your own machine. The rest of this file is a reference for how the
> pieces work.

```
   speak or type  →  agent (tools)  →  spoken reply
                        ↓
        Home Assistant · MQTT · ComfyUI · Alexa · shell · host stats
```

## Quick start

**Windows** — double-click `start.cmd`.

**Linux / macOS** — `./start.sh`.

Either launcher looks for a Python 3.10+ already on the machine and uses it if
there is one; if there is none, it downloads a private Python (~30 MB) and uses
that. It then creates a virtual environment, installs what is missing, starts the
app and opens your browser at <http://localhost:8070>. The first run downloads
the voice model (~80 MB).

Everything — runtime, packages, models, settings, chat history — stays inside the
app folder, so the folder can be copied to a USB stick and run anywhere. See
[SETUP.md](SETUP.md) for the full guide.

Both launchers hand off to `scripts/bootstrap.py`, which owns the whole
startup pipeline. The launcher's only job is to find a Python, because a machine
with no Python cannot run the pipeline at all.

Nothing else is required to get a working agent. She starts in *offline mode* —
she can talk and be spoken to, but has no language model behind her until you
point her at one.

## Giving her a brain

RexenIOT talks to any OpenAI-compatible endpoint, so you can use whichever local
runtime you already prefer. Open the admin panel at `/admin` → **Model Backend**
and press **Detect local models**: RexenIOT probes the usual ports and fills the
form in with whatever answers, so you do not have to remember a URL or a model
name. Detection only reads — press **Save** to accept it.

| Runtime | Base URL | Notes |
|---|---|---|
| **Ollama** | `http://localhost:11434` | Easiest. `ollama pull qwen3:8b` |
| **LM Studio** | `http://localhost:1234/v1` | Load a model, start the server |
| **llama.cpp** | `http://localhost:8080/v1` | Point at your `llama-server` |
| **vLLM** | `http://localhost:8000/v1` | GPU throughput |
| Hosted API | `https://api.openai.com/v1` | Works, but nothing stays local |

Provider detection is automatic from the URL, so pasting a base URL is usually
enough. Keys are read from `REXENIOT_API_KEY` (or `OPENAI_API_KEY` /
`ANTHROPIC_API_KEY`) in preference to the config file, so secrets can stay in
the environment.

If nothing is listening yet, the panel says so and lists the ports it tried.

### When the settings are managed by NixOS

On NixOS the module can declare the model instead of leaving it to the panel. If
you set `services.rexeniot.llm.provider`, `llm.baseUrl` or `llm.model`, those
three are re-applied on every service start, so a change made in the panel is
reverted the next time the service restarts. That is deliberate — it is what
makes the model selection declarative.

The practical consequence: if you configured the model in Nix, editing it in the
panel appears to work and then silently reverts later. Change it in the Nix
configuration and rebuild instead:

```nix
services.rexeniot.llm = {
  provider = "openai";
  baseUrl = "http://127.0.0.1:8080/v1";
  model = "qwen3-8b";   # must match a key in llama-swap's `models` attribute
};
```

Leave those options unset to let the panel own the setting. Every other field
(temperature, voice, wake words) is preserved either way.

The **Detect local models** button is still useful in this case: it tells you the
exact model name to put in the Nix file.

## Hardware scan and model recommendations

The admin panel opens on **Hardware & Models**. Press **Scan this machine** and
RexenIOT probes:

- CPU brand, physical and logical cores, AVX2 support
- total and free memory
- free disk on the data volume
- GPU: `nvidia-smi` (with VRAM and compute capability), `rocm-smi`, Apple
  silicon, and `wmic` on Windows
- which runtimes are importable: ONNX Runtime, PyTorch, transformers, MQTT

She then scores every model in the catalog against that profile and reports
which fit, which do not, and why not. **Apply recommendations** sets the speech
tier and (if unset) a language model, then loads the new speech model
immediately.

Recommendations scale with the machine:

| Machine | Speech tier | Voices | Language model |
|---|---|---|---|
| 4-core CPU, 8 GB | Mini (80M) | 8 | Qwen2.5 3B |
| CPU, 32 GB | Mini (80M) | 8 | Qwen3 8B |
| **RTX 5070 (12 GB), 32 GB** | **Full 1.7B** | **47 + cloning** | **Qwen3.8 27B** |

The reasoning: the ONNX tiers need no PyTorch and stay fast on any CPU, so they
are the default. The 1.7B model is a large download and much slower per
character on a CPU, so it is only recommended when a GPU is present to carry it.
Add a GPU later, rescan, and RexenIOT upgrades herself.

**On Qwen3.8:** it is the newest Qwen generation, with vision, tool calling and
a thinking mode. It also only ships at **27B** — 18 GB of weights in the default
Q4_K_M quant, and no small tag. An RTX 5070 has 12 GB of VRAM, so the model
loads partly on the card and spills the rest into the 32 GB of system RAM, which
is exactly why 32 GB is the recommended floor. It runs, but at a few tokens per
second rather than the hundreds a resident model manages; the admin panel says
so under the entry instead of presenting it as a free fit. For fast, fully
GPU-resident replies, `qwen3:8b` is the better everyday pick on the same card.

## Speech

Speech comes from KittenTTS, wrapped in `app/tts/engine.py`. Two families load
through the same interface:

| Tier | Model | Size | Voices | Needs |
|---|---|---|---|---|
| nano | kitten-tts-nano-0.8-int8 | 25 MB | 8 | CPU |
| micro | kitten-tts-micro-0.8 | 41 MB | 8 | CPU |
| mini | kitten-tts-mini-0.8 | 80 MB | 8 | CPU |
| full | kitten-tts-2 | 506 MB | 47 + cloning | PyTorch |

Measured on a 4-core CPU with the mini model: **5 seconds of audio in 1.0
seconds** (0.20 real-time factor), so replies speak faster than they play.

Generated audio is cached on disk, keyed by text, voice, speed and emotion, so
repeated phrases — greetings, confirmations — are instant.

### Running the speech on a GPU

The default install is CPU-only on purpose: `onnxruntime` is small and works
everywhere. Acceleration is opt-in, and which package you need depends on the
tier you run.

| Tier family | Runtime | GPU package |
|---|---|---|
| nano / micro / mini (ONNX) | onnxruntime | `requirements-gpu.txt`, or `requirements-directml.txt` on Windows |
| full (KittenTTS 2) | torch | `requirements-full.txt`, plus a CUDA-index torch on Windows |

`requirements-gpu.txt` installs `onnxruntime-gpu`, a drop-in replacement with the
same import name and API, so nothing in the code changes. It needs an **NVIDIA**
card, because it is the CUDA build. Install it *instead of* the CPU package:

```bash
.venv/bin/python -m pip uninstall -y onnxruntime
.venv/bin/python -m pip install -r requirements-gpu.txt
```

On Windows there is a second route that needs no CUDA toolkit and reaches **any**
DirectX 12 GPU — NVIDIA, AMD or Intel. It is the answer for an AMD or Intel card,
which `onnxruntime-gpu` cannot help, and for an NVIDIA card whose CUDA build will
not load:

```bash
.venv/bin/python -m pip uninstall -y onnxruntime
.venv/bin/python -m pip install -r requirements-directml.txt
```

**Windows and the full tier.** PyPI publishes a CUDA-linked `torch` wheel for
Linux and a **CPU-only** one for Windows and macOS at the same version number. So
`pip install -r requirements-full.txt` on Windows gives you a torch that cannot
touch your GPU, and reinstalling it changes nothing. On Windows, replace torch
from the CUDA index afterwards — keep this step last:

```bash
.venv\Scripts\python.exe -m pip install -r requirements-full.txt
.venv\Scripts\python.exe -m pip install torch torchaudio --upgrade --index-url https://download.pytorch.org/whl/cu130
```

`--index-url` rather than `--extra-index-url` is deliberate: PyPI's torch is a
newer version than the CUDA index's, so an extra index would lose the resolution
and quietly give you the CPU wheel back. cu130 carries torch 2.14.1 and ships
sm_120 kernels, so it covers RTX 50-series (Blackwell) and older cards. The
Hardware & Models tab prints the exact command for the card it detects.

`tts.device` controls the target:

| Value | Behaviour |
|---|---|
| `auto` (default) | Hands the choice to the backend, which picks a GPU when it can reach one |
| `cpu` | Always CPU |
| `cuda` | Requires the GPU; falls back to `auto` with a log warning if it is unreachable |

Under `auto` the engine passes neither `device` nor `backend` and lets the
library decide. That is already GPU-aware in both families: the torch loader
selects `cuda` when `torch.cuda.is_available()`, and the ONNX loader forwards an
empty provider list, which onnxruntime reads as "use every available provider in
default precedence" — so installing `onnxruntime-gpu` is enough to get CUDA
without changing this setting.

Changing it takes effect on the next engine load, or immediately from **Reload
engine** in the admin panel.

The admin panel's **GPU acceleration** card reports what is *actually* usable,
which is not the same as what is present: a machine can have a GPU that the
installed runtime cannot reach. When that happens the card prints the exact
command to fix it — and distinguishes a CPU-only torch install from a CUDA build
that simply cannot see the driver.

### Speech input

Dictation has two paths, chosen in the **Voice** tab of the admin panel:

- **On this machine** (the default). The browser records a short clip and posts it
  to `/api/transcribe`, where a local faster-whisper model turns it into text.
  Nothing is sent to a third party, and any browser works — not just Chrome.
- **Browser recogniser.** The browser's own `SpeechRecognition` does the work.
  This needs Chrome or Edge.

The launcher installs the dictation set on a normal run, so the first path works
out of the box. Skip it with `REXENIOT_NO_STT=1`, or install it later from the
admin panel or with `pip install -r requirements-stt.txt`.

Press and hold the microphone button, or hold the space bar.

## Personalities

A personality is a selectable character: a system prompt, a voice, delivery
settings and an accent colour. Ten ship built in, all female by default:

| Personality | Character | Voice |
|---|---|---|
| **Sister RexenIOT** | The default voice of the house | Luna |
| The Matron | Warm, maternal, unflappable | Bella |
| Vanguard | Crisp tactical operator | Kiki |
| Juno | Upbeat companion | Rosie |
| Vigil | Watchful house guardian | Bella |
| Oracle | Patient analytical explainer | Luna |
| Prism | High-energy creative partner | Kiki |
| Muse | Contemplative creative | Luna |
| Mentor | Encouraging teacher | Bella |
| Plain | No character, maximum clarity | Kiki |

The admin can edit any of them, duplicate them, or write new ones from scratch.
Users pick between the enabled ones in the chat header; switching clears the
conversation, since a different character deserves a clean slate.

Each personality carries a **Chat initials** field, which sets the letters shown
on the assistant's avatar in the message list. Leaving it blank derives them
from the name: one letter per word when the name has several words
(`Sister RexenIOT` → `SR`), or the first two letters of a single word
(`Sister` → `SI`). Values are stripped to alphanumerics and capped at two
characters, so `9x!` becomes `9X`. The resolved value is exposed as the
read-only `avatar` field on every personality payload, which is what the chat
renders.

Each personality's `traits` field is the personality prompt. It is layered on a
shared base prompt that keeps the assistant safe and concise regardless of
character. The app-level **gender** setting is appended too, so a
weakly-steered local model does not drift into a neutral voice.

## Tools

RexenIOT can act, not just talk. Tools are grouped by integration and degrade
independently — a missing one disables only its own tools.

| Tool | Does | Requires |
|---|---|---|
| `home_assistant_states` | List entities and states | HA URL + token |
| `home_assistant_call_service` | Lights, climate, scenes, locks | HA URL + token |
| `mqtt_publish` | Publish to an MQTT topic | Broker host |
| `generate_image` | Text-to-image via ComfyUI | ComfyUI URL |
| `comfyui_status` | Check ComfyUI reachability | — |
| `system_status` | CPU, memory, disk, battery, GPU | — |
| `list_processes` | Top processes by CPU or memory | — |
| `run_shell_command` | Run a command on the host | Explicit opt-in + allowlist |
| `fetch_url` | Read a page or call a JSON API | Domain allowlist |
| `set_timer` | Delayed reminder, announced aloud | — |
| `remember` / `recall` | Durable facts about you and the home | — |
| `share_file` | Send a file to the chat as a preview or download | — |

Alexa tools are added by the Alexa skill, and only when a provider is set:

| Tool | Does | Requires |
|---|---|---|
| `alexa_devices` | List reachable Echo devices | An Alexa provider |
| `alexa_speak` | Announce out loud on a device (one-way) | An Alexa provider |
| `alexa_command` | Send a live voice command — smart home, music, timers | `alexacli` |
| `alexa_ask` | Ask Alexa and read her answer back | `alexacli` |
| `alexa_routine` | List or fire one of her routines | `alexacli` / Voice Monkey |

`run_shell_command` is off by default and refuses anything whose first word is
not on the admin's allowlist.

## Files

Both directions are supported. Drag a file onto the composer, paste it, or use
the attach button, and RexenIOT receives it with a short summary of what it is.
Uploads are classified as `text`, `image`, `pdf`, `archive`, `video`, `audio` or
`binary`. Images, audio and video render inline with a preview and a download
link; text is inlined into the prompt so she can read it directly, and the rest
are offered as downloads.

In the other direction, `share_file` puts a host file into the chat. Anything
that can be previewed in a browser shows up as a player or an image, with a
download link alongside. ComfyUI's `generate_image` shares its output
automatically, so a generated picture appears in the conversation without being
asked for.

Each upload is served back through `GET /api/uploads/{id}/raw`, which supports
HTTP range requests, so audio and video scrub rather than downloading whole.
Unknown ids and path traversal are rejected. Uploads live under
`data/uploads/<session>/`; see `app/uploads.py`.

## Skills

Skills are bundles of capability the admin switches on and off, in the style of
Alexa skills. A skill carries the instructions RexenIOT follows while it is active
and claims the tools that go with it, so turning one off removes both — her
prompt stops mentioning the capability and the tools disappear from the model's
list, rather than being offered and failing.

Built-ins: **Home**, **Computer**, **Web**, **Memory**, **Timers**, **Images**
and **Alexa**. Add your own from the admin panel with a name, an instruction
block and the tools it owns. Layout: `app/skills.py`.

A skill can also declare a `provider_field`. The Alexa skill is available only
once a provider is configured, so an unconfigured bridge never shows the model
tools that could only fail.

## Alexa

Amazon publishes no API that lets another assistant command an Echo or make
Alexa speak. RexenIOT therefore drives a *bridge*, and you choose which:

| Provider | Can do | Notes |
|---|---|---|
| **Voice Monkey** | Announce, fire routines | Hosted HTTP API. No live voice command, no reply. |
| **alexacli** | Announce, command, ask, routines | Local CLI on the unofficial Alexa API. Refresh token lasts ~2 weeks. |
| **Home Assistant** | Announce | Via the Alexa Media Player integration, through the HA tools. |

Set the provider under **Home & tools → Alexa**. The Alexa skill stays dormant
until a provider is set, and each tool says plainly when the chosen provider
cannot do what was asked instead of pretending it succeeded.

alexacli requires a device for `speak`, `command` and `ask`, so set a **default
device** there too — run `alexa_devices` to see the names it accepts. `alexa_speak`
can also broadcast to every Echo at once with `announce: true`.

Both Voice Monkey and alexacli rest on unofficial interfaces and can break when
Amazon changes them. Home Assistant's Alexa Media Player is the steadiest of the
three, at the cost of being announce-only. Alexa routines are marked
work-in-progress in alexacli itself, so treat `alexa_routine` as best-effort.

## Task routing

Different jobs suit different backends, so RexenIOT routes by intent:

- **Conversation, reasoning, tool calls** → your chat endpoint (Ollama, LM
  Studio, llama.cpp, vLLM, hosted)
- **Image and 3D generation** → ComfyUI
- **Speech** → KittenTTS
- **Echo devices** → the configured Alexa bridge

The router is keyword-based, so it works with no model loaded. Ask
`POST /api/route` to see where a given message would go. The admin panel shows
each backend's readiness.

## Layout

```
start.cmd / start.sh     launchers: find Python, then hand off to bootstrap.py
scripts/bootstrap.py     the shared startup pipeline both launchers call
requirements.txt         runtime dependencies
app/
  main.py                FastAPI app and static serving
  config.py              typed config, JSON persistence, secret masking
  hardware.py            hardware probes, model scoring and recommendations
  model_catalog.py       every downloadable model and its requirements
  personalities.py       personality templates and CRUD
  skills.py              skill templates, tool gating and prompt sections
  agent.py               the tool-calling loop, streaming and non-streaming
  routing.py             per-task backend selection
  uploads.py             attachment ingest, classification and artifact serving
  tts/engine.py          KittenTTS wrapper: lazy load, tiers, cache
  llm/client.py          OpenAI / Anthropic / Ollama / offline
  tools/                 tool registry, built-ins, ComfyUI, Alexa
  routers/               chat, admin, status
web/
  index.html             the HUD
  admin.html             control room
  css/  js/              styles and client logic
data/                    config, personalities, skills, models, cache, log
```

`scripts/` holds the launcher's support scripts plus the check scripts used
during development (`test_tts_device.py`, `test_recommend.py`,
`test_admin_gpu.py`, `test_attach_ui.py` and friends); run them with
`PYTHONPATH=. .venv/bin/python scripts/<name>.py`.

## API

| Route | Purpose |
|---|---|
| `POST /api/chat` | One turn, returns reply plus optional speech |
| `POST /api/chat/stream` | Same, streamed as SSE |
| `POST /api/speak` | Text → WAV |
| `POST /api/uploads` | Attach files to a turn |
| `GET  /api/uploads/{id}/raw` | Serve an upload (range requests supported) |
| `GET  /api/voices` | Available voices and engine state |
| `GET  /api/status` | Model, speech, tools, active personality |
| `GET  /api/routing` | Backend readiness |
| `POST /api/route` | Explain where a message would route |
| `POST /api/admin/hardware/scan` | Probe hardware, return recommendations |
| `POST /api/admin/hardware/apply` | Adopt the recommendations |
| `GET/POST/PUT/DELETE /api/admin/personalities` | Personality CRUD |
| `GET/POST/PUT/DELETE /api/admin/skills` | Skill CRUD |
| `POST /api/admin/skills/restore` | Re-add missing built-in skills |
| `POST /api/admin/integrations/test` | Probe one integration (`alexa` included) |

Interactive docs at `/docs`.

## Configuration

`data/config.json` holds everything the admin can change. Secrets are never
written to it by the server unless you type them in, and they are masked as
`***` on the way out.

Environment variables take precedence:

| Variable | Effect |
|---|---|
| `REXENIOT_API_KEY` | API key for the chat backend |
| `ADMIN_TOKEN` | Require this token for every admin write |
| `HOME_ASSISTANT_TOKEN` | Home Assistant token |
| `MQTT_PASSWORD` | MQTT password |
| `COMFYUI_URL` | ComfyUI address |
| `ALEXA_PROVIDER` | Alexa bridge: `voicemonkey`, `alexacli` or `home_assistant` |
| `VOICEMONKEY_TOKEN` | Voice Monkey API token |
| `ALEXACLI_PATH` | Full path to the `alexacli` binary |
| `ALEXACLI_DEVICE` | Default Echo device for alexacli |

Set `ADMIN_TOKEN` if the app is reachable by anyone but you — binding to
`0.0.0.0` exposes it to your network.

## Requirements

- Python 3.10 or newer
- ~400 MB of disk for the environment, plus model downloads
- No GPU required. A GPU unlocks the 1.7B speech model, GPU speech synthesis,
  and faster local LLMs.

## Notes and limitations

- The 1.7B speech tier needs the PyTorch stack (~2.5 GB). The launcher does not
  install it; the hardware scan tells you when it would be worth it, and
  `requirements-full.txt` installs it when you decide to.
- GPU speech is opt-in. `requirements-gpu.txt` swaps the CPU ONNX Runtime for the
  GPU build; see [Running the speech on a GPU](#running-the-speech-on-a-gpu). A
  GPU the installed runtime cannot reach falls back to CPU rather than failing.
- Emotion tags are early beta in KittenTTS and steer delivery rather than
  guaranteeing it.
- The task router is keyword-based. It is honest about its confidence, and
  falls back to chat when ComfyUI is not configured.
- Alexa has no official control API. Every provider above uses an unofficial
  interface or a hosted bridge, so treat it as convenient rather than durable.
  Home Assistant's Alexa Media Player is the most stable, and is announce-only.
- The full-screen button is hidden on iOS Safari, which exposes the Fullscreen
  API for video only. The TV and kiosk layouts still scale, and Home Screen
  bookmarking gives a chromeless window instead.
- `color-mix()` and `dvh` need a fairly recent browser. Where they are missing
  the stylesheet has flat-palette fallbacks and a JavaScript-measured viewport
  height, so the HUD renders and the composer stays on screen.
- Models are licensed separately from the KittenTTS code. Check the licence on
  each model repository before commercial use.

## Credits

Speech by [KittenTTS](https://github.com/KittenML/KittenTTS) (Apache-2.0 code;
model weights carry their own licences).

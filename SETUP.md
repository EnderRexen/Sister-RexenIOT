# Sister RexenIOT — Setup Guide

This guide takes you from a fresh download to a working voice assistant you can
talk to. It is written for a Windows PC with an **NVIDIA RTX 5070 (12 GB) and
32 GB of RAM**, which is a strong target for this app, but every step also works
on a weaker machine — the app detects your hardware and adjusts.

You do not need to know Python or edit code. Everything is configured through the
web page.

There is one launcher per platform:

| File | Use it on |
|------|-----------|
| `start.cmd` | Windows (double-click this) |
| `start.sh` | Linux / macOS |

You do not need to install Python first. Each launcher looks for a Python 3.10+
already on the machine and uses it if there is one; if there is none, it
downloads a private Python (~30 MB) and uses that instead. Either way it keeps
everything — runtime, packages, models, settings, chat history — inside the app
folder, so the folder can be copied to a USB stick and run on a machine that has
never had Python installed. The first run downloads more (~330 MB), and
afterwards `REXENIOT_OFFLINE=1` runs it with no network at all.

Both launchers do the same six things — check Python, create a private
environment, install dependencies, check configuration, prepare speech, and start
the app — and both share one implementation of those steps,
`scripts/bootstrap.py`. The launcher only finds a Python; the pipeline lives in
one file, so the two cannot drift apart.

---

## 1. What you need first

**Required**

- **Python 3.10 or newer is recommended but not required.** If you already have
  it, download from <https://www.python.org/downloads/> and **tick "Add
  python.exe to PATH"** during install. If you do not, the launcher downloads a
  private Python for you on first run (~30 MB).
- About **3 GB of free disk space** for the app plus a small model, or **~25 GB**
  if you plan to run a large local model.

**Recommended for your hardware**

- An **NVIDIA driver** installed (the RTX 5070 needs a recent one). You do not
  need to install CUDA yourself — the app's GPU packages bring what they need.
- **Ollama** or **LM Studio** for the "brain". This is the one part the app
  cannot install for you; see [section 4](#4-give-her-a-brain-the-important-part).

**Not required**

- No Docker, no Node.js, no database. The app stores its settings in plain JSON
  files under `data/`.

---

## 2. Start it

1. Unzip the folder somewhere you can write to — for example
   `C:\RexenIOT`. Avoid `C:\Program Files`, which blocks writes.
2. Double-click **`start.cmd`**. If there is no Python on the machine, it
   downloads one for you — no separate step needed.
3. A black window opens and works through six steps. **The first run is slow**
   (roughly 3–10 minutes): it downloads Python packages and then the voice model
   (~80 MB). Later runs skip all of it and start in seconds.
4. Your browser opens **<http://localhost:8070/>** automatically.

Leave the black window open while you use the app. Closing it stops the server.
Press `Ctrl+C` in that window to shut down cleanly.

> **Port already in use?** If something else uses port 8070, the launcher warns
> you. You can change the port in the admin panel (Server tab) or by setting the
> `REXENIOT_PORT` environment variable before starting.

### If the launcher stops with an error

| Message | What to do |
|---------|-----------|
| `Python was not found on PATH` | Re-run the Python installer and tick "Add python.exe to PATH". Then open a **new** Command Prompt and try again. |
| `Python 3.10 or newer is required` | Install a newer Python from python.org. |
| `Could not create the virtual environment` | Install the venv module: `sudo apt install python3-venv` (Linux). On Windows, re-run the Python installer and choose "Modify" → ensure `pip` and `venv` are included. |
| `Dependency installation failed` | Run the command the launcher prints — `".venv\Scripts\python.exe" -m pip install -r requirements.txt` — to see the real error. Usually a temporary network problem. |

---

## 3. First run: the admin panel

The app opens on the **chat page**. Click the **gear icon** (top right) to open
the **admin panel** at <http://localhost:8070/admin>.

The admin panel has seven tabs. On a first run, visit them in this order:

1. **Hardware & Models** — press **Scan**. The app reads your CPU, RAM, disk and
   GPU, then recommends what your machine can run well.
2. **Model Backend** — where you point the app at a "brain". See section 4.
3. **Voice** — the speech engine. It works out of the box; see section 5.
4. **Personalities** — the characters you can switch between. See section 6.
5. **Home & Tools** — connect lights, plugs and other devices. See section 7.
6. **Skills** — bundles of extra capability you can toggle on and off.
7. **Server** — port, host and the admin password.

### Set an admin password

In the **Server** tab, set an **admin token**. This protects every change you make
in the admin panel. Until you set one, the panel is open to anyone who can reach
the address — fine on a home PC, not fine on a shared network.

> **Important:** if you set a token, the panel asks for it on the next write. Keep
> it somewhere safe; there is no recovery, but you can edit `data/config.json` by
> hand if you get locked out.

---

## 4. Give her a brain (the important part)

Out of the box, the app has **no language model**. The chat page, voice,
personalities and all 17 tools work, but she cannot answer questions until you
connect a model. This is deliberate — models are large, and you should choose
which one to run.

### Step 1 — Install a model runner

Pick **one**. Both are free and run entirely on your PC.

**Option A — Ollama** (simplest, recommended)

1. Download from <https://ollama.com/download> and install.
2. Open a Command Prompt and pull a model:
   ```
   ollama pull qwen3:8b
   ```
3. Ollama runs in the background on port **11434**. Leave it running.

**Option B — LM Studio** (graphical, good for browsing models)

1. Download from <https://lmstudio.ai/> and install.
2. Search for a model, download it, then start the **local server** (the
   `<->` icon). It listens on port **1234** by default.

### Step 2 — Point the app at it

In the admin panel, open the **Model Backend** tab and fill in:

| Field | For Ollama | For LM Studio |
|-------|-----------|---------------|
| **Provider** | `auto` | `auto` |
| **Base URL** | `http://localhost:11434` | `http://localhost:1234/v1` |
| **Model** | `qwen3:8b` | the model name LM Studio shows |

Leave **Provider** as `auto` — the app reads the address and works out which
service it is talking to. Click **Test** / **Save**, then send a message on the
chat page.

### Which model should you run?

Your RTX 5070 has **12 GB of VRAM** and you have **32 GB of RAM**. That is a good
position. Run the **Hardware & Models** scan and the app will rank the options for
you, but in short:

| Model | Fits how | Expect |
|-------|----------|--------|
| `qwen3:8b` | Entirely in VRAM | Fast, smooth. **Best starting point.** |
| `qwen2.5:14b-instruct-q4_K_M` | Mostly in VRAM | Stronger reasoning, still quick. |
| `qwen3.8:27b` | Split: VRAM + RAM | Best answers, but only a few words per second. |

Start with `qwen3:8b`. Once everything works, try the 27B model if you want
better answers and can accept slower replies. The app is honest about this: the
hardware scan marks the 27B model with a note that it "spills to RAM" rather than
pretending it fits.

### Using a cloud model instead

If you would rather not run a model locally, set **Provider** to `openai` or
`anthropic`, enter the matching **Base URL** and **API key**, and name a model.
Keys can also be supplied through environment variables (`OPENAI_API_KEY`,
`ANTHROPIC_API_KEY`) so they are never written to disk. Note that a cloud model
means your conversations leave your machine.

### A note on your sandbox / hosted preview

If you are viewing this app through a hosted preview rather than your own PC, the
configured address `http://localhost:8080/v1` refers to the *preview machine*, not
your computer. Nothing will be listening there and replies will fail. Run the app
locally with `start.cmd` to use a real model.

---

## 5. Speech

Speech works without any setup. On first start the app downloads a small voice
model (~80 MB, one time only).

The launcher prepares speech **before** the app starts: it checks that the
packages the configured tier needs are installed, loads the model, and speaks one
test line in the active personality's voice. You will see:

```
[5/6] Preparing speech engine...
      the first run downloads the voice model (~80 MB, one time only)
      loading it and speaking a test line (this can take a minute) ...
      model:  KittenML/kitten-tts-mini-0.8
      persona: Sister RexenIOT (voice Luna)
      speech ready: 3.2s of audio at 24000 Hz
```

That means the first spoken reply will be immediate rather than pausing to
download or load. If the step fails it says so and **the app still starts** — the
Voice tab in the admin panel can repair speech afterwards. A failure never blocks
the launch.

### Voice quality tiers

The **Voice** tab lets you choose how much quality you want:

| Tier | Size | Voices | Needs | Notes |
|------|------|--------|-------|-------|
| Nano | 25 MB | 8 | ONNX only | Fastest. Slightly robotic. |
| Micro | 41 MB | 8 | ONNX only | Good balance. |
| **Mini** | **80 MB** | **8** | **ONNX only** | **Default. Best quality that still runs on any CPU.** |
| Full (KittenTTS 2) | 506 MB | 47 | PyTorch stack (~2.5 GB) | Best voices, cloning and emotion tags. Much slower on CPU. |

The three ONNX tiers need **no PyTorch** — the launcher installs the speech
package with `--no-deps` and lists the ONNX requirements explicitly, which keeps a
working install near 300 MB instead of 2.5 GB.

The hardware scan recommends the **Full** tier only when a GPU is present **and**
the PyTorch stack is already installed, so it never suggests a tier the app cannot
load. To opt in deliberately:

```
".venv\Scripts\python.exe" -m pip install -r requirements-full.txt
```

Then set the model to `KittenML/kitten-tts-2` on the Voice tab.

### Making speech faster on your RTX 5070

By default the app installs the **CPU** speech runtime. It works everywhere, but
it does not use your graphics card. To use the GPU, replace it with the GPU build
— install one **instead of** the other, never both:

```
".venv\Scripts\python.exe" -m pip uninstall -y onnxruntime
".venv\Scripts\python.exe" -m pip install -r requirements-gpu.txt
```

`requirements-gpu.txt` is the CUDA build, so it needs an NVIDIA card. If your card
is AMD or Intel, or the CUDA build refuses to load, Windows has a second route
that reaches any DirectX 12 GPU with no CUDA toolkit:

```
".venv\Scripts\python.exe" -m pip uninstall -y onnxruntime
".venv\Scripts\python.exe" -m pip install -r requirements-directml.txt
```

The **Hardware & Models** tab tells you whether the GPU is actually usable. If it
says the runtime cannot reach your GPU, one of those two is the fix.

### The full tier on Windows needs one extra step

PyPI gives Windows a **CPU-only** `torch` wheel. It is the same version number as
the Linux CUDA build, so `pip install -r requirements-full.txt` on Windows
installs a torch that cannot use your RTX 5070 — and running it again changes
nothing. After the requirements install, replace torch from the CUDA index. Keep
this step last:

```
".venv\Scripts\python.exe" -m pip install -r requirements-full.txt
".venv\Scripts\python.exe" -m pip install torch torchaudio --upgrade --index-url https://download.pytorch.org/whl/cu130
```

The index has to be restricted with `--index-url`. PyPI's torch is a newer version
than the CUDA index's, so `--extra-index-url` would lose the resolution and hand
you the CPU wheel again. cu130 carries torch 2.14.1 and includes sm_120 kernels,
which is what RTX 50-series (Blackwell) cards need. The admin panel prints the
exact command for whatever card it finds, so you can copy it from there.

### Voice input (talking to her)

Voice input has two paths, set under **Capture** in the **Voice** tab.

**On this machine** is the default. The browser records a short clip and posts it
to the server, which transcribes it locally with faster-whisper. Your audio never
reaches a third party, and any browser works. The launcher installs the small
speech-to-text set on a normal run (~100 MB, one time); skip it with
`REXENIOT_NO_STT=1` and dictation falls back to the browser recogniser.

**Browser recogniser** uses the browser's own `SpeechRecognition`. It needs
Chrome or Edge, and sends the audio to that vendor. Choose it if you prefer not
to install anything else.

- Press the **microphone** button to dictate a message.
- **Wake word mode** lets you say **"hey rexeniot"** (or "okay rexeniot",
  "hey sister", "wake up rexeniot") hands-free. She answers "I'm here." and keeps
  listening until you say **"stop listening"**, "go to sleep", "goodbye rexeniot"
  or similar, or until 45 seconds pass with no speech.

Wake word mode uses the browser recogniser, so it needs Chrome or Edge.

Both the wake words and the sleep words are editable in the **Voice** tab.

---

## 6. Personalities

The app ships with **10 personalities**. You pick one from the **picker in the
chat header**; the admin decides which are available.

Each personality is a bundle of *behaviour*, not just a name: its own system
prompt (how she thinks and speaks), its own voice, delivery settings and colours
in the interface.

| Personality | Voice | Character |
|-------------|-------|-----------|
| Sister RexenIOT | Luna | The voice of the house — the primary character. |
| The Matron | Bella | Warm, maternal, unflappable. |
| Vanguard | Kiki | Crisp tactical operator. |
| Juno | Rosie | Upbeat companion. |
| Vigil | Bella | Watchful house guardian. |
| Oracle | Luna | Patient analytical explainer. |
| Prism | Kiki | High-energy creative partner. |
| Muse | Luna | Contemplative creative. |
| Mentor | Bella | Encouraging teacher. |
| Plain | Kiki | No character, maximum clarity. |

**To create your own:** open the **Personalities** tab, duplicate any entry, then
edit its name, tagline, description, system prompt and voice. You can also write
one from scratch. Toggle **enabled** off to hide a personality from the chat
picker without deleting it.

All built-ins are female characters, matching the app's default. You can add
male or neutral characters — there is a gender setting on each personality.

---

## 7. Connecting your home

The **Home & Tools** tab connects the assistant to real devices. All of it is
optional; skip what you do not have.

### Home Assistant (recommended for lights, plugs, sensors)

1. In Home Assistant, create a **long-lived access token** (Profile → Security).
2. In the admin panel, enter the **Home Assistant URL** (e.g.
   `http://homeassistant.local:8123`) and paste the **token**.
3. Ask her something like *"what lights are on?"* or *"turn off the office lamp."*

### MQTT (for devices that do not speak Home Assistant)

Enter your broker's **host**, **port** (default 1883) and credentials. This covers
ESPHome, Tasmota, Zigbee2MQTT and similar setups.

### ComfyUI (images and 3D models)

ComfyUI handles picture and 3D requests, which a chat model cannot do. Install
ComfyUI, start it, then set its URL in the admin panel. After that you can say
*"draw me a picture of a lighthouse at dusk"* and the app routes it automatically.

### Alexa

Alexa has no official way to be commanded, so the app supports three routes. Pick
whichever you can set up:

| Provider | What it can do |
|----------|---------------|
| `voicemonkey` | Announcements and routines (hosted HTTP bridge). |
| `alexacli` | Announce, command, ask and routines (local, unofficial API). |
| `home_assistant` | Announcements via the Alexa Media Player integration. |

Set the provider in the **Home & Tools** tab. The **Skills** tab then shows which
Alexa abilities are live, so you are never promised something the provider cannot
do.

### Running commands on the computer

The `run_shell_command` tool is **off by default** and should stay off unless you
have a specific reason. If you enable it, it is restricted to an allowlist
(`echo`, `dir`, `ls`, `whoami` by default). Anything the assistant runs has your
full user permissions.

---

## 8. Using it day to day

- **Chat page:** <http://localhost:8070/>
- **Admin page:** <http://localhost:8070/admin>
- **Stop the app:** `Ctrl+C` in the launcher window, or close it.
- **Restart:** double-click `start.cmd` again. Your settings and downloaded models
  are reused, so it starts in seconds.

To reach the app from your **phone or another device** on the same network, set
the **host** to `0.0.0.0` in the Server tab (or pass `--host 0.0.0.0`). Then browse
to `http://<your-pc-ip>:8070/`. **Set an admin token first** if you do this — it
exposes the panel to your network.

The interface is built to work on a phone, including in portrait.

---

## 9. Troubleshooting

| Symptom | Cause and fix |
|---------|--------------|
| **She replies with an error or nothing** | No model is reachable. Check Ollama/LM Studio is running and the **Base URL** matches its port. Test with `curl http://localhost:11434/api/tags` (Ollama) or open the Model Backend tab's test button. |
| **Speech is silent** | Check the **Voice** tab shows the model as loaded. The first run downloads it; a slow connection can take a few minutes. Also confirm the browser tab is not muted. |
| **Voice input does nothing** | Speech recognition needs Chrome or Edge. Firefox and Safari do not support it. |
| **Wake word never triggers** | Allow microphone access for the page, and keep the tab in the foreground — browsers suspend recognition in background tabs. |
| **"GPU detected, but the installed runtime cannot reach it"** | Install the GPU speech runtime (section 5). |
| **Speech is slow** | You are on the CPU runtime. Install `requirements-gpu.txt`, or drop to a smaller voice tier. |
| **Replies are very slow** | The model is too large for your VRAM and is spilling into RAM. Try `qwen3:8b`, or accept it for better answers. |
| **Port 8070 already in use** | Close the other process, or set `REXENIOT_PORT` before starting. |
| **Tools fail with "not configured"** | That integration has no credentials yet — see section 7. She reports this honestly rather than pretending success. |
| **A skill disappeared after an update** | Built-in skills and personalities are re-seeded on load if missing. Your own edits are never overwritten. |

Logs are written to `data/rexeniot.log` and also printed in the launcher window.
The admin panel's Hardware tab shows a live status of the backend.

---

## 10. Reference

### Where settings live

Everything the admin panel changes is stored in **`data/config.json`** (plain
JSON, safe to back up or edit by hand). Personalities and skills live in
`data/personalities.json` and `data/skills.json`. None of these are committed to
version control, and none contain your API keys unless you typed one in.

### Environment variables

Useful for automation or keeping secrets out of files. Set these **before**
starting the app.

| Variable | Purpose |
|----------|---------|
| `REXENIOT_PORT` | Port to serve on (default 8070). |
| `REXENIOT_HOME` | Store `data/` somewhere other than the app folder. |
| `REXENIOT_PORTABLE` | Keep all state inside the app folder. |
| `REXENIOT_API_KEY` | Model API key (overrides the stored one). |
| `ADMIN_TOKEN` | Admin panel password (overrides the stored one). |
| `OPENAI_API_KEY` / `ANTHROPIC_API_KEY` | Provider-specific model keys. |
| `COMFYUI_URL` | ComfyUI address. |
| `HOME_ASSISTANT_TOKEN` | Home Assistant token. |
| `MQTT_PASSWORD` | MQTT password. |
| `ALEXA_PROVIDER` | `voicemonkey`, `alexacli` or `home_assistant`. |
| `VOICEMONKEY_TOKEN`, `ALEXACLI_PATH`, `ALEXACLI_DEVICE` | Alexa bridge settings. |

Environment variables always win over values stored in `config.json`, which is how
you keep keys out of the file entirely.

### The 17 tools she can use

Home: `home_assistant_states`, `home_assistant_call_service`, `mqtt_publish`.
Alexa: `alexa_speak`, `alexa_command`, `alexa_ask`, `alexa_routine`,
`alexa_devices`. Media: `generate_media`, `comfyui_status`. Computer:
`system_status`, `list_processes`, `run_shell_command` (off by default).
Web: `fetch_url`. Memory and time: `remember`, `recall`, `set_timer`.

### Which backend handles what

The app routes each request to the right place automatically:

- **Chat, reasoning and tool calls** → Ollama, LM Studio, vLLM or a hosted API.
- **Images and 3D** → ComfyUI.
- **Speech** → KittenTTS.

Routing is keyword-based, so it works even before a model is loaded. If ComfyUI is
not configured, an image request falls back to chat and she says why.

---

## Credits

Speech is provided by **KittenTTS** — <https://github.com/KittenML/KittenTTS>.
Thank you to the KittenML team for making a small, fast, good-sounding voice
model that runs on ordinary hardware.

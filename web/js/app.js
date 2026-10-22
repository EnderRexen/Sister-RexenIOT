/* ============================================================================
   Sister RexenIOT HUD client
   - streams replies over SSE
   - speaks replies through the server TTS (KittenTTS)
   - listens through the Web Speech API
   - drives the reactor visualiser from speech state
   ========================================================================== */

import { initOnScreenKeyboard } from "./osk.js";

const $ = (sel) => document.querySelector(sel);

const state = {
  personalities: [],
  active: null,
  history: [],
  busy: false,
  muted: false,
  listening: false,
  speaking: false,
  ttsEnabled: true,
  ttsModelUsable: true,
  ttsFallbackNoted: false,
  speechSource: null,
  browserSpeaking: null,
  audioCtx: null,
  analyser: null,
  sourceNode: null,
  currentAudio: null,
  recognition: null,
  autoSend: true,
  attachments: [],
  config: null,
  osk: null,
  // Local (server) capture: the browser records a clip and posts it to
  // /api/transcribe. Used for push-to-talk when the optional speech-to-text set
  // is installed, because it works in any browser and keeps the audio here.
  recorder: null,
  recording: false,
  recordChunks: [],
  captureStream: null,
};

/* --------------------------------------------------------------- utilities */

function toast(message, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  $("#toasts").appendChild(el);
  setTimeout(() => {
    el.style.transition = "opacity .4s";
    el.style.opacity = "0";
    setTimeout(() => el.remove(), 400);
  }, 4200);
}

async function api(path, options = {}) {
  const res = await fetch(`/api${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = await res.json();
      detail = body.detail || detail;
    } catch { /* non-JSON error body */ }
    throw new Error(detail);
  }
  const type = res.headers.get("content-type") || "";
  return type.includes("json") ? res.json() : res;
}

function setReactor(mode) {
  $("#reactor").dataset.state = mode;
  const label = { idle: "IDLE", listening: "LISTENING", thinking: "PROCESSING", speaking: "SPEAKING" };
  $("#reactor-state").textContent = label[mode] || mode.toUpperCase();
}

/* ------------------------------------------------------- persona selection */

function applyAccent(hex) {
  document.documentElement.style.setProperty("--accent", hex || "#39d0ff");
}

function renderPersonas() {
  const list = $("#persona-list");
  list.innerHTML = "";
  const enabled = state.personalities.filter((p) => p.enabled !== false);
  if (!enabled.length) {
    list.innerHTML = `<li><div class="pl-body"><div class="pl-name">No personalities</div>
      <div class="pl-desc">Add one in the admin panel.</div></div></li>`;
    return;
  }
  for (const p of enabled) {
    const li = document.createElement("li");
    li.setAttribute("role", "option");
    li.setAttribute("aria-selected", String(p.id === state.active?.id));
    li.dataset.id = p.id;
    li.innerHTML = `
      <span class="pl-dot" style="background:${p.accent};box-shadow:0 0 8px ${p.accent}"></span>
      <span class="pl-body">
        <span class="pl-name">${escapeHtml(p.name)}</span>
        <span class="pl-desc">${escapeHtml(p.tagline || p.description || "")}</span>
      </span>`;
    li.addEventListener("click", () => selectPersona(p.id));
    list.appendChild(li);
  }
}

function renderActivePersona() {
  const p = state.active;
  if (!p) return;
  $("#persona-name").textContent = p.name;
  $("#persona-tag").textContent = p.tagline || "";
  $("#persona-dot").style.background = p.accent;
  $("#persona-dot").style.boxShadow = `0 0 10px ${p.accent}`;
  applyAccent(p.accent);
  $("#s-tts").textContent = `${p.voice} · ${p.speed}x`;
}

async function selectPersona(id) {
  const p = state.personalities.find((x) => x.id === id);
  if (!p) return;
  state.active = p;
  state.history = [];          // a new character means a clean conversation
  $("#messages").innerHTML = "";
  renderActivePersona();
  renderPersonas();
  togglePersonaList(false);
  await api(`/admin/personalities/${id}/activate`, { method: "POST" }).catch(() => {});
  greet();
}

function togglePersonaList(show) {
  const list = $("#persona-list");
  const open = show ?? list.hidden;
  list.hidden = !open;
  $("#persona-button").setAttribute("aria-expanded", String(open));
}

/* -------------------------------------------------------------- messaging */

function escapeHtml(text) {
  return String(text).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/** Minimal markdown: fenced code, inline code, bold, paragraphs. */
function renderMarkdown(text) {
  let html = escapeHtml(text);
  html = html.replace(/```([\s\S]*?)```/g, (_, code) => `<pre><code>${code.trim()}</code></pre>`);
  html = html.replace(/`([^`]+)`/g, "<code>$1</code>");
  html = html.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  html = html.replace(/([*_])([^*_]+)\1/g, "<em>$2</em>");
  return html.split(/\n{2,}/).map((p) => `<p>${p.replace(/\n/g, "<br>")}</p>`).join("");
}

/* ------------------------------------------------------- file previews */

/** Build the DOM node that previews one shared file.
 *
 * Images render inline, audio and video get a player, and everything else
 * (PDFs, archives, code) falls back to a download link. The raw endpoint serves
 * the bytes with the right content type, and Range support comes from the
 * server, so seeking in audio and video works without extra work here.
 */
function buildFilePreview(file) {
  const card = document.createElement("div");
  card.className = "file-card";

  const head = document.createElement("div");
  head.className = "file-head";
  const title = document.createElement("span");
  title.className = "file-name";
  title.textContent = file.name || "file";
  const meta = document.createElement("span");
  meta.className = "file-meta";
  meta.textContent = [file.kind, humanSize(file.size)].filter(Boolean).join(" · ");
  head.append(title, meta);
  card.appendChild(head);

  const url = file.raw_url || `/api/uploads/${encodeURIComponent(file.id)}/raw`;
  const downloadUrl = `${url}?download=true`;

  if (file.kind === "image") {
    const img = document.createElement("img");
    img.className = "file-image";
    img.src = url;
    img.alt = file.name || "shared image";
    img.loading = "lazy";
    img.addEventListener("click", () => window.open(url, "_blank", "noopener"));
    card.appendChild(img);
  } else if (file.kind === "audio") {
    const audio = document.createElement("audio");
    audio.className = "file-audio";
    audio.controls = true;
    audio.preload = "metadata";
    audio.src = url;
    card.appendChild(audio);
  } else if (file.kind === "video") {
    const video = document.createElement("video");
    video.className = "file-video";
    video.controls = true;
    video.preload = "metadata";
    video.src = url;
    card.appendChild(video);
  } else {
    const icon = document.createElement("div");
    icon.className = "file-generic";
    icon.textContent = `📄 ${file.kind || "file"}`;
    card.appendChild(icon);
  }

  if (file.summary) {
    const note = document.createElement("p");
    note.className = "file-note";
    note.textContent = file.summary;
    card.appendChild(note);
  }

  const actions = document.createElement("div");
  actions.className = "file-actions";
  const link = document.createElement("a");
  link.className = "file-download";
  link.href = downloadUrl;
  link.download = file.name || "file";
  link.textContent = "Download";
  actions.appendChild(link);
  card.appendChild(actions);

  return card;
}

function humanSize(bytes) {
  const n = Number(bytes) || 0;
  const units = ["B", "KB", "MB", "GB"];
  let value = n;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${unit === 0 ? value : value.toFixed(1)} ${units[unit]}`;
}

/** Attach preview cards for files to a message bubble. */
function addFilePreviews(body, files) {
  const usable = (files || []).filter((f) => f && (f.id || f.raw_url));
  if (!usable.length) return;
  const tray = document.createElement("div");
  tray.className = "file-tray";
  for (const file of usable) tray.appendChild(buildFilePreview(file));
  body.appendChild(tray);
}

function addMessage(role, text, opts = {}) {
  const wrap = document.createElement("div");
  wrap.className = `msg ${role}`;
  const avatar = document.createElement("div");
  avatar.className = "avatar";
  avatar.textContent = role === "user" ? "YOU" : (state.active?.avatar || "AI");

  const body = document.createElement("div");
  const bubble = document.createElement("div");
  bubble.className = "bubble";
  if (opts.pending) {
    bubble.innerHTML = `<span class="typing"><i></i><i></i><i></i></span>`;
  } else {
    bubble.innerHTML = renderMarkdown(text);
  }
  body.appendChild(bubble);

  // Files the user shared, or the agent produced, appear under the text so they
  // can be seen and played right in the conversation.
  if (!opts.pending && opts.files?.length) addFilePreviews(body, opts.files);

  if (role === "assistant" && !opts.pending && text) {
    const meta = document.createElement("div");
    meta.className = "msg-meta";
    const btn = document.createElement("button");
    btn.className = "speak-btn";
    btn.title = "Speak this reply again";
    btn.innerHTML = `<svg viewBox="0 0 24 24"><path d="M4 9v6h4l5 4V5L8 9H4z" fill="currentColor"/><path d="M16 8.5a5 5 0 010 7" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg>`;
    btn.addEventListener("click", () => speak(text));
    meta.appendChild(btn);
    meta.appendChild(document.createTextNode(opts.model ? `via ${opts.model}` : ""));
    body.appendChild(meta);
  }

  wrap.append(avatar, body);
  const container = $("#messages");
  container.appendChild(wrap);
  container.scrollTop = container.scrollHeight;
  return { wrap, bubble, body };
}

function addToolChip(tool, ok, preview) {
  const tray = $("#tool-tray");
  tray.hidden = false;
  const li = document.createElement("li");
  li.className = ok ? "ok" : "bad";
  li.textContent = `${tool}${ok ? "" : " failed"}`;
  li.title = preview || "";
  $("#tool-list").appendChild(li);
}

/* ------------------------------------------------------------ speech (TTS) */

/** True when the browser can synthesise speech on its own. */
function browserTtsAvailable() {
  return typeof window.speechSynthesis !== "undefined"
    && typeof window.SpeechSynthesisUtterance !== "undefined";
}

/**
 * Speak through the browser's own engine.
 *
 * This is the offline fallback, so it must not depend on the server or on any
 * of the model assets. It mirrors what the model path does around playback --
 * same reactor states, same wake-word pause -- or the microphone would hear her
 * speak and treat it as a command.
 */
function speakWithBrowser(text, opts = {}) {
  if (!browserTtsAvailable()) return false;
  stopSpeaking();
  const utter = new SpeechSynthesisUtterance(text);
  if (opts.speed) utter.rate = Math.max(0.5, Math.min(2, Number(opts.speed)));
  const chosen = state.config?.tts?.voice || "";
  const match = window.speechSynthesis.getVoices().find((v) => v.name === chosen);
  if (match) utter.voice = match;
  utter.onstart = () => { state.speaking = true; setReactor("speaking"); };
  utter.onend = utter.onerror = () => {
    state.speaking = false;
    state.browserSpeaking = null;
    setReactor("idle");
    resumeWakeAfterSpeech();
  };
  state.browserSpeaking = utter;
  pauseWakeForSpeech();
  window.speechSynthesis.speak(utter);
  return true;
}

/** The operator asked for browser speech, or the model cannot run. */
function shouldUseBrowserTts() {
  const mode = state.config?.tts?.engine || "model";
  if (mode === "browser") return true;
  if (mode === "model") return false;
  // "auto": prefer the model, fall back when it is not usable.
  return !state.ttsEnabled || state.ttsModelUsable === false;
}

async function speak(text, opts = {}) {
  if (state.muted || !state.ttsEnabled || !text.trim()) return;

  if (shouldUseBrowserTts()) {
    if (speakWithBrowser(text, opts)) {
      noteSpeechSource("browser");
      return;
    }
    if ((state.config?.tts?.engine || "model") === "browser") {
      toast("This browser has no speech synthesis.", "err");
      return;
    }
  }

  try {
    stopSpeaking();
    const res = await fetch("/api/speak", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        text,
        personality_id: state.active?.id,
        voice: opts.voice,
        speed: opts.speed,
      }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      fallbackToBrowser(text, opts, body.detail || "Speech failed");
      return;
    }
    const blob = await res.blob();
    const audio = $("#player");
    audio.src = URL.createObjectURL(blob);
    state.currentAudio = audio;
    // The microphone would otherwise pick up her own voice and wake her.
    pauseWakeForSpeech();
    audio.onplay = () => { state.speaking = true; setReactor("speaking"); attachAnalyser(audio); };
    audio.onended = () => {
      state.speaking = false;
      state.currentAudio = null;
      detachAnalyser();
      setReactor("idle");
      resumeWakeAfterSpeech();
    };
    audio.onerror = () => {
      state.speaking = false;
      detachAnalyser();
      setReactor("idle");
      resumeWakeAfterSpeech();
    };
    await audio.play();
    noteSpeechSource("model");
    clearAudioBlockedHint();
  } catch (err) {
    // Browsers refuse to play audio until the user has interacted with the page.
    // That is expected on the opening greeting, not an error worth alarming about.
    if (err.name === "NotAllowedError") {
      resumeWakeAfterSpeech();
      showAudioBlockedHint(text);
      return;
    }
    fallbackToBrowser(text, opts, err.message);
  }
}

/**
 * The model path failed. Speak through the browser rather than leaving silence.
 *
 * The reason is surfaced once per session: a toast on every reply would bury the
 * actual conversation.
 */
function fallbackToBrowser(text, opts, reason) {
  resumeWakeAfterSpeech();
  if (speakWithBrowser(text, opts)) {
    noteSpeechSource("browser");
    if (!state.ttsFallbackNoted) {
      state.ttsFallbackNoted = true;
      toast(`Vocal model unavailable (${reason}). Using browser speech.`, "warn");
    }
    return;
  }
  toast(`Could not play speech: ${reason}`, "err");
}

/** Show which engine is speaking, so the operator is never guessing. */
function noteSpeechSource(source) {
  state.speechSource = source;
  const el = $("#speech-source");
  if (!el) return;
  const label = source === "browser" ? "browser speech" : "vocal model";
  el.textContent = label;
  el.dataset.source = source;
}

function pauseWakeForSpeech() {
  if (WAKE.mode === "off") return;
  // Only an "awake" session has to go quiet. While asleep she is only waiting
  // for a wake word, so her own announcement cannot trigger anything and the
  // microphone can safely stay on. Pausing here would make the mic depend on
  // playback finishing to come back, which silently kills wake mode whenever
  // that completion is missed (blocked autoplay, a failed clip).
  if (WAKE.mode !== "awake") return;
  WAKE.resumeAfterSpeech = true;
  clearTimeout(WAKE.restartTimer);
  WAKE.restartTimer = null;
  try { state.recognition.stop(); } catch { /* not running */ }
}

function resumeWakeAfterSpeech() {
  if (!WAKE.resumeAfterSpeech) return;
  WAKE.resumeAfterSpeech = false;
  if (WAKE.mode === "off") return;
  restartWakeIn(250);
}

/** Offer a one-tap way to start audio after the browser blocks autoplay. */
function showAudioBlockedHint(pendingText) {
  if ($("#audio-hint")) return;
  const hint = document.createElement("button");
  hint.id = "audio-hint";
  hint.className = "audio-hint";
  hint.innerHTML = `<svg viewBox="0 0 24 24" width="16" height="16"><path d="M4 9v6h4l5 4V5L8 9H4z" fill="currentColor"/><path d="M16 8.5a5 5 0 010 7" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/></svg> Tap to enable sound`;
  hint.addEventListener("click", () => {
    clearAudioBlockedHint();
    if (pendingText) speak(pendingText);
  });
  $("#toasts").appendChild(hint);
}

function clearAudioBlockedHint() {
  $("#audio-hint")?.remove();
}

function stopSpeaking() {
  const audio = state.currentAudio;
  if (audio) {
    audio.pause();
    audio.currentTime = 0;
    if (audio.src) URL.revokeObjectURL(audio.src);
  }
  // Browser utterances outlive the audio element, so cancel them separately or
  // muting would leave the browser voice talking over the next reply.
  if (state.browserSpeaking && browserTtsAvailable()) {
    window.speechSynthesis.cancel();
    state.browserSpeaking = null;
  }
  state.speaking = false;
  detachAnalyser();
  setReactor(state.listening ? "listening" : "idle");
}

/* ------------------------------------------------------------ analyser/viz */

function attachAnalyser(audioEl) {
  try {
    if (!state.audioCtx) state.audioCtx = new (window.AudioContext || window.webkitAudioContext)();
    const ctx = state.audioCtx;
    if (ctx.state === "suspended") ctx.resume();
    detachAnalyser();
    state.sourceNode = ctx.createMediaElementSource(audioEl);
    state.analyser = ctx.createAnalyser();
    state.analyser.fftSize = 512;
    state.sourceNode.connect(state.analyser);
    state.analyser.connect(ctx.destination);
  } catch {
    // MediaElementSource can only bind once per element; ignore and fall back
    // to a synthetic waveform so the visualiser still animates.
    state.analyser = null;
  }
}

function detachAnalyser() {
  try { state.sourceNode?.disconnect(); } catch { /* already gone */ }
  state.sourceNode = null;
  state.analyser = null;
}

const viz = $("#viz");
const vizCtx = viz.getContext("2d");
const bars = new Array(48).fill(0);

function drawViz() {
  const w = viz.width, h = viz.height;
  vizCtx.clearRect(0, 0, w, h);
  const accent = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim() || "#39d0ff";

  let data = null;
  if (state.analyser) {
    data = new Uint8Array(state.analyser.frequencyBinCount);
    state.analyser.getByteFrequencyData(data);
  }

  const n = bars.length;
  const bw = w / n;
  for (let i = 0; i < n; i++) {
    let target;
    if (data) {
      const idx = Math.floor((i / n) * data.length * 0.65);
      target = data[idx] / 255;
    } else if (state.speaking || state.listening) {
      // Synthetic fallback: a slow travelling wave.
      const t = performance.now() / 260;
      target = Math.abs(Math.sin(t + i * 0.34)) * (state.speaking ? 0.55 : 0.35);
    } else {
      target = 0.035;
    }
    bars[i] += (target - bars[i]) * 0.22;
    const bh = Math.max(2, bars[i] * h * 0.92);
    vizCtx.fillStyle = accent;
    vizCtx.globalAlpha = 0.28 + bars[i] * 0.72;
    const x = i * bw + bw * 0.18;
    const y = (h - bh) / 2;
    vizCtx.beginPath();
    const r = Math.min(bw * 0.32, 3);
    vizCtx.roundRect(x, y, bw * 0.64, bh, r);
    vizCtx.fill();
  }
  vizCtx.globalAlpha = 1;
  requestAnimationFrame(drawViz);
}

function buildTicks() {
  const g = document.getElementById("ticks");
  const cx = 100, cy = 100, r1 = 94, r2 = 100;
  for (let i = 0; i < 60; i++) {
    const a = (i / 60) * Math.PI * 2;
    const long = i % 5 === 0;
    const line = document.createElementNS("http://www.w3.org/2000/svg", "line");
    line.setAttribute("x1", cx + Math.cos(a) * r1);
    line.setAttribute("y1", cy + Math.sin(a) * r1);
    line.setAttribute("x2", cx + Math.cos(a) * (long ? r1 - 7 : r1 - 3));
    line.setAttribute("y2", cy + Math.sin(a) * (long ? r1 - 7 : r1 - 3));
    if (!long) line.setAttribute("opacity", "0.15");
    g.appendChild(line);
  }
}

/* --------------------------------------------------------- speech (input) */

/* Wake mode.
 *
 * A browser speech session cannot run indefinitely, so it is restarted on
 * `onend` for as long as wake mode is armed. Three states matter:
 *
 *   asleep   - nothing heard yet, or the session lapsed. Utterances are
 *              scanned for a wake word and otherwise discarded.
 *   awake    - the wake word was heard. Every utterance is sent to the agent
 *              until a sleep word is heard or the idle timer expires.
 *
 * Wake mode stays armed until the user switches it off. Restarts back off
 * instead of giving up, and only one is ever in flight, so a burst of `onend`
 * events cannot stack `start()` calls into a microphone on/off loop.
 *
 * While the agent is speaking its own voice reaches the microphone, so
 * recognition is paused for the duration of playback. That also means a reply
 * cannot wake her, which is the behaviour we want.
 */

const WAKE = {
  mode: "off", // off | asleep | awake
  sessionTimer: null,
  restartTimer: null, // pending restart, if any
  watchdog: null, // interval that recovers a stuck start()
  starting: false, // start() issued, onstart has not fired yet
  startIssuedAt: 0, // when start() was issued, for the watchdog
  restarts: 0,
  warned: false, // the "keeps dropping" toast has been shown
  sessionStartedAt: 0,
  lastActivity: 0,
};

function normalize(text) {
  return text
    .toLowerCase()
    .replace(/[^\w\s']/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

/** Match a phrase allowing for the recogniser splitting or slurring words. */
function matchesPhrase(transcript, phrase) {
  const said = normalize(transcript);
  const want = normalize(phrase);
  if (!want) return false;
  if (said.includes(want)) return true;

  // Also accept the phrase with no spaces, which is how "hey rexeniot" often
  // arrives ("heyrexeniot").
  const squashed = said.replace(/\s/g, "");
  return squashed.includes(want.replace(/\s/g, ""));
}

function findPhrase(transcript, phrases) {
  return phrases.find((p) => matchesPhrase(transcript, p)) || null;
}

function wakeConfig() {
  return state.config?.voice_input || {};
}

function setWakeMode(mode) {
  WAKE.mode = mode;
  // Leaving "awake" retires the idle timer. Switching wake mode off also
  // retires any queued restart, so a disarm cannot be undone by a late start().
  if (mode !== "awake") {
    clearTimeout(WAKE.sessionTimer);
    WAKE.sessionTimer = null;
  }
  if (mode === "off") {
    clearTimeout(WAKE.restartTimer);
    WAKE.restartTimer = null;
    WAKE.starting = false;
  }
  const badge = $("#wake-badge");
  if (badge) {
    badge.hidden = mode === "off";
    badge.dataset.mode = mode;
    badge.textContent = mode === "awake" ? "AWAKE" : mode === "asleep" ? "LISTENING FOR WAKE WORD" : "";
  }
  $("#btn-wake")?.setAttribute("aria-pressed", String(mode !== "off"));
}

function armWakeMode() {
  if (!state.recognition) {
    toast("This browser has no speech recognition. Use Chrome or Edge.", "err");
    return;
  }
  const cfg = wakeConfig();
  if (!cfg.wake_enabled) {
    toast("Wake mode is switched off in the admin panel.", "err");
    return;
  }
  if (WAKE.mode !== "off") {
    disarmWakeMode();
    return;
  }
  WAKE.restarts = 0;
  WAKE.warned = false;
  setWakeMode("asleep");
  startWakeRecognition();
  startWakeWatchdog();
  toast("Wake mode on. It stays listening until you switch it off.", "ok");
}

function disarmWakeMode() {
  WAKE.resumeAfterSpeech = false;
  stopWakeWatchdog();
  setWakeMode("off");
  try { state.recognition.stop(); } catch { /* not running */ }
  toast("Wake mode off.");
}

/** Queue a single restart. Only one is ever pending, so `onend` bursts cannot
 *  stack `start()` calls and turn the microphone into an on/off loop. */
function restartWakeIn(delayMs) {
  if (WAKE.mode === "off") return;
  if (WAKE.restartTimer || WAKE.starting) return;
  WAKE.restartTimer = setTimeout(() => {
    WAKE.restartTimer = null;
    startWakeRecognition();
  }, delayMs);
}

/**
 * Safety net for the promise that wake mode keeps listening.
 *
 * Recovery is driven by `onend`, so the one state that can strand the
 * microphone is a `start()` whose `onstart` never arrives: `WAKE.starting`
 * stays true, and every later restart is refused as "already starting" while
 * the badge still claims to be listening. This clears that state and retries.
 *
 * A session that dies without `onend` is NOT detectable from here -- nothing
 * distinguishes it from a healthy but silent session -- so it is left alone
 * rather than risk fighting a working microphone.
 */
function startWakeWatchdog() {
  if (WAKE.watchdog) return;
  WAKE.watchdog = setInterval(() => {
    if (WAKE.mode === "off") return;
    if (!WAKE.starting) return;
    if (Date.now() - WAKE.startIssuedAt < 5000) return;
    WAKE.starting = false;
    state.listening = false;
    startWakeRecognition();
  }, 2000);
}

function stopWakeWatchdog() {
  clearInterval(WAKE.watchdog);
  WAKE.watchdog = null;
}

function startWakeRecognition() {
  if (WAKE.mode === "off") return;
  if (WAKE.starting) return; // a start() is already in flight
  WAKE.starting = true;
  WAKE.startIssuedAt = Date.now();
  try {
    state.recognition.start();
  } catch {
    // Already running: no onstart will follow, so clear the flag here.
    WAKE.starting = false;
  }
}

function touchWakeSession() {
  WAKE.lastActivity = Date.now();
  clearTimeout(WAKE.sessionTimer);
  const idleMs = (wakeConfig().session_idle_s || 45) * 1000;
  WAKE.sessionTimer = setTimeout(() => {
    if (WAKE.mode === "awake") {
      setWakeMode("asleep");
      speak("Standing by.");
    }
  }, idleMs);
}

function escapeRegex(text) {
  return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
}

/**
 * Build a matcher for a phrase that tolerates the recogniser running words
 * together ("hey rexeniot" arriving as "heyrexeniot"). Input is normalised to word
 * characters and single spaces first, so no word boundaries are needed.
 */
function phraseRegex(phrase) {
  const words = normalize(phrase).split(" ").filter(Boolean).map(escapeRegex);
  if (!words.length) return null;
  return new RegExp(words.join("\\s*"), "i");
}

/** Remove a wake word from an utterance and return the command that follows. */
function stripWakeWord(transcript) {
  const cfg = wakeConfig();
  const said = normalize(transcript);
  for (const word of cfg.wake_words || []) {
    const re = phraseRegex(word);
    if (!re) continue;
    const match = re.exec(said);
    if (match) {
      return said.slice(match.index + match[0].length).trim();
    }
  }
  return "";
}

function initRecognition() {
  const SR = window.SpeechRecognition || window.webkitSpeechRecognition;
  if (!SR) return null;
  const rec = new SR();
  rec.continuous = true;
  rec.interimResults = true;
  rec.lang = wakeConfig().language || "en-US";

  rec.onstart = () => {
    WAKE.starting = false;
    // Deliberately does NOT reset WAKE.restarts: onstart can fire for a session
    // that drops immediately, and resetting here would cancel the backoff and
    // restore the tight restart loop. onend decides, from how long the session
    // actually lasted.
    WAKE.warned = false;
    WAKE.sessionStartedAt = Date.now();
    state.listening = true;
    $("#btn-mic").setAttribute("aria-pressed", "true");
    $("#listen-hint").hidden = false;
    setReactor(WAKE.mode === "awake" ? "listening" : "idle");
  };

  rec.onresult = (event) => {
    let finalText = "", interim = "";
    for (let i = event.resultIndex; i < event.results.length; i++) {
      const chunk = event.results[i][0].transcript;
      if (event.results[i].isFinal) finalText += chunk;
      else interim += chunk;
    }
    const heard = (finalText || interim).trim();

    if (WAKE.mode !== "off") {
      handleWakeUtterance(heard, Boolean(finalText));
      return;
    }

    // Manual push-to-talk.
    $("#input").value = heard;
    autoGrow($("#input"));
    if (finalText && state.autoSend) {
      rec.stop();
      setTimeout(send, 120);
    }
  };

  rec.onerror = (event) => {
    WAKE.starting = false;
    if (event.error === "not-allowed" || event.error === "service-not-allowed") {
      // No microphone access, so there is nothing to keep retrying.
      toast("Microphone permission denied. Wake mode off.", "err");
      disarmWakeMode();
    } else if (event.error === "no-speech" || event.error === "aborted") {
      // Routine in a long-lived session; the onend handler restarts it.
    } else if (event.error === "network") {
      // Chrome's recogniser needs the network. Keep listening and retry with
      // backoff rather than giving up -- a brief drop should not end the session.
      if (!WAKE.warned) {
        WAKE.warned = true;
        toast("Speech recognition lost its network connection. Still retrying.", "err");
      }
    } else if (event.error === "audio-capture") {
      if (!WAKE.warned) {
        WAKE.warned = true;
        toast("No microphone was found. Still retrying.", "err");
      }
    } else if (WAKE.mode === "off") {
      toast(`Speech input error: ${event.error}`, "err");
    }
  };

  rec.onend = () => {
    state.listening = false;
    WAKE.starting = false;
    $("#btn-mic").setAttribute("aria-pressed", "false");
    $("#listen-hint").hidden = true;

    // A browser session ends on its own after a pause. Restart it so wake mode
    // keeps listening. Only a session that actually lasted resets the counter,
    // so a session that dies instantly backs off instead of looping on the
    // microphone. Wake mode is never switched off here -- only the user does
    // that -- so this keeps retrying indefinitely, just not rapidly.
    if (WAKE.mode !== "off") {
      // Paused on purpose for playback. Restarting here would put the
      // microphone back on while she is still talking, so she would hear
      // herself. resumeWakeAfterSpeech restarts it once playback ends.
      if (WAKE.resumeAfterSpeech) return;
      if (Date.now() - WAKE.sessionStartedAt > 2000) WAKE.restarts = 0;
      WAKE.restarts += 1;
      if (WAKE.restarts > 8 && !WAKE.warned) {
        WAKE.warned = true;
        toast("The microphone keeps dropping. Still listening; check the input device.", "err");
      }
      restartWakeIn(WAKE.restarts > 3 ? 1000 : 120);
      if (!state.speaking) setReactor("idle");
      return;
    }

    if (!state.speaking) setReactor("idle");
  };

  return rec;
}

function handleWakeUtterance(heard, isFinal) {
  const cfg = wakeConfig();
  if (!heard) return;

  if (WAKE.mode === "asleep") {
    // Show what was heard so the user can tell the microphone is live.
    $("#listen-hint").hidden = false;
    if (!isFinal) return;

    const hit = findPhrase(heard, cfg.wake_words || []);
    if (!hit) return;

    setWakeMode("awake");
    touchWakeSession();
    const remainder = stripWakeWord(heard);
    if (cfg.acknowledge !== false && cfg.acknowledgement) {
      speak(cfg.acknowledgement);
    }
    if (remainder.length > 1) {
      // "hey rexeniot turn on the lights" in one breath.
      setTimeout(() => sendText(remainder), cfg.acknowledge !== false ? 900 : 0);
    }
    return;
  }

  // Awake: the nearest conversation is treated as addressed to the agent.
  if (!isFinal) {
    $("#input").value = heard;
    autoGrow($("#input"));
    return;
  }

  touchWakeSession();
  const sleep = findPhrase(heard, cfg.sleep_words || []);
  if (sleep) {
    setWakeMode("asleep");
    $("#input").value = "";
    autoGrow($("#input"));
    speak("Going quiet.");
    return;
  }

  const command = stripWakeWord(heard) || heard;
  if (command.trim().length < 2) return;
  $("#input").value = "";
  autoGrow($("#input"));
  sendText(command.trim());
}

/* ------------------------------------------------------- local (server) STT */

/**
 * Whether dictation should be recorded here and transcribed by the server.
 *
 * The admin's voice_input.engine picks the path. "browser" always uses the
 * vendor recogniser. "server" and "auto" both use this machine once the
 * speech-to-text set is installed -- that is the documented preference, since it
 * works in any browser and keeps the audio local. They differ when the set is
 * absent: "auto" quietly falls back to the browser, while "server" refuses so
 * the user is told what to install instead of silently getting the vendor.
 */
function useLocalCapture() {
  const vi = (state.config && state.config.voice_input) || {};
  const stt = (state.config && state.config.stt) || {};
  if (vi.engine === "browser") return false;
  return Boolean(stt.enabled && stt.available);
}

/** True when the admin demanded local capture but its packages are missing. */
function localCaptureMissing() {
  const vi = (state.config && state.config.voice_input) || {};
  const stt = (state.config && state.config.stt) || {};
  return vi.engine === "server" && !(stt.enabled && stt.available);
}

/** Pick a recording mime the browser actually supports, best first. */
function pickMimeType() {
  const candidates = [
    "audio/webm;codecs=opus",
    "audio/webm",
    "audio/ogg;codecs=opus",
    "audio/mp4",
  ];
  for (const type of candidates) {
    if (window.MediaRecorder?.isTypeSupported?.(type)) return type;
  }
  return "";
}

async function startLocalCapture() {
  if (!navigator.mediaDevices?.getUserMedia) {
    toast("This browser cannot record audio.", "err");
    return;
  }
  let stream;
  try {
    stream = await navigator.mediaDevices.getUserMedia({ audio: true });
  } catch (err) {
    toast(`Microphone unavailable: ${err.message}`, "err");
    return;
  }
  const mimeType = pickMimeType();
  let recorder;
  try {
    recorder = new MediaRecorder(stream, mimeType ? { mimeType } : undefined);
  } catch (err) {
    stream.getTracks().forEach((t) => t.stop());
    toast(`Recording is unavailable: ${err.message}`, "err");
    return;
  }

  state.captureStream = stream;
  state.recorder = recorder;
  state.recordChunks = [];
  recorder.addEventListener("dataavailable", (e) => {
    if (e.data && e.data.size) state.recordChunks.push(e.data);
  });
  recorder.addEventListener("stop", () => {
    const blob = new Blob(state.recordChunks, { type: mimeType || "audio/webm" });
    state.recordChunks = [];
    state.captureStream?.getTracks().forEach((t) => t.stop());
    state.captureStream = null;
    state.recorder = null;
    transcribeRecording(blob);
  });

  stopSpeaking();
  recorder.start();
  state.listening = true;
  setReactor("listening");
  $("#btn-mic").setAttribute("aria-pressed", "true");
  $("#listen-hint").hidden = false;
}

function stopLocalCapture() {
  if (state.recorder && state.recorder.state !== "inactive") {
    state.recorder.stop(); // the stop handler sends it
  } else {
    state.captureStream?.getTracks().forEach((t) => t.stop());
    state.captureStream = null;
  }
  state.listening = false;
  $("#btn-mic").setAttribute("aria-pressed", "false");
  $("#listen-hint").hidden = true;
  setReactor("idle");
}

/** Post a recorded clip to /api/transcribe and put the text in the composer. */
async function transcribeRecording(blob) {
  if (!blob || !blob.size) {
    toast("Nothing was recorded.", "err");
    return;
  }
  setReactor("thinking");
  try {
    const form = new FormData();
    form.append("audio", blob, "speech.webm");
    const res = await fetch("/api/transcribe", { method: "POST", body: form });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || `HTTP ${res.status}`);
    const text = (data.text || "").trim();
    if (!text) {
      toast("I didn't catch that.", "err");
      return;
    }
    if (state.autoSend) {
      await sendText(text);
    } else {
      const input = $("#input");
      input.value = input.value ? `${input.value} ${text}` : text;
      autoGrow(input);
      input.focus();
    }
  } catch (err) {
    toast(`Transcription failed: ${err.message}`, "err");
  } finally {
    setReactor("idle");
  }
}

function toggleListening() {
  // Local capture is preferred whenever it is configured and installed: it works
  // in every browser and never sends the audio to a third party.
  if (localCaptureMissing()) {
    toast("Dictation is set to this machine, but its speech-to-text set is not "
      + "installed. Run: pip install -r requirements-stt.txt", "err");
    return;
  }
  if (useLocalCapture()) {
    if (WAKE.mode !== "off") {
      disarmWakeMode();
      return;
    }
    if (state.listening) stopLocalCapture();
    else startLocalCapture();
    return;
  }
  if (!state.recognition) {
    toast("This browser has no speech recognition. Use Chrome or Edge, or type instead.", "err");
    return;
  }
  if (WAKE.mode !== "off") {
    disarmWakeMode();
    return;
  }
  if (state.listening) {
    state.recognition.stop();
  } else {
    stopSpeaking();
    try { state.recognition.start(); } catch { /* already started */ }
  }
}

/* ------------------------------------------------------------- attachments */

/* Files are uploaded as soon as they are picked, so the agent has them indexed
 * before the message is sent. The returned ids ride along with the chat call.
 */

function renderAttachments() {
  const strip = $("#attach-strip");
  strip.innerHTML = "";
  strip.hidden = state.attachments.length === 0;
  for (const file of state.attachments) {
    const chip = document.createElement("span");
    chip.className = `attach-chip ${file.error ? "err" : ""}`;
    chip.title = file.summary || file.name;
    chip.innerHTML = `<span class="attach-name"></span><button type="button" class="attach-x" aria-label="Remove">×</button>`;
    chip.querySelector(".attach-name").textContent =
      file.error ? `${file.name} — ${file.error}` : `${file.name} · ${file.kind}`;
    chip.querySelector(".attach-x").addEventListener("click", () => {
      state.attachments = state.attachments.filter((f) => f !== file);
      if (file.id) api(`/uploads/${file.id}`, { method: "DELETE" }).catch(() => {});
      renderAttachments();
    });
    strip.appendChild(chip);
  }
}

async function uploadFiles(fileList) {
  const files = Array.from(fileList || []);
  if (!files.length) return;

  const form = new FormData();
  for (const file of files) form.append("files", file);
  form.append("session", "default");

  toast(`Reading ${files.length} file${files.length > 1 ? "s" : ""}…`);
  try {
    const res = await fetch("/api/uploads", { method: "POST", body: form });
    const body = await res.json();
    if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
    for (const file of body.files || []) state.attachments.push(file);
    for (const err of body.errors || []) toast(err, "err");
    renderAttachments();
    const ok = (body.files || []).length;
    if (ok) toast(`${ok} file${ok > 1 ? "s" : ""} ready. Ask away.`, "ok");
  } catch (err) {
    toast(`Upload failed: ${err.message}`, "err");
  }
}

/* ------------------------------------------------------------ send / stream */

function autoGrow(el) {
  el.style.height = "auto";
  el.style.height = `${Math.min(el.scrollHeight, 148)}px`;
}

async function send() {
  const input = $("#input");
  const text = input.value.trim();
  if (!text && !state.attachments.length) return;
  input.value = "";
  autoGrow(input);
  return sendText(text);
}

/** Send a message that did not come from the textarea (e.g. a spoken command). */
async function sendText(text) {
  if (state.busy) return;
  const clean = (text || "").trim();
  const attachments = state.attachments.filter((f) => f.id).map((f) => f.id);
  if (!clean && !attachments.length) return;

  state.busy = true;
  $("#btn-send").disabled = true;
  setReactor("thinking");
  $("#tool-list").innerHTML = "";
  $("#tool-tray").hidden = false;

  const sentFiles = state.attachments.filter((f) => f.id);
  const shown = attachments.length
    ? `${clean}${clean ? "\n\n" : ""}📎 ${sentFiles.map((f) => f.name).join(", ")}`
    : clean;
  addMessage("user", shown, { files: sentFiles });
  state.history.push({ role: "user", content: shown });
  state.attachments = [];
  renderAttachments();
  const assistantMsg = addMessage("assistant", "", { pending: true });
  const { bubble } = assistantMsg;
  // Cards for generated files are appended here as the stream reports them.
  const artifactTray = document.createElement("div");
  artifactTray.className = "file-tray";
  assistantMsg.body.appendChild(artifactTray);

  let accumulated = "";

  try {
    const res = await fetch("/api/chat/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        message: clean,
        personality_id: state.active?.id,
        history: state.history.slice(-24),
        speak: true,
        attachments,
      }),
    });
    if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    let speechText = null;
    let finished = false;

    while (!finished) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const parts = buffer.split("\n\n");
      buffer = parts.pop() ?? "";
      for (const part of parts) {
        const line = part.split("\n").find((l) => l.startsWith("data:"));
        if (!line) continue;
        let event;
        try { event = JSON.parse(line.slice(5).trim()); } catch { continue; }

        if (event.type === "token") {
          accumulated += event.text;
          bubble.innerHTML = renderMarkdown(accumulated);
          bubble.classList.add("cursor");
          $("#messages").scrollTop = $("#messages").scrollHeight;
        } else if (event.type === "attachments") {
          for (const file of event.files || []) addToolChip("read_file", true, file.summary);
        } else if (event.type === "artifact") {
          // A file the agent produced. Show it under the reply as it arrives.
          if (event.file) artifactTray.appendChild(buildFilePreview(event.file));
        } else if (event.type === "tool") {
          addToolChip(event.tool, event.ok, event.result);
        } else if (event.type === "status") {
          if (event.message.includes("speech")) setReactor("speaking");
        } else if (event.type === "speech") {
          speechText = event.speak_text;
        } else if (event.type === "speech_error") {
          toast(event.message, "err");
        } else if (event.type === "error") {
          throw new Error(event.message);
        } else if (event.type === "done") {
          accumulated = event.text || accumulated;
        } else if (event.type === "complete") {
          finished = true;
        }
      }
    }

    bubble.classList.remove("cursor");
    bubble.innerHTML = renderMarkdown(accumulated || "(no reply)");
    state.history.push({ role: "assistant", content: accumulated });
    if (speechText) speak(speechText);
    else if (accumulated) speak(accumulated);
  } catch (err) {
    bubble.innerHTML = `<p style="color:var(--err)">${escapeHtml(err.message)}</p>`;
    toast(err.message, "err");
    setReactor("idle");
  } finally {
    state.busy = false;
    $("#btn-send").disabled = false;
    if (!state.speaking) setReactor("idle");
    trimHistory();
  }
}

function trimHistory() {
  if (state.history.length > 40) state.history = state.history.slice(-40);
}

/* ------------------------------------------------------------------ status */

async function refreshStatus() {
  try {
    const s = await api("/status");
    state.ttsEnabled = s.tts?.state !== "disabled";
    // "auto" mode needs to know whether the vocal model can actually run, not
    // just whether speech is switched on: a model that failed to load must send
    // the next reply to the browser rather than to a broken /api/speak.
    state.ttsModelUsable = !["error", "unavailable"].includes(s.tts?.state);
    $("#s-model").textContent = s.llm?.model || "not set";
    $("#s-tools").textContent = s.tools_enabled ? `${s.tools_enabled ? "on" : "off"}` : "off";
    $("#s-home").textContent = s.tools_enabled ? "ready" : "off";
    $("#brand-sub").textContent = s.hardware || "local agent";

    const llmPill = $("#llm-pill");
    llmPill.className = `pill ${s.llm?.configured ? "ok" : "warn"}`;
    llmPill.querySelector("span").textContent = s.llm?.model || s.llm?.provider || "no model";

    const ttsPill = $("#tts-pill");
    const ttsOk = s.tts?.state === "ready";
    ttsPill.className = `pill ${ttsOk ? "ok" : "warn"}`;
    ttsPill.querySelector("span").textContent = s.tts?.tier ? `${s.tts.tier} tts` : "tts loading";

    if (s.personality && (!state.active || state.active.id !== s.personality.id)) {
      state.active = s.personality;
      renderActivePersona();
    }
  } catch { /* status is best-effort */ }
}

async function refreshReminders() {
  try {
    const r = await api("/reminders");
    const block = $("#reminder-block");
    const list = $("#reminders");
    const items = [...(r.pending || []), ...(r.fired || [])];
    block.hidden = items.length === 0;
    list.innerHTML = "";
    for (const t of items) {
      const li = document.createElement("li");
      li.textContent = t.label ? `${t.label} (${t.seconds}s)` : t.text;
      list.appendChild(li);
    }
  } catch { /* ignore */ }
}

/* -------------------------------------------------------------------- boot */

async function greet() {
  try {
    const g = await api(`/greeting${state.active ? `?personality_id=${state.active.id}` : ""}`);
    addMessage("assistant", g.text);
    state.history.push({ role: "assistant", content: g.text });
    if (g.speech?.speak_text) speak(g.speech.speak_text);
  } catch { /* a missing greeting should not block the UI */ }
}

async function loadPersonalities() {
  const data = await api("/admin/personalities").catch(async () => {
    // Admin token may gate the route; fall back to reading it from status.
    const s = await api("/status");
    return { personalities: [s.personality], active: s.personality?.id };
  });
  state.personalities = data.personalities || [];
  state.active = state.personalities.find((p) => p.id === data.active) || state.personalities[0] || null;
  renderActivePersona();
  renderPersonas();
}

function wireEvents() {
  $("#form").addEventListener("submit", (e) => { e.preventDefault(); send(); });

  $("#input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); send(); }
  });
  $("#input").addEventListener("input", (e) => autoGrow(e.target));
  // Keep the soft keyboard's cursor where the user last left the caret, so soft
  // keys insert at the right spot even when the field is not focused.
  ["click", "keyup", "select"].forEach((type) =>
    $("#input").addEventListener(type, () => state.osk?.syncCaret()));
  $("#input").addEventListener("paste", (e) => {
    const files = Array.from(e.clipboardData?.files || []);
    if (files.length) { e.preventDefault(); uploadFiles(files); }
  });

  $("#btn-send").addEventListener("click", send);
  $("#btn-mic").addEventListener("click", toggleListening);
  $("#btn-wake")?.addEventListener("click", armWakeMode);

  $("#btn-attach").addEventListener("click", () => $("#file-input").click());
  $("#file-input").addEventListener("change", (e) => {
    uploadFiles(e.target.files);
    e.target.value = "";
  });

  $("#btn-mute").addEventListener("click", () => {
    state.muted = !state.muted;
    $("#btn-mute").setAttribute("aria-pressed", String(state.muted));
    if (state.muted) stopSpeaking();
    toast(state.muted ? "Spoken replies muted." : "Spoken replies on.", "ok");
  });

  $("#btn-admin").addEventListener("click", () => { window.location.href = "/admin"; });

  // The soft keyboard is optional and never opens itself; the toggle is offered
  // on every device so a touch screen or a kiosk can reach it, and a desktop
  // user can still call it up deliberately.
  state.osk = initOnScreenKeyboard({
    input: $("#input"),
    toggleButton: $("#btn-keyboard"),
    panel: $("#osk"),
    onChange: () => autoGrow($("#input")),
  });
  $("#btn-keyboard")?.addEventListener("click", () => state.osk?.toggle());

  initFullscreen();

  $("#persona-button").addEventListener("click", () => togglePersonaList());
  document.addEventListener("click", (e) => {
    if (!e.target.closest(".persona-picker")) togglePersonaList(false);
  });

  // Drop files anywhere to attach them.
  ["dragenter", "dragover"].forEach((type) =>
    document.addEventListener(type, (e) => {
      if (!e.dataTransfer?.types?.includes("Files")) return;
      e.preventDefault();
      document.body.classList.add("dropping");
    })
  );
  document.addEventListener("dragleave", (e) => {
    if (!e.relatedTarget) document.body.classList.remove("dropping");
  });
  document.addEventListener("drop", (e) => {
    document.body.classList.remove("dropping");
    if (!e.dataTransfer?.files?.length) return;
    e.preventDefault();
    uploadFiles(e.dataTransfer.files);
  });

  // Hold-to-talk on the space bar when the input is empty.
  document.addEventListener("keydown", (e) => {
    if (e.code === "Space" && e.target === document.body && !state.listening) {
      e.preventDefault();
      toggleListening();
    }
    // Escape drops out of a wake session without reaching for the mouse.
    if (e.key === "Escape" && WAKE.mode === "awake") {
      setWakeMode("asleep");
      toast("Back to wake-word listening.");
    }
  });
}

/** Load the public config the HUD needs: voice input and wake settings. */
async function loadPublicConfig() {
  try {
    state.config = await api("/config");
  } catch {
    state.config = {};
  }
  const vi = state.config.voice_input || {};
  state.autoSend = vi.auto_send !== false;
  if (state.recognition && vi.language) state.recognition.lang = vi.language;

  // The mic is usable with either capture path. Boot-time titles assume the
  // native recogniser, so correct them once the config says which path is live.
  const mic = $("#btn-mic");
  if (mic) {
    if (useLocalCapture()) {
      mic.title = "Push to talk (recorded and transcribed on this machine)";
    } else if (!state.recognition) {
      mic.title = "Speech input unavailable in this browser";
    }
  }

  const btn = $("#btn-wake");
  if (btn) {
    const available = Boolean(state.recognition) && vi.wake_enabled;
    btn.hidden = !available;
    if (available) {
      btn.title = `Stay listening for the wake word (${(vi.wake_words || []).join(", ")})`;
    } else if (WAKE.mode !== "off") {
      // The admin switched wake mode off while it was armed here.
      disarmWakeMode();
    }
  }
}

/**
 * Publish the height of the bottom furniture (composer plus soft keyboard) as
 * --dock-h, so the toast stack can sit above it instead of over the Send button.
 */
function trackDockHeight() {
  const dock = $(".composer");
  const osk = $("#osk");
  const topbar = $(".topbar");
  const root = document.documentElement;
  const measure = () => {
    const composer = dock?.getBoundingClientRect().height ?? 0;
    const keyboard = osk && !osk.hidden ? osk.getBoundingClientRect().height : 0;
    root.style.setProperty("--dock-h", `${Math.round(composer + keyboard)}px`);
    // The header wraps to two rows on narrow screens, so the persona dropdown
    // (position: fixed on phones) needs its real bottom edge to sit under it.
    const bar = topbar?.getBoundingClientRect().bottom ?? 0;
    root.style.setProperty("--topbar-b", `${Math.round(bar)}px`);
  };
  measure();
  if (typeof ResizeObserver !== "function") {
    window.addEventListener("resize", measure);
    trackViewportHeight();
    return;
  }
  const ro = new ResizeObserver(measure);
  if (dock) ro.observe(dock);
  if (osk) ro.observe(osk);
  if (topbar) ro.observe(topbar);
  window.addEventListener("resize", measure);
  trackViewportHeight();
}

/**
 * Keep --vh at 1% of the *visible* height. Only the engines without `dvh` read
 * it, where it stands in for the unit the mobile URL bar would otherwise skew.
 */
function trackViewportHeight() {
  const set = () =>
    document.documentElement.style.setProperty("--vh", `${window.innerHeight * 0.01}px`);
  set();
  window.addEventListener("resize", set);
  window.addEventListener("orientationchange", () => setTimeout(set, 120));
  window.visualViewport?.addEventListener("resize", set);
}

/**
 * Full-screen toggle. Hidden on iOS Safari, which has no element-level
 * Fullscreen API: iPhone only exposes it for video. The button therefore appears
 * on desktop, Android and tablets, and the kiosk/TV layouts carry the rest.
 */
function initFullscreen() {
  const btn = $("#btn-fullscreen");
  if (!btn) return;
  const supported =
    !!(document.fullscreenEnabled || document.webkitFullscreenEnabled) &&
    typeof (document.documentElement.requestFullscreen || document.documentElement.webkitRequestFullscreen) === "function";
  if (!supported) return;
  btn.hidden = false;

  const current = () => document.fullscreenElement || document.webkitFullscreenElement || null;
  const sync = () => {
    const on = !!current();
    btn.setAttribute("aria-pressed", String(on));
    btn.title = on ? "Exit full screen" : "Full screen";
  };

  btn.addEventListener("click", async () => {
    try {
      if (current()) {
        await (document.exitFullscreen?.() ?? document.webkitExitFullscreen?.());
      } else {
        const el = document.documentElement;
        await (el.requestFullscreen?.({ navigationUI: "hide" }) ?? el.webkitRequestFullscreen?.());
      }
    } catch (err) {
      // Denied by permissions policy or a non-user gesture; leave the state alone.
      toast(`Full screen unavailable: ${err.message}`, "err");
    }
    sync();
  });

  ["fullscreenchange", "webkitfullscreenchange"].forEach((type) =>
    document.addEventListener(type, sync)
  );
  sync();
}

async function boot() {
  buildTicks();
  drawViz();
  wireEvents();
  trackDockHeight();

  state.recognition = initRecognition();
  if (!state.recognition) $("#btn-mic").title = "Speech input unavailable in this browser";

  const status = $("#boot-status");
  try {
    status.textContent = "Loading personalities…";
    await loadPersonalities();
    status.textContent = "Checking systems…";
    await loadPublicConfig();
    await refreshStatus();
    $("#boot").classList.add("done");
    $("#app").hidden = false;
    greet();
  } catch (err) {
    status.textContent = `Startup problem: ${err.message}`;
    toast(`Startup problem: ${err.message}`, "err");
    $("#app").hidden = false;
  }

  setInterval(refreshStatus, 20000);
  setInterval(refreshReminders, 15000);
  refreshReminders();
}

boot();

// Expose a few internals for automated checks. Module scope is otherwise
// unreachable from the page context, and a browser-driven test is the only way
// to exercise the record -> upload -> transcribe path end to end.
window.__rexeniot = { state, useLocalCapture, localCaptureMissing, toggleListening };

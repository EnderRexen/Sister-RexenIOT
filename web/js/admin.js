/* ============================================================================
   Sister RexenIOT · Admin panel client
   ========================================================================== */

const $ = (sel) => document.querySelector(sel);
const $$ = (sel) => Array.from(document.querySelectorAll(sel));

const state = {
  token: sessionStorage.getItem("rexeniot_admin_token") || "",
  config: null,
  personalities: [],
  editing: null,
  hardware: null,
  voices: [],
  profiles: [],
  profileCurrent: {},
  profileActive: "",
  plan: null,
  modelFilter: "all",
  knownTools: [],
};

/* ---------------------------------------------------------------- helpers */

function toast(message, kind = "") {
  const el = document.createElement("div");
  el.className = `toast ${kind}`;
  el.textContent = message;
  $("#toasts").appendChild(el);
  setTimeout(() => {
    el.style.transition = "opacity .4s";
    el.style.opacity = "0";
    setTimeout(() => el.remove(), 400);
  }, 4600);
}

function setSaveState(text, kind = "") {
  const pill = $("#save-state");
  pill.className = `pill ${kind}`;
  pill.querySelector("span").textContent = text;
}

async function api(path, options = {}) {
  const headers = { "Content-Type": "application/json", ...(options.headers || {}) };
  if (state.token) headers["X-Admin-Token"] = state.token;
  const res = await fetch(`/api${path}`, { ...options, headers });
  if (res.status === 401) {
    showGate(true);
    throw new Error("Admin token required");
  }
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try { detail = (await res.json()).detail || detail; } catch { /* ignore */ }
    throw new Error(detail);
  }
  const type = res.headers.get("content-type") || "";
  return type.includes("json") ? res.json() : res;
}

function get(obj, path) {
  return path.split(".").reduce((acc, k) => (acc == null ? acc : acc[k]), obj);
}

function escapeHtml(t) {
  return String(t).replace(/[&<>"']/g, (c) =>
    ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

/* ------------------------------------------------------------------ gate */

function showGate(show) {
  $("#gate").hidden = !show;
}

async function checkSession() {
  try {
    const s = await api("/admin/session");
    if (!s.requires_token) { showGate(false); return true; }
    if (!state.token) { showGate(true); return false; }
    try {
      await api("/admin/verify", { method: "POST" });
      showGate(false);
      return true;
    } catch {
      sessionStorage.removeItem("rexeniot_admin_token");
      state.token = "";
      showGate(true);
      return false;
    }
  } catch (err) {
    toast(`Could not reach the server: ${err.message}`, "err");
    return false;
  }
}

/* ------------------------------------------------------------- tab logic */

function selectTab(name) {
  $$(".tab").forEach((t) => t.classList.toggle("active", t.dataset.tab === name));
  $$(".panel").forEach((p) => p.classList.toggle("active", p.dataset.panel === name));
  if (name === "server") refreshStorage();
  if (name === "skills") loadSkills();
  if (name === "voice") loadProfiles();
}

/* ---------------------------------------------------- config form binding */

function fillConfigForm(cfg) {
  state.config = cfg;
  $$("[data-cfg]").forEach((el) => {
    const key = el.dataset.cfg;
    if (key.endsWith("-list")) {
      const real = key.replace("-list", "");
      const value = get(cfg, real) || [];
      el.value = Array.isArray(value) ? value.join(", ") : value;
      return;
    }
    const value = get(cfg, key);
    if (el.type === "checkbox") el.checked = Boolean(value);
    else if (value !== undefined && value !== null) el.value = value;
    else if (el.tagName === "SELECT") el.selectedIndex = 0;
  });
  renderLlmModels(cfg.llm);
}

function collectConfigPatch() {
  const patch = {};
  $$("[data-cfg]").forEach((el) => {
    let key = el.dataset.cfg;
    let value;
    if (key.endsWith("-list")) {
      key = key.replace("-list", "");
      value = el.value.split(",").map((s) => s.trim()).filter(Boolean);
    } else if (el.type === "checkbox") {
      value = el.checked;
    } else if (el.type === "number") {
      value = el.value === "" ? undefined : Number(el.value);
    } else {
      value = el.value;
    }
    if (value === undefined) return;
    // Skip the masked placeholder so a stored secret is not overwritten by "***".
    if (value === "***") return;
    const parts = key.split(".");
    let node = patch;
    for (let i = 0; i < parts.length - 1; i++) {
      node[parts[i]] = node[parts[i]] || {};
      node = node[parts[i]];
    }
    node[parts[parts.length - 1]] = value;
  });
  return patch;
}

async function saveConfig(sections = null) {
  setSaveState("saving…", "warn");
  try {
    const patch = collectConfigPatch();
    const payload = sections ? filterSections(patch, sections) : patch;
    const cfg = await api("/admin/config", {
      method: "PUT",
      body: JSON.stringify({ patch: payload }),
    });
    fillConfigForm(cfg);
    setSaveState("saved", "ok");
    setTimeout(() => setSaveState("idle"), 2200);
    return cfg;
  } catch (err) {
    setSaveState("error", "err");
    toast(`Save failed: ${err.message}`, "err");
    throw err;
  }
}

function filterSections(patch, sections) {
  const out = {};
  for (const s of sections) if (patch[s] !== undefined) out[s] = patch[s];
  return out;
}

/* --------------------------------------------------------- hardware scan */

function renderHardware(profile) {
  state.hardware = profile;
  const grid = $("#hw-grid");
  const cpu = profile.cpu, mem = profile.memory, disk = profile.disk, gpus = profile.gpu || [];
  const cells = [
    ["CPU", `${cpu.brand} · ${cpu.logical_cores} threads`],
    ["Memory", `${mem.total_gb} GB total · ${mem.available_gb} GB free`],
    ["Disk", `${disk.free_gb} GB free of ${disk.total_gb} GB`],
    ["Platform", (profile.os?.system ? `${profile.os.system} ${profile.os.release || ""}`.trim() : profile.runtimes?.platform) || "—"],
    ["GPU", gpus.length ? gpus.map((g) => `${g.name} (${g.vram_gb} GB)`).join(", ") : "none detected"],
    ["Runtimes", Object.entries(profile.runtimes || {})
      .filter(([k]) => ["onnxruntime", "torch", "transformers", "mqtt"].includes(k))
      .map(([k, v]) => `${k}: ${v ? "yes" : "no"}`).join(" · ")],
  ];
  grid.innerHTML = cells.map(([k, v]) =>
    `<div class="hw-cell"><span class="hw-k">${escapeHtml(k)}</span><span class="hw-v">${escapeHtml(v)}</span></div>`
  ).join("");
  $("#hw-summary").textContent = profile.summary || "";
  $("#hw-summary-card").hidden = false;

  const recs = profile.recommendations || {};
  const notes = [];
  notes.push(`<p><strong>Recommended speech:</strong> ${escapeHtml(recs.recommended_tts_name || recs.recommended_tts || "—")}
    <span class="muted">(${escapeHtml(recs.recommended_tts_tier || "")} tier)</span></p>`);
  if (recs.recommended_llm) {
    notes.push(`<p><strong>Recommended language model:</strong> ${escapeHtml(recs.recommended_llm)}</p>`);
  }
  if (recs.gpu_present) {
    notes.push(`<p class="ok-note">A GPU was detected, so the full 1.7B speech model and GPU language
      models are available.</p>`);
  } else {
    notes.push(`<p class="muted">No GPU detected. RexenIOT stays on the ONNX speech models and CPU-friendly
      language models; adding a GPU unlocks the larger ones.</p>`);
  }
  if (!recs.torch_installed) {
    notes.push(`<p class="muted">The PyTorch stack is not installed. The full speech tier would need it;
      the lighter tier works without.</p>`);
  }
  $("#hw-notes").innerHTML = notes.join("");
  $("#hw-notes-card").hidden = false;
  $("#btn-apply-recs").disabled = false;

  renderModels(recs.models || []);
  renderTtsStatus();
  renderSttStatus();
  renderAccelerator();
  renderStorageStatus();
}

function renderModels(models) {
  const list = $("#model-list");
  const filtered = models.filter((m) => state.modelFilter === "all" || m.kind === state.modelFilter);
  if (!filtered.length) {
    list.innerHTML = `<p class="muted">Nothing in this category.</p>`;
    return;
  }
  list.innerHTML = filtered.map((m) => {
    const req = m.requirements || {};
    const reqBits = [];
    if (req.ram_gb) reqBits.push(`${req.ram_gb} GB RAM`);
    if (req.vram_gb) reqBits.push(`${req.vram_gb} GB VRAM`);
    if (req.gpu) reqBits.push("GPU");
    const blockers = (m.blockers || []).map(escapeHtml).join("; ");
    const caveats = (m.caveats || []).map(escapeHtml).join("; ");
    const pre = m.prerequisites || {};
    // A model whose prerequisites are unmet is downloadable but not runnable, so
    // it must not read as ready. Offer the install instead of a silent dead end.
    const needsSetup = Boolean(pre.needed && !pre.satisfied);
    // A package that is present but will not import needs reinstalling, not
    // installing. Saying "missing" there sends the reader to do the wrong thing,
    // which is how a broken torch stays broken through several attempts.
    const brokenLabel = (pre.broken || []).length
      ? `${escapeHtml(pre.broken.join(", "))} is installed but cannot be imported`
      : `Needs ${escapeHtml(pre.label)}`;
    const primary = needsSetup
      ? `<button class="btn-primary" data-install="${escapeHtml(pre.key)}"
           title="${escapeHtml(pre.detail || "")}">Install ${escapeHtml(pre.label)}</button>`
      : `<button class="btn-ghost" data-download="${escapeHtml(m.repo)}" data-kind="${m.kind}"
           ${m.fits ? "" : "disabled"}>${m.kind === "llm" ? "How to get" : "Download"}</button>`;
    return `
      <div class="model-row ${m.fits ? "" : "unfit"} ${m.active ? "in-use" : ""}">
        <div class="model-main">
          <div class="model-title">
            <span class="kind-tag kind-${m.kind}">${m.kind}</span>
            <strong>${escapeHtml(m.name)}</strong>
            ${m.active ? '<span class="badge ok">in use</span>' : ""}
            ${m.downloaded ? '<span class="badge ok">downloaded</span>' : ""}
            ${m.fits ? '<span class="badge ok">fits</span>' : '<span class="badge bad">unfit</span>'}
          </div>
          <div class="model-repo">${escapeHtml(m.repo)}</div>
          <div class="model-note">${escapeHtml(m.note || "")}</div>
          ${reqBits.length ? `<div class="model-req">Needs ${reqBits.join(", ")} · ${m.size_mb ? m.size_mb + " MB" : ""}</div>` : ""}
          ${blockers ? `<div class="model-block">Blocked: ${blockers}</div>` : ""}
          ${caveats ? `<div class="model-caveat">${caveats}</div>` : ""}
          ${needsSetup ? `<div class="model-caveat">${brokenLabel} — ${escapeHtml(pre.detail || "")} ${escapeHtml(pre.reason || "")} ${escapeHtml(pre.command || "")}</div>` : ""}
        </div>
        <div class="model-actions">
          ${primary}
          ${m.kind === "tts" ? `<button class="btn-ghost ${m.active ? "is-active" : ""}"
            data-usetts="${escapeHtml(m.repo)}" ${m.active ? "disabled" : ""}
            >${m.active ? "✓ In use" : "Use"}</button>` : ""}
        </div>
      </div>`;
  }).join("");

  list.querySelectorAll("[data-download]").forEach((btn) => {
    btn.addEventListener("click", () => downloadModel(btn.dataset.download, btn.dataset.kind));
  });
  list.querySelectorAll("[data-install]").forEach((btn) => {
    btn.addEventListener("click", () => installPrerequisites(btn.dataset.install));
  });
  list.querySelectorAll("[data-usetts]").forEach((btn) => {
    btn.addEventListener("click", () => useTtsModel(btn.dataset.usetts));
  });
}

async function runScan(auto = false) {
  setSaveState("scanning…", "warn");
  try {
    const profile = await api("/admin/hardware/scan", { method: "POST" });
    renderHardware(profile);
    setSaveState("scan complete", "ok");
    setTimeout(() => setSaveState("idle"), 2500);
    if (!auto) toast("Hardware scan complete.", "ok");
  } catch (err) {
    setSaveState("scan failed", "err");
    toast(`Scan failed: ${err.message}`, "err");
  }
}

async function applyRecommendations() {
  if (!confirm("Apply the recommended speech tier and language model to the running configuration?")) return;
  setSaveState("applying…", "warn");
  try {
    const result = await api("/admin/hardware/apply", { method: "POST" });
    fillConfigForm(result.config);
    if (result.error) {
      // The scan succeeded but the model could not be loaded, so nothing was
      // written. Say so instead of claiming an apply that did not happen.
      setSaveState("failed", "err");
      toast(result.error, "err");
      renderTtsStatus();
      return;
    }
    setSaveState("applied", "ok");
    toast(`Applied: ${result.summary}`, "ok");
    renderTtsStatus();
  } catch (err) {
    setSaveState("failed", "err");
    toast(`Apply failed: ${err.message}`, "err");
  }
}

async function downloadModel(repo, kind) {
  setSaveState("downloading…", "warn");
  try {
    const result = await api("/admin/models/download", {
      method: "POST",
      body: JSON.stringify({ repo, kind }),
    });
    if (result.ok) {
      toast(`Downloaded ${repo}.`, "ok");
      setSaveState("done", "ok");
    } else {
      toast(result.detail || "Not downloadable from here.", "err");
      setSaveState("idle");
    }
  } catch (err) {
    toast(`Download failed: ${err.message}`, "err");
    setSaveState("idle");
  }
  await refreshModels();
}

async function installPrerequisites(key) {
  if (!confirm(
    "Install this requirement set with pip on the app's Python?\n\n"
    + "This downloads a large package set and may take several minutes. "
    + "The app keeps running; speech is unaffected while it installs."
  )) return;
  setSaveState("installing prerequisites…", "warn");
  let job;
  try {
    job = await api("/admin/models/prerequisites/install", {
      method: "POST",
      body: JSON.stringify({ set: key }),
    });
  } catch (err) {
    toast(`Could not start the install: ${err.message}`, "err");
    setSaveState("idle");
    return;
  }

  // Poll until the job leaves "running", then re-read the catalog so the row
  // reflects what actually happened rather than what we asked for.
  const deadline = Date.now() + 31 * 60 * 1000;
  while (Date.now() < deadline) {
    await new Promise((r) => setTimeout(r, 3000));
    let status;
    try {
      status = await api("/admin/models/prerequisites/status");
    } catch {
      continue;
    }
    const install = status.install || {};
    if (install.state === "running") {
      setSaveState(`installing ${install.label || key}…`, "warn");
      continue;
    }
    if (install.state === "done") {
      setSaveState("installed", "ok");
      toast(`${install.label || key} installed.`, "ok");
    } else if (install.state === "error") {
      setSaveState("install failed", "err");
      toast(`Install failed: ${install.detail || "unknown error"}`, "err");
      if (install.log) console.warn("pip output:\n" + install.log);
    }
    break;
  }
  await refreshModels();
}

async function refreshModels() {
  try {
    const data = await api("/admin/models");
    if (state.hardware) {
      state.hardware.recommendations = {
        ...(state.hardware.recommendations || {}),
        models: data.catalog || [],
      };
    }
    renderModels(data.catalog || []);
  } catch { /* the next scan will correct the list */ }
}

async function useTtsModel(repo) {
  setSaveState("loading model…", "warn");
  try {
    await api("/admin/models/load", { method: "POST", body: JSON.stringify({ repo, kind: "tts" }) });
    toast(`Now using ${repo}.`, "ok");
    setSaveState("ready", "ok");
    renderTtsStatus();
    await refreshConfig();
    // Re-read the catalog so the "in use" badge and green button land on the row
    // that just became active, rather than only after the next scan.
    await refreshModels();
  } catch (err) {
    toast(`Could not load model: ${err.message}`, "err");
    setSaveState("idle");
  }
}

async function renderTtsStatus() {
  try {
    const s = await api("/admin/tts/status");
    $("#tts-status").innerHTML = Object.entries({
      State: s.state, Tier: s.tier, Backend: s.backend,
      "Model": s.model_id || "—", Voices: s.voice_count,
      Device: s.device || "—",
      Requested: s.device_requested || "auto",
    }).map(([k, v]) => `<div class="kv-row"><span>${escapeHtml(k)}</span><strong>${escapeHtml(String(v ?? "—"))}</strong></div>`).join("");
  } catch { /* ignore */ }
}

async function renderSttStatus() {
  const box = $("#stt-status");
  if (!box) return;
  let s;
  try {
    s = await api("/admin/stt/status");
  } catch {
    return;
  }
  const rows = {
    State: s.state,
    "Model": s.model_id || "—",
    Device: s.device || "—",
    Requested: s.device_requested || "auto",
    Precision: s.compute_type || "—",
    "Max clip": s.max_seconds ? `${s.max_seconds}s` : "—",
  };
  if (!s.available) {
    rows.State = "not installed";
    rows.Detail = "Optional; dictation falls back to the browser recogniser.";
  }
  box.innerHTML = Object.entries(rows)
    .map(([k, v]) => `<div class="kv-row"><span>${escapeHtml(k)}</span><strong>${escapeHtml(String(v ?? "—"))}</strong></div>`)
    .join("");
  const install = $("#btn-stt-install");
  if (install) install.hidden = Boolean(s.available);
  const preview = $("#btn-stt-preview");
  if (preview) preview.disabled = !s.available;
}

async function previewStt() {
  const out = $("#stt-preview-result");
  if (out) out.textContent = "speaking a test line, then transcribing it…";
  setSaveState("testing dictation…", "warn");
  try {
    const r = await api("/admin/stt/preview", { method: "POST" });
    const ok = r.heard && r.expected && r.heard.toLowerCase().replace(/[^a-z ]/g, "")
      === r.expected.toLowerCase().replace(/[^a-z ]/g, "");
    if (out) out.textContent = `${ok ? "Match" : "Heard"} — ${r.heard}`;
    setSaveState("dictation works", "ok");
  } catch (err) {
    if (out) out.textContent = `Failed: ${err.message}`;
    setSaveState("dictation failed", "err");
  }
}

async function renderAccelerator() {
  const box = $("#accelerator-status");
  if (!box) return;
  try {
    const a = await api("/admin/accelerator");
    const t = a.torch || {};
    const o = a.onnxruntime || {};
    const gpuName = (a.gpus || []).map((g) => `${g.name} (${g.vram_gb} GB)`).join(", ") || "none detected";

    // A version string like "2.14.1+cu130" is the build tag, not evidence the GPU
    // is reachable -- the local tag is baked in at compile time. Print the two
    // facts separately or the row reads as a contradiction ("CUDA 13.0 · CPU only").
    const runtime = (info, buildLabel) => {
      if (!info.installed) return "not installed";
      const build = info.cuda_version ? ` (${buildLabel} ${info.cuda_version} build)` : "";
      // DirectML is the one accelerated provider that is not CUDA, so naming it
      // avoids the row reading "CPU only" on a machine that is in fact using the
      // GPU through DirectX.
      const via = info.directml ? "GPU reachable (DirectML)" : (info.cuda ? "GPU reachable" : "CPU only");
      return `${info.version}${build} · ${via}`;
    };
    box.innerHTML = Object.entries({
      "GPU present": a.gpu_present ? gpuName : "no",
      "In use now": `${a.current?.device || "—"} (tier ${a.current?.tier || "—"})`,
      "torch": runtime(t, "CUDA"),
      "onnxruntime": runtime(o, "CUDA"),
      "vLLM": (a.vllm?.installed)
        ? `${a.vllm.version || "installed"} · ${a.vllm.cuda ? "GPU reachable" : "no GPU"}`
        : "not installed",
      "Recommendation": a.recommended_device || "—",
      "Speech backend": a.recommended_backend
        ? `${a.recommended_backend}${a.backend_usable ? " (ready)" : ""}`
        : "—",
    }).map(([k, v]) => `<div class="kv-row"><span>${escapeHtml(k)}</span><strong>${escapeHtml(String(v ?? "—"))}</strong></div>`).join("");

    if (a.device_reason) {
      box.innerHTML += `<p class="muted">${escapeHtml(a.device_reason)}</p>`;
    }
    // The backend advice is separate from the device advice: a GPU can be
    // perfectly usable while the faster backend is still an install away.
    if (a.backend_reason) {
      box.innerHTML += `<p class="muted">${escapeHtml(a.backend_reason)}</p>`;
    }

    const hints = $("#accelerator-hints");
    if (hints) {
      hints.innerHTML = (a.hints || []).length
        ? `<div class="hint-list">${a.hints.map((h) => `
             <div class="hint">
               <div><strong>${escapeHtml(h.for)}</strong></div>
               <code>${escapeHtml(h.command)}</code>
               <div class="muted">${escapeHtml(h.note || "")}</div>
             </div>`).join("")}</div>`
        : "";
    }
  } catch { /* ignore */ }
}

/* -------------------------------------------------- hardware verification */

// The verification step: check the machine, show what it means, and let the
// operator apply the settings it proposes -- optionally proving they work first.
async function runVerification() {
  const btn = $("#btn-verify");
  btn.disabled = true;
  setSaveState("verifying…", "warn");
  try {
    const plan = await api("/admin/hardware/plan");
    state.plan = plan;
    renderVerification(plan);
    setSaveState("verified", "ok");
    setTimeout(() => setSaveState("idle"), 2500);
    toast("Verification complete.", "ok");
  } catch (err) {
    setSaveState("verification failed", "err");
    toast(`Verification failed: ${err.message}`, "err");
  } finally {
    btn.disabled = false;
  }
}

function renderVerification(plan) {
  const wrap = $("#verify-steps");
  const badge = $("#verify-badge");
  const stateBox = $("#verify-state");
  if (!wrap) return;

  const steps = plan.steps || [];
  stateBox.hidden = false;

  // The headline is the worst finding, so a warning is never hidden behind an
  // otherwise reassuring list.
  const warn = steps.filter((s) => s.status === "warn").length;
  const fail = steps.filter((s) => s.status === "fail").length;
  const level = fail ? "bad" : warn ? "warn" : "ok";
  const label = fail ? `${fail} problem${fail > 1 ? "s" : ""}`
    : warn ? `${warn} thing${warn > 1 ? "s" : ""} to note`
    : "all checks passed";
  badge.className = `badge ${level === "ok" ? "ok" : level === "warn" ? "" : "bad"}`;
  badge.textContent = label;
  $("#verify-summary").textContent = plan.summary || "";

  wrap.innerHTML = steps.map((s) => {
    const icon = s.status === "ok" ? "✓" : s.status === "warn" ? "!" : s.status === "fail" ? "✕" : "·";
    return `
      <div class="verify-step ${escapeHtml(s.status)}">
        <span class="verify-icon">${icon}</span>
        <div class="verify-body">
          <div class="verify-title">${escapeHtml(s.title)}
            ${s.actionable ? '<span class="badge">action needed</span>' : ""}
          </div>
          <div class="muted">${escapeHtml(s.detail)}</div>
          ${s.command ? `<code class="verify-cmd">${escapeHtml(s.command)}</code>` : ""}
        </div>
      </div>`;
  }).join("");

  // Show exactly which settings would change, so "apply" is never a blind write.
  const diff = $("#verify-diff");
  const changed = plan.changed || [];
  if (changed.length) {
    diff.hidden = false;
    diff.innerHTML = `
      <div class="verify-title">Settings that would change</div>
      <div class="muted">${changed.map((c) => `<code>${escapeHtml(c)}</code>`).join(" ")}</div>
      ${(plan.notes || []).map((n) => `<div class="muted">${escapeHtml(n)}</div>`).join("")}`;
  } else {
    diff.hidden = false;
    diff.innerHTML = `<div class="muted">Already configured for this machine — nothing to change.</div>
      ${(plan.notes || []).map((n) => `<div class="muted">${escapeHtml(n)}</div>`).join("")}`;
  }

  $("#btn-verify-apply").disabled = changed.length === 0;
  $("#btn-verify-apply-fast").disabled = changed.length === 0;
  $("#btn-verify-apply-fast").hidden = false;
}

async function applyPlan(verify) {
  const label = verify ? "Apply verified settings" : "Apply without testing";
  if (!confirm(`${label}?\n\n${verify
    ? "The model will be loaded and a test phrase spoken before anything is saved."
    : "Settings are saved immediately, without testing the model first."}`)) return;

  const btns = [$("#btn-verify-apply"), $("#btn-verify-apply-fast")];
  btns.forEach((b) => { b.disabled = true; });
  setSaveState(verify ? "testing model…" : "applying…", "warn");
  try {
    const res = await api("/admin/hardware/plan/run", {
      method: "POST",
      body: JSON.stringify({ verify }),
    });
    if (res.error) {
      // The plan was fine but the model could not run, so nothing was written.
      setSaveState("failed", "err");
      toast(res.error, "err");
      renderMeasured(null, res.error);
      return;
    }
    fillConfigForm(res.config);
    setSaveState("applied", "ok");
    toast(`Applied: ${res.summary || "settings updated"}`, "ok");
    renderMeasured(res.measured, null);
    renderTtsStatus();
    renderSttStatus();
    renderAccelerator();
    // Re-verify so the checklist reflects the config that is now live.
    await runVerification();
  } catch (err) {
    setSaveState("apply failed", "err");
    toast(`Could not apply: ${err.message}`, "err");
  } finally {
    btns.forEach((b) => { b.disabled = false; });
  }
}

// What the test run actually measured. Only shown when a test ran, because an
// unmeasured claim is the thing this panel exists to avoid.
function renderMeasured(measured, error) {
  const box = $("#verify-measured");
  if (!box) return;
  if (error) {
    box.hidden = false;
    box.innerHTML = `<div class="verify-step fail"><span class="verify-icon">✕</span>
      <div class="verify-body"><div class="verify-title">Test failed</div>
      <div class="muted">${escapeHtml(error)}</div></div></div>`;
    return;
  }
  if (!measured || !measured.model_id) {
    box.hidden = true;
    box.innerHTML = "";
    return;
  }
  box.hidden = false;
  const rows = {
    "Model": measured.model_id,
    "Loaded in": `${measured.load_seconds}s`,
    "Test phrase spoken in": `${measured.synth_seconds}s`,
    "Device used": measured.device || "—",
    "Backend": measured.backend || "—",
    "Voices": measured.voice_count ?? "—",
    "Audio produced": `${Math.round((measured.audio_bytes || 0) / 1024)} KB`,
  };
  box.innerHTML = `
    <div class="verify-step ok">
      <span class="verify-icon">✓</span>
      <div class="verify-body">
        <div class="verify-title">Tested and working</div>
        <div class="kv">${Object.entries(rows).map(([k, v]) =>
          `<div class="kv-row"><span>${escapeHtml(k)}</span><strong>${escapeHtml(String(v))}</strong></div>`
        ).join("")}</div>
      </div>
    </div>`;
}

async function renderStorageStatus() {
  const box = $("#storage-status");
  if (!box) return;
  try {
    const s = await api("/admin/storage");
    const uploads = s.uploads || {};
    box.innerHTML = Object.entries({
      Mode: s.portable ? "Portable (self-contained)" : "Installed",
      "Home folder": s.home,
      "Data folder": s.data_dir,
      "Data size": `${s.data_mb} MB`,
      "Models": `${s.models_mb} MB`,
      "Speech cache": `${s.cache_mb} MB`,
      "Uploaded files": `${uploads.files || 0} (${uploads.mb || 0} MB)`,
    }).map(([k, v]) => `<div class="kv-row"><span>${escapeHtml(k)}</span><strong>${escapeHtml(String(v ?? "—"))}</strong></div>`).join("");
  } catch { /* ignore */ }
}

/* -------------------------------------------------------- personalities */

function renderPersonaIndex() {
  const index = $("#persona-index");
  const activeId = state.config?.active_personality;
  index.innerHTML = state.personalities.map((p) => `
    <li data-id="${p.id}" class="${state.editing?.id === p.id ? "selected" : ""}">
      <span class="pi-dot" style="background:${p.accent}"></span>
      <span class="pi-body">
        <span class="pi-name">${escapeHtml(p.name)}</span>
        <span class="pi-tag">${escapeHtml(p.tagline || "")}</span>
      </span>
      ${p.id === activeId ? '<span class="badge ok">active</span>' : ""}
      ${p.enabled === false ? '<span class="badge">off</span>' : ""}
    </li>`).join("");

  index.querySelectorAll("li").forEach((li) => {
    li.addEventListener("click", () => editPersona(li.dataset.id));
  });
}

function editPersona(id) {
  const p = state.personalities.find((x) => x.id === id);
  if (!p) return;
  state.editing = p;
  const form = $("#persona-form");
  form.name.value = p.name || "";
  form.tagline.value = p.tagline || "";
  form.description.value = p.description || "";
  form.greeting.value = p.greeting || "";
  form.traits.value = p.traits || "";
  form.voice.value = p.voice || "Luna";
  form.speed.value = p.speed ?? 1.0;
  form.emotion.value = p.emotion || "";
  form.preset.value = p.preset || "stable";
  form.accent.value = p.accent || "#ff8fb1";
  form.icon.value = p.icon || "dot";
  form.initials.value = p.initials || "";
  form.enabled.checked = p.enabled !== false;

  $("#editor-title").textContent = p.name;
  $("#editor-badge").hidden = !p.builtin;
  $("#btn-save-persona").disabled = false;
  $("#btn-duplicate-persona").disabled = false;
  $("#btn-delete-persona").disabled = false;
  $("#preview-result").textContent = "";
  renderPersonaIndex();
}

function newPersona() {
  state.editing = { id: null, builtin: false };
  const form = $("#persona-form");
  form.reset();
  form.name.value = "New personality";
  form.tagline.value = "What she sounds like";
  form.traits.value = "Your manner: ";
  form.greeting.value = "";
  form.voice.value = "Luna";
  form.speed.value = 1.0;
  form.preset.value = "stable";
  form.accent.value = "#ff8fb1";
  form.icon.value = "dot";
  form.initials.value = "";
  form.enabled.checked = true;
  $("#editor-title").textContent = "New personality";
  $("#editor-badge").hidden = true;
  $("#btn-save-persona").disabled = false;
  $("#btn-duplicate-persona").disabled = true;
  $("#btn-delete-persona").disabled = true;
  $("#preview-result").textContent = "";
  renderPersonaIndex();
}

function personaFromForm() {
  const form = $("#persona-form");
  return {
    name: form.name.value.trim(),
    tagline: form.tagline.value.trim(),
    description: form.description.value.trim(),
    greeting: form.greeting.value.trim(),
    traits: form.traits.value.trim(),
    voice: form.voice.value,
    speed: Number(form.speed.value) || 1.0,
    emotion: form.emotion.value,
    preset: form.preset.value,
    accent: form.accent.value,
    icon: form.icon.value.trim() || "dot",
    initials: form.initials.value.trim().slice(0, 2),
    enabled: form.enabled.checked,
  };
}

async function savePersona(event) {
  event.preventDefault();
  const payload = personaFromForm();
  if (!payload.name) { toast("A name is required.", "err"); return; }
  setSaveState("saving…", "warn");
  try {
    let saved;
    if (state.editing?.id) {
      saved = await api(`/admin/personalities/${state.editing.id}`, {
        method: "PUT",
        body: JSON.stringify({ payload }),
      });
    } else {
      saved = await api("/admin/personalities", {
        method: "POST",
        body: JSON.stringify({ payload }),
      });
    }
    await loadPersonalities();
    editPersona(saved.id);
    setSaveState("saved", "ok");
    toast(`Saved ${saved.name}.`, "ok");
    setTimeout(() => setSaveState("idle"), 2000);
  } catch (err) {
    setSaveState("error", "err");
    toast(`Save failed: ${err.message}`, "err");
  }
}

async function deletePersona() {
  if (!state.editing?.id) return;
  if (!confirm(`Delete "${state.editing.name}"? This cannot be undone.`)) return;
  try {
    await api(`/admin/personalities/${state.editing.id}`, { method: "DELETE" });
    state.editing = null;
    await loadPersonalities();
    $("#persona-form").reset();
    $("#editor-title").textContent = "Select a personality";
    $("#btn-save-persona").disabled = true;
    $("#btn-duplicate-persona").disabled = true;
    $("#btn-delete-persona").disabled = true;
    toast("Personality deleted.", "ok");
  } catch (err) {
    toast(`Delete failed: ${err.message}`, "err");
  }
}

async function duplicatePersona() {
  if (!state.editing?.id) return;
  try {
    const copy = await api(`/admin/personalities/${state.editing.id}/duplicate`, { method: "POST" });
    await loadPersonalities();
    editPersona(copy.id);
    toast(`Created ${copy.name}.`, "ok");
  } catch (err) {
    toast(`Duplicate failed: ${err.message}`, "err");
  }
}

async function previewPersona() {
  const payload = personaFromForm();
  $("#preview-result").textContent = "Generating…";
  try {
    const result = await api("/admin/personalities/preview", {
      method: "POST",
      body: JSON.stringify({
        text: payload.greeting || "All systems are online. How may I be of assistance?",
        voice: payload.voice,
        speed: payload.speed,
        emotion: payload.emotion,
        preset: payload.preset,
      }),
    });
    $("#preview-result").textContent = "";
    const audio = new Audio(`data:${result.mime};base64,${result.audio_base64}`);
    await audio.play();
  } catch (err) {
    $("#preview-result").textContent = "";
    toast(`Preview failed: ${err.message}`, "err");
  }
}

async function loadPersonalities() {
  const data = await api("/admin/personalities");
  state.personalities = data.personalities || [];
  if (!state.config) state.config = {};
  state.config.active_personality = data.active;
  renderPersonaIndex();
}

function populateVoiceSelectors() {
  const voices = state.voices;
  const voiceOpts = voices.map((v) => `<option value="${v.id}">${escapeHtml(v.name)} — ${escapeHtml(v.character)}</option>`).join("");
  $("#voice-select").innerHTML = voiceOpts;
  $("#tts-voice-select").innerHTML = voiceOpts;

  const emotions = ["", "angry", "contemplative", "excited", "joyful", "mundane",
    "nervous", "sad", "stern", "surprised", "tender"];
  const emoOpts = emotions.map((e) => `<option value="${e}">${e || "(none)"}</option>`).join("");
  $("#emotion-select").innerHTML = emoOpts;
  $("#tts-emotion-select").innerHTML = emoOpts;

  const tiers = [
    ["KittenML/kitten-tts-nano-0.8-int8", "Nano (15M, fastest)"],
    ["KittenML/kitten-tts-micro-0.8", "Micro (40M)"],
    ["KittenML/kitten-tts-mini-0.8", "Mini (80M, best CPU)"],
    ["KittenML/kitten-tts-2", "Full 1.7B (GPU, 47 voices)"],
  ];
  $("#tts-model-select").innerHTML = tiers.map(([id, label]) => `<option value="${id}">${label}</option>`).join("");
}

/* ---------------------------------------------------------- voice profiles */

// Only the settings a profile is allowed to carry. Filtering here means a new
// tts.* control is included automatically, while llm.* and tools.* stay out.
function collectVoicePatch() {
  const full = collectConfigPatch();
  const patch = {};
  for (const section of ["tts", "voice_input"]) {
    if (full[section] && Object.keys(full[section]).length) patch[section] = full[section];
  }
  return patch;
}

// A profile is a saved patch of the voice settings. Applying one writes the
// patch server-side, then the form is refilled so the controls reflect it.
async function loadProfiles() {
  try {
    const data = await api("/admin/profiles");
    state.profiles = data.profiles || [];
    state.profileCurrent = data.current || {};
    state.profileActive = data.active || "";
    renderProfileSelect();
  } catch (err) {
    toast(`Could not load voice profiles: ${err.message}`, "err");
  }
}

function renderProfileSelect() {
  const sel = $("#profile-select");
  if (!sel) return;
  const selected = sel.value;
  const profiles = state.profiles || [];
  const active = state.profileActive || "";
  const opts = [`<option value="">(custom — not from a profile)</option>`];
  for (const p of profiles) {
    const flag = p.builtin ? "built in" : "saved";
    // Name the reason it is unusable here, so the option list itself warns
    // rather than leaving it to the description below.
    const warn = (p.problems || []).length ? " — cannot apply" : "";
    const mark = p.id === active ? " ✓ in use" : "";
    opts.push(`<option value="${escapeHtml(p.id)}">${escapeHtml(p.name)} (${flag})${mark}${warn}</option>`);
  }
  sel.innerHTML = opts.join("");
  // Default to showing the profile actually in effect, so the panel opens on
  // the truth rather than on an empty selection.
  sel.value = selected || active || "";
  updateProfileDescription();
}

function updateProfileDescription() {
  const id = $("#profile-select").value;
  const p = (state.profiles || []).find((x) => x.id === id);
  const box = $("#profile-description-view");
  const activeBadge = $("#profile-active-badge");
  const selectedBadge = $("#profile-selected-badge");
  const applyBtn = $("#btn-profile-apply");
  const deleteBtn = $("#btn-profile-delete");
  if (!box) return;

  const active = state.profileActive || "";
  const problems = p?.problems || [];
  const blocked = !p || problems.length > 0;
  const alreadyInUse = Boolean(p && id === active);

  // Badges state what is true: which profile is actually in effect, and whether
  // the highlighted one is merely selected.
  if (activeBadge) {
    activeBadge.hidden = !alreadyInUse;
    activeBadge.className = "badge ok";
  }
  if (selectedBadge) {
    selectedBadge.hidden = !p || alreadyInUse;
  }

  // Apply is pointless when nothing is selected, when it cannot apply, or when
  // it is already in effect. Delete is only for saved profiles.
  if (applyBtn) applyBtn.disabled = blocked || alreadyInUse;
  if (deleteBtn) deleteBtn.disabled = !p || Boolean(p.builtin);

  if (!p) {
    box.textContent = "The current voice settings do not match any saved profile.";
    return;
  }
  const lines = [];
  if (p.description) lines.push(p.description);
  if (problems.length) lines.push(`Cannot apply: ${problems.join("; ")}`);
  else if (alreadyInUse) lines.push("This profile is in effect.");
  box.textContent = lines.join(" ");
}

async function applyProfile() {
  const id = $("#profile-select").value;
  const out = $("#profile-result");
  if (!id) {
    out.textContent = "Pick a profile first.";
    return;
  }
  out.textContent = "applying…";
  try {
    const res = await api(`/admin/profiles/${encodeURIComponent(id)}/apply`, { method: "POST" });
    state.config = res.config || state.config;
    await refreshConfig();
    fillConfigForm(state.config);
    // Re-read so the "in use" badges reflect what was just applied rather than
    // what the panel assumed.
    await loadProfiles();
    $("#profile-select").value = id;
    updateProfileDescription();
    out.textContent = "applied";
    toast(`Applied ${id}.`, "ok");
    renderTtsStatus();
    renderSttStatus();
    renderAccelerator();
  } catch (err) {
    out.textContent = `failed — ${err.message}`;
    toast(`Could not apply profile: ${err.message}`, "err");
  }
}

async function saveProfile() {
  const out = $("#profile-result");
  const name = ($("#profile-name").value || "").trim();
  if (!name) {
    out.textContent = "Give the profile a name.";
    return;
  }
  out.textContent = "saving…";
  try {
    // Save what the form currently holds, so "save" means the settings the
    // operator is looking at rather than whatever was last written to disk.
    const settings = collectVoicePatch();
    const saved = await api("/admin/profiles", {
      method: "POST",
      body: JSON.stringify({
        name,
        description: $("#profile-description").value || "",
        settings,
        id: "",
      }),
    });
    await loadProfiles();
    $("#profile-select").value = saved.id;
    updateProfileDescription();
    out.textContent = `saved as ${saved.name}`;
    toast(`Saved profile "${saved.name}".`, "ok");
  } catch (err) {
    out.textContent = `failed — ${err.message}`;
    toast(`Could not save profile: ${err.message}`, "err");
  }
}

async function deleteProfile() {
  const id = $("#profile-select").value;
  const out = $("#profile-result");
  if (!id) {
    out.textContent = "Pick a saved profile first.";
    return;
  }
  const p = (state.profiles || []).find((x) => x.id === id);
  if (p?.builtin) {
    out.textContent = "Built-in profiles cannot be deleted. Save a copy instead.";
    return;
  }
  out.textContent = "deleting…";
  try {
    await api(`/admin/profiles/${encodeURIComponent(id)}`, { method: "DELETE" });
    await loadProfiles();
    out.textContent = "deleted";
    toast("Profile deleted.", "ok");
  } catch (err) {
    out.textContent = `failed — ${err.message}`;
  }
}

/* ------------------------------------------------------------ llm section */

function renderLlmModels(llm) {
  const dl = $("#llm-models");
  if (dl && llm?.model) dl.innerHTML = `<option value="${escapeHtml(llm.model)}"></option>`;
}

async function testLlm() {
  $("#llm-test-result").textContent = "testing…";
  try {
    const result = await api("/admin/integrations/test", {
      method: "POST",
      body: JSON.stringify({ target: "llm" }),
    });
    $("#llm-test-result").textContent = `${result.ok ? "OK" : "FAILED"} — ${result.detail}`;
    toast(result.ok ? "Model reachable." : `Model unreachable: ${result.detail}`, result.ok ? "ok" : "err");
  } catch (err) {
    $("#llm-test-result").textContent = `error — ${err.message}`;
  }
}

async function listLlmModels() {
  try {
    const data = await api("/admin/models");
    const dl = $("#llm-models");
    dl.innerHTML = (data.installed_llm || []).map((m) => `<option value="${escapeHtml(m)}"></option>`).join("");
    toast(`${(data.installed_llm || []).length} local models found.`, "ok");
  } catch (err) {
    toast(`Could not list models: ${err.message}`, "err");
  }
}

/* Finds a local model server and fills the form in, so a first run does not
   need the URL and model name typed from memory. Detection only reads; the
   operator still presses Save. */
async function detectLocalModels() {
  const out = $("#llm-detect-result");
  out.textContent = "looking…";
  try {
    const data = await api("/admin/llm/discover");
    const found = data.found || [];
    if (!found.length) {
      const tried = (data.candidates || []).map((c) => c.label).join(", ");
      out.textContent = `nothing found. Tried: ${tried}.`;
      return;
    }
    const best = found[0];
    $("[name='llm.provider']").value = "auto";
    $("[name='llm.base_url']").value = best.base_url;
    if (best.models?.length) $("[name='llm.model']").value = best.models[0];
    const dl = $("#llm-models");
    if (dl) dl.innerHTML = best.models.map((m) => `<option value="${escapeHtml(m)}"></option>`).join("");
    out.textContent = `found ${best.label} at ${best.base_url} (${best.models.join(", ")}). Press Save.`;
    toast(`Found ${best.label}. Press Save to use it.`, "ok");
  } catch (err) {
    out.textContent = `error — ${err.message}`;
    toast(`Detection failed: ${err.message}`, "err");
  }
}

/* -------------------------------------------------------------- tools/status */

async function loadIntegrations() {
  try {
    const data = await api("/admin/integrations");
    $("#tool-registry").innerHTML = (data.tools || []).map((t) => `
      <li class="${t.dangerous ? "dangerous" : ""}">
        <strong>${escapeHtml(t.name)}</strong>
        <span class="muted">${escapeHtml(t.description)}</span>
        ${t.requires ? `<span class="req">needs: ${escapeHtml(t.requires)}</span>` : ""}
      </li>`).join("");
  } catch (err) {
    toast(`Could not load integrations: ${err.message}`, "err");
  }
}

/* ------------------------------------------------------------------ skills */

async function loadSkills() {
  try {
    const data = await api("/admin/skills");
    state.knownTools = data.known_tools || [];
    renderSkills(data.skills || []);
  } catch (err) {
    toast(`Could not load skills: ${err.message}`, "err");
  }
}

function renderSkills(items) {
  const list = $("#skill-list");
  if (!list) return;
  if (!items.length) {
    list.innerHTML = `<li class="muted">No skills. Restore the built-ins to start again.</li>`;
    return;
  }
  list.innerHTML = items.map((s) => {
    // A skill whose provider is missing is on but inert; say so rather than
    // showing it as fully live.
    const status = s.active
      ? `<span class="req ok">active</span>`
      : s.enabled
        ? `<span class="req">waiting on: ${escapeHtml(s.requires || "its integration")}</span>`
        : `<span class="req">off</span>`;
    const tools = (s.tools || []).length
      ? `<span class="muted">tools: ${s.tools.map(escapeHtml).join(", ")}</span>`
      : "";
    return `<li data-skill="${escapeHtml(s.id)}">
      <label class="checkbox">
        <input type="checkbox" data-skill-toggle="${escapeHtml(s.id)}" ${s.enabled ? "checked" : ""} />
        <span>
          <strong>${escapeHtml(s.name)}</strong>
          <span class="muted">${escapeHtml(s.tagline || "")}</span>
        </span>
      </label>
      ${status} ${tools}
      <span class="muted">${escapeHtml(s.description || "")}</span>
      ${s.builtin ? "" : `<button class="btn-ghost" data-skill-delete="${escapeHtml(s.id)}">Delete</button>`}
    </li>`;
  }).join("");

  list.querySelectorAll("[data-skill-toggle]").forEach((box) => {
    box.addEventListener("change", () => toggleSkill(box.dataset.skillToggle, box.checked));
  });
  list.querySelectorAll("[data-skill-delete]").forEach((btn) => {
    btn.addEventListener("click", () => deleteSkill(btn.dataset.skillDelete));
  });
}

async function toggleSkill(id, enabled) {
  try {
    await api(`/admin/skills/${encodeURIComponent(id)}`, {
      method: "PUT",
      body: JSON.stringify({ payload: { enabled } }),
    });
    toast(`${enabled ? "Enabled" : "Disabled"} skill '${id}'.`, "ok");
    loadSkills();
  } catch (err) {
    toast(`Could not update skill: ${err.message}`, "err");
    loadSkills();
  }
}

async function deleteSkill(id) {
  try {
    await api(`/admin/skills/${encodeURIComponent(id)}`, { method: "DELETE" });
    toast(`Deleted skill '${id}'.`, "ok");
    loadSkills();
  } catch (err) {
    toast(`Could not delete skill: ${err.message}`, "err");
  }
}

async function addSkill() {
  const out = $("#skill-add-result");
  const name = $("#skill-new-name").value.trim();
  if (!name) {
    if (out) out.textContent = "Give the skill a name first.";
    return;
  }
  const payload = {
    name,
    tagline: $("#skill-new-tagline").value.trim(),
    instructions: $("#skill-new-instructions").value.trim(),
    tools: $("#skill-new-tools").value.split(",").map((s) => s.trim()).filter(Boolean),
  };
  try {
    await api("/admin/skills", { method: "POST", body: JSON.stringify({ payload }) });
    if (out) out.textContent = `Added '${name}'.`;
    ["#skill-new-name", "#skill-new-tagline", "#skill-new-instructions", "#skill-new-tools"]
      .forEach((sel) => { $(sel).value = ""; });
    loadSkills();
  } catch (err) {
    if (out) out.textContent = `error — ${err.message}`;
  }
}

async function testIntegration(target) {
  const out = document.querySelector(`[data-test-result="${target}"]`);
  if (out) out.textContent = "testing…";
  try {
    const result = await api("/admin/integrations/test", {
      method: "POST",
      body: JSON.stringify({ target }),
    });
    if (out) out.textContent = `${result.ok ? "OK" : "FAILED"} — ${result.detail}`;
  } catch (err) {
    if (out) out.textContent = `error — ${err.message}`;
  }
}

async function refreshStorage() {
  try {
    const s = await api("/admin/stats");
    $("#storage-stats").innerHTML = Object.entries({
      "Cached speech files": s.cache_files,
      "Speech cache size": `${s.cache_mb} MB`,
      "Downloaded models": `${s.models_mb} MB`,
      "Personalities": s.personalities,
      "Registered tools": s.tools,
    }).map(([k, v]) => `<div class="kv-row"><span>${k}</span><strong>${v}</strong></div>`).join("");
  } catch { /* ignore */ }
}

async function refreshLog() {
  try {
    const data = await api("/admin/logs?lines=200");
    const view = $("#log-view");
    view.textContent = (data.lines || []).join("\n") || "(no log yet)";
    view.scrollTop = view.scrollHeight;
  } catch (err) {
    $("#log-view").textContent = `Could not read log: ${err.message}`;
  }
}

async function refreshConfig() {
  const cfg = await api("/admin/config");
  fillConfigForm(cfg);
}

/* ------------------------------------------------------------------ wire */

function wireEvents() {
  $$(".tab").forEach((t) => t.addEventListener("click", () => selectTab(t.dataset.tab)));
  $("#btn-back").addEventListener("click", () => { window.location.href = "/"; });

  $("#gate-form").addEventListener("submit", async (e) => {
    e.preventDefault();
    state.token = $("#gate-token").value.trim();
    sessionStorage.setItem("rexeniot_admin_token", state.token);
    $("#gate-error").hidden = true;
    if (await checkSession()) {
      boot();
    } else {
      const err = $("#gate-error");
      err.textContent = "That token was not accepted.";
      err.hidden = false;
    }
  });

  $("#btn-scan").addEventListener("click", () => runScan(false));
  $("#btn-apply-recs").addEventListener("click", applyRecommendations);
  $("#btn-verify").addEventListener("click", runVerification);
  $("#btn-verify-apply").addEventListener("click", () => applyPlan(true));
  $("#btn-verify-apply-fast").addEventListener("click", () => applyPlan(false));

  $$("#model-filter .seg-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      state.modelFilter = btn.dataset.kind;
      $$("#model-filter .seg-btn").forEach((b) => b.classList.toggle("active", b === btn));
      renderModels(state.hardware?.recommendations?.models || []);
    });
  });

  $("#btn-tts-warm").addEventListener("click", async () => {
    try {
      const cfg = collectConfigPatch();
      await api("/admin/models/load", {
        method: "POST",
        body: JSON.stringify({ repo: cfg.tts?.model_id || state.config?.tts?.model_id, kind: "tts" }),
      });
      toast("Speech engine loaded.", "ok");
      renderTtsStatus();
    } catch (err) { toast(err.message, "err"); }
  });

  $("#btn-tts-reload")?.addEventListener("click", async () => {
    try {
      const r = await api("/admin/tts/reload", { method: "POST" });
      toast(`Engine reloaded on ${r.engine?.device || "auto"}.`, "ok");
      renderTtsStatus();
      renderAccelerator();
    } catch (err) { toast(err.message, "err"); }
  });

  $("#btn-tts-clear").addEventListener("click", async () => {
    try {
      const r = await api("/admin/tts/clear-cache", { method: "POST" });
      toast(`Removed ${r.removed} cached clips.`, "ok");
      refreshStorage();
    } catch (err) { toast(err.message, "err"); }
  });

  $("#btn-stt-preview")?.addEventListener("click", previewStt);

  $("#btn-stt-install")?.addEventListener("click", async () => {
    await installPrerequisites("stt");
    renderSttStatus();
  });

  $("#btn-stt-reload")?.addEventListener("click", async () => {
    try {
      const r = await api("/admin/stt/reload", { method: "POST" });
      toast(`Dictation engine reloaded on ${r.engine?.device || "auto"}.`, "ok");
      renderSttStatus();
    } catch (err) { toast(err.message, "err"); }
  });

  $("#profile-select").addEventListener("change", updateProfileDescription);
  $("#btn-profile-apply").addEventListener("click", applyProfile);
  $("#btn-profile-save").addEventListener("click", saveProfile);
  $("#btn-profile-delete").addEventListener("click", deleteProfile);

  $("#btn-voice-preview").addEventListener("click", async () => {
    const out = $("#voice-preview-result");
    out.textContent = "generating…";
    try {
      const r = await api("/admin/personalities/preview", {
        method: "POST",
        body: JSON.stringify({
          text: "All systems are online. How may I be of assistance?",
          voice: $("#tts-voice-select").value,
          speed: Number($("#tts-status") ? ($("[name='tts.speed']").value || 1) : 1),
          emotion: $("#tts-emotion-select").value,
          preset: $("#tts-status") ? $("[name='tts.preset']").value : "stable",
        }),
      });
      out.textContent = `${r.bytes > 0 ? "ready" : ""}`;
      new Audio(`data:${r.mime};base64,${r.audio_base64}`).play();
    } catch (err) {
      out.textContent = "";
      toast(`Preview failed: ${err.message}`, "err");
    }
  });

  $("#btn-new-persona").addEventListener("click", newPersona);
  $("#persona-form").addEventListener("submit", savePersona);
  $("#btn-delete-persona").addEventListener("click", deletePersona);
  $("#btn-duplicate-persona").addEventListener("click", duplicatePersona);
  $("#btn-preview-persona").addEventListener("click", previewPersona);

  $("#btn-restore-builtins").addEventListener("click", async () => {
    try {
      await api("/admin/personalities/restore", { method: "POST" });
      await loadPersonalities();
      toast("Built-in personalities restored.", "ok");
    } catch (err) { toast(err.message, "err"); }
  });

  $("#btn-llm-test").addEventListener("click", testLlm);
  $("#btn-llm-list").addEventListener("click", listLlmModels);
  $("#btn-llm-detect")?.addEventListener("click", detectLocalModels);

  $$("#llm-quickpicks .chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      $("[name='llm.provider']").value = chip.dataset.provider;
      $("[name='llm.base_url']").value = chip.dataset.url;
      toast("Filled connection details. Press Save when ready.", "ok");
    });
  });

  $$("[data-test]").forEach((btn) => {
    btn.addEventListener("click", () => testIntegration(btn.dataset.test));
  });

  $("#btn-refresh-log").addEventListener("click", refreshLog);

  $("#btn-add-skill")?.addEventListener("click", addSkill);
  $("#btn-restore-skills")?.addEventListener("click", async () => {
    try {
      await api("/admin/skills/restore", { method: "POST" });
      toast("Built-in skills restored.", "ok");
      loadSkills();
    } catch (err) {
      toast(`Could not restore: ${err.message}`, "err");
    }
  });

  // Debounced auto-save so the operator does not lose edits.
  let saveTimer = null;
  $$("[data-cfg]").forEach((el) => {
    const evt = el.tagName === "SELECT" || el.type === "checkbox" ? "change" : "input";
    el.addEventListener(evt, () => {
      clearTimeout(saveTimer);
      setSaveState("unsaved", "warn");
      saveTimer = setTimeout(() => saveConfig().catch(() => {}), 1400);
    });
  });

  $$(".panel-actions .btn-primary, #btn-save-persona").forEach((b) => b.addEventListener("click", () => {}));
}

/* ------------------------------------------------------------------- boot */

async function boot() {
  wireEvents();
  const ok = await checkSession();
  if (!ok) return;

  try {
    await refreshConfig();
    await loadPersonalities();
    populateVoiceSelectors();

    const voices = await fetch("/api/voices").then((r) => r.json()).catch(() => ({ voices: [] }));
    state.voices = voices.voices || [];
    populateVoiceSelectors();
    // Re-apply the stored values now that the option lists exist.
    fillConfigForm(state.config);

    const cached = await api("/admin/hardware").catch(() => ({ scanned: false }));
    if (cached.scanned) renderHardware(cached);
    else runScan(true);

    renderTtsStatus();
    renderSttStatus();
    renderAccelerator();
    loadIntegrations();
    refreshStorage();
    refreshLog();
    loadProfiles();
  } catch (err) {
    toast(`Admin panel problem: ${err.message}`, "err");
  }
}

boot();

/**
 * Exercises the real wake-mode state machine from web/js/app.js against a
 * stubbed DOM. This is the same code the browser runs, not a reimplementation.
 */
import fs from "fs";

const src = fs.readFileSync("web/js/app.js", "utf8")
  // Drop the boot() call and the top-level module wiring; we drive by hand.
  .replace(/\nboot\(\);\s*$/, "\n")
  .replace(/^const \$ = .*$/m, "const $ = globalThis.__$;");

const els = new Map();
function makeEl(id) {
  const el = {
    id, hidden: false, value: "", textContent: "", innerHTML: "",
    dataset: {}, attrs: {}, children: [],
    setAttribute(k, v) { this.attrs[k] = v; },
    getAttribute(k) { return this.attrs[k]; },
    appendChild(c) { this.children.push(c); },
    addEventListener() {},
    remove() {},
    closest() { return null; },
    querySelector() { return makeEl("anon"); },
    querySelectorAll() { return []; },
    classList: { add() {}, remove() {}, toggle() {} },
    style: {},
    getContext: () => ({
      clearRect() {}, fillRect() {}, beginPath() {}, roundRect() {}, fill() {},
      arc() {}, stroke() {}, moveTo() {}, lineTo() {}, save() {}, restore() {},
      fillText() {}, measureText: () => ({ width: 10 }),
      createLinearGradient: () => ({ addColorStop() {} }),
      getByteFrequencyData() {},
      frequencyBinCount: 32,
      canvas: { width: 300, height: 100 },
    }),
    get scrollHeight() { return 10; },
    get width() { return 300; },
    get height() { return 100; },
  };
  return el;
}
globalThis.__$ = (sel) => {
  if (!els.has(sel)) els.set(sel, makeEl(sel));
  return els.get(sel);
};
globalThis.document = {
  querySelector: globalThis.__$,
  querySelectorAll: () => [],
  addEventListener() {},
  createElement: () => makeEl("created"),
  documentElement: { style: { setProperty() {} } },
  getElementById: (id) => globalThis.__$(id),
};
globalThis.window = {};
globalThis.performance = { now: () => Date.now() };
globalThis.requestAnimationFrame = () => {};
globalThis.setTimeout = (fn, ms) => { pending.push({ fn, ms }); return pending.length; };
globalThis.clearTimeout = () => {};
const pending = [];

// Capture what the machine decides to send and speak.
const sent = [];
const spoken = [];

const mod = new Function(
  "sent", "spoken", "__export",
  `${src}
   __export({ WAKE, state, armWakeMode, disarmWakeMode, handleWakeUtterance, setWakeMode,
              findPhrase, stripWakeWord, matchesPhrase });
   return null;`
);

let exported = null;
mod(sent, spoken, (o) => { exported = o; });

const { WAKE, handleWakeUtterance } = exported;

// Seed the settings the real app loads from /api/config.
exported.state.config = {
  voice_input: {
    wake_words: ["hey rexeniot", "okay rexeniot", "hey sister", "wake up rexeniot"],
    sleep_words: ["stop listening", "go to sleep", "that's all", "stand down"],
    session_idle_s: 45,
    acknowledge: false,
    acknowledgement: "I'm here.",
  },
};
// The app starts in wake mode only after the button is pressed; start asleep.
exported.setWakeMode("asleep");

// Drive the state machine through a realistic conversation.
const events = [
  ["ambient chatter, nobody talking to her", "asleep"],
  ["hey rexeniot", "awake"],
  ["turn on the kitchen lights", "awake"],
  ["and dim them to twenty percent", "awake"],
  ["stop listening", "asleep"],
  ["hey rexeniot what is the weather", "awake"],
  ["go to sleep", "asleep"],
];

let failures = 0;
console.log("Wake-mode state machine (real app.js code)\n");
for (const [utterance, expect] of events) {
  handleWakeUtterance(utterance, true);
  const ok = WAKE.mode === expect;
  if (!ok) failures++;
  console.log(`  ${ok ? "PASS" : "FAIL"}  ${JSON.stringify(utterance).padEnd(42)} -> ${WAKE.mode}` +
              (ok ? "" : `  (expected ${expect})`));
}

// A wake word said in the same breath as a command must yield the command.
console.log("\nWake-word stripping\n");
for (const [said, want] of [
  ["hey rexeniot turn on the porch light", "turn on the porch light"],
  ["okay rexeniot play some music", "play some music"],
  ["heyrexeniot set a timer for five minutes", "set a timer for five minutes"],
]) {
  const got = exported.stripWakeWord(said);
  const ok = got === want;
  if (!ok) failures++;
  console.log(`  ${ok ? "PASS" : "FAIL"}  ${JSON.stringify(said).padEnd(42)} -> ${JSON.stringify(got)}`);
}

// The agent's own reply must never trigger a wake.
console.log("\nSelf-trigger guard\n");
const selfReply = "I'm here. The kitchen lights are on.";
const matched = exported.findPhrase(selfReply, ["hey rexeniot", "okay rexeniot", "hey sister"]);
const ok = matched === null;
if (!ok) failures++;
console.log(`  ${ok ? "PASS" : "FAIL"}  her own reply does not match a wake word`);

console.log(`\n${failures === 0 ? "ALL PASSED" : failures + " FAILED"}`);
process.exit(failures === 0 ? 0 : 1);

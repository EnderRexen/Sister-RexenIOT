/* ============================================================================
   Optional on-screen keyboard.

   For touch screens and kiosks with no physical keys. Rows are generated from
   one layout table so left/right shift and the symbol layer stay in sync, and
   keys write through the textarea's own value so the existing auto-grow and
   Enter-to-send behaviour keep working untouched.
   ========================================================================== */

const LAYOUTS = {
  letters: [
    ["q", "w", "e", "r", "t", "y", "u", "i", "o", "p"],
    ["a", "s", "d", "f", "g", "h", "j", "k", "l"],
    ["shift", "z", "x", "c", "v", "b", "n", "m", "backspace"],
    ["symbols", "comma", "space", "period", "enter"],
  ],
  symbols: [
    ["1", "2", "3", "4", "5", "6", "7", "8", "9", "0"],
    ["@", "#", "$", "%", "&", "-", "_", "+", "="],
    ["(", ")", "*", '"', "'", "/", "\\", "backspace"],
    ["letters", ":", "space", "?", "enter"],
  ],
};

/* What each named key inserts, and how the button reads. */
const KEYS = {
  shift: { label: "⇧ Shift", cls: "wide" },
  backspace: { label: "⌫ Delete", cls: "wide" },
  enter: { label: "⏎ Send", cls: "wide" },
  space: { label: "Space", cls: "space" },
  comma: { label: ",", cls: "wide" },
  period: { label: ".", cls: "wide" },
  symbols: { label: "#+=", cls: "wide" },
  letters: { label: "ABC", cls: "wide" },
};

export function initOnScreenKeyboard({ input, toggleButton, panel, onChange }) {
  if (!input || !panel) return null;

  const rowsBox = panel.querySelector("#osk-rows");
  const closeButton = panel.querySelector("#osk-close");
  const modeButtons = [...panel.querySelectorAll(".osk-mode")];

  let layout = "letters";
  let shift = false;
  let visible = false;
  let capsLock = false;
  let caret = null;
  let settleTimer = null;

  const isShifted = () => shift || capsLock;
  /* A fine pointer means a real keyboard is likely; a coarse one means the OS
     would pop its own soft keyboard over ours, so we leave the field unfocused. */
  const hasPhysicalPointer = () => window.matchMedia?.("(pointer: fine)")?.matches ?? true;

  /* Where the next character lands. When the field has focus we defer to the
     real selection; otherwise we remember the caret ourselves, because an
     unfocused textarea reports a selection of zero and text would land at the
     front of the line. */
  function selection() {
    if (document.activeElement === input) {
      const start = input.selectionStart ?? input.value.length;
      return [start, input.selectionEnd ?? start];
    }
    const at = caret ?? input.value.length;
    return [at, at];
  }

  function placeCaret(at) {
    caret = at;
    input.setSelectionRange(at, at);
  }

  function labelFor(key) {
    const spec = KEYS[key];
    if (spec) return spec;
    const isLetter = /^[a-z]$/.test(key);
    return {
      label: isLetter && isShifted() ? key.toUpperCase() : key,
      cls: isLetter ? "letter" : "",
    };
  }

  function render() {
    rowsBox.innerHTML = "";
    const keys = LAYOUTS[layout];
    keys.forEach((rowKeys, rowIndex) => {
      const row = document.createElement("div");
      row.className = "osk-row";
      rowKeys.forEach((key, colIndex) => {
        const spec = labelFor(key);
        const btn = document.createElement("button");
        btn.type = "button";
        btn.className = `osk-key ${spec.cls || ""}`.trim();
        btn.textContent = spec.label;
        btn.dataset.key = key;
        if (key === "shift") {
          btn.classList.toggle("active", shift);
          btn.title = capsLock ? "Caps lock on (tap again to clear)" : "Shift";
        }
        // Stagger each key so the board assembles itself as the tray seats.
        btn.style.animationDelay = `${rowIndex * 45 + colIndex * 14}ms`;
        row.appendChild(btn);
      });
      rowsBox.appendChild(row);
    });
  }

  function insertText(text) {
    const [start, end] = selection();
    input.value = input.value.slice(0, start) + text + input.value.slice(end);
    placeCaret(start + text.length);
    onChange?.();
  }

  function backspace() {
    const [start, end] = selection();
    if (start !== end) {
      input.value = input.value.slice(0, start) + input.value.slice(end);
      placeCaret(start);
    } else if (start > 0) {
      input.value = input.value.slice(0, start - 1) + input.value.slice(start);
      placeCaret(start - 1);
    }
    onChange?.();
  }

  function setLayout(name) {
    layout = name;
    shift = false;
    modeButtons.forEach((b) => b.classList.toggle("active", b.dataset.layout === name));
    render();
  }

  function press(key) {
    if (key === "shift") {
      // Pressing shift while it is already on becomes a sticky caps lock; the
      // next press clears it. A short tap on an already-off shift is a one-shot.
      if (capsLock) {
        capsLock = false;
        shift = false;
      } else if (shift) {
        capsLock = true;
      } else {
        shift = true;
      }
      render();
      return;
    }
    if (key === "backspace") return backspace();
    if (key === "enter") return enterPressed();
    if (key === "space") return insertText(" ");
    if (layout === "letters" && key === "symbols") return setLayout("symbols");
    if (layout === "symbols" && key === "letters") return setLayout("letters");

    const isLetter = /^[a-z]$/.test(key);
    const text = isLetter && isShifted() ? key.toUpperCase() : key;
    insertText(text);
    // One-shot shift, but caps lock stays on.
    if (isLetter && shift && !capsLock) {
      shift = false;
      render();
    }
  }

  function enterPressed() {
    // Shift+Enter is a newline everywhere else in the app, so the soft key
    // follows that rather than silently sending.
    if (isShifted()) return insertText("\n");
    const event = new KeyboardEvent("keydown", { key: "Enter", bubbles: true, cancelable: true });
    const allowed = input.dispatchEvent(event);
    if (allowed) {
      // The app's own listener calls preventDefault when it handles the key. If
      // nothing handled it, submit anyway so the key always sends.
      input.form?.requestSubmit();
    }
  }

  rowsBox.addEventListener("pointerdown", (e) => {
    const btn = e.target.closest(".osk-key");
    if (!btn) return;
    // Press on pointerdown for immediate feedback, and keep focus off the key so
    // the textarea never loses its caret or closes the panel.
    e.preventDefault();
    btn.classList.add("pressed");
    press(btn.dataset.key);
  });
  rowsBox.addEventListener("pointerup", (e) => {
    e.target.closest(".osk-key")?.classList.remove("pressed");
  });
  rowsBox.addEventListener("pointerleave", (e) => {
    e.target.closest(".osk-key")?.classList.remove("pressed");
  });

  modeButtons.forEach((b) => b.addEventListener("click", () => setLayout(b.dataset.layout)));
  closeButton?.addEventListener("click", () => setVisible(false));

  // A physical Enter or Escape should behave like the soft keys.
  input.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && visible) setVisible(false);
  });

  function setVisible(next) {
    if (next === visible) return;
    visible = next;
    if (toggleButton) toggleButton.setAttribute("aria-pressed", String(next));
    clearTimeout(settleTimer);

    if (next) {
      panel.hidden = false;
      render();
      // Force a style flush so the tray starts down before the slot opens;
      // otherwise the browser collapses both changes into one frame and the
      // slide never plays.
      void panel.offsetHeight;
      panel.classList.add("open");
      if (!hasPhysicalPointer()) {
        // Focus for the caret and for hardware keys, then immediately blur so
        // the OS does not raise its own keyboard over this one.
        input.focus({ preventScroll: true });
        placeCaret(caret ?? input.value.length);
        input.blur();
      }
    } else {
      panel.classList.remove("open");
      // Keep the slot mounted until the tray has finished tucking away.
      settleTimer = setTimeout(() => { if (!visible) panel.hidden = true; }, 540);
    }
  }

  render();
  return {
    toggle: () => setVisible(!visible),
    show: () => setVisible(true),
    hide: () => setVisible(false),
    /** Adopt the caret the user just placed by tapping or typing in the field. */
    syncCaret: () => {
      if (document.activeElement === input) caret = input.selectionStart ?? null;
    },
    get visible() { return visible; },
  };
}

"""Verify the voice-engine setting is wired end to end.

The requirement is that the vocal model is the default, but the operator can
switch to browser speech, and that an unusable model degrades instead of going
silent. Three things have to hold, and each is easy to break on its own:

1. ``tts.engine`` exists with model as the default, and rejects junk values.
2. ``/api/config`` publishes the engine, or the browser never learns the choice.
3. ``web/js/app.js`` implements the model-first order and the browser path.

Run: PYTHONPATH=. ./.venv/bin/python scripts/test_tts_fallback.py
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.config import AppConfig  # noqa: E402

failures: list[str] = []
notes: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


print("tts.engine schema")
field = AppConfig.model_fields["tts"].annotation
engine = field.model_fields.get("engine") if hasattr(field, "model_fields") else None
check(engine is not None, "TTSConfig declares an engine field")
if engine is not None:
    check(engine.default == "model", f"default is model (got {engine.default!r})")
    allowed = set(getattr(engine.annotation, "__args__", ()))
    check(
        allowed == {"model", "browser", "auto"},
        f"accepts exactly model/browser/auto (got {sorted(allowed)})",
    )

print("\nrejects an unknown engine")
try:
    AppConfig(tts={"engine": "carrier-pigeon"})
    check(False, "a bogus engine is rejected")
except Exception:
    check(True, "a bogus engine is rejected")

print("\nadmin page exposes the choice")
html = (ROOT / "web" / "admin.html").read_text()
check('data-cfg="tts.engine"' in html, "admin.html binds tts.engine")
for value in ("model", "browser", "auto"):
    check(
        f'value="{value}"' in html.split('data-cfg="tts.engine"')[1].split("</select>")[0],
        f"admin.html offers the {value!r} option",
    )

print("\npublic config publishes it")
chat = (ROOT / "app" / "routers" / "chat.py").read_text()
public = chat.split("def public_config")[1] if "def public_config" in chat else ""
check('"engine": cfg.tts.engine' in public, "/api/config returns the engine")

print("\nfrontend implements model-first with a browser path")
js = (ROOT / "web" / "js" / "app.js").read_text()
check("function speakWithBrowser" in js, "app.js has a browser speech path")
check("function fallbackToBrowser" in js, "app.js has a fallback path")
check("SpeechSynthesisUtterance" in js, "app.js uses the browser speech API")
check("speechSynthesis.cancel()" in js, "app.js cancels browser speech on stop")

# The default must stay model-first: "model" returns before any browser call, and
# only "browser" (or an unusable model in "auto") diverts.
should = js.split("function shouldUseBrowserTts()")[1].split("\n}")[0] if "function shouldUseBrowserTts()" in js else ""
check('mode === "model") return false' in should, "model mode stays on the vocal model")
check('mode === "browser") return true' in should, "browser mode diverts")
check("ttsModelUsable === false" in should, "auto mode diverts when the model is unusable")

# The model path must still be attempted before the browser in the default case.
speak_body = js.split("async function speak(")[1].split("\n}\n")[0] if "async function speak(" in js else ""
check("/api/speak" in speak_body, "the default path still calls /api/speak")
check(
    speak_body.index("shouldUseBrowserTts") < speak_body.index("/api/speak"),
    "the engine decision happens before the model request",
)

print("\nstop path clears browser speech")
stop = js.split("function stopSpeaking()")[1].split("\n}")[0] if "function stopSpeaking()" in js else ""
check("browserSpeaking" in stop, "stopSpeaking clears a pending browser utterance")

print()
for n in notes:
    print(f"note: {n}")
if failures:
    print(f"{len(failures)} FAILED")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("all checks passed")

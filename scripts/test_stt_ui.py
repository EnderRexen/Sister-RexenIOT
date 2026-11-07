"""Dictation in a real browser: record -> upload -> transcribe -> composer.

This is the only test that exercises the chain that actually ships. It feeds
Chromium a synthetic microphone (a WAV of real speech made by the TTS engine),
clicks the mic button, and requires the composer to receive the exact words. A
unit test of the engine cannot catch a wrong FormData field name, a bad mime
type, or a recorder that never releases the device; this can.

    python scripts/test_stt_ui.py [base_url]
"""
import io
import sys
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8070"
CLIP = "/tmp/rexeniot_fake_mic.wav"
PHRASE = "Turn off the kitchen lights."
CHROME = "/usr/bin/chromium"

FAILS = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


def main() -> int:
    from app.tts import engine as tts

    Path(CLIP).write_bytes(tts.wav_bytes(PHRASE, use_cache=False))
    print(f"synthetic microphone: {PHRASE!r} -> {CLIP}")

    with sync_playwright() as p:
        browser = p.chromium.launch(
            executable_path=CHROME,
            args=[
                "--no-sandbox",
                "--use-fake-ui-for-media-stream",
                "--use-fake-device-for-media-stream",
                f"--use-file-for-fake-audio-capture={CLIP}",
                "--autoplay-policy=no-user-gesture-required",
            ],
        )
        page = browser.new_page()
        errors = []
        page.on("pageerror", lambda e: errors.append(str(e)))

        page.goto(BASE + "/", wait_until="load")
        page.wait_for_selector("#boot.done", timeout=30000)

        print("\n=== configuration ===")
        cfg = page.evaluate("() => window.__rexeniot.state.config")
        stt = (cfg or {}).get("stt") or {}
        vi = (cfg or {}).get("voice_input") or {}
        print(f"    stt={stt}  voice_input.engine={vi.get('engine')}")
        check("the server reports dictation available", stt.get("available") is True)
        check("capture is wired for this machine",
              page.evaluate("() => window.__rexeniot.useLocalCapture()") is True)
        check("the mic button says so",
              "this machine" in (page.get_attribute("#btn-mic", "title") or ""))

        print("\n=== record -> upload -> transcribe ===")
        # Leave auto-send off so the text can be inspected before it is sent.
        page.evaluate("() => { window.__rexeniot.state.autoSend = false; }")
        page.click("#btn-mic")
        page.wait_for_timeout(2600)
        check("the recorder is running", page.evaluate("() => window.__rexeniot.state.listening") is True)

        page.click("#btn-mic")
        try:
            page.wait_for_function(
                "() => (document.querySelector('#input').value || '').trim().length > 0",
                timeout=60000,
            )
            heard = page.input_value("#input")
        except Exception as exc:
            heard = ""
            print(f"    (timed out: {exc})")
        print(f"    composer: {heard!r}")
        check("the transcript reached the composer", bool(heard.strip()), heard)
        check("and it is exactly what was spoken",
              heard.lower().strip(" .") == PHRASE.lower().strip(" ."), heard)
        # A recorder that keeps the device makes the next press fail.
        check("the microphone was released",
              page.evaluate("() => window.__rexeniot.state.listening") is False)

        print("\n=== admin panel ===")
        page.goto(BASE + "/admin", wait_until="load")
        page.wait_for_timeout(4000)
        card = page.evaluate(
            "() => { const e = document.querySelector('#stt-status');"
            " return e ? e.textContent : null; }"
        )
        print(f"    #stt-status: {card!r}")
        check("the dictation card rendered", bool(card and card.strip()))
        check("it reports the engine ready", "ready" in (card or "").lower())

        check("no page errors", not errors, "; ".join(errors[:3]))
        browser.close()

    print()
    if FAILS:
        print(f"{len(FAILS)} FAILURE(S)")
        for f in FAILS:
            print(f"  - {f}")
        return 1
    print("all dictation UI checks passed")
    return 0


if __name__ == "__main__":
    sys.exit(main())

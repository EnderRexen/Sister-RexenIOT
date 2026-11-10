"""Sister RexenIOT application entry point.

Serves the JSON API under ``/api`` and the static HUD from ``/``. Run it with
``python -m app.main`` or let ``start.cmd`` do the prerequisite checks first.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, config, hardware, model_catalog, personalities
from .llm import adopt_first_local, adopt_first_local_in_background
from .routers import admin, chat, status
from .security import assert_binding_is_safe
from .stt import available as stt_available, engine as stt_engine
from .tts import engine

log = logging.getLogger("rexeniot")


def _dev_origins() -> list[str]:
    """Origins allowed to call the API from a browser.

    The UI is served from the same origin and needs no grant. Anything else is
    restricted to loopback, so a website you happen to visit cannot script this
    agent in your browser. ``allow_origins=["*"]`` used to do exactly that.
    """
    port = config.load().server.port
    return [f"http://127.0.0.1:{port}", f"http://localhost:{port}"]


def configure_logging() -> None:
    config.ensure_dirs()
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stdout)]
    try:
        handlers.append(logging.FileHandler(config.DATA_DIR / "rexeniot.log", encoding="utf-8"))
    except Exception:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )


@asynccontextmanager
async def lifespan(app: FastAPI):
    configure_logging()
    cfg = config.load()
    profile = hardware.cached()
    log.info("Sister RexenIOT %s starting", __version__)
    if profile:
        log.info("hardware: %s", hardware.summary(profile))
    else:
        log.info("no hardware scan yet; run it from the admin panel")

    personalities.load()  # seed built-ins on first boot
    log.info("personalities: %d loaded, active '%s'", len(personalities.load()), personalities.active().name)

    # A fresh install should be able to talk without anyone opening the admin
    # panel first, so look for a local model server once. Guarded on the model
    # being unset, so this never overwrites a configuration that already works
    # and never runs again on a later boot.
    if not cfg.llm.model and cfg.llm.provider != "offline":
        try:
            adopted = await adopt_first_local()
        except Exception as exc:
            # A model server being down is a normal state, not a boot failure.
            log.warning("local model discovery failed: %s", exc)
            adopted = None
        if adopted:
            log.info(
                "adopted local model %s at %s (%s)",
                adopted["model"],
                adopted["base_url"],
                adopted["label"],
            )
        else:
            # The model server is a separate service and may not be listening
            # yet, so keep looking in the background. This is the only chance:
            # a one-shot probe that misses leaves the app offline until someone
            # restarts it or opens the admin panel.
            log.info(
                "no local model server found yet; looking in the background, and "
                "chat stays offline until one is configured at /admin"
            )

            async def retry_adopt() -> None:
                try:
                    found = await adopt_first_local_in_background()
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    log.warning("background model discovery failed: %s", exc)
                    return
                if found:
                    log.info(
                        "adopted local model %s at %s (%s) after waiting",
                        found["model"],
                        found["base_url"],
                        found["label"],
                    )

            asyncio.create_task(retry_adopt())

    if cfg.tts.enabled:
        # Warm the model in the background so the first spoken reply is not slow.
        async def warm() -> None:
            try:
                await asyncio.to_thread(engine.ensure_loaded)
                log.info("TTS ready: %s", engine.status)
            except Exception as exc:
                log.warning("TTS warm-up failed: %s", exc)

        asyncio.create_task(warm())

    if cfg.stt.enabled and stt_available():
        # Same reasoning as TTS: the first dictation should not pay the load.
        # Guarded on availability because this is an optional install and a
        # missing set is expected, not a failure worth logging on every boot.
        async def warm_stt() -> None:
            try:
                await asyncio.to_thread(stt_engine.ensure_loaded)
                log.info("STT ready: %s", stt_engine.status)
            except Exception as exc:
                log.warning("STT warm-up failed: %s", exc)

        asyncio.create_task(warm_stt())

    yield
    log.info("Sister RexenIOT shutting down")


def create_app() -> FastAPI:
    app = FastAPI(
        title="Sister RexenIOT",
        description="Sister RexenIOT: an at-home AI agent with voice, text and home control.",
        version=__version__,
        lifespan=lifespan,
    )
    app.add_middleware(
        CORSMiddleware,
        allow_origins=_dev_origins(),
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    app.include_router(status.router, prefix="/api")
    app.include_router(chat.router, prefix="/api")
    app.include_router(admin.public_router, prefix="/api")
    app.include_router(admin.router, prefix="/api")

    @app.get("/apple-touch-icon.png", include_in_schema=False)
    async def icon() -> JSONResponse:
        return JSONResponse({"detail": "not found"}, status_code=404)

    web_dir = config.WEB_DIR
    if web_dir.exists():
        # Mount assets first, then a catch-all that always returns index.html so
        # client-side routes work on refresh.
        assets = web_dir / "assets"
        if assets.exists():
            app.mount("/assets", StaticFiles(directory=str(assets)), name="assets")

        @app.get("/", include_in_schema=False)
        async def index() -> FileResponse:
            return FileResponse(web_dir / "index.html")

        @app.get("/admin", include_in_schema=False)
        async def admin_page() -> FileResponse:
            return FileResponse(web_dir / "admin.html")

        app.mount("/", StaticFiles(directory=str(web_dir), html=True), name="static")

    return app


app = create_app()


#: Modules each speech backend needs at runtime. The two backends share almost
#: nothing, so checking the wrong set would either raise a false alarm or miss the
#: real gap. requirements.txt covers the ONNX set; the torch set lives in
#: requirements-full.txt.
_TTS_PREREQS: dict[str, tuple[str, ...]] = {
    "onnx": ("numpy", "onnxruntime", "soundfile", "espeakng_loader", "phonemizer", "kittenml"),
    "torch": ("numpy", "soundfile", "torch", "transformers", "kittenml"),
}


def tts_backend() -> str:
    """The backend the configured speech model needs: ``"onnx"`` or ``"torch"``."""
    tier = model_catalog.tier_of_repo(config.load().tts.model_id)
    entry = model_catalog.KITTEN_TTS_MODELS.get(tier or "")
    return (entry or {}).get("backend", "onnx")


def check_tts_prereqs() -> tuple[bool, list[str]]:
    """Verify the speech stack for the configured tier is importable.

    ``find_spec`` is used rather than importing, so this stays fast and cannot be
    tripped by a module that imports but fails to initialise.
    """
    import importlib.util

    missing: list[str] = []
    for name in _TTS_PREREQS[tts_backend()]:
        try:
            if importlib.util.find_spec(name) is None:
                missing.append(name)
        except (ImportError, ValueError):
            missing.append(name)
    return (not missing), missing


def prepare_tts() -> int:
    """Download and load the speech model, then prove it can actually speak.

    Loading alone is not enough to call the install ready: the weights can load
    while phonemisation or the voice table is broken, which would surface as a
    silent or failed first reply. So this synthesises one short line with the
    active personality's own voice settings and reports the result.

    Returns a process exit code. A failure is reported but not fatal -- the app
    still runs, and the admin panel can repair speech later -- so the launchers
    print the message and carry on.
    """
    cfg = config.load()
    if not cfg.tts.enabled:
        print("      speech is disabled in the configuration, skipping")
        return 0

    backend = tts_backend()
    ok, missing = check_tts_prereqs()
    if not ok:
        print(f"      ERROR: the {backend} speech backend is missing: {', '.join(missing)}")
        extra = "requirements-full.txt" if backend == "torch" else "requirements.txt"
        print(f"      Install it with: python -m pip install -r {extra}")
        return 1

    personality = personalities.active()
    print(f"      model:  {cfg.tts.model_id}")
    print(f"      persona: {personality.name} (voice {personality.voice or cfg.tts.voice})")

    try:
        engine.ensure_loaded()
    except Exception as exc:
        print(f"      ERROR: the speech model could not be loaded: {exc}")
        print("      Install the speech package, then try again:")
        print("          python -m pip install -r requirements.txt")
        return 1

    # Speak the persona's greeting, or a neutral line when it has none, so the
    # probe exercises the same code path the first real reply will use.
    probe = (personality.greeting or "Speech is ready.").strip()
    try:
        audio, rate = engine.synthesize(
            probe,
            voice=personality.voice or None,
            speed=personality.speed,
            preset=personality.preset,
            emotion=personality.emotion,
        )
    except Exception as exc:
        print(f"      ERROR: the model loaded but could not speak: {exc}")
        return 1

    seconds = len(audio) / float(rate or 1)
    print(f"      speech ready: {seconds:.1f}s of audio at {rate} Hz")
    return 0


def prepare_stt() -> int:
    """Download the dictation model and prove it transcribes real audio.

    Loading alone proves little: the failure modes that matter (a decoder that
    cannot read the container, a bad compute type) only appear on a real
    inference. So this synthesises a known line with the TTS engine and
    transcribes it back, which exercises decode, load and inference together.

    Skipped without complaint when the optional set is absent -- dictation is a
    fallback-capable feature, not a hard requirement of a working install.
    """
    cfg = config.load()
    if not cfg.stt.enabled:
        print("      dictation is disabled in the configuration, skipping")
        return 0
    if not stt_available():
        print("      dictation set not installed, skipping (optional)")
        print("      Install it with: python -m pip install -r requirements-stt.txt")
        return 0

    print(f"      model:  {cfg.stt.model_id}")
    try:
        stt_engine.ensure_loaded()
    except Exception as exc:
        print(f"      ERROR: the dictation model could not be loaded: {exc}")
        return 1

    probe = "Systems online. Dictation is working."
    try:
        audio = engine.wav_bytes(probe, use_cache=False)
        result = stt_engine.transcribe(audio)
    except Exception as exc:
        print(f"      ERROR: the model loaded but could not transcribe: {exc}")
        return 1

    print(f"      heard:  {result['text']!r}")
    print(f"      dictation ready on {stt_engine.status.get('device')} "
          f"({result['duration_s']}s of audio)")
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Sister RexenIOT server.")
    parser.add_argument("--host", default=None)
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--reload", action="store_true", help="auto-reload on code changes")
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument(
        "--prepare-tts",
        action="store_true",
        help="download and load the speech model, speak a test line, then exit",
    )
    parser.add_argument(
        "--prepare-stt",
        action="store_true",
        help="download and load the dictation model, transcribe a test line, then exit",
    )
    args = parser.parse_args()

    if args.prepare_tts:
        configure_logging()
        personalities.load()  # seed built-ins so a first run has a persona
        raise SystemExit(prepare_tts())

    if args.prepare_stt:
        configure_logging()
        personalities.load()
        raise SystemExit(prepare_stt())

    cfg = config.load()
    host = args.host or cfg.server.host
    port = args.port or cfg.server.port

    # Refuse to expose an unauthenticated agent. This runs before the socket
    # opens, so a misconfiguration cannot be reached even briefly.
    assert_binding_is_safe(host, port)

    if not args.no_browser and cfg.server.open_browser:
        import threading
        import webbrowser

        def _open() -> None:
            import time

            time.sleep(1.5)
            webbrowser.open(f"http://localhost:{port}/")

        threading.Thread(target=_open, daemon=True).start()

    import uvicorn

    uvicorn.run(
        "app.main:app" if args.reload else app,
        host=host,
        port=port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()

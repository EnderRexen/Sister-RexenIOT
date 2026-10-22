"""Chat and speech routes."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

from fastapi import APIRouter, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

from .. import agent, config, personalities, uploads
from ..llm import client as llm_client
from ..stt import STTError, available as stt_available, engine as stt_engine
from ..tools import builtin as builtin_tools
from ..tts import TTSError, engine

log = logging.getLogger("rexeniot.api.chat")
router = APIRouter(tags=["chat"])

#: Characters that TTS engines read badly; stripped before speaking.
_SPEAK_STRIP = re.compile(r"```.*?```|`([^`]*)`|[*_#>|]|\bhttps?://\S+", re.S)


def for_speech(text: str) -> str:
    """Turn markdown-ish output into something worth hearing."""
    text = _SPEAK_STRIP.sub(lambda m: m.group(1) or " ", text)
    text = re.sub(r"^\s*[-*+]\s+", "", text, flags=re.M)
    text = re.sub(r"\n{2,}", ". ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


class ChatRequest(BaseModel):
    message: str = ""
    personality_id: str | None = None
    history: list[dict[str, Any]] = Field(default_factory=list)
    speak: bool = True
    #: Ids returned by /api/uploads. Their contents are folded into the prompt.
    attachments: list[str] = Field(default_factory=list)


class SpeakRequest(BaseModel):
    text: str
    voice: str | None = None
    speed: float | None = None
    preset: str | None = None
    emotion: str | None = None
    personality_id: str | None = None


def _load_attachments(ids: list[str], session: str) -> list[uploads.UploadedFile]:
    """Resolve attachment ids to files, ignoring any that have gone missing."""
    found: list[uploads.UploadedFile] = []
    for uid in ids[:12]:
        item = uploads.find(uid, session)
        if item:
            found.append(item)
    return found


def _with_attachments(message: str, items: list[uploads.UploadedFile]) -> str:
    """Fold attachment contents into the user's message."""
    if not items:
        return message
    block = uploads.render_for_model(items)
    instruction = (
        "The user attached the following file(s) with this message. Read them and "
        "answer the user's request using their contents."
    )
    body = message or "Please look at the attached file and tell me what you make of it."
    return f"{body}\n\n---\n{instruction}\n\n{block}\n---"


@router.post("/chat")
async def chat(req: ChatRequest) -> dict[str, Any]:
    message = (req.message or "").strip()
    session = "default"
    items = _load_attachments(req.attachments, session)
    if not message and not items:
        raise HTTPException(400, "message or an attachment is required")

    personality = personalities.active()
    if req.personality_id:
        found = personalities.get(req.personality_id)
        if found:
            personality = found

    prompt = _with_attachments(message, items)
    images = uploads.image_data_urls(items)
    started = time.time()
    result = await agent.run(
        prompt, history=req.history, personality=personality, images=images
    )
    elapsed = round(time.time() - started, 2)

    payload: dict[str, Any] = {
        "reply": result.text,
        "personality": personality.id,
        "tools_used": result.tool_names,
        "steps": result.steps,
        "elapsed_s": elapsed,
        "llm": llm_client.describe(),
        "attachments": [i.to_dict() for i in items],
        "artifacts": [a.to_dict() for a in uploads.pending_artifacts()],
    }
    from .. import routing as routing_mod

    payload["route"] = routing_mod.classify(message or "attachment").to_dict()
    if req.speak and config.load().tts.enabled:
        payload["speech"] = _speech_payload(result.text, personality)
    return payload


@router.post("/chat/stream")
async def chat_stream(req: ChatRequest) -> StreamingResponse:
    message = (req.message or "").strip()
    session = "default"
    items = _load_attachments(req.attachments, session)
    if not message and not items:
        raise HTTPException(400, "message or an attachment is required")

    personality = personalities.active()
    if req.personality_id:
        found = personalities.get(req.personality_id)
        if found:
            personality = found

    prompt = _with_attachments(message, items)
    images = uploads.image_data_urls(items)
    cfg = config.load()

    async def events():
        yield _sse({"type": "start", "personality": personality.id})
        if items:
            yield _sse({
                "type": "attachments",
                "files": [i.to_dict() for i in items],
            })
        final_text = ""
        tools_used: list[str] = []
        try:
            async for event in agent.stream(prompt, history=req.history, personality=personality, images=images):
                if event.get("type") == "done":
                    final_text = event.get("text", "")
                    tools_used = event.get("tools_used", [])
                yield _sse(event)
        except Exception as exc:  # a stream must always terminate cleanly
            log.exception("stream failed")
            yield _sse({"type": "error", "message": f"{type(exc).__name__}: {exc}"})
            return

        # Anything the agent generated (an image, a recording) is handed to the
        # browser now, so it appears in the same turn that produced it.
        for artifact in uploads.pending_artifacts():
            yield _sse({"type": "artifact", "file": artifact.to_dict()})

        if cfg.tts.enabled and req.speak and final_text:
            yield _sse({"type": "status", "message": "Generating speech…"})
            try:
                speech = _speech_payload(final_text, personality)
                yield _sse({"type": "speech", **speech})
            except TTSError as exc:
                yield _sse({"type": "speech_error", "message": str(exc)})
        yield _sse({"type": "complete", "tools_used": tools_used})

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, default=str)}\n\n"


def _speech_payload(text: str, personality: personalities.Personality) -> dict[str, Any]:
    spoken = for_speech(text)
    cfg = config.load()
    return {
        "speak_text": spoken,
        "voice": personality.voice or cfg.tts.voice,
        "speed": personality.speed,
        "preset": personality.preset,
        "emotion": personality.emotion,
        "has_audio": len(spoken) > 0,
    }


@router.post("/speak")
async def speak(req: SpeakRequest) -> Response:
    """Synthesize text and return a WAV body."""
    cfg = config.load()
    if not cfg.tts.enabled:
        raise HTTPException(503, "Text-to-speech is disabled in the admin panel.")
    text = (req.text or "").strip()
    if not text:
        raise HTTPException(400, "text is required")

    voice, speed, preset, emotion = req.voice, req.speed, req.preset, req.emotion
    if req.personality_id:
        p = personalities.get(req.personality_id)
        if p:
            voice = voice or p.voice
            speed = p.speed if speed is None else speed
            preset = preset or p.preset
            emotion = p.emotion if emotion is None else emotion

    try:
        audio = engine.wav_bytes(
            text, voice=voice, speed=speed, preset=preset, emotion=emotion or ""
        )
    except TTSError as exc:
        raise HTTPException(503, str(exc)) from exc
    return Response(content=audio, media_type="audio/wav", headers={"Cache-Control": "no-store"})


@router.post("/transcribe")
async def transcribe(
    audio: UploadFile = File(...),
    language: str = Form(""),
) -> dict[str, Any]:
    """Transcribe a recorded utterance to text, entirely on this machine.

    Takes multipart audio because that is what the browser's MediaRecorder
    produces and what ``FormData`` posts most cheaply -- no base64 inflation, no
    JSON escaping of a binary blob. The body is decoded in memory; nothing is
    written to disk.
    """
    cfg = config.load()
    if not cfg.stt.enabled:
        raise HTTPException(503, "Speech-to-text is disabled in the admin panel.")
    data = await audio.read()
    try:
        # Transcription is CPU/GPU bound and blocking; keep it off the event loop.
        result = await asyncio.to_thread(
            stt_engine.transcribe, data, language=(language or None)
        )
    except STTError as exc:
        raise HTTPException(503, str(exc)) from exc
    result["engine"] = stt_engine.status
    return result


@router.get("/voices")
async def voices() -> dict[str, Any]:
    cfg = config.load()
    return {
        "voices": engine.voices(),
        "current": cfg.tts.voice,
        "engine": engine.status,
    }


@router.get("/greeting")
async def greeting(personality_id: str | None = None) -> dict[str, Any]:
    p = personalities.get(personality_id) if personality_id else None
    p = p or personalities.active()
    payload: dict[str, Any] = {"text": p.greeting or "Ready.", "personality": p.id}
    if config.load().tts.enabled:
        payload["speech"] = _speech_payload(p.greeting or "Ready.", p)
    return payload


@router.post("/uploads")
async def upload_files(
    files: list[UploadFile] = File(...),
    session: str = Form("default"),
) -> dict[str, Any]:
    """Accept files from the chat composer and index them for the agent."""
    saved: list[dict[str, Any]] = []
    errors: list[str] = []
    for upload in files[:12]:
        try:
            content = await upload.read()
            item = uploads.ingest(
                upload.filename or "upload",
                content,
                session=session,
                mime=upload.content_type or "",
            )
            saved.append(item.to_dict())
        except uploads.UploadError as exc:
            errors.append(f"{upload.filename}: {exc}")
        except Exception as exc:  # noqa: BLE001 - one bad file must not fail the batch
            log.warning("upload failed for %s: %s", upload.filename, exc)
            errors.append(f"{upload.filename}: could not be processed")
    if not saved and errors:
        raise HTTPException(400, "; ".join(errors))
    return {"files": saved, "errors": errors}


@router.get("/uploads/{uid}/raw")
async def serve_upload(uid: str, session: str = "default", download: bool = False) -> FileResponse:
    """Serve an upload's bytes so the browser can preview or play it.

    Only files inside the session's upload folder are reachable, and only by a
    well-formed id, so this cannot be pointed at arbitrary paths on the host.
    Starlette's FileResponse handles Range requests, which is what lets the
    browser seek in audio and video.
    """
    item = uploads.find(uid, session)
    if item is None:
        raise HTTPException(404, "No such file")
    path = item.path.resolve()
    # Defence in depth: the resolved path must sit inside the upload folder.
    root = uploads.uploads_dir(session).resolve()
    if not path.is_file() or root not in path.parents:
        raise HTTPException(404, "No such file")

    disposition = "attachment" if download else "inline"
    # Quote the filename and strip anything that could break out of the header.
    safe = re.sub(r'[^A-Za-z0-9._ -]', "_", item.name) or "file"
    return FileResponse(
        path,
        media_type=item.mime or "application/octet-stream",
        headers={
            "Content-Disposition": f'{disposition}; filename="{safe}"',
            "Cache-Control": "private, max-age=3600",
            "X-Content-Type-Options": "nosniff",
        },
    )


@router.get("/uploads")
async def list_uploads(session: str = "default") -> dict[str, Any]:
    folder = uploads.uploads_dir(session)
    items = []
    for path in sorted(folder.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)[:60]:
        name = path.name.split("__", 1)[1] if "__" in path.name else path.name
        items.append({
            "id": path.name.split("__", 1)[0],
            "name": name,
            "size": path.stat().st_size,
        })
    return {"files": items}


@router.delete("/uploads/{uid}")
async def delete_upload(uid: str, session: str = "default") -> dict[str, Any]:
    folder = uploads.uploads_dir(session)
    matches = list(folder.glob(f"{re.escape(uid)}__*"))
    for path in matches:
        try:
            path.unlink()
        except OSError:
            pass
    return {"ok": bool(matches)}


@router.get("/config")
async def public_config() -> dict[str, Any]:
    """Settings the HUD needs. Never exposes secrets."""
    cfg = config.load()
    return {
        "branding": cfg.branding.model_dump(),
        "voice_input": {
            "enabled": cfg.voice_input.enabled,
            "engine": cfg.voice_input.engine,
            "language": cfg.voice_input.language,
            "auto_send": cfg.voice_input.auto_send,
            "wake_enabled": cfg.voice_input.wake_enabled,
            "wake_words": cfg.voice_input.wake_words,
            "sleep_words": cfg.voice_input.sleep_words,
            "session_idle_s": cfg.voice_input.session_idle_s,
            "acknowledge": cfg.voice_input.acknowledge,
            "acknowledgement": cfg.voice_input.acknowledgement,
        },
        # The HUD needs to know whether dictation can be transcribed locally
        # before it offers the microphone, so it does not record audio that has
        # nowhere to go.
        "stt": {
            "enabled": cfg.stt.enabled,
            "available": stt_available() if cfg.stt.enabled else False,
        },
        "tts_enabled": cfg.tts.enabled,
        # The HUD needs the engine choice and the nominal voice/speed to drive the
        # browser fallback. Secrets stay out of this response.
        "tts": {
            "enabled": cfg.tts.enabled,
            "engine": cfg.tts.engine,
            "voice": cfg.tts.voice,
            "speed": cfg.tts.speed,
        },
    }


@router.get("/routing")
async def routing_info() -> dict[str, Any]:
    """Which backend handles what, and how a given message would be routed."""
    from .. import routing as routing_mod

    return {"backends": routing_mod.backends()}


class RouteProbe(BaseModel):
    message: str


@router.post("/route")
async def route_probe(body: RouteProbe) -> dict[str, Any]:
    """Explain where a message would go. Useful for tuning the router."""
    from .. import routing as routing_mod

    return routing_mod.classify(body.message).to_dict()


@router.get("/reminders")
async def reminders() -> dict[str, Any]:
    return {
        "pending": builtin_tools.active_timers(),
        "fired": builtin_tools.pending_reminders(mark_read=False),
    }

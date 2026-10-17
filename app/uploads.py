"""Uploaded-file handling.

Files the user attaches in the chat are written to ``data/uploads/<session>/``
and summarised into text the agent can reason about. Text-like files are read
in full up to a character budget; images are described by their metadata and
handed to a vision model only if the active provider supports one. Everything
else gets a header summary plus a text extraction attempt, so the agent can
still answer questions about it.
"""

from __future__ import annotations

import base64
import json
import logging
import mimetypes
import re
import shutil
import time
import uuid
import zipfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import config

log = logging.getLogger("rexeniot.uploads")

#: Extensions read directly as text.
TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".rst", ".log", ".csv", ".tsv", ".json", ".jsonl",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf", ".env", ".xml", ".html",
    ".htm", ".css", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".py", ".rb",
    ".go", ".rs", ".java", ".kt", ".c", ".h", ".cpp", ".hpp", ".cs", ".php",
    ".sh", ".bash", ".zsh", ".ps1", ".cmd", ".bat", ".sql", ".graphql", ".svg",
}

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp"}
PDF_EXTENSIONS = {".pdf"}
ARCHIVE_EXTENSIONS = {".zip"}
AUDIO_EXTENSIONS = {".wav", ".mp3", ".m4a", ".flac", ".ogg", ".opus", ".webm"}
VIDEO_EXTENSIONS = {".mp4", ".webm", ".mov", ".mkv", ".m4v", ".ogv"}

#: Characters of a text file handed to the model. Beyond this the file is
#: truncated and the agent is told it was truncated.
TEXT_BUDGET = 24_000
#: Hard ceiling on a single upload.
MAX_BYTES = 64 * 1024 * 1024


class UploadError(RuntimeError):
    pass


@dataclass
class UploadedFile:
    id: str
    name: str
    path: Path
    size: int
    mime: str
    kind: str  # text | image | pdf | archive | audio | binary
    summary: str = ""
    text: str = ""
    truncated: bool = False
    notes: list[str] = field(default_factory=list)
    #: "upload" for files the user sent, "generated" for files the agent made.
    #: Both are served and previewed the same way.
    origin: str = "upload"
    created_at: float = field(default_factory=time.time)
    #: Set once an artifact has been sent to the browser, so it is shown once.
    claimed: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "size": self.size,
            "mime": self.mime,
            "kind": self.kind,
            "summary": self.summary,
            "truncated": self.truncated,
            "notes": self.notes,
            "path": str(self.path),
            "origin": self.origin,
            #: Where the browser fetches the bytes to preview or play it.
            "raw_url": f"/api/uploads/{self.id}/raw",
        }


def uploads_dir(session: str = "default") -> Path:
    safe = re.sub(r"[^a-zA-Z0-9_-]", "", session or "default")[:48] or "default"
    path = config.DATA_DIR / "uploads" / safe
    path.mkdir(parents=True, exist_ok=True)
    return path


def _classify(path: Path, mime: str) -> str:
    ext = path.suffix.lower()
    if ext in TEXT_EXTENSIONS:
        return "text"
    if ext in IMAGE_EXTENSIONS or mime.startswith("image/"):
        return "image"
    if ext in PDF_EXTENSIONS:
        return "pdf"
    if ext in ARCHIVE_EXTENSIONS:
        return "archive"
    # Video is checked before audio: .webm appears in both sets, and a .webm is
    # far more often a video than a sound file.
    if ext in VIDEO_EXTENSIONS or mime.startswith("video/"):
        return "video"
    if ext in AUDIO_EXTENSIONS or mime.startswith("audio/"):
        return "audio"
    if mime.startswith("text/"):
        return "text"
    return "binary"


def _human_size(size: int) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size / 1:.1f} {unit}"
        size /= 1024
    return f"{size} B"


def _read_text(path: Path) -> tuple[str, bool]:
    """Read a text file, tolerating encoding surprises."""
    raw = path.read_bytes()
    for encoding in ("utf-8", "utf-8-sig", "utf-16", "latin-1"):
        try:
            text = raw.decode(encoding)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")

    if len(text) > TEXT_BUDGET:
        return text[:TEXT_BUDGET], True
    return text, False


def _extract_pdf(path: Path) -> tuple[str, list[str]]:
    notes: list[str] = []
    try:
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        pages = [page.extract_text() or "" for page in reader.pages]
        text = "\n\n".join(pages).strip()
        notes.append(f"{len(reader.pages)} page(s)")
        if not text:
            notes.append("no selectable text; likely a scan")
        return text, notes
    except ImportError:
        notes.append("install pypdf to read PDF text")
    except Exception as exc:  # noqa: BLE001 - report rather than fail the upload
        notes.append(f"could not parse PDF: {exc}")
    return "", notes


def _list_archive(path: Path) -> tuple[str, list[str]]:
    notes: list[str] = []
    try:
        with zipfile.ZipFile(path) as zf:
            names = zf.namelist()
            notes.append(f"{len(names)} entries")
            listing = "\n".join(names[:400])
            if len(names) > 400:
                listing += f"\n… and {len(names) - 400} more"
            return listing, notes
    except Exception as exc:  # noqa: BLE001
        notes.append(f"could not read archive: {exc}")
    return "", notes


def _extract_json_summary(path: Path) -> str:
    try:
        data = json.loads(path.read_text(encoding="utf-8", errors="replace"))
    except Exception:  # noqa: BLE001
        return ""
    if isinstance(data, list):
        return f"JSON array with {len(data)} items"
    if isinstance(data, dict):
        keys = list(data.keys())[:25]
        return f"JSON object with keys: {', '.join(map(str, keys))}"
    return f"JSON {type(data).__name__}"


def ingest(
    filename: str,
    content: bytes,
    *,
    session: str = "default",
    mime: str = "",
) -> UploadedFile:
    """Persist an upload and build the text summary the agent will see."""
    if not content:
        raise UploadError("The file was empty.")
    if len(content) > MAX_BYTES:
        raise UploadError(f"{filename} is larger than {_human_size(MAX_BYTES)}.")

    safe_name = Path(filename or "upload").name
    safe_name = re.sub(r"[^\w.\- ()\[\]]", "_", safe_name)[:160] or "upload"
    uid = uuid.uuid4().hex[:10]
    target = uploads_dir(session) / f"{uid}__{safe_name}"
    target.write_bytes(content)

    guessed = mime or ""
    # Browsers and curl often report application/octet-stream for known types;
    # the filename is the more reliable signal.
    if not guessed or guessed == "application/octet-stream":
        guessed = mimetypes.guess_type(safe_name)[0] or guessed or "application/octet-stream"
    kind = _classify(target, guessed)

    item = UploadedFile(
        id=uid,
        name=safe_name,
        path=target,
        size=len(content),
        mime=guessed,
        kind=kind,
    )

    if kind == "text":
        text, truncated = _read_text(target)
        item.text, item.truncated = text, truncated
        item.notes.append(f"{len(text.splitlines())} lines")
        if target.suffix.lower() == ".json":
            note = _extract_json_summary(target)
            if note:
                item.notes.append(note)
    elif kind == "image":
        item.notes.append(_describe_image(target, guessed))
    elif kind == "pdf":
        text, notes = _extract_pdf(target)
        item.text, item.notes = text, item.notes + notes
        if len(item.text) > TEXT_BUDGET:
            item.text, item.truncated = item.text[:TEXT_BUDGET], True
    elif kind == "archive":
        text, notes = _list_archive(target)
        item.text, item.notes = text, item.notes + notes
    elif kind == "audio":
        item.notes.append("audio file; transcribe it to make it searchable")
    else:
        item.notes.append("binary file; only metadata is available")

    item.summary = _summarise(item)
    return item


def _describe_image(path: Path, mime: str) -> str:
    try:
        from PIL import Image

        with Image.open(path) as img:
            return f"{img.width}x{img.height} {img.mode} image"
    except ImportError:
        return "image (install Pillow for dimensions)"
    except Exception:  # noqa: BLE001
        return "image"


def _summarise(item: UploadedFile) -> str:
    parts = [f"{item.name} ({item.kind}, {_human_size(item.size)}, {item.mime})"]
    if item.notes:
        parts.append("; ".join(item.notes))
    return " — ".join(parts)


def render_for_model(items: list[UploadedFile]) -> str:
    """Format uploads as a prompt block the agent can answer questions about."""
    if not items:
        return ""
    blocks: list[str] = []
    for item in items:
        lines = [f"### Attached file: {item.name}", f"Type: {item.kind} ({item.mime}), size {_human_size(item.size)}"]
        if item.notes:
            lines.append("Notes: " + "; ".join(item.notes))
        if item.text:
            label = "Contents (truncated)" if item.truncated else "Contents"
            lines.append(f"{label}:")
            lines.append(item.text)
        elif item.kind == "image":
            lines.append(
                "The image itself is attached to this message. Describe what you see "
                "and answer the user's question about it."
            )
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def image_data_urls(items: list[UploadedFile]) -> list[dict[str, Any]]:
    """OpenAI-style image parts for any image uploads."""
    parts: list[dict[str, Any]] = []
    for item in items:
        if item.kind != "image":
            continue
        try:
            b64 = base64.b64encode(item.path.read_bytes()).decode()
        except OSError:
            continue
        parts.append(
            {"type": "image_url", "image_url": {"url": f"data:{item.mime};base64,{b64}"}}
        )
    return parts


def find(uid: str, session: str = "default") -> UploadedFile | None:
    """Re-derive an upload from its stored file, so no state is kept in memory."""
    if not re.fullmatch(r"[0-9a-f]{6,32}", uid or ""):
        return None
    folder = uploads_dir(session)
    matches = list(folder.glob(f"{uid}__*"))
    if not matches:
        return None
    path = matches[0]
    name = path.name.split("__", 1)[1] if "__" in path.name else path.name
    guessed = mimetypes.guess_type(name)[0] or "application/octet-stream"
    item = UploadedFile(
        id=uid,
        name=name,
        path=path,
        size=path.stat().st_size,
        mime=guessed,
        kind=_classify(path, guessed),
    )
    # Rebuild the derived text; the raw file is the only source of truth.
    if item.kind == "text":
        item.text, item.truncated = _read_text(path)
        item.notes.append(f"{len(item.text.splitlines())} lines")
        if path.suffix.lower() == ".json":
            note = _extract_json_summary(path)
            if note:
                item.notes.append(note)
    elif item.kind == "image":
        item.notes.append(_describe_image(path, guessed))
    elif item.kind == "pdf":
        item.text, notes = _extract_pdf(path)
        item.notes = notes
        if len(item.text) > TEXT_BUDGET:
            item.text, item.truncated = item.text[:TEXT_BUDGET], True
    elif item.kind == "archive":
        item.text, notes = _list_archive(path)
        item.notes = notes
    elif item.kind == "audio":
        item.notes.append("audio file; transcribe it to make it searchable")
    elif item.kind == "binary":
        item.notes.append("binary file; only metadata is available")
    item.summary = _summarise(item)
    return item


# --------------------------------------------------------------- artifacts
# Files the agent generates (images, audio, video) are registered here so they
# can be served and previewed in the chat exactly like a user's uploads. The
# queue is what lets a generated file ride back to the browser in the same turn
# that produced it.

#: Artifacts waiting to be sent to the browser, newest last.
_pending_artifacts: list[UploadedFile] = []
#: How many unclaimed artifacts to hold before dropping the oldest.
_ARTIFACT_BACKLOG = 40

#: Extensions we are willing to hand to the browser for inline preview. Anything
#: else is registered for download only, never rendered.
PREVIEWABLE = {
    "image": IMAGE_EXTENSIONS,
    "audio": AUDIO_EXTENSIONS,
    "video": VIDEO_EXTENSIONS,
}


def register_artifact(
    path: str | Path,
    *,
    session: str = "default",
    name: str = "",
    mime: str = "",
) -> UploadedFile:
    """Register a file the agent produced so it can be previewed in the chat.

    The file is copied into the session's upload folder, which is the only
    directory the serving endpoint will read from. That keeps a generated
    artifact addressable without opening up arbitrary paths on the host.
    """
    source = Path(path)
    if not source.is_file():
        raise UploadError(f"No such file to share: {source}")
    size = source.stat().st_size
    if size > MAX_BYTES:
        raise UploadError(f"{source.name} is larger than {_human_size(MAX_BYTES)}.")

    safe_name = re.sub(r"[^\w.\- ()\[\]]", "_", Path(name or source.name).name)[:160] or "artifact"
    guessed = mime or mimetypes.guess_type(safe_name)[0] or "application/octet-stream"
    uid = uuid.uuid4().hex[:10]
    target = uploads_dir(session) / f"{uid}__{safe_name}"
    shutil.copyfile(source, target)

    kind = _classify(target, guessed)
    item = UploadedFile(
        id=uid,
        name=safe_name,
        path=target,
        size=size,
        mime=guessed,
        kind=kind,
        origin="generated",
    )
    if kind == "image":
        item.notes.append(_describe_image(target, guessed))
    elif kind in ("audio", "video"):
        item.notes.append(f"{kind} generated by the agent")
    item.summary = _summarise(item)

    _pending_artifacts.append(item)
    del _pending_artifacts[:-_ARTIFACT_BACKLOG]
    log.debug("registered artifact %s (%s)", item.id, item.name)
    return item


def pending_artifacts(mark_read: bool = True) -> list[UploadedFile]:
    """Artifacts the browser has not been told about yet."""
    out = [a for a in _pending_artifacts if not a.claimed]
    if mark_read:
        for item in out:
            item.claimed = True
    return out


def previewable_kind(kind: str) -> bool:
    """Whether a file of this kind may be rendered inline rather than downloaded."""
    return kind in PREVIEWABLE


def cleanup(session: str, keep: int = 200) -> int:
    """Trim old uploads so a long-lived install does not grow without bound."""
    folder = uploads_dir(session)
    files = sorted(folder.glob("*"), key=lambda p: p.stat().st_mtime, reverse=True)
    removed = 0
    for stale in files[keep:]:
        try:
            stale.unlink()
            removed += 1
        except OSError:
            continue
    return removed


def stats() -> dict[str, Any]:
    root = config.DATA_DIR / "uploads"
    if not root.exists():
        return {"files": 0, "mb": 0.0, "sessions": 0}
    files = [p for p in root.rglob("*") if p.is_file()]
    total = sum(p.stat().st_size for p in files)
    return {
        "files": len(files),
        "mb": round(total / 1_048_576, 2),
        "sessions": len([d for d in root.iterdir() if d.is_dir()]),
    }

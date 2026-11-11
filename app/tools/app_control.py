"""Desktop and application control for a KDE Plasma session.

Sister runs as the desktop user, so the session bus is reachable and the
freedesktop and KDE interfaces below are live. A missing interface is reported
to the model as a tool error rather than crashing the turn.
"""

from __future__ import annotations

import asyncio
import os
import shlex
import time
from pathlib import Path
from typing import Any

from .base import ToolError, registry

_DESKTOP_DIRS = (
    "/run/current-system/sw/share/applications",
    str(Path.home() / ".local/share/applications"),
)

_NOTIFY = (
    "busctl", "--user", "call",
    "org.freedesktop.Notifications",
    "/org/freedesktop/Notifications",
    "org.freedesktop.Notifications", "Notify",
)

_MPRIS = "org.mpris.MediaPlayer2"


def _session_pids() -> list[int]:
    """PIDs of long-lived session processes, plasmashell first."""
    hits: list[tuple[int, str]] = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text().strip()
        except OSError:
            continue
        if comm in ("plasmashell", "kwin_wayland", "kwin_x11", "systemd"):
            hits.append((int(entry.name), comm))
    hits.sort(key=lambda item: item[1] != "plasmashell")
    return [pid for pid, _ in hits]


def _session_env() -> dict[str, str]:
    """Environment for reaching the user's graphical session.

    The systemd unit has no DBUS_SESSION_BUS_ADDRESS, and launching a GUI app
    also needs WAYLAND_DISPLAY/DISPLAY, which exist only inside the session.
    Both are harvested from a process that already has them.
    """
    env = dict(os.environ)
    uid = os.getuid()
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{uid}")
    env.setdefault("DBUS_SESSION_BUS_ADDRESS", f"unix:path=/run/user/{uid}/bus")

    wanted = {"WAYLAND_DISPLAY", "DISPLAY", "XAUTHORITY"}
    if wanted <= env.keys():
        return env
    for pid in _session_pids():
        try:
            raw = Path(f"/proc/{pid}/environ").read_bytes()
        except OSError:
            continue
        for item in raw.split(b"\0"):
            key, _, value = item.partition(b"=")
            name = key.decode("utf-8", "replace")
            if name in wanted and name not in env:
                env[name] = value.decode("utf-8", "replace")
        if wanted <= env.keys():
            break
    return env


async def _run(*args: str, timeout: float = 20.0) -> str:
    proc = await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        env=_session_env(),
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise ToolError(f"{args[0]} timed out after {timeout:.0f}s.")
    text = (out or b"").decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        raise ToolError(text[:600] or f"{args[0]} exited with {proc.returncode}.")
    return text


def _desktop_entries() -> dict[str, dict[str, str]]:
    """Launchable apps keyed by lowercased name and desktop-file stem."""
    apps: dict[str, dict[str, str]] = {}
    for folder in _DESKTOP_DIRS:
        base = Path(folder)
        if not base.is_dir():
            continue
        for path in sorted(base.glob("*.desktop")):
            try:
                lines = path.read_text("utf-8", "replace").splitlines()
            except OSError:
                continue
            entry: dict[str, str] = {}
            in_entry = False
            for line in lines:
                if line.startswith("["):
                    in_entry = line.strip() == "[Desktop Entry]"
                    continue
                if not in_entry or "=" not in line:
                    continue
                key, _, value = line.partition("=")
                entry.setdefault(key.strip(), value.strip())
            if entry.get("Type", "Application") != "Application":
                continue
            if entry.get("NoDisplay", "").lower() == "true":
                continue
            if not entry.get("Exec"):
                continue
            for key in {path.stem.lower(), entry.get("Name", "").lower()}:
                if key:
                    apps.setdefault(key, entry)
    return apps


def _exec_argv(exec_line: str) -> list[str]:
    """Split an Exec line, dropping the %f/%U style field codes."""
    argv = [t for t in shlex.split(exec_line) if not (len(t) == 2 and t.startswith("%"))]
    if not argv:
        raise ToolError("That application's launch command is empty.")
    return argv


def _resolve_app(name: str) -> tuple[str, dict[str, str]]:
    wanted = name.strip().lower()
    if not wanted:
        raise ToolError("Name an application to open.")
    apps = _desktop_entries()
    if wanted in apps:
        return wanted, apps[wanted]
    matches = sorted(k for k in apps if wanted in k)
    if len(matches) == 1:
        return matches[0], apps[matches[0]]
    if matches:
        raise ToolError(f"'{name}' is ambiguous: " + ", ".join(matches[:8]))
    raise ToolError(f"No installed application matches '{name}'.")


@registry.add(
    "notify_user",
    "Show a desktop notification. Use when the user should see something even "
    "with the HUD closed.",
    {
        "type": "object",
        "properties": {
            "summary": {"type": "string", "description": "Short headline."},
            "body": {"type": "string", "description": "The message itself."},
            "urgency": {
                "type": "string",
                "enum": ["low", "normal", "critical"],
                "default": "normal",
                "description": "Use 'critical' only for something that needs attention now.",
            },
        },
        "required": ["summary", "body"],
    },
    tags=["desktop"],
)
async def notify_user(summary: str, body: str, urgency: str = "normal") -> Any:
    level = {"low": 0, "normal": 1, "critical": 2}.get((urgency or "normal").lower(), 1)
    out = await _run(
        *_NOTIFY,
        "susssasa{sv}i",
        "Sister", "0", "",
        summary[:120], body[:600],
        "0",
        "1", "urgency", "y", str(level),
        "8000",
    )
    return {"shown": True, "notification_id": out}


@registry.add(
    "open_app",
    "Open an installed application on the user's desktop by name.",
    {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Application name, e.g. 'firefox'."},
        },
        "required": ["name"],
    },
    tags=["desktop"],
)
async def open_app(name: str) -> Any:
    slug, entry = _resolve_app(name)
    argv = _exec_argv(entry["Exec"])
    safe = "".join(c if c.isalnum() else "-" for c in slug)[:40]
    # A fixed unit name would make the second launch of the same app collide
    # with the still-running first one, so make each launch unique.
    unit = f"sister-open-{safe}-{int(time.time())}"
    await _run("systemd-run", "--user", "--collect", f"--unit={unit}", *argv)
    return {"opened": entry.get("Name", slug), "command": argv}


@registry.add(
    "list_apps",
    "List installed applications that can be opened.",
    {
        "type": "object",
        "properties": {
            "search": {"type": "string", "description": "Only names containing this text."},
        },
    },
    tags=["desktop"],
)
async def list_apps(search: str = "") -> Any:
    apps = _desktop_entries()
    names = sorted({e.get("Name", k) for k, e in apps.items()})
    if search.strip():
        needle = search.strip().lower()
        names = [n for n in names if needle in n.lower()]
    return {"count": len(names), "apps": names[:80]}


@registry.add(
    "control_media",
    "Control or inspect media playback in the active player.",
    {
        "type": "object",
        "properties": {
            "action": {
                "type": "string",
                "enum": ["play_pause", "next", "previous", "stop", "now_playing"],
            },
        },
        "required": ["action"],
    },
    tags=["desktop"],
)
async def control_media(action: str) -> Any:
    listing = await _run("busctl", "--user", "--list", "--no-pager")
    players = [ln.split()[0] for ln in listing.splitlines() if ln.startswith(_MPRIS)]
    if not players:
        raise ToolError("Nothing is playing; no media player is on the session bus.")
    bus = players[0]
    if action == "now_playing":
        props = await _run("busctl", "--user", "get-property", bus, "/org/mpris/MediaPlayer2",
                           "org.mpris.MediaPlayer2.Player", "Metadata")
        return {"player": bus, "metadata": props[:800]}
    method = {
        "play_pause": "PlayPause",
        "next": "Next",
        "previous": "Previous",
        "stop": "Stop",
    }.get(action)
    if not method:
        raise ToolError(f"Unsupported media action '{action}'.")
    await _run("busctl", "--user", "call", bus, "/org/mpris/MediaPlayer2",
               "org.mpris.MediaPlayer2.Player", method)
    return {"player": bus, "action": action}


@registry.add(
    "set_power_profile",
    "Set or report the machine's power profile.",
    {
        "type": "object",
        "properties": {
            "profile": {
                "type": "string",
                "enum": ["power-saver", "balanced", "performance", "current"],
            },
        },
        "required": ["profile"],
    },
    tags=["desktop"],
)
async def set_power_profile(profile: str) -> Any:
    path = "/org/kde/Solid/PowerManagement/Actions/PowerProfile"
    iface = "org.kde.Solid.PowerManagement.Actions.PowerProfile"
    root = "org.kde.Solid.PowerManagement"
    if profile == "current":
        return {"profile": await _run("qdbus", root, path, f"{iface}.currentProfile")}
    if profile not in ("power-saver", "balanced", "performance"):
        raise ToolError(f"Unknown power profile '{profile}'.")
    await _run("qdbus", root, path, f"{iface}.setProfile", profile)
    return {"profile": profile, "set": True}





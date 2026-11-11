"""Built-in tool implementations.

Grouped by integration:

* ``home_assistant`` -- control lights, switches, climate, scenes via the HA REST API.
* ``mqtt`` -- publish to a broker for devices HA does not own.
* ``system`` -- CPU, memory, disk, battery, processes on the host running Sister RexenIOT.
* ``web`` -- fetch a URL or call a JSON API, restricted to an allowlist.
* ``timer`` -- delayed reminders and recurring checks.
* ``memory`` -- remember and recall facts the user states.
"""

from __future__ import annotations

import asyncio
import json
import logging
import platform
import re
import socket
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import httpx

from .. import config
from .base import ToolError, registry

log = logging.getLogger("rexeniot.tools.builtin")

# --------------------------------------------------------------------------- timers

_timers: dict[str, dict[str, Any]] = {}
_reminders: list[dict[str, Any]] = []


# --------------------------------------------------------------------- home assistant


def _ha_ready() -> tuple[str, dict[str, str]]:
    cfg = config.load().tools
    url = (cfg.home_assistant_url or "").rstrip("/")
    token = cfg.resolved_ha_token()
    if not url or not token:
        raise ToolError(
            "Home Assistant is not configured. Add the base URL and a long-lived "
            "access token in the admin panel."
        )
    return url, {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


@registry.add(
    "home_assistant_states",
    "List Home Assistant entities and their current state. Optionally filter by a "
    "domain such as 'light', 'switch', 'climate', 'sensor' or 'media_player'.",
    {
        "type": "object",
        "properties": {
            "domain": {"type": "string", "description": "Entity domain to filter by, e.g. 'light'."},
            "search": {"type": "string", "description": "Only entities whose id or friendly name contains this text."},
            "limit": {"type": "integer", "description": "Maximum entities to return.", "default": 60},
        },
    },
    requires="Home Assistant URL + token",
    tags=["home"],
)
async def home_assistant_states(
    domain: str = "", search: str = "", limit: int = 60
) -> Any:
    url, headers = _ha_ready()
    async with httpx.AsyncClient(timeout=config.load().tools.request_timeout_s) as client:
        resp = await client.get(f"{url}/api/states", headers=headers)
    if resp.status_code >= 400:
        raise ToolError(f"Home Assistant returned HTTP {resp.status_code}")
    rows = []
    needle = search.lower().strip()
    for item in resp.json():
        entity_id = item.get("entity_id", "")
        if domain and not entity_id.startswith(f"{domain}."):
            continue
        name = (item.get("attributes") or {}).get("friendly_name", "")
        if needle and needle not in entity_id.lower() and needle not in name.lower():
            continue
        rows.append({"entity_id": entity_id, "name": name, "state": item.get("state")})
    rows.sort(key=lambda r: r["entity_id"])
    return {"count": len(rows), "entities": rows[: max(1, min(limit, 500))]}


@registry.add(
    "home_assistant_call_service",
    "Call a Home Assistant service, e.g. turn on a light, set a thermostat, run a "
    "scene or lock a door. Example: domain 'light', service 'turn_on', "
    "target {'entity_id': 'light.kitchen'}.",
    {
        "type": "object",
        "properties": {
            "domain": {"type": "string", "description": "Service domain, e.g. 'light'."},
            "service": {"type": "string", "description": "Service name, e.g. 'turn_on'."},
            "target": {
                "type": "object",
                "description": "Target block, usually {'entity_id': 'light.kitchen'}.",
            },
            "data": {
                "type": "object",
                "description": "Extra service data, e.g. {'brightness_pct': 40}.",
            },
        },
        "required": ["domain", "service"],
    },
    requires="Home Assistant URL + token",
    tags=["home"],
)
async def home_assistant_call_service(
    domain: str, service: str, target: dict[str, Any] | None = None, data: dict[str, Any] | None = None
) -> Any:
    url, headers = _ha_ready()
    if not re.fullmatch(r"[a-z_]+", domain or ""):
        raise ToolError(f"Invalid service domain '{domain}'.")
    if not re.fullmatch(r"[a-z_]+", service or ""):
        raise ToolError(f"Invalid service name '{service}'.")
    body: dict[str, Any] = {}
    if target:
        body.update(target)
    if data:
        body.update(data)
    async with httpx.AsyncClient(timeout=config.load().tools.request_timeout_s) as client:
        resp = await client.post(
            f"{url}/api/services/{domain}/{service}", headers=headers, json=body
        )
    if resp.status_code >= 400:
        raise ToolError(f"Home Assistant returned HTTP {resp.status_code}: {resp.text[:200]}")
    changed = [
        {"entity_id": s.get("entity_id"), "state": s.get("state")}
        for s in (resp.json() or [])
        if isinstance(s, dict)
    ]
    return {"ok": True, "domain": domain, "service": service, "changed": changed[:20]}


# --------------------------------------------------------------------------- mqtt


def _mqtt_ready() -> config.ToolConfig:
    cfg = config.load().tools
    if not cfg.mqtt_host:
        raise ToolError("MQTT is not configured. Add a broker host in the admin panel.")
    return cfg


@registry.add(
    "mqtt_publish",
    "Publish a message to an MQTT topic on the home broker. Use for IoT devices that "
    "Home Assistant does not manage.",
    {
        "type": "object",
        "properties": {
            "topic": {"type": "string", "description": "MQTT topic, e.g. 'home/office/lamp/set'."},
            "payload": {"type": "string", "description": "Message body. Use 'ON'/'OFF' for simple switches."},
            "retain": {"type": "boolean", "description": "Whether the broker keeps the last value.", "default": False},
        },
        "required": ["topic", "payload"],
    },
    requires="MQTT broker host",
    tags=["iot"],
)
async def mqtt_publish(topic: str, payload: str, retain: bool = False) -> Any:
    cfg = _mqtt_ready()
    if not topic or len(topic) > 256:
        raise ToolError("Invalid MQTT topic.")
    try:
        import paho.mqtt.client as mqtt
    except ImportError as exc:
        raise ToolError("The paho-mqtt package is not installed.") from exc

    def _publish() -> None:
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        if cfg.mqtt_username:
            client.username_pw_set(cfg.mqtt_username, cfg.resolved_mqtt_password())
        client.connect(cfg.mqtt_host, cfg.mqtt_port, 10)
        info = client.publish(topic, payload, retain=retain)
        info.wait_for_publish(timeout=5)
        client.disconnect()

    try:
        await asyncio.to_thread(_publish)
    except Exception as exc:
        raise ToolError(f"MQTT publish failed: {exc}") from exc
    return {"ok": True, "topic": topic, "payload": payload}


# --------------------------------------------------------------------------- system


@registry.add(
    "system_status",
    "Report CPU load, memory, disk, battery, uptime and OS.",
    {"type": "object", "properties": {}},
    tags=["system"],
)
async def system_status() -> Any:
    import psutil

    try:
        load = psutil.getloadavg()
    except Exception:
        load = (0.0, 0.0, 0.0)
    try:
        battery = psutil.sensors_battery()
        battery_out = (
            {"percent": round(battery.percent, 1), "plugged": battery.power_plugged}
            if battery
            else None
        )
    except Exception:
        battery_out = None

    from .. import hardware

    cached = hardware.cached()
    return {
        "os": f"{platform.system()} {platform.release()}",
        "hostname": socket.gethostname(),
        "cpu_cores": psutil.cpu_count(logical=True),
        "cpu_percent": psutil.cpu_percent(interval=0.3),
        "load_average": [round(v, 2) for v in load],
        "memory": {
            "total_gb": round(psutil.virtual_memory().total / 1024**3, 1),
            "used_percent": psutil.virtual_memory().percent,
        },
        "disk_used_percent": psutil.disk_usage(str(config.DATA_DIR)).percent,
        "uptime_hours": round(time.time() - psutil.boot_time(), 0) / 3600,
        "battery": battery_out,
        "gpu": (cached or {}).get("gpu") or [],
    }


@registry.add(
    "list_processes",
    "List the processes using the most CPU or memory on the host, to answer "
    "'what is slowing my computer down'.",
    {
        "type": "object",
        "properties": {
            "sort_by": {"type": "string", "enum": ["cpu", "memory"], "default": "cpu"},
            "limit": {"type": "integer", "default": 8},
        },
    },
    tags=["system"],
)
async def list_processes(sort_by: str = "cpu", limit: int = 8) -> Any:
    import psutil

    key = "memory_percent" if sort_by == "memory" else "cpu_percent"
    limit = max(1, min(int(limit), 40))
    procs = []
    for proc in psutil.process_iter(["pid", "name", "cpu_percent", "memory_percent"]):
        try:
            info = proc.info
            procs.append(
                {
                    "pid": info["pid"],
                    "name": info["name"],
                    "cpu_percent": round(info.get("cpu_percent") or 0.0, 1),
                    "memory_percent": round(info.get("memory_percent") or 0.0, 1),
                }
            )
        except Exception:
            continue
    procs.sort(key=lambda p: p[key], reverse=True)
    return {"sorted_by": sort_by, "processes": procs[:limit]}


@registry.add(
    "run_shell_command",
    "Run a shell command on the host computer. Disabled by default; only commands "
    "whose first word is on the admin allowlist are permitted.",
    {
        "type": "object",
        "properties": {"command": {"type": "string", "description": "The full command line to run."}},
        "required": ["command"],
    },
    requires="'Allow shell' enabled in admin panel",
    dangerous=True,
    tags=["system"],
)
async def run_shell_command(command: str) -> Any:
    cfg = config.load().tools
    if not cfg.allow_shell:
        raise ToolError("Shell access is disabled. Enable it in the admin panel to use this.")
    parts = command.strip().split()
    if not parts:
        raise ToolError("Empty command.")
    first = parts[0].lower()
    allow = [a.lower() for a in cfg.shell_allowlist]
    if first not in allow:
        raise ToolError(f"'{first}' is not on the shell allowlist: {', '.join(allow) or 'none'}")

    proc = await asyncio.create_subprocess_shell(
        command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=20)
    except asyncio.TimeoutError:
        proc.kill()
        raise ToolError("Command timed out after 20 seconds.")
    text = (out or b"").decode("utf-8", "replace")
    return {"exit_code": proc.returncode, "output": text[:4000]}


# ------------------------------------------------------------------------------ web


def _check_allowlist(url: str) -> None:
    allow = [d.lower().strip() for d in config.load().tools.web_allowlist if d.strip()]
    if not allow:
        raise ToolError(
            "The web allowlist is empty. Add the domains the assistant may call in the admin panel."
        )
    host = (urlparse(url).hostname or "").lower()
    if not any(host == d or host.endswith(f".{d}") for d in allow):
        raise ToolError(f"'{host}' is not on the web allowlist: {', '.join(allow)}")


@registry.add(
    "fetch_url",
    "Fetch a URL and return its text, for reading a page or calling a JSON API. "
    "Only allowlisted domains are reachable.",
    {
        "type": "object",
        "properties": {
            "url": {"type": "string", "description": "The absolute http(s) URL to fetch."},
            "method": {"type": "string", "enum": ["GET", "POST"], "default": "GET"},
            "json_body": {"type": "object", "description": "JSON body to send with a POST."},
        },
        "required": ["url"],
    },
    requires="Web allowlist entries",
    tags=["web"],
)
async def fetch_url(url: str, method: str = "GET", json_body: dict[str, Any] | None = None) -> Any:
    if not url.startswith(("http://", "https://")):
        raise ToolError("Only http and https URLs are supported.")
    _check_allowlist(url)
    async with httpx.AsyncClient(
        timeout=config.load().tools.request_timeout_s, follow_redirects=True
    ) as client:
        resp = await client.request(method.upper(), url, json=json_body)
    body = resp.text
    content_type = resp.headers.get("content-type", "")
    if "json" in content_type:
        try:
            return {"status": resp.status_code, "json": resp.json()}
        except Exception:
            pass
    # Strip tags for a crude but useful text view.
    text = re.sub(r"<script.*?</script>|<style.*?</style>", " ", body, flags=re.S | re.I)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return {"status": resp.status_code, "content_type": content_type, "text": text[:6000]}


# --------------------------------------------------------------------------- timers


@registry.add(
    "set_timer",
    "Set a timer that announces itself through the assistant when it fires. Use for "
    "'remind me in ten minutes'.",
    {
        "type": "object",
        "properties": {
            "label": {"type": "string", "description": "What the timer is for."},
            "seconds": {"type": "integer", "description": "Delay in seconds."},
        },
        "required": ["label", "seconds"],
    },
    tags=["utility"],
)
async def set_timer(label: str, seconds: int) -> Any:
    seconds = max(1, min(int(seconds), 60 * 60 * 12))
    timer_id = f"t{int(time.time() * 1000)}"
    entry = {
        "id": timer_id,
        "label": label,
        "seconds": seconds,
        "fires_at": time.time() + seconds,
        "status": "pending",
    }
    _timers[timer_id] = entry

    async def _fire() -> None:
        await asyncio.sleep(seconds)
        entry["status"] = "fired"
        _reminders.append(
            {
                "id": timer_id,
                "text": f"Timer finished: {label}",
                "at": time.time(),
                "spoken": False,
            }
        )

    asyncio.create_task(_fire())
    return {"ok": True, "id": timer_id, "label": label, "seconds": seconds}


def pending_reminders(mark_read: bool = True) -> list[dict[str, Any]]:
    out = [r for r in _reminders if not r["spoken"]]
    if mark_read:
        for r in out:
            r["spoken"] = True
    return out


def active_timers() -> list[dict[str, Any]]:
    return [t for t in _timers.values() if t["status"] == "pending"]


# -------------------------------------------------------------------------- memory

_facts: list[dict[str, Any]] = []
MEMORY_PATH = None


def _memory_path():
    global MEMORY_PATH
    if MEMORY_PATH is None:
        MEMORY_PATH = config.DATA_DIR / "agent_memory.json"
        try:
            if MEMORY_PATH.exists():
                _facts.extend(json.loads(MEMORY_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    return MEMORY_PATH


def _persist_memory() -> None:
    try:
        path = _memory_path()
        path.write_text(json.dumps(_facts[-200:], indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


@registry.add(
    "remember",
    "Store a durable fact about the user or the home, for recall in later "
    "conversations.",
    {
        "type": "object",
        "properties": {
            "fact": {"type": "string", "description": "The fact to remember, stated plainly."},
            "topic": {"type": "string", "description": "A short category, e.g. 'preferences'."},
        },
        "required": ["fact"],
    },
    tags=["memory"],
)
async def remember(fact: str, topic: str = "general") -> Any:
    _memory_path()
    fact = fact.strip()[:500]
    if not fact:
        raise ToolError("Nothing to remember.")
    _facts.append({"fact": fact, "topic": topic, "at": time.time()})
    _persist_memory()
    return {"ok": True, "remembered": fact, "total": len(_facts)}


@registry.add(
    "recall",
    "Search the stored facts about the user and the home.",
    {
        "type": "object",
        "properties": {"query": {"type": "string", "description": "Text to search for."}},
        "required": ["query"],
    },
    tags=["memory"],
)
async def recall(query: str) -> Any:
    _memory_path()
    needle = query.lower().strip()
    hits = [f for f in _facts if needle in f["fact"].lower() or needle in f.get("topic", "").lower()]
    if not hits:
        hits = _facts[-10:]
    return {"count": len(hits), "facts": hits}


# ------------------------------------------------------------------ sharing


@registry.add(
    "share_file",
    "Show a file in the chat for preview or playback. Use after creating a file "
    "so the user sees the result rather than a filename. Images render inline, "
    "audio and video get a player, anything else a download.",
    {
        "type": "object",
        "properties": {
            "path": {
                "type": "string",
                "description": "Absolute path to the file to show.",
            },
            "caption": {
                "type": "string",
                "description": "A short note to display with the file.",
            },
        },
        "required": ["path"],
    },
    tags=["sharing"],
)
async def share_file(path: str, caption: str = "") -> Any:
    from .. import uploads as uploads_mod

    try:
        item = uploads_mod.register_artifact(path, name=Path(path).name)
    except uploads_mod.UploadError as exc:
        raise ToolError(str(exc)) from exc

    kind = item.kind
    if kind in ("image", "audio", "video"):
        note = f"shown in the chat as {kind}"
    else:
        note = "offered to the user as a download"
    return {
        "ok": True,
        "name": item.name,
        "kind": kind,
        "size": item.size,
        "caption": caption,
        "note": f"{item.name} is now {note}.",
    }

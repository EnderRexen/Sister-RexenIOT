"""Alexa integration.

Amazon publishes no API that lets another assistant command an Echo device or make
Alexa speak. Everything below therefore drives a *bridge* that the operator
supplies, and each bridge has a different reach:

* ``voicemonkey`` -- a hosted service with an HTTP API. Announcements and routine
  triggers work. It cannot send an arbitrary voice command or return Alexa's reply.
* ``alexacli`` -- a local CLI that talks to the same unofficial API the Alexa app
  uses (github.com/buddyh/alexa-cli). It is the only route here that can send a
  live voice command and read her answer back, but it needs a refresh token
  re-seeded roughly every two weeks, and every command must name a device.
* ``home_assistant`` -- the Alexa Media Player custom integration, reached through
  the Home Assistant tools already configured. Announce and routine only.

Because the reach differs, every tool reports what it actually did rather than
assuming the bridge could do everything.
"""

from __future__ import annotations

import asyncio
import logging
import shutil
from typing import Any

import httpx

from .. import config
from .base import ToolError, registry

log = logging.getLogger("rexeniot.tools.alexa")

VOICEMONKEY_API = "https://api.voicemonkey.io"

PROVIDERS = ("voicemonkey", "alexacli", "home_assistant")


def _tools_cfg() -> Any:
    return config.load().tools


def _provider() -> str:
    name = _tools_cfg().resolved_alexa_provider()
    if not name:
        raise ToolError(
            "Alexa is not configured. Choose a provider in the admin panel under "
            "Home & tools, then fill in that provider's settings."
        )
    if name not in PROVIDERS:
        raise ToolError(
            f"Unknown Alexa provider '{name}'. Supported providers: {', '.join(PROVIDERS)}."
        )
    return name


def _alexacli() -> str:
    cfg = _tools_cfg()
    override = cfg.resolved_alexacli_path()
    if override:
        return override
    found = shutil.which("alexacli")
    if not found:
        raise ToolError(
            "The 'alexacli' command was not found on PATH. Install it with "
            "'brew install buddyh/tap/alexacli', or set its full path in the admin panel."
        )
    return found


def _device(name: str) -> str:
    """Resolve the device to target.

    alexacli requires a device for speak, command and ask -- it does not fall
    back to the CLI's own default -- so a tool call that omits one uses the
    device saved in the admin panel, and failing that says what to do.
    """
    resolved = name.strip() or _tools_cfg().resolved_alexacli_device()
    if not resolved:
        raise ToolError(
            "No device was given and none is saved. Name one in the call, or set a "
            "default device in the admin panel under Home & tools (run alexa_devices "
            "to see the names alexacli accepts)."
        )
    return resolved


def _vm_credentials() -> tuple[str, dict[str, str]]:
    cfg = _tools_cfg()
    token = cfg.resolved_voicemonkey_token()
    if not token:
        raise ToolError(
            "No Voice Monkey token is set. Create one at voicemonkey.io and paste it "
            "into the admin panel under Home & tools."
        )
    return token, {"Content-Type": "application/json"}


async def _run_cli(args: list[str], timeout: float = 25.0) -> Any:
    """Run alexacli with ``--json`` and return its payload.

    Every subcommand wraps its result in ``{"success": true, "data": ...}``, and
    ``Success`` results use ``message`` instead. Unwrapping here means callers see
    the payload itself rather than the envelope.
    """
    cmd = [_alexacli(), *args, "--json"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except FileNotFoundError as exc:
        raise ToolError(f"Could not run alexacli: {exc}") from exc
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except asyncio.TimeoutError:
        proc.kill()
        raise ToolError(f"alexacli {args[0]} timed out after {timeout:.0f}s.") from None
    text = (stdout or b"").decode("utf-8", "replace").strip()
    err = (stderr or b"").decode("utf-8", "replace").strip()
    if proc.returncode != 0:
        # main.go prints "Error: <detail>" on stderr and exits 1; alexacli's own
        # hints (an expired token, a missing device) are worth passing through.
        raise ToolError(err or text or f"alexacli exited with code {proc.returncode}")
    if not text:
        return {"ok": True}
    import json

    try:
        payload = json.loads(text)
    except Exception:
        # A subcommand that does not honour --json; hand back the plain text.
        return {"ok": True, "detail": text, "raw": text}
    if isinstance(payload, dict) and "success" in payload:
        if not payload.get("success"):
            raise ToolError(str(payload.get("error") or "alexacli reported a failure."))
        if "data" in payload:
            return payload["data"]
        return {"ok": True, "detail": payload.get("message", "done")}
    return payload


# ------------------------------------------------------------------------ devices


@registry.add(
    "alexa_devices",
    "List the Amazon Echo devices this Alexa bridge can reach, with the identifiers "
    "the other alexa_* tools expect.",
    {"type": "object", "properties": {}},
    requires="An Alexa provider",
    tags=["alexa"],
)
async def alexa_devices() -> Any:
    provider = _provider()
    if provider == "alexacli":
        # `devices` returns the list directly under "data".
        data = await _run_cli(["devices"])
        devices = data if isinstance(data, list) else data.get("devices", data)
        return {"provider": provider, "devices": devices}
    if provider == "voicemonkey":
        token, headers = _vm_credentials()
        async with httpx.AsyncClient(timeout=20.0) as client:
            # Voice Monkey calls the bearer credential a "secret token"; the docs
            # use `token` as the query/body name for the announce API.
            resp = await client.get(
                f"{VOICEMONKEY_API}/devices", params={"token": token}, headers=headers
            )
        if resp.status_code >= 400:
            return {"ok": False, "provider": provider, "detail": f"HTTP {resp.status_code}: {resp.text[:200]}"}
        return {"provider": provider, "devices": resp.json()}
    # home_assistant: the Alexa devices are media_player entities.
    data = await registry.call("home_assistant_states", {"domain": "media_player", "limit": 50})
    return {
        "provider": provider,
        "devices": [
            {"name": e.get("friendly_name") or e.get("entity_id"), "entity_id": e.get("entity_id")}
            for e in data.get("entities", [])
        ],
        "note": "These are Home Assistant media_player entities, which may include non-Alexa players.",
    }


# ----------------------------------------------------------------- announcement


@registry.add(
    "alexa_speak",
    "Speak a message out loud on an Echo device. One-way: this announces through "
    "Alexa, it does not get a reply. Use alexa_ask if you need her answer.",
    {
        "type": "object",
        "properties": {
            "text": {"type": "string", "description": "What to say out loud."},
            "device": {
                "type": "string",
                "description": "Target device name, partial and case-insensitive. Omit for the default.",
            },
            "announce": {
                "type": "boolean",
                "description": "Broadcast to every Echo instead of one device.",
            },
        },
        "required": ["text"],
    },
    requires="An Alexa provider",
    tags=["alexa"],
)
async def alexa_speak(text: str, device: str = "", announce: bool = False) -> Any:
    provider = _provider()
    if not text.strip():
        raise ToolError("Refusing to announce empty text.")
    if provider == "alexacli":
        if announce:
            return {
                "ok": True,
                "provider": provider,
                "device": "all",
                "detail": await _run_cli(["speak", text, "--announce"]),
            }
        target = _device(device)
        return {
            "ok": True,
            "provider": provider,
            "device": target,
            "detail": await _run_cli(["speak", text, "-d", target]),
        }
    if provider == "voicemonkey":
        cfg = _tools_cfg()
        token, headers = _vm_credentials()
        target = device or cfg.voicemonkey_device or cfg.voicemonkey_announce_device
        if not target:
            raise ToolError(
                "No Voice Monkey device is set. List devices with alexa_devices and put the "
                "id in the admin panel, or pass one to this tool."
            )
        payload = {"token": token, "device": target, "speech": text}
        async with httpx.AsyncClient(timeout=25.0) as client:
            resp = await client.post(
                f"{VOICEMONKEY_API}/announce", json=payload, headers=headers
            )
        if resp.status_code >= 400:
            raise ToolError(f"Voice Monkey announce failed: HTTP {resp.status_code} {resp.text[:200]}")
        return {"ok": True, "provider": provider, "device": target, "detail": "announced"}
    # home_assistant
    cfg = _tools_cfg()
    entity = cfg.alexa_ha_entity
    if not entity:
        raise ToolError(
            "No Alexa media_player entity is set for the Home Assistant provider. "
            "Set one in the admin panel under Home & tools."
        )
    await registry.call(
        "home_assistant_call_service",
        {
            "domain": "media_player",
            "service": "play_media",
            "entity_id": entity,
            "data": {"media_content_type": "custom", "media_content_id": text},
        },
    )
    return {"ok": True, "provider": provider, "entity": entity, "detail": "announced"}


# --------------------------------------------------------------- voice command


@registry.add(
    "alexa_command",
    "Send a live voice command to an Echo device, as if the user had said it: "
    "smart-home control, music, timers, shopping lists. Requires the alexacli provider.",
    {
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "The phrase to say, e.g. 'turn off the kitchen lights'."},
            "device": {"type": "string", "description": "Target device name. Omit for the default."},
        },
        "required": ["command"],
    },
    requires="The alexacli Alexa provider",
    tags=["alexa"],
)
async def alexa_command(command: str, device: str = "") -> Any:
    provider = _provider()
    if provider != "alexacli":
        raise ToolError(
            f"Sending a voice command needs the 'alexacli' provider; '{provider}' cannot do it. "
            "Voice Monkey can only announce and trigger routines, and Home Assistant can only "
            "announce. Switch provider in the admin panel, or use alexa_speak / alexa_routine."
        )
    target = _device(device)
    return {
        "ok": True,
        "provider": provider,
        "device": target,
        "detail": await _run_cli(["command", command, "-d", target]),
    }


@registry.add(
    "alexa_ask",
    "Ask Alexa a question and read her answer back. Use this when you need her "
    "reply rather than just to speak. Requires the alexacli provider.",
    {
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "What to ask Alexa."},
            "device": {"type": "string", "description": "Target device name. Omit for the default."},
        },
        "required": ["question"],
    },
    requires="The alexacli Alexa provider",
    tags=["alexa"],
)
async def alexa_ask(question: str, device: str = "") -> Any:
    provider = _provider()
    if provider != "alexacli":
        raise ToolError(
            f"Reading Alexa's reply needs the 'alexacli' provider; '{provider}' cannot do it. "
            "Announcements through Voice Monkey or Home Assistant are one-way."
        )
    target = _device(device)
    result = await _run_cli(["ask", question, "-d", target])
    # `ask --json` returns {"question", "response", "device"} under "data".
    answer = result.get("response") if isinstance(result, dict) else None
    return {
        "ok": True,
        "provider": provider,
        "question": question,
        "device": target,
        "response": answer if answer is not None else result,
    }


@registry.add(
    "alexa_routine",
    "Fire one of the user's Alexa routines by name, or list the available ones. "
    "Routine support is work-in-progress in alexacli and may not be available yet.",
    {
        "type": "object",
        "properties": {
            "routine": {"type": "string", "description": "Routine name to run. Omit to list routines instead."},
            "list_routines": {"type": "boolean", "description": "Set true to list routines rather than run one."},
        },
    },
    requires="An Alexa provider",
    tags=["alexa"],
)
async def alexa_routine(routine: str = "", list_routines: bool = False) -> Any:
    # NOTE: the parameter is deliberately not named "list" -- that would shadow
    # the builtin used below to tell a JSON array from an object.
    provider = _provider()
    if list_routines or not routine.strip():
        if provider == "alexacli":
            # `routine list --json` returns the routine objects under "data".
            data = await _run_cli(["routine", "list"])
            routines = data if isinstance(data, list) else data.get("routines", data)
            return {"ok": True, "provider": provider, "routines": routines}
        return {
            "ok": False,
            "provider": provider,
            "detail": (
                "Listing routines is only available through the alexacli provider. "
                "With Voice Monkey, routines are triggered by name or by their trigger id."
            ),
        }
    if provider == "alexacli":
        return {
            "ok": True,
            "provider": provider,
            "routine": routine,
            "detail": await _run_cli(["routine", "run", routine]),
        }
    if provider == "voicemonkey":
        token, headers = _vm_credentials()
        payload = {"token": token, "routine": routine}
        async with httpx.AsyncClient(timeout=25.0) as client:
            resp = await client.post(f"{VOICEMONKEY_API}/trigger", json=payload, headers=headers)
        if resp.status_code >= 400:
            raise ToolError(f"Voice Monkey routine trigger failed: HTTP {resp.status_code} {resp.text[:200]}")
        return {"ok": True, "provider": provider, "routine": routine, "detail": "triggered"}
    raise ToolError(
        "The Home Assistant Alexa provider cannot trigger routines directly. "
        "Expose the routine as a script or scene in Home Assistant and call that instead."
    )

"""The agent loop.

Ties a personality, the conversation history and the tool registry together:

1. Build the system prompt from the active personality.
2. Ask the model for a reply, offering the tools it is allowed to use.
3. Execute any tool calls and feed the results back.
4. Repeat until the model answers in prose or the step budget runs out.

The loop is provider-agnostic: it consumes the normalised shape the LLM client
returns for both OpenAI-style and Anthropic-style responses.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, AsyncIterator

from . import config, personalities, skills
from .llm import LLMError, client
from .tools import ToolError, registry

log = logging.getLogger("rexeniot.agent")

#: Hard ceiling on tool round-trips per user turn, so a looping model cannot spin.
MAX_STEPS = 6

TOOL_PROTOCOL_NOTE = (
    "You have tools available. Call them when the request needs an action or live data. "
    "Do not describe a tool call in prose; either call the tool or answer directly. "
    "After tools run, answer the user in one short spoken paragraph."
)


def usable_tools(cfg: config.AppConfig) -> list[Any]:
    """Tools the model may be offered.

    Starts from the registry, then withholds anything owned by a skill that is
    switched off or whose provider is not configured yet -- so an unconfigured
    Alexa bridge never shows the model tools that could only fail.
    """
    withheld = skills.withheld_tool_names()
    return [
        t
        for t in registry.available(include_dangerous=cfg.tools.allow_shell)
        if t.name not in withheld
    ]


class AgentResult:
    def __init__(self, text: str, steps: list[dict[str, Any]], tool_names: list[str]) -> None:
        self.text = text
        self.steps = steps
        self.tool_names = tool_names

    def to_dict(self) -> dict[str, Any]:
        return {"text": self.text, "steps": self.steps, "tools_used": self.tool_names}


def build_system_prompt(personality: personalities.Personality, cfg: config.AppConfig) -> str:
    extra_bits: list[str] = []
    if cfg.llm.system_prompt_extra.strip():
        extra_bits.append(cfg.llm.system_prompt_extra.strip())

    # Reinforce the configured self-presentation so a weakly-steered model does
    # not slip into a neutral or male default voice.
    gender = cfg.branding.gender
    if gender == "female":
        extra_bits.append(
            "You present as a woman. Use she and her for yourself, and keep the voice "
            "consistent with the personality above."
        )
    elif gender == "male":
        extra_bits.append("You present as a man. Use he and him for yourself.")
    else:
        extra_bits.append("Keep your self-description neutral.")

    enabled_tools = cfg.tools.enabled
    if enabled_tools:
        usable = usable_tools(cfg)
        names = ", ".join(t.name for t in usable)
        extra_bits.append(f"{TOOL_PROTOCOL_NOTE}\nTools available to you: {names}.")
        if cfg.tools.home_assistant_url and cfg.tools.resolved_ha_token():
            extra_bits.append("Home Assistant is configured; prefer it for lights, climate, scenes and locks.")
        if cfg.tools.mqtt_host:
            extra_bits.append("An MQTT broker is configured for other IoT devices.")
        if cfg.tools.resolved_comfyui_url():
            extra_bits.append("ComfyUI is configured; use it for image and 3D generation requests.")
        if cfg.tools.resolved_alexa_provider():
            extra_bits.append(
                f"Alexa is reachable through the '{cfg.tools.resolved_alexa_provider()}' provider."
            )
        extra_bits.extend(skills.prompt_sections())
    return personality.system_prompt("\n\n".join(extra_bits))


def _history_messages(history: list[dict[str, Any]], turns: int) -> list[dict[str, Any]]:
    """Keep only user/assistant prose, most recent ``turns`` exchanges."""
    cleaned = [
        {"role": m["role"], "content": m.get("content", "")}
        for m in history
        if m.get("role") in ("user", "assistant") and isinstance(m.get("content"), str)
        and m.get("content").strip()
    ]
    return cleaned[-turns * 2 :] if turns else cleaned


def _user_message(text: str, images: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Build a user turn, attaching images in the OpenAI content-parts shape.

    Providers that do not accept image parts still receive the text; the vision
    parts are simply ignored by their request builders.
    """
    if not images:
        return {"role": "user", "content": text}
    parts: list[dict[str, Any]] = [{"type": "text", "text": text}]
    parts.extend(images)
    return {"role": "user", "content": parts}


async def run(
    user_message: str,
    history: list[dict[str, Any]] | None = None,
    personality: personalities.Personality | None = None,
    *,
    use_tools: bool = True,
    images: list[dict[str, Any]] | None = None,
) -> AgentResult:
    cfg = config.load()
    personality = personality or personalities.active()
    client.refresh()

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": build_system_prompt(personality, cfg)}
    ]
    messages.extend(_history_messages(history or [], cfg.llm.history_turns))

    # Surface any timers that fired since the last turn.
    from .tools import builtin as builtin_tools

    for reminder in builtin_tools.pending_reminders(mark_read=True):
        messages.append(
            {"role": "system", "content": f"A timer you set has finished: {reminder['text']}"}
        )

    messages.append(_user_message(user_message, images))

    tools = None
    if use_tools and cfg.tools.enabled:
        tools = [t.schema() for t in usable_tools(cfg)] or None

    steps: list[dict[str, Any]] = []
    used: list[str] = []

    for step in range(MAX_STEPS):
        try:
            reply = await client.chat(messages, tools=tools)
        except LLMError as exc:
            log.warning("agent step %d failed: %s", step, exc)
            return AgentResult(
                text=f"I could not reach the model. {exc}",
                steps=steps,
                tool_names=used,
            )

        assert isinstance(reply, dict)
        content = (reply.get("content") or "").strip()
        tool_calls = reply.get("tool_calls") or []

        if not tool_calls:
            # No tool calls: this is the answer, unless it is empty.
            if content:
                return AgentResult(text=content, steps=steps, tool_names=used)
            return AgentResult(
                text="I don't have a reply for that. Could you rephrase?",
                steps=steps,
                tool_names=used,
            )

        messages.append(
            {
                "role": "assistant",
                "content": content,
                "tool_calls": tool_calls,
            }
        )

        for call in tool_calls:
            fn = call.get("function", {})
            name = fn.get("name", "")
            used.append(name)
            started = time.time()
            try:
                result = await registry.call(name, fn.get("arguments", "{}"))
                ok = True
            except ToolError as exc:
                result = {"error": str(exc)}
                ok = False
            except Exception as exc:  # defensive: never kill the turn
                result = {"error": f"{type(exc).__name__}: {exc}"}
                ok = False

            duration = round(time.time() - started, 2)
            steps.append(
                {
                    "tool": name,
                    "arguments": fn.get("arguments"),
                    "ok": ok,
                    "duration_s": duration,
                    "result_preview": json.dumps(result, default=str)[:400],
                }
            )
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id", "call"),
                    "name": name,
                    "content": json.dumps(result, default=str)[:6000],
                }
            )

    # Step budget exhausted with tools still pending: ask for a wrap-up with no tools.
    try:
        final = await client.chat(messages, tools=None)
        assert isinstance(final, dict)
        text = (final.get("content") or "").strip() or "I ran out of steps before finishing that."
    except LLMError as exc:
        text = f"I ran out of steps and could not summarize. {exc}"
    return AgentResult(text=text, steps=steps, tool_names=used)


async def stream(
    user_message: str,
    history: list[dict[str, Any]] | None = None,
    personality: personalities.Personality | None = None,
    *,
    images: list[dict[str, Any]] | None = None,
) -> AsyncIterator[dict[str, Any]]:
    """Yield event dicts for the browser: ``status``, ``token``, ``tool``, ``done``.

    Tools run first (non-streamed, because tool calls do not survive streaming on
    every provider); the final prose answer is then streamed token by token.
    """
    cfg = config.load()
    personality = personality or personalities.active()
    client.refresh()

    messages: list[dict[str, Any]] = [
        {"role": "system", "content": build_system_prompt(personality, cfg)}
    ]
    messages.extend(_history_messages(history or [], cfg.llm.history_turns))

    from .tools import builtin as builtin_tools

    for reminder in builtin_tools.pending_reminders(mark_read=False):
        messages.append(
            {"role": "system", "content": f"A timer you set has finished: {reminder['text']}"}
        )

    messages.append(_user_message(user_message, images))

    tools = None
    if cfg.tools.enabled:
        tools = [t.schema() for t in usable_tools(cfg)] or None

    used: list[str] = []

    # --- Phase 1: resolve tool calls without streaming -----------------------
    for _ in range(MAX_STEPS):
        try:
            reply = await client.chat(messages, tools=tools)
        except LLMError as exc:
            yield {"type": "error", "message": str(exc)}
            return
        assert isinstance(reply, dict)
        tool_calls = reply.get("tool_calls") or []
        if not tool_calls:
            break  # nothing to run; fall through to streaming
        content = (reply.get("content") or "").strip()
        messages.append({"role": "assistant", "content": content, "tool_calls": tool_calls})
        for call in tool_calls:
            fn = call.get("function", {})
            name = fn.get("name", "")
            used.append(name)
            yield {"type": "status", "message": f"Running {name.replace('_', ' ')}…"}
            try:
                result = await registry.call(name, fn.get("arguments", "{}"))
                ok = True
            except ToolError as exc:
                result = {"error": str(exc)}
                ok = False
            except Exception as exc:
                result = {"error": f"{type(exc).__name__}: {exc}"}
                ok = False
            yield {
                "type": "tool",
                "tool": name,
                "ok": ok,
                "result": json.dumps(result, default=str)[:300],
            }
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.get("id", "call"),
                    "name": name,
                    "content": json.dumps(result, default=str)[:6000],
                }
            )
    else:
        messages.append(
            {"role": "system", "content": "Stop using tools and answer the user now in one short paragraph."}
        )

    # --- Phase 2: stream the prose answer -----------------------------------
    yield {"type": "status", "message": "Composing…"}
    pieces: list[str] = []
    try:
        stream_result = await client.chat(messages, tools=None, stream=True)
        async for delta in stream_result:  # type: ignore[union-attr]
            pieces.append(delta)
            yield {"type": "token", "text": delta}
    except LLMError as exc:
        yield {"type": "error", "message": str(exc)}
        return

    text = "".join(pieces).strip()
    if not text:
        # Some providers return nothing on a stream-only call; retry unstreamed.
        try:
            reply = await client.chat(messages, tools=None)
            assert isinstance(reply, dict)
            text = (reply.get("content") or "").strip()
            if text:
                yield {"type": "token", "text": text}
        except LLMError:
            text = ""

    if not text:
        text = "I don't have a reply for that. Could you rephrase?"

    builtin_tools.pending_reminders(mark_read=True)
    yield {"type": "done", "text": text, "tools_used": used}

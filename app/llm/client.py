"""LLM client.

One interface over every backend we care about:

* **openai** -- any OpenAI-compatible ``/chat/completions`` endpoint (OpenAI,
  Together, Groq, vLLM, LM Studio, LocalAI, llama.cpp server).
* **anthropic** -- the Messages API.
* **ollama** -- Ollama's OpenAI-compatible endpoint, which is also the default
  when a bare ``host:11434`` has no ``/v1`` suffix.
* **offline** -- no network. Returns a scripted reply so the UI, TTS and voice
  loop can all be exercised before a model is chosen.

Native tool calling is used where the provider supports it. When it does not, the
agent layer falls back to a text protocol, so capabilities degrade rather than
disappear.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator

import httpx

from .. import config

log = logging.getLogger("rexeniot.llm")


class LLMError(RuntimeError):
    pass


def detect_provider(cfg: config.LLMConfig) -> str:
    """Resolve ``auto`` to a concrete provider from the base URL."""
    if cfg.provider != "auto":
        return cfg.provider
    url = (cfg.base_url or "").lower()
    if not url:
        return "offline"
    if "anthropic" in url:
        return "anthropic"
    if "11434" in url or "ollama" in url:
        return "ollama"
    if "openai.com" in url or "/v1" in url or "generativelanguage" in url:
        return "openai"
    return "openai"


#: Model families that emit a hidden reasoning trace before the answer.
_REASONING_MARKERS = ("qwen3", "qwq", "deepseek-r1", "magistral", "glm-z", "o1", "o3", "o4")


def reasoning_effort_for(cfg: config.LLMConfig, provider: str) -> str:
    """How much hidden reasoning to ask for, or "" to send no such field.

    A reasoning model on a local box spends its whole ``max_tokens`` budget on
    the trace and can return an empty answer, so the default here is to turn
    the trace off for models known to produce one. An explicit
    ``llm.reasoning_effort`` always wins. Anthropic has no equivalent field.
    """
    if provider == "anthropic":
        return ""
    setting = (cfg.reasoning_effort or "").strip().lower()
    if setting:
        return "" if setting == "default" else setting
    name = (cfg.model or "").lower()
    if any(marker in name for marker in _REASONING_MARKERS):
        return "none"
    return ""


def _normalize_base_url(provider: str, base_url: str) -> str:
    url = (base_url or "").strip().rstrip("/")
    if provider == "anthropic":
        return url or "https://api.anthropic.com/v1"
    if provider == "ollama":
        # Accept "localhost:11434" as well as a full URL.
        if not url.startswith("http"):
            url = f"http://{url}"
        if not url.endswith("/v1"):
            url = f"{url}/v1"
        return url
    if provider == "openai":
        if not url:
            return "https://api.openai.com/v1"
        if not url.startswith("http"):
            url = f"http://{url}"
        return url
    return url


# Well-known local model servers, in the order a first run should prefer them.
# llama-swap/llama.cpp leads because it is the documented local stack; then the
# usual suspects. host.docker.internal lets a container reach a server on the
# host, which is how the NixOS setup runs.
LOCAL_CANDIDATES: list[tuple[str, str]] = [
    ("http://localhost:8080/v1", "llama-swap / llama.cpp"),
    ("http://localhost:11434/v1", "Ollama"),
    ("http://localhost:1234/v1", "LM Studio"),
    ("http://localhost:8000/v1", "vLLM"),
    ("http://host.docker.internal:8080/v1", "llama-swap on the host"),
    ("http://host.docker.internal:11434/v1", "Ollama on the host"),
]


async def _probe(base_url: str, label: str) -> dict[str, Any] | None:
    """Ask one candidate what it serves. None when nothing usable answers."""
    try:
        async with httpx.AsyncClient(timeout=2.5) as http:
            resp = await http.get(f"{base_url.rstrip('/')}/models")
            if resp.status_code >= 400:
                return None
            payload = resp.json()
    except Exception:
        return None
    ids = [m.get("id") for m in payload.get("data", []) if m.get("id")]
    if not ids:
        return None
    return {"base_url": base_url, "label": label, "models": sorted(ids)}


async def discover_local() -> list[dict[str, Any]]:
    """Report every local model server that answers, best candidate first.

    Read-only: nothing is written, so the admin panel can show what is available
    without disturbing a configuration that is already working.
    """
    results = await asyncio.gather(*(_probe(url, label) for url, label in LOCAL_CANDIDATES))
    return [r for r in results if r]


async def probe_url(base_url: str) -> dict[str, Any] | None:
    """Ask one specific server what it serves. None when it does not answer."""
    return await _probe(base_url, "")


async def adopt_first_local() -> dict[str, Any] | None:
    """Point the LLM at the first local server that answers, and persist it.

    Used once on first boot so a fresh install can chat without anyone opening
    the admin panel. Returns what was adopted, or None when nothing local is
    listening -- in which case the caller leaves the app in offline mode rather
    than inventing an endpoint.

    Only ever called when no model is configured, so it cannot overwrite a
    working setup.
    """
    found = await discover_local()
    if not found:
        return None
    best = found[0]
    model = best["models"][0]
    config.update(
        {"llm": {"provider": "auto", "base_url": best["base_url"], "model": model}}
    )
    client.refresh()
    return {"base_url": best["base_url"], "model": model, "label": best.get("label", "")}


#: How long to keep looking for a model server after boot, and how often.
#:
#: The app and a local model server are separate services with no ordering
#: between them, so the app can win the boot race and probe before the model
#: server is listening. A one-shot probe misses it and the app then sits offline
#: for the life of the process. Retrying covers that window, and the ordinary
#: case of starting the model server later by hand.
ADOPT_RETRY_ATTEMPTS = 20
ADOPT_RETRY_INTERVAL_S = 15.0


async def adopt_first_local_in_background(
    interval_s: float | None = None,
    attempts: int | None = None,
) -> dict[str, Any] | None:
    """Keep probing until a local model server appears, then adopt it.

    Returns what was adopted, or None if none appeared within the budget. The
    arguments default to the module constants at call time so a caller -- or a
    test -- can shorten them.

    Every attempt re-reads the config first, so a model chosen in the admin panel
    while this is running wins and the loop exits rather than overwriting it.
    """
    interval = ADOPT_RETRY_INTERVAL_S if interval_s is None else interval_s
    tries = ADOPT_RETRY_ATTEMPTS if attempts is None else attempts

    for _ in range(max(1, tries)):
        # Wait first: the caller has already probed once and found nothing.
        await asyncio.sleep(interval)
        cfg = config.load(force=True).llm
        if cfg.model or cfg.provider == "offline":
            return None
        try:
            adopted = await adopt_first_local()
        except Exception as exc:
            # Still a normal state, not a failure worth stopping for.
            log.debug("background model discovery failed: %s", exc)
            continue
        if adopted:
            return adopted
    return None


class LLMClient:
    def __init__(self, cfg: config.LLMConfig | None = None) -> None:
        self.cfg = cfg or config.load().llm

    def refresh(self) -> None:
        self.cfg = config.load().llm

    @property
    def provider(self) -> str:
        return detect_provider(self.cfg)

    @property
    def base_url(self) -> str:
        return _normalize_base_url(self.provider, self.cfg.base_url)

    def describe(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "base_url": self.base_url or None,
            "model": self.cfg.model or None,
            "configured": bool(self.cfg.model) and self.provider != "offline",
        }

    # ------------------------------------------------------------------ health

    async def health(self) -> dict[str, Any]:
        provider = self.provider
        info = self.describe()
        if provider == "offline":
            return {**info, "ok": True, "detail": "offline mode (no model configured)"}
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                if provider == "ollama":
                    root = self.base_url.replace("/v1", "")
                    resp = await client.get(f"{root}/api/tags")
                    if resp.status_code == 200:
                        names = [m.get("name") for m in resp.json().get("models", [])]
                        return {
                            **info, "ok": True,
                            "detail": f"broker reachable, {len(names)} local models",
                            "models": names,
                        }
                    return {**info, "ok": False, "detail": f"HTTP {resp.status_code}"}
                headers = self._headers()
                resp = await client.get(f"{self.base_url}/models", headers=headers)
                ok = resp.status_code < 400
                detail = f"HTTP {resp.status_code}"
                if ok:
                    try:
                        ids = [m["id"] for m in resp.json().get("data", [])][:50]
                        detail = f"reachable, {len(ids)} models"
                    except Exception:
                        detail = "reachable"
                return {**info, "ok": ok, "detail": detail}
        except Exception as exc:
            return {**info, "ok": False, "detail": f"{type(exc).__name__}: {exc}"}

    async def list_models(self) -> list[str]:
        provider = self.provider
        if provider == "offline":
            return []
        try:
            async with httpx.AsyncClient(timeout=8.0) as client:
                if provider == "ollama":
                    root = self.base_url.replace("/v1", "")
                    resp = await client.get(f"{root}/api/tags")
                    if resp.status_code == 200:
                        return sorted(m.get("name", "") for m in resp.json().get("models", []))
                    return []
                resp = await client.get(f"{self.base_url}/models", headers=self._headers())
                if resp.status_code < 400:
                    return sorted(m.get("id", "") for m in resp.json().get("data", []))
        except Exception:
            pass
        return []

    # ------------------------------------------------------------------ headers

    def _headers(self) -> dict[str, str]:
        key = self.cfg.resolved_api_key()
        if self.provider == "anthropic":
            if not key:
                raise LLMError("Anthropic needs an API key (set ANTHROPIC_API_KEY)")
            return {
                "x-api-key": key,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            }
        headers = {"content-type": "application/json"}
        if key:
            headers["authorization"] = f"Bearer {key}"
        return headers

    # ------------------------------------------------------------------- chat

    async def chat(
        self,
        messages: list[dict[str, Any]],
        *,
        tools: list[dict[str, Any]] | None = None,
        stream: bool = False,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> dict[str, Any] | AsyncIterator[str]:
        if stream:
            return self._stream(messages, tools, temperature, max_tokens or self.cfg.max_tokens)
        return await self._complete(messages, tools, temperature, max_tokens)

    async def _complete(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        temperature: float | None,
        max_tokens: int | None,
    ) -> dict[str, Any]:
        provider = self.provider
        if provider == "offline":
            return self._offline_reply(messages)

        temperature = self.cfg.temperature if temperature is None else temperature
        max_tokens = max_tokens or self.cfg.max_tokens
        model = self.cfg.model or ("claude-3-5-haiku-latest" if provider == "anthropic" else "gpt-4o-mini")

        if provider == "anthropic":
            body: dict[str, Any] = {
                "model": model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "messages": self._anthropic_messages(messages),
            }
            system = self._system_text(messages)
            if system:
                body["system"] = system
            if tools:
                body["tools"] = [self._anthropic_tool(t) for t in tools]
            payload = await self._post(f"{self.base_url}/messages", body)
            return self._parse_anthropic(payload)

        body = {
            "model": model,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "messages": messages,
        }
        effort = reasoning_effort_for(self.cfg, provider)
        if effort:
            body["reasoning_effort"] = effort
        if tools:
            body["tools"] = tools
            body["tool_choice"] = "auto"
        payload = await self._post(f"{self.base_url}/chat/completions", body)
        return self._parse_openai(payload)

    async def _post(self, url: str, body: dict[str, Any], timeout: float | None = None) -> dict[str, Any]:
        try:
            async with httpx.AsyncClient(timeout=timeout or self.cfg.timeout_s) as client:
                resp = await client.post(url, headers=self._headers(), json=body)
        except httpx.HTTPError as exc:
            raise LLMError(f"Could not reach the model at {url}: {exc}") from exc
        if resp.status_code >= 400:
            detail = resp.text[:400]
            raise LLMError(f"Model returned HTTP {resp.status_code}: {detail}")
        try:
            return resp.json()
        except Exception as exc:
            raise LLMError(f"Model returned non-JSON response: {exc}") from exc

    # -------------------------------------------------------------- tool format

    @staticmethod
    def _anthropic_tool(tool: dict[str, Any]) -> dict[str, Any]:
        fn = tool.get("function", tool)
        return {
            "name": fn["name"],
            "description": fn.get("description", ""),
            "input_schema": fn.get("parameters", {"type": "object", "properties": {}}),
        }

    @staticmethod
    def _system_text(messages: list[dict[str, Any]]) -> str:
        return "\n\n".join(
            m.get("content", "") for m in messages if m.get("role") == "system"
        ).strip()

    @staticmethod
    def _anthropic_content(content: Any) -> Any:
        """Translate OpenAI-style content parts into Anthropic blocks.

        Only image parts need translating; text parts are already compatible.
        """
        if not isinstance(content, list):
            return content or ""
        blocks: list[dict[str, Any]] = []
        for part in content:
            if not isinstance(part, dict):
                blocks.append({"type": "text", "text": str(part)})
                continue
            if part.get("type") == "text":
                blocks.append({"type": "text", "text": part.get("text", "")})
            elif part.get("type") == "image_url":
                url = (part.get("image_url") or {}).get("url", "")
                if url.startswith("data:") and ";base64," in url:
                    header, b64 = url.split(";base64,", 1)
                    media = header[5:] or "image/png"
                    blocks.append(
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": media, "data": b64},
                        }
                    )
                elif url:
                    blocks.append({"type": "image", "source": {"type": "url", "url": url}})
        return blocks or ""

    @staticmethod
    def _anthropic_messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert internal OpenAI-shaped messages to Anthropic's shape."""
        out: list[dict[str, Any]] = []
        for msg in messages:
            role = msg.get("role")
            if role == "system":
                continue
            if role == "tool":
                out.append(
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "tool_result",
                                "tool_use_id": msg.get("tool_call_id", "call"),
                                "content": str(msg.get("content", ""))[:6000],
                            }
                        ],
                    }
                )
                continue
            if role == "assistant" and msg.get("tool_calls"):
                blocks: list[dict[str, Any]] = []
                if msg.get("content"):
                    blocks.append({"type": "text", "text": msg["content"]})
                for call in msg["tool_calls"]:
                    fn = call.get("function", {})
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except Exception:
                        args = {}
                    blocks.append(
                        {
                            "type": "tool_use",
                            "id": call.get("id", "call"),
                            "name": fn.get("name", ""),
                            "input": args,
                        }
                    )
                out.append({"role": "assistant", "content": blocks})
                continue
            out.append({"role": role or "user", "content": LLMClient._anthropic_content(msg.get("content"))})
        # Anthropic rejects two same-role turns in a row; merge them.
        merged: list[dict[str, Any]] = []
        for msg in out:
            if merged and merged[-1]["role"] == msg["role"]:
                prev, cur = merged[-1]["content"], msg["content"]
                merged[-1]["content"] = (
                    prev + "\n\n" + cur
                    if isinstance(prev, str) and isinstance(cur, str)
                    else [*(prev if isinstance(prev, list) else [{"type": "text", "text": prev}]),
                          *(cur if isinstance(cur, list) else [{"type": "text", "text": cur}])]
                )
            else:
                merged.append(msg)
        if not merged:
            merged = [{"role": "user", "content": "Hello"}]
        return merged

    # --------------------------------------------------------------- parsing

    @staticmethod
    def _parse_openai(payload: dict[str, Any]) -> dict[str, Any]:
        choices = payload.get("choices") or []
        if not choices:
            raise LLMError(f"Model returned no choices: {str(payload)[:300]}")
        message = choices[0].get("message") or {}
        return {
            "content": message.get("content") or "",
            "reasoning": message.get("reasoning_content") or "",
            "tool_calls": message.get("tool_calls") or [],
            "finish_reason": choices[0].get("finish_reason"),
            "usage": payload.get("usage") or {},
            "raw": payload,
        }

    @staticmethod
    def _parse_anthropic(payload: dict[str, Any]) -> dict[str, Any]:
        text_parts: list[str] = []
        tool_calls: list[dict[str, Any]] = []
        for block in payload.get("content", []):
            if block.get("type") == "text":
                text_parts.append(block.get("text", ""))
            elif block.get("type") == "tool_use":
                tool_calls.append(
                    {
                        "id": block.get("id", "call"),
                        "type": "function",
                        "function": {
                            "name": block.get("name", ""),
                            "arguments": json.dumps(block.get("input", {})),
                        },
                    }
                )
        return {
            "content": "\n".join(p for p in text_parts if p),
            "reasoning": "",
            "tool_calls": tool_calls,
            "finish_reason": payload.get("stop_reason"),
            "usage": payload.get("usage") or {},
            "raw": payload,
        }

    # --------------------------------------------------------------- streaming

    async def _stream(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None,
        temperature: float | None,
        max_tokens: int,
    ) -> AsyncIterator[str]:
        """Yield text deltas. Tool calls are not streamed; use ``chat`` for those."""
        provider = self.provider
        if provider == "offline":
            reply = self._offline_reply(messages)["content"]
            for chunk in _chunk_text(reply, 24):
                yield chunk
            return

        temperature = self.cfg.temperature if temperature is None else temperature
        model = self.cfg.model or ("claude-3-5-haiku-latest" if provider == "anthropic" else "gpt-4o-mini")

        if provider == "anthropic":
            body: dict[str, Any] = {
                "model": model,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "messages": self._anthropic_messages(messages),
                "stream": True,
            }
            system = self._system_text(messages)
            if system:
                body["system"] = system
            if tools:
                body["tools"] = [self._anthropic_tool(t) for t in tools]
            url = f"{self.base_url}/messages"
        else:
            body = {
                "model": model,
                "temperature": temperature,
                "max_tokens": max_tokens,
                "messages": messages,
                "stream": True,
            }
            effort = reasoning_effort_for(self.cfg, provider)
            if effort:
                body["reasoning_effort"] = effort
            if tools:
                body["tools"] = tools
            url = f"{self.base_url}/chat/completions"

        try:
            async with httpx.AsyncClient(timeout=self.cfg.timeout_s) as client:
                async with client.stream("POST", url, headers=self._headers(), json=body) as resp:
                    if resp.status_code >= 400:
                        text = (await resp.aread()).decode("utf-8", "replace")[:400]
                        raise LLMError(f"Model returned HTTP {resp.status_code}: {text}")
                    async for line in resp.aiter_lines():
                        if not line or not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        try:
                            event = json.loads(data)
                        except Exception:
                            continue
                        delta = self._delta_from_event(provider, event)
                        if delta:
                            yield delta
        except httpx.HTTPError as exc:
            raise LLMError(f"Stream failed: {exc}") from exc

    @staticmethod
    def _delta_from_event(provider: str, event: dict[str, Any]) -> str:
        if provider == "anthropic":
            if event.get("type") == "content_block_delta":
                return (event.get("delta") or {}).get("text", "") or ""
            return ""
        choices = event.get("choices") or []
        if not choices:
            return ""
        return (choices[0].get("delta") or {}).get("content") or ""

    # ---------------------------------------------------------------- offline

    @staticmethod
    def _offline_reply(messages: list[dict[str, Any]]) -> dict[str, Any]:
        user = next(
            (m.get("content", "") for m in reversed(messages) if m.get("role") == "user"),
            "",
        )
        text = (
            "I'm running in offline mode, so I can't reason about that yet. "
            "Open the admin panel and point me at a model -- a local Ollama server or an "
            "OpenAI-compatible endpoint both work -- and I'll answer properly."
        )
        return {
            "content": text, "reasoning": "", "tool_calls": [],
            "finish_reason": "offline", "usage": {}, "raw": {"echo": user[:200]},
        }


def _chunk_text(text: str, size: int) -> list[str]:
    """Split into roughly sentence-sized pieces so playback starts sooner."""
    words = text.split(" ")
    out: list[str] = []
    current = ""
    for word in words:
        if len(current) + len(word) + 1 > size and current:
            out.append(current + " ")
            current = word
        else:
            current = f"{current} {word}".strip()
    if current:
        out.append(current)
    return out or [text]


client = LLMClient()

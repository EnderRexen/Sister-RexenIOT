"""A reasoning model is asked to answer instead of thinking, and dead tools are withheld.

Two problems cost a local setup most of its latency and tokens, and neither
showed up in a test before.

First, reasoning models. Ollama's qwen3 and deepseek-r1 emit a hidden trace
before the answer. On a local box the trace eats the whole ``max_tokens``
budget, so a plain question can come back empty or take twenty seconds instead
of one. The fix is to send ``reasoning_effort: "none"`` on the OpenAI-compatible
path. That field is easy to add and just as easy to lose, so this asserts it is
present for a reasoning model, absent for a plain one, and overridable from
config.

Second, dead tools. A skill whose provider is not configured still published
its tools, so the model was offered Home Assistant and ComfyUI functions that
could only ever fail. The skill gating now withholds them. This asserts that an
unconfigured install offers no tool whose provider is missing.

The model server is a stand-in that records the request body, so nothing here
needs a real model or a running desktop.

Run: PYTHONPATH=. ./.venv/bin/python scripts/test_reasoning_and_gating.py
"""

from __future__ import annotations

import asyncio
import json
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import config, skills  # noqa: E402
from app.llm.client import LLMClient  # noqa: E402
from app.tools import registry  # noqa: E402

failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


#: Every chat-completion body the stand-in server received.
seen: list[dict] = []


class _Recorder(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        seen.append(body)
        payload = {
            "id": "chatcmpl-1",
            "object": "chat.completion",
            "choices": [{
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "done"},
            }],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        }
        raw = json.dumps(payload).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def log_message(self, *args):
        pass


def ask(model: str, **overrides) -> dict:
    """Send one real chat() through LLMClient and return the body the server saw."""
    port = _server.server_address[1]
    cfg = config.LLMConfig(
        provider="ollama",
        base_url=f"http://127.0.0.1:{port}/v1",
        model=model,
        **overrides,
    )
    seen.clear()
    asyncio.run(LLMClient(cfg).chat([{"role": "user", "content": "hello"}]))
    assert seen, "the model server was never called"
    return seen[-1]


_server = HTTPServer(("127.0.0.1", 0), _Recorder)
threading.Thread(target=_server.serve_forever, daemon=True).start()

print("reasoning effort")
check(ask("qwen3:8b").get("reasoning_effort") == "none",
      "a qwen3 model is asked not to think")
check(ask("deepseek-r1:7b").get("reasoning_effort") == "none",
      "a deepseek-r1 model is asked not to think")
check("reasoning_effort" not in ask("gemma3:12b"),
      "a non-reasoning model gets no such field")
check("reasoning_effort" not in ask("qwen3:8b", reasoning_effort="default"),
      "config 'default' leaves the field off")
check(ask("qwen3:8b", reasoning_effort="high").get("reasoning_effort") == "high",
      "an explicit config value wins")
check("reasoning_effort" not in ask("llama3.1:8b"),
      "a plain model gets no such field")

print("\ntool gating")
withheld = skills.withheld_tool_names()
offered = {t.name for t in registry.available()} - withheld
check("home_assistant_call_service" in withheld,
      "Home Assistant is withheld with no URL configured")
check("generate_media" in withheld, "ComfyUI is withheld with no URL configured")
check("alexa_speak" in withheld, "Alexa is withheld with no provider configured")
check("set_timer" in offered, "a built-in tool with no provider stays offered")
check("notify_user" in offered, "a desktop tool with no provider stays offered")

# The skill list is what the prompt renders from, so a disabled skill must not
# contribute its tools either.
home = skills.get("home")
if home is not None:
    home.enabled = False
    check("home_assistant_states" in skills.withheld_tool_names(),
          "disabling a skill also withholds its tools")

print()
if failures:
    print(f"{len(failures)} check(s) failed:")
    for item in failures:
        print(f"  - {item}")
    sys.exit(1)
print("all checks passed")

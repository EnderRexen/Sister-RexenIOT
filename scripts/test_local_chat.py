"""A local OpenAI-compatible model answers a chat, and can call a tool.

The other local-model tests stop at discovery: they prove the app finds a server
and persists its model id. None of them sends a completion, so nothing covered
the path that actually matters for a local setup -- that a request reaches the
model, that the reply comes back through the agent, and that tool calling works
against a local server.

That last one is the fragile part. llama-server only accepts the `tools` field
with `--jinja`; without it the request fails with HTTP 500. So a green
discovery check says nothing about whether the agent can act.

This runs the real server against a throwaway REXENIOT_HOME with a stand-in
OpenAI server that behaves like llama-swap: it serves /v1/models and
/v1/chat/completions, requests a tool call first and prose after, and answers
streamed and unstreamed. Nothing on the machine is touched.

Run: PYTHONPATH=. ./.venv/bin/python scripts/test_local_chat.py
"""

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PY = sys.executable
MODEL = "qwen3-8b"
failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


#: What the stand-in server saw, so the test can assert the app offered tools.
seen: dict[str, object] = {"tool_requests": 0, "stream_requests": 0, "fallback": 0}


class _FakeOpenAI(BaseHTTPRequestHandler):
    """llama-swap's two endpoints: /v1/models and /v1/chat/completions."""

    def _json(self, payload: dict, code: int = 200) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if not self.path.startswith("/v1/models"):
            self.send_error(404)
            return
        self._json({"object": "list", "data": [{"id": MODEL, "object": "model"}]})

    def do_POST(self):
        if not self.path.startswith("/v1/chat/completions"):
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", 0))
        body = json.loads(self.rfile.read(length) or b"{}")
        messages = body.get("messages") or []
        tools_offered = bool(body.get("tools"))
        if tools_offered:
            seen["tool_requests"] += 1
        # The agent sends the tool result back as role="tool". Until that shows
        # up, ask for a tool call; afterwards answer in prose so the loop ends.
        after_tool = any(m.get("role") == "tool" for m in messages)
        # A non-streamed call that offers no tools, after a tool result, is the
        # agent's fallback for a stream that produced nothing. Phase 1 always
        # sends the tool schemas, so it cannot be mistaken for the fallback.
        if after_tool and not body.get("stream") and not tools_offered:
            seen["fallback"] += 1
        if tools_offered and not after_tool:
            self._json({
                "id": "chatcmpl-1",
                "object": "chat.completion",
                "choices": [{
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [{
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "set_timer",
                                "arguments": json.dumps({"label": "tea", "seconds": 60}),
                            },
                        }],
                    },
                }],
                "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
            })
            return

        answer = "Your timer is set for one minute."
        if body.get("stream"):
            seen["stream_requests"] += 1
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.end_headers()
            for word in answer.split(" "):
                chunk = {"choices": [{"index": 0, "delta": {"content": word + " "}}]}
                self.wfile.write(f"data: {json.dumps(chunk)}\n\n".encode())
                self.wfile.flush()
            self.wfile.write(b"data: [DONE]\n\n")
            self.wfile.flush()
            return

        self._json({
            "id": "chatcmpl-2",
            "object": "chat.completion",
            "choices": [{
                "index": 0,
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": answer},
            }],
            "usage": {"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28},
        })

    def log_message(self, *args):
        pass


def start_fake(port: int) -> HTTPServer:
    srv = HTTPServer(("127.0.0.1", port), _FakeOpenAI)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def start_app(home: Path, port: int) -> subprocess.Popen:
    env = {**os.environ, "REXENIOT_HOME": str(home), "PATH": os.environ.get("PATH", "")}
    return subprocess.Popen(
        [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ROOT), env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
    )


def post(base: str, path: str, payload: dict, timeout: int = 120):
    req = urllib.request.Request(
        f"{base}{path}", data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    return urllib.request.urlopen(req, timeout=timeout)


def main() -> None:
    # REXENIOT_HOME relocates the whole app root, so link the read-only assets in
    # and leave only data/ fresh.
    home = Path(tempfile.mkdtemp(prefix="rexeniot-local-chat-"))
    (home / "data").mkdir()
    for asset in ["web", *[p.name for p in ROOT.glob("requirements*.txt")]]:
        (home / asset).symlink_to(ROOT / asset)

    llm_port = free_port()
    app_port = free_port()

    # Point the app at the stand-in. Speech is switched off so the server does
    # not spend a minute loading models it will not use here.
    (home / "data" / "config.json").write_text(json.dumps({
        "llm": {"provider": "openai", "base_url": f"http://127.0.0.1:{llm_port}/v1", "model": MODEL},
        "tts": {"enabled": False},
        "stt": {"enabled": False},
        "voice_input": {"engine": "browser"},
    }))

    srv = start_fake(llm_port)
    proc = start_app(home, app_port)
    try:
        base = f"http://127.0.0.1:{app_port}"
        deadline = time.time() + 240
        ready = False
        while time.time() < deadline:
            try:
                with urllib.request.urlopen(f"{base}/api/status", timeout=5):
                    ready = True
                    break
            except Exception:
                time.sleep(2)
        check(ready, f"the server came up on port {app_port}")
        if not ready:
            print(proc.stdout.read().decode()[-1500:] if proc.stdout else "")
            return

        with urllib.request.urlopen(f"{base}/api/status", timeout=15) as r:
            llm = json.load(r).get("llm", {})
        check(llm.get("configured") is True, f"a model is configured ({llm.get('model')!r})")
        check(llm.get("base_url", "").endswith("/v1"), f"it points at the local server ({llm.get('base_url')!r})")

        print("\nthe model is asked for a tool call, then answers")
        with post(base, "/api/chat", {"message": "set a timer for one minute", "speak": False}) as r:
            reply = json.load(r)
        check(reply.get("reply") == "Your timer is set for one minute.",
              f"the prose reply came back ({reply.get('reply')!r})")
        check("set_timer" in (reply.get("tools_used") or []),
              f"the tool the model asked for ran ({reply.get('tools_used')})")
        steps = reply.get("steps") or []
        check(len(steps) >= 1, f"the loop recorded the step ({len(steps)})")
        check(any(s.get("tool") == "set_timer" and s.get("ok") for s in steps),
              f"the step shows the tool succeeded ({steps[:1]})")
        check(seen["tool_requests"] >= 1, "the app offered tool schemas to the local server")
        check(seen["stream_requests"] == 0, "the non-streamed call did not ask for a stream")

        print("\nthe same turn streams token by token")
        events = []
        with post(base, "/api/chat/stream", {"message": "set a timer for one minute", "speak": False}) as r:
            for raw in r:
                line = raw.decode().strip()
                if line.startswith("data:"):
                    events.append(json.loads(line[5:].strip()))
        kinds = [e.get("type") for e in events]
        check("start" in kinds, "the stream opened with a start event")
        tokens = [e.get("text") for e in events if e.get("type") == "token"]
        # The agent falls back to one unstreamed call and emits the whole answer
        # as a single token when the stream yields nothing. That makes a naive
        # "tokens > 0" check pass even with streaming broken, so require the
        # answer to arrive in several pieces.
        check(len(tokens) > 1,
              f"tokens arrived incrementally, not as one fallback blob ({len(tokens)} token events)")
        done = next((e for e in events if e.get("type") == "done"), None)
        check(done is not None, "the stream finished with a done event")
        if done:
            check(done.get("text") == "Your timer is set for one minute.",
                  f"the streamed text reassembles correctly ({done.get('text')!r})")
            check("set_timer" in (done.get("tools_used") or []),
                  f"the streamed turn also ran the tool ({done.get('tools_used')})")
        check("error" not in kinds, f"no stream errors ({[e for e in events if e.get('type')=='error']})")
        check(seen["stream_requests"] >= 1, "the streaming call did ask the local server to stream")
        check(seen["fallback"] == 0,
              f"the agent never needed its unstreamed fallback ({seen['fallback']} fallback calls)")

        print("\nthe timer the model set is real state, not a claim")
        with urllib.request.urlopen(f"{base}/api/reminders", timeout=15) as r:
            pending = json.load(r).get("pending", [])
        labels = [t.get("label") for t in pending]
        check("tea" in labels, f"set_timer registered a live timer ({labels})")
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
        srv.shutdown()
        shutil.rmtree(home, ignore_errors=True)

    print()
    if failures:
        print(f"{len(failures)} FAILED")
        for f in failures:
            print(f"  - {f}")
        sys.exit(1)
    print("all checks passed")


main()

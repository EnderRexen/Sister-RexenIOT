"""The app finds a model server that starts *after* it does.

The app and the model server are separate services with no ordering between
them, so the app can win the boot race and probe before the model server is
listening. A one-shot probe misses it and the app then sits offline for the life
of the process -- it never looks again.

So discovery retries in the background, and it must do so without ever
overwriting a model that was configured in the meantime. Three properties:

  1. a server that appears after boot is adopted,
  2. a model chosen while the retry is running wins, and
  3. the retry is bounded, so nothing spins forever.

Runs the real server against a throwaway REXENIOT_HOME, with a stand-in OpenAI
server it starts partway through, so nothing on the machine is touched.
"""

import asyncio
import importlib
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
FAILS = []


def check(label, ok, detail=""):
    print(f"  {'PASS' if ok else 'FAIL'}  {label}" + (f" — {detail}" if detail else ""))
    if not ok:
        FAILS.append(label)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class _FakeOpenAI(BaseHTTPRequestHandler):
    """Answers /v1/models the way llama-swap does, with one model."""

    def do_GET(self):
        if not self.path.startswith("/v1/models"):
            self.send_error(404)
            return
        body = json.dumps({"object": "list", "data": [{"id": "qwen3-8b", "object": "model"}]}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def start_fake_server(port):
    srv = HTTPServer(("127.0.0.1", port), _FakeOpenAI)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


def write_sitecustomize(home, llm_port, interval, attempts):
    """Point discovery at one port and shorten the retry budget."""
    (home / "sitecustomize.py").write_text(
        "import importlib, os\n"
        "m = importlib.import_module('app.llm.client')\n"
        f"m.LOCAL_CANDIDATES = [('http://127.0.0.1:{llm_port}/v1', 'late llama-swap')]\n"
        f"m.ADOPT_RETRY_INTERVAL_S = {interval}\n"
        f"m.ADOPT_RETRY_ATTEMPTS = {attempts}\n",
        encoding="utf-8",
    )


def start_app(home, port):
    env = {
        **os.environ,
        "REXENIOT_HOME": str(home),
        "PYTHONPATH": f"{home}{os.pathsep}{ROOT}",
    }
    return subprocess.Popen(
        [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def status_llm(base):
    with urllib.request.urlopen(f"{base}/api/status", timeout=10) as r:
        return json.load(r).get("llm", {})


def wait_for(pred, deadline_s):
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        if pred():
            return True
        time.sleep(1)
    return False


def scenario_late_server():
    """A model server that appears after boot is adopted, and persisted."""
    print("\na server that starts after the app is adopted")
    home = Path(tempfile.mkdtemp(prefix="rexeniot-late-"))
    llm_port = free_port()
    app_port = free_port()
    write_sitecustomize(home, llm_port, interval=1.5, attempts=20)
    proc = start_app(home, app_port)
    srv = None
    try:
        base = f"http://127.0.0.1:{app_port}"
        ready = wait_for(lambda: _reachable(base), 240)
        check("server came up", ready, f"port {app_port}")
        if not ready:
            return

        # Nothing is listening yet, so the first probe must miss.
        llm = status_llm(base)
        check(
            "starts offline while nothing is listening",
            not llm.get("configured"),
            json.dumps(llm),
        )

        # The model server comes up a moment later, as llama-swap would.
        srv = start_fake_server(llm_port)
        adopted = wait_for(lambda: status_llm(base).get("configured"), 90)
        check("the late server is adopted without a restart", adopted, json.dumps(status_llm(base)))
        if not adopted:
            return

        llm = status_llm(base)
        check("it adopted the served model", llm.get("model") == "qwen3-8b", json.dumps(llm))
        check(
            "and the probed base_url",
            llm.get("base_url", "").endswith(f":{llm_port}/v1"),
            llm.get("base_url", ""),
        )

        saved = json.loads((home / "data" / "config.json").read_text(encoding="utf-8"))
        check("the late adoption was persisted", saved["llm"]["model"] == "qwen3-8b",
              saved["llm"]["model"])
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=20)
        except subprocess.TimeoutExpired:
            proc.kill()
        if srv is not None:
            srv.shutdown()
        shutil.rmtree(home, ignore_errors=True)


def _reachable(base):
    try:
        with urllib.request.urlopen(f"{base}/api/status", timeout=5):
            return True
    except Exception:
        return False


def scenario_configured_model_wins():
    """A model chosen while the retry runs is not overwritten."""
    print("\na model configured during the wait is not overwritten")
    home = Path(tempfile.mkdtemp(prefix="rexeniot-guard-"))
    llm_port = free_port()
    srv = start_fake_server(llm_port)
    old_home = os.environ.get("REXENIOT_HOME")
    try:
        os.environ["REXENIOT_HOME"] = str(home)
        for mod in [m for m in list(sys.modules) if m.startswith("app")]:
            del sys.modules[mod]
        sys.path.insert(0, str(ROOT))
        from app import config
        llm_client = importlib.import_module("app.llm.client")

        llm_client.LOCAL_CANDIDATES = [(f"http://127.0.0.1:{llm_port}/v1", "stand-in")]
        # The panel picked a model while the retry was still waiting.
        config.update({"llm": {"provider": "openai", "base_url": "http://elsewhere:9/v1",
                               "model": "hand-picked"}})

        adopted = asyncio.run(
            llm_client.adopt_first_local_in_background(interval_s=0.3, attempts=3)
        )
        check("the retry declined to adopt", adopted is None, repr(adopted))
        live = config.load(force=True).llm
        check("the configured model is intact", live.model == "hand-picked", live.model)
        check("and its base_url", live.base_url == "http://elsewhere:9/v1", live.base_url)
    finally:
        sys.path.remove(str(ROOT))
        if old_home is None:
            os.environ.pop("REXENIOT_HOME", None)
        else:
            os.environ["REXENIOT_HOME"] = old_home
        for mod in [m for m in list(sys.modules) if m.startswith("app")]:
            del sys.modules[mod]
        srv.shutdown()
        shutil.rmtree(home, ignore_errors=True)


def scenario_bounded():
    """With nothing ever listening the retry gives up instead of spinning."""
    print("\nthe retry is bounded")
    home = Path(tempfile.mkdtemp(prefix="rexeniot-bounded-"))
    llm_port = free_port()
    old_home = os.environ.get("REXENIOT_HOME")
    try:
        os.environ["REXENIOT_HOME"] = str(home)
        for mod in [m for m in list(sys.modules) if m.startswith("app")]:
            del sys.modules[mod]
        sys.path.insert(0, str(ROOT))
        llm_client = importlib.import_module("app.llm.client")

        llm_client.LOCAL_CANDIDATES = [(f"http://127.0.0.1:{llm_port}/v1", "dead")]
        started = time.monotonic()
        adopted = asyncio.run(
            llm_client.adopt_first_local_in_background(interval_s=0.2, attempts=3)
        )
        elapsed = time.monotonic() - started
        check("it returns None when nothing appears", adopted is None, repr(adopted))
        check("and stops after its attempts", elapsed < 10, f"{elapsed:.1f}s")
    finally:
        sys.path.remove(str(ROOT))
        if old_home is None:
            os.environ.pop("REXENIOT_HOME", None)
        else:
            os.environ["REXENIOT_HOME"] = old_home
        for mod in [m for m in list(sys.modules) if m.startswith("app")]:
            del sys.modules[mod]
        shutil.rmtree(home, ignore_errors=True)


def main():
    scenario_late_server()
    scenario_configured_model_wins()
    scenario_bounded()

    print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILURE(S)'}")
    for f in FAILS:
        print("  -", f)
    sys.exit(1 if FAILS else 0)


main()

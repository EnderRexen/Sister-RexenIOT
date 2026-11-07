"""First boot: the app finds a local model server and starts using it.

A fresh install used to come up with no model at all, so the very first thing a
new user saw was a chat that could not answer. It now probes the usual local
ports once and adopts the first server that answers. Two properties matter:

  1. it adopts what is actually being served, and
  2. it never overwrites a model that is already configured, so a restart does
     not undo a choice made in the admin panel.

Runs the real server against a throwaway REXENIOT_HOME, with a stand-in OpenAI
server on one of the probe ports, so nothing on the machine is touched.
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


def start_app(home, port, llm_port):
    """Boot the real server with discovery narrowed to the fake server's port."""
    env = {
        **os.environ,
        "REXENIOT_HOME": str(home),
        "PATH": os.environ.get("PATH", ""),
        # The probe list is a module constant; a sitecustomize hook is the least
        # invasive way to point it at the stand-in without editing the source.
        "REXENIOT_TEST_LLM_PORT": str(llm_port),
    }
    sitecustomize = home / "sitecustomize.py"
    sitecustomize.write_text(
        "import importlib, os\n"
        "m = importlib.import_module('app.llm.client')\n"
        "p = os.environ.get('REXENIOT_TEST_LLM_PORT')\n"
        "if p:\n"
        "    m.LOCAL_CANDIDATES = [(f'http://127.0.0.1:{p}/v1', 'stand-in')]\n",
        encoding="utf-8",
    )
    env["PYTHONPATH"] = f"{home}{os.pathsep}{ROOT}"
    return subprocess.Popen(
        [PY, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=str(ROOT),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )


def wait_ready(base, deadline_s=240):
    deadline = time.time() + deadline_s
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"{base}/api/status", timeout=5):
                return True
        except Exception:
            time.sleep(2)
    return False


def status_llm(base):
    with urllib.request.urlopen(f"{base}/api/status", timeout=10) as r:
        return json.load(r).get("llm", {})


def main():
    home = Path(tempfile.mkdtemp(prefix="rexeniot-autodetect-"))
    llm_port = free_port()
    app_port = free_port()
    srv = start_fake_server(llm_port)
    proc = None
    try:
        # ---- first boot: nothing configured, a server is listening -------------
        proc = start_app(home, app_port, llm_port)
        base = f"http://127.0.0.1:{app_port}"
        ready = wait_ready(base)
        check("fresh server came up", ready, f"port {app_port}")
        if not ready:
            print((proc.stdout.read().decode()[-2000:] if proc.stdout else ""))
            return

        llm = status_llm(base)
        check(
            "first boot adopts the served model",
            llm.get("model") == "qwen3-8b" and llm.get("configured") is True,
            json.dumps(llm),
        )
        check(
            "adopted base_url is the probed server",
            llm.get("base_url", "").endswith(f":{llm_port}/v1"),
            llm.get("base_url", ""),
        )

        saved = json.loads((home / "data" / "config.json").read_text(encoding="utf-8"))
        check("the choice was persisted, not just in memory",
              saved["llm"]["model"] == "qwen3-8b", saved["llm"]["model"])

        # ---- restart: the configured model must survive ------------------------
        proc.terminate()
        proc.wait(timeout=20)
        proc = start_app(home, app_port, llm_port)
        ready = wait_ready(base)
        check("server came back up", ready, f"port {app_port}")
        if ready:
            llm2 = status_llm(base)
            check("restart keeps the configured model",
                  llm2.get("model") == "qwen3-8b", json.dumps(llm2))
    finally:
        if proc is not None:
            try:
                proc.terminate()
                proc.wait(timeout=20)
            except subprocess.TimeoutExpired:
                proc.kill()
        srv.shutdown()
        shutil.rmtree(home, ignore_errors=True)

    print(f"\n{'ALL PASS' if not FAILS else str(len(FAILS)) + ' FAILURE(S)'}")
    for f in FAILS:
        print("  -", f)
    sys.exit(1 if FAILS else 0)


main()

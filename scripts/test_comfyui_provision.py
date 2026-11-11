"""Checks for ComfyUI provisioning and the tool-name migration.

Run: PYTHONPATH=. python scripts/test_comfyui_provision.py
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Redirect the app's state before anything imports app.config, which resolves its
# paths at import time. This test writes a comfyui_url to exercise the verify
# path; without this it would land in the operator's real config.
os.environ["REXENIOT_HOME"] = tempfile.mkdtemp(prefix="rexeniot-comfyui-test-")

failures: list[str] = []


def check(cond: bool, label: str) -> None:
    print(f"  {'ok  ' if cond else 'FAIL'}  {label}")
    if not cond:
        failures.append(label)


print("ComfyUI provisioning")

from app import config, skills  # noqa: E402
from app import comfyui_provision as prov  # noqa: E402

# ---------------------------------------------------------------- model specs
repo, filename, subdir = prov._parse_model_spec("ace-step")
check(repo == "ACE-Step/ACE-Step-v1-3.5B", "a known short name resolves to a repo")
check(subdir == "checkpoints", "a known short name carries its models subdir")

repo, filename, subdir = prov._parse_model_spec("org/repo/file.safetensors=loras")
check(
    (repo, filename, subdir) == ("org/repo", "file.safetensors", "loras"),
    "an explicit spec parses (repo id, filename, subdir)",
)
try:
    prov._parse_model_spec("nonsense")
    check(False, "an unrecognised model spec is rejected")
except ValueError:
    check(True, "an unrecognised model spec is rejected")

# ------------------------------------------------------------------ node name
check(
    prov._node_name("https://github.com/ltdrdata/ComfyUI-Manager.git")
    == "ComfyUI-Manager",
    "a .git suffix is stripped from a node pack name",
)
check(
    prov._node_name("ace-step") == "ComfyUI-ACE-Step",
    "a known node name maps to its directory",
)

# ------------------------------------------------------- missing() is offline
# A spec that is not installed must be reported missing without any network call
# and without needing a real ComfyUI on disk.
with tempfile.TemporaryDirectory() as tmp:
    root = Path(tmp)
    (root / "custom_nodes").mkdir()
    (root / "models").mkdir()

    class T:
        comfyui_root = str(root)
        comfyui_nodes = ["ace-step"]
        comfyui_models = ["ace-step"]

    report = prov.missing(T())
    check(report["root"] == str(root), "an explicit root is used as-is")
    check(report["nodes_missing"] == ["ace-step"], "a missing node pack is reported")
    check(report["models_missing"] == ["ace-step"], "a missing model is reported")

    # Now create them and confirm they are seen as present.
    (root / "custom_nodes" / "ComfyUI-ACE-Step").mkdir()
    (root / "models" / "checkpoints").mkdir(parents=True)
    (root / "models" / "checkpoints" / "ace_step_v1_3.5b.safetensors").write_bytes(b"x")
    report = prov.missing(T())
    check(report["nodes_missing"] == [], "an installed node pack is not reported missing")
    check(report["models_missing"] == [], "a present model file is not reported missing")

    # An unknown root is reported as unknown, not as "everything missing".
    class Bad:
        comfyui_root = str(root / "does-not-exist")
        comfyui_nodes = ["ace-step"]
        comfyui_models = []

    report = prov.missing(Bad())
    check(report["root"] is None, "an unusable root resolves to None")
    check("detail" in report, "an unusable root explains itself")

# ------------------------------------------------------ provision is opt-in
class NoneWanted:
    comfyui_root = ""
    comfyui_nodes = []
    comfyui_models = []


report = prov.provision(NoneWanted())
check(report.get("detail") == "nothing configured", "provisioning does nothing when unconfigured")
check("errors" not in report or not report["errors"], "doing nothing is not an error")

# --------------------------------------------- workflow classes are compared
graph = {
    "1": {"class_type": "CheckpointLoaderSimple"},
    "2": {"class_type": "SaveAudio"},
    "3": {"class_type": "ACEStepSampler"},
    "4": {"class_type": "Note", "inputs": {}},
}
check(
    prov.required_classes(graph)
    == {"CheckpointLoaderSimple", "SaveAudio", "ACEStepSampler", "Note"},
    "every class_type in a graph is collected",
)
check(prov.required_classes({}) == set(), "an empty graph needs nothing")

# ------------------------------------------------------- renamed tool migrate
old = skills.Skill(
    id="images",
    name="Images",
    tools=["generate_image", "comfyui_status"],
)
changed = skills._migrate_tool_names([old])
check(changed, "the migration reports that it changed something")
check(
    old.tools == ["generate_media", "comfyui_status"],
    "generate_image is renamed in a persisted skill record",
)
check(
    not skills._migrate_tool_names([old]),
    "the migration is idempotent",
)
check(
    skills._migrate_tool_names([skills.Skill(id="x", name="X", tools=["set_timer"])]) is False,
    "unrelated tool names are left alone",
)

# ------------------------------------------- the renamed tool is registered
from app.tools import registry  # noqa: E402

check(registry.get("generate_media") is not None, "generate_media is registered")
check(registry.get("generate_image") is None, "the old generate_image name is gone")

# ------------------------------------------------- outputs beyond images
from app.tools import comfyui as comfy_tool  # noqa: E402

outputs = {
    "9": {"audio": [{"filename": "song.flac", "subfolder": "", "type": "output"}]},
    "8": {"images": [{"filename": "pic.png", "subfolder": "sub", "type": "output"}]},
    "7": {"gifs": [{"filename": "clip.mp4", "subfolder": "", "type": "output"}]},
    "6": {"text": ["a plain string, not a file"]},
    "5": {"images": [{"not_a_filename": True}]},
}
found = comfy_tool._collect_outputs(outputs)
names = sorted(f["filename"] for f in found)
check(
    names == ["clip.mp4", "pic.png", "song.flac"],
    "audio, image and video outputs are all collected",
)
check(all(f.get("kind") for f in found), "each output records which key it came from")
check(
    not any(f["filename"] == "a plain string, not a file" for f in found),
    "a non-file output value is ignored",
)
check(len(found) == 3, "an entry with no filename is skipped")

# ------------------------------------ generate_media refuses what it cannot do
#
# A missing music workflow must fail with an explanation, not silently submit the
# built-in image graph and return a picture of the song request. Checked inside
# the configured block below, so the URL check passes and the workflow check is
# the one under test.

# ------------------------------------------------- verify against a fake server
#
# This exercises the real code path end to end: a live HTTP server answering
# /object_info, a workflow file on disk naming classes the server does not have.
import http.server  # noqa: E402
import threading  # noqa: E402
import asyncio  # noqa: E402

_SERVED = {
    "object_info": {
        "CheckpointLoaderSimple": {},
        "KSampler": {},
        "CLIPTextEncode": {},
        "VAEDecode": {},
        "EmptyLatentImage": {},
        "SaveImage": {},
        # SaveAudio and ACEStepSampler are deliberately absent.
    },
    "system_stats": {"system": {}},
}


class _Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802
        key = self.path.strip("/").split("?")[0]
        if key in _SERVED:
            body = json.dumps(_SERVED[key]).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *args):  # silence
        pass


import os  # noqa: E402
import tempfile  # noqa: E402

server = http.server.HTTPServer(("127.0.0.1", 0), _Handler)
port = server.server_address[1]
threading.Thread(target=server.serve_forever, daemon=True).start()

# config.load() reads the file named at import time, so point that at a real
# throwaway directory rather than reassigning DATA_DIR (which the resolver
# ignores). Everything written below stays inside the isolated home.
_home = Path(os.environ["REXENIOT_HOME"])
(_home / "data").mkdir(parents=True, exist_ok=True)

with tempfile.TemporaryDirectory():
    # A workflow naming a node class the fake server does not offer.
    workflows = _home / "data" / "workflows"
    workflows.mkdir(parents=True, exist_ok=True)
    (workflows / "audio.json").write_text(
        json.dumps(
            {
                "1": {"class_type": "CheckpointLoaderSimple"},
                "2": {"class_type": "ACEStepSampler"},
                "3": {"class_type": "SaveAudio"},
            }
        )
    )

    cfg = config.load()
    cfg.tools.comfyui_url = f"http://127.0.0.1:{port}"
    config.save(cfg)

    report = asyncio.run(prov.verify())
    check(report["reachable"], "a reachable ComfyUI is reported as reachable")
    check("audio" in report["workflows"], "the audio workflow is inspected")
    check(
        report["workflows"]["audio"]["missing"] == ["ACEStepSampler", "SaveAudio"],
        "the missing node classes are named exactly",
    )
    check(
        not report["workflows"]["audio"]["ready"],
        "a workflow needing absent nodes is not ready",
    )
    check(
        "builtin:image" in report["workflows"]
        and report["workflows"]["builtin:image"]["ready"],
        "the built-in image graph is checked and found ready",
    )
    check(
        report["missing_classes"] == ["ACEStepSampler", "SaveAudio"],
        "the combined missing list is deduplicated",
    )

    # With ComfyUI now configured, a request for a kind with no workflow file must
    # refuse with an actionable message rather than submit the image graph.
    # 'video' is used because this test has already written audio.json above.
    async def _kind_without_workflow() -> str:
        try:
            await comfy_tool.generate_media("a clip of rain", kind="video")
        except comfy_tool.ToolError as exc:
            return str(exc)
        return ""

    message = asyncio.run(_kind_without_workflow())
    check("workflow" in message.lower(), "asking for video without a workflow explains why")
    check("video.json" in message, "the refusal names the file the operator must add")

    # A non-image workflow that exists but has no text node must also refuse: the
    # fallback builds an image graph, so substituting it would return a picture
    # of the song request instead of failing.
    (workflows / "3d.json").write_text(
        json.dumps({"1": {"class_type": "LoadImage"}, "2": {"class_type": "SaveGLB"}})
    )

    async def _textless_workflow() -> str:
        try:
            await comfy_tool.generate_media("a wooden chair", kind="3d")
        except comfy_tool.ToolError as exc:
            return str(exc)
        return ""

    message = asyncio.run(_textless_workflow())
    check("text node" in message, "a non-image workflow with no text node is refused")
    check("kind='image'" in message, "the refusal suggests the working alternative")

server.shutdown()

print()
if failures:
    print(f"FAILED: {len(failures)} check(s)")
    for f in failures:
        print(f"  - {f}")
    sys.exit(1)
print("all checks passed")

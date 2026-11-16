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
check(repo == "Comfy-Org/ACE-Step-1.5", "a known short name resolves to a repo")
check(
    filename == "checkpoints/ace_step_1.5_turbo_aio.safetensors",
    "ace-step is the all-in-one checkpoint, not a directory of components",
)
check(
    subdir == "",
    "ace-step carries no extra models subdir: the filename already has its path",
)
check(
    (Path("models") / subdir / filename).as_posix()
    == "models/checkpoints/ace_step_1.5_turbo_aio.safetensors",
    "the file lands in models/checkpoints/, where ComfyUI looks for a checkpoint",
)

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
    "ace-step" not in prov.KNOWN_NODES,
    "no ACE-Step node pack is known: 1.5 runs on ComfyUI core and needs none",
)
check(
    "manager" in prov.KNOWN_NODES,
    "the manager pack is still known, for operators who want it",
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
        comfyui_nodes = ["manager"]
        comfyui_models = ["ace-step"]

    report = prov.missing(T())
    check(report["root"] == str(root), "an explicit root is used as-is")
    check(report["nodes_missing"] == ["manager"], "a missing node pack is reported")
    check(report["models_missing"] == ["ace-step"], "a missing model is reported")

    # A half-finished download must still read as missing: this is several GB,
    # so stopping part way is the expected failure. For a single-file model that
    # means an absent file, however much of the tree around it exists.
    ckpt_dir = root / "models" / "checkpoints"
    ckpt_dir.mkdir(parents=True)
    report = prov.missing(T())
    check(
        report["models_missing"] == ["ace-step"],
        "a checkpoint that is not on disk is reported missing",
    )

    # A partial file must not count as present, which is why the check is for a
    # file rather than for a directory that merely exists.
    (ckpt_dir / "ace_step_1.5_turbo_aio.safetensors.partial").write_bytes(b"x")
    report = prov.missing(T())
    check(
        report["models_missing"] == ["ace-step"],
        "a partially downloaded checkpoint is still reported missing",
    )

    # Now create the real file and confirm it is seen as present.
    (root / "custom_nodes" / "ComfyUI-Manager").mkdir()
    (ckpt_dir / "ace_step_1.5_turbo_aio.safetensors").write_bytes(b"x")
    report = prov.missing(T())
    check(report["nodes_missing"] == [], "an installed node pack is not reported missing")
    check(report["models_missing"] == [], "a downloaded checkpoint is not reported missing")

    # An unknown root is reported as unknown, not as "everything missing".
    class Bad:
        comfyui_root = str(root / "does-not-exist")
        comfyui_nodes = ["manager"]
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

# ------------------------------------------------- the built-in music graph
music = comfy_tool._music_workflow("dreamy synth pop", "harsh noise", "", 90.0, 8)
classes = {n["class_type"] for n in music.values()}
check(
    classes
    == {
        "CheckpointLoaderSimple",
        "TextEncodeAceStepAudio1.5",
        "EmptyAceStep1.5LatentAudio",
        "ModelSamplingAuraFlow",
        "ConditioningZeroOut",
        "KSampler",
        "VAEDecodeAudio",
        "SaveAudioMP3",
    },
    "the music graph is the ACE-Step 1.5 chain on ComfyUI core",
)
check(
    all(k.isdigit() for k in music),
    "every node is keyed by a numeric id, as the API format requires",
)
check(
    not any("API" in n["class_type"] or "Client" in n["class_type"] for n in music.values()),
    "the music graph names no hosted-service node",
)
check(
    music["1"]["inputs"]["ckpt_name"] == comfy_tool.ACE_STEP_15_CHECKPOINT,
    "the loader names the ACE-Step 1.5 all-in-one checkpoint",
)
check(
    music["2"]["inputs"]["tags"] == "dreamy synth pop",
    "the style prompt reaches the tags field",
)
check(
    music["2"]["inputs"]["lyrics"] == comfy_tool._INSTRUMENTAL_LYRICS,
    "no lyrics produces an instrumental, not an empty string",
)
check(
    music["2"]["inputs"]["duration"] == 90.0 and music["3"]["inputs"]["seconds"] == 90.0,
    "the duration reaches both the text encoder and the empty latent",
)
check(
    music["6"]["inputs"]["steps"] == 8
    and music["6"]["inputs"]["cfg"] == 1.0
    and music["6"]["inputs"]["sampler_name"] == "euler"
    and music["6"]["inputs"]["scheduler"] == "simple",
    "the sampler uses the step-distilled turbo settings",
)
check(
    music["6"]["inputs"]["positive"] == ["2", 0]
    and music["6"]["inputs"]["negative"] == ["5", 0]
    and music["5"]["inputs"]["conditioning"] == ["2", 0],
    "the sampler reads the encoder, and the negative is zeroed-out conditioning",
)
check(
    music["4"]["inputs"]["model"] == ["1", 0] and music["6"]["inputs"]["model"] == ["4", 0],
    "the model is routed through ModelSamplingAuraFlow before sampling",
)
check(
    music["7"]["inputs"]["vae"] == ["1", 2],
    "the audio decoder takes the VAE from the checkpoint loader",
)
check(
    music["8"]["inputs"]["audio"] == ["7", 0],
    "the audio saver reads the decoder's audio output",
)
check(
    isinstance(music["6"]["inputs"]["seed"], int)
    and music["2"]["inputs"]["seed"] == music["6"]["inputs"]["seed"],
    "a seed is generated so repeat runs differ, and both nodes share it",
)

# ------------------------------------- the prompt/lyrics split in ACE-Step 1.5
# 1.5 keeps both pieces of text in ONE node, as two fields. Writing the style
# into `lyrics` (or the words into `tags`) would look fine and sing the wrong
# thing, so each target is checked by field and not just by node.
prompt_target, lyrics_target = comfy_tool._music_text_nodes(music)
check(
    prompt_target == ("2", "tags"),
    f"the style prompt is aimed at the tags field, got {prompt_target}",
)
check(
    lyrics_target == ("2", "lyrics"),
    f"the lyrics are aimed at the lyrics field, got {lyrics_target}",
)
check(
    prompt_target[0] == lyrics_target[0],
    "both targets are the same node, as 1.5 folds them into one",
)

user_music = comfy_tool._music_workflow("x", "y", "words here", 60.0, 8)
comfy_tool._inject_prompt(user_music, "new style", "noise", "audio", "new words")
check(
    user_music["2"]["inputs"]["tags"] == "new style",
    "the prompt is injected into the tags field",
)
check(
    user_music["2"]["inputs"]["lyrics"] == "new words",
    "the lyrics are injected into the lyrics field",
)

# A v1-shaped music workflow must still inject correctly: the two fields live in
# two separate nodes there, so the field-level logic has to cope with both shapes.
v1_music = {
    "2": {"class_type": "MultiLinePromptACES", "inputs": {"multi_line_prompt": ""}},
    "3": {"class_type": "MultiLineLyrics", "inputs": {"multi_line_prompt": ""}},
}
comfy_tool._inject_prompt(v1_music, "v1 style", "noise", "audio", "v1 words")
check(
    v1_music["2"]["inputs"]["multi_line_prompt"] == "v1 style"
    and v1_music["3"]["inputs"]["multi_line_prompt"] == "v1 words",
    "a v1 two-node music workflow still injects into the right node each",
)

# An image workflow is untouched by the music path.
img = {
    "1": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
    "2": {"class_type": "CLIPTextEncode", "inputs": {"text": ""}},
    "3": {
        "class_type": "KSampler",
        "inputs": {"positive": ["1", 0], "negative": ["2", 0]},
    },
}
comfy_tool._inject_prompt(img, "a cat", "blurry", "image")
check(img["1"]["inputs"]["text"] == "a cat", "an image workflow still gets its positive text")
check(img["2"]["inputs"]["text"] == "blurry", "an image workflow still gets its negative text")

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
        # The ACE-Step 1.5 nodes ship with ComfyUI itself, so a real server
        # always offers them. Listing them here is what lets the built-in music
        # graph be reported ready with no node pack installed -- the point of
        # moving off the v1 pack.
        "TextEncodeAceStepAudio1.5": {},
        "EmptyAceStep1.5LatentAudio": {},
        "ModelSamplingAuraFlow": {},
        "ConditioningZeroOut": {},
        "VAEDecodeAudio": {},
        "SaveAudioMP3": {},
        # SaveAudio and ACEStepSampler are deliberately absent: they stand in
        # for a workflow that names node classes the server does not have.
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
        "builtin:music" in report["workflows"],
        "the built-in music graph is checked too",
    )
    check(
        report["workflows"]["builtin:music"]["missing"] == [],
        "the built-in music graph needs no node pack: its classes are ComfyUI core",
    )
    check(
        report["workflows"]["builtin:music"]["ready"],
        "the built-in music graph is ready, so a song works with nothing installed",
    )
    check(
        report["missing_classes"] == sorted(set(report["missing_classes"])),
        "the combined missing list is deduplicated",
    )
    check(
        {"ACEStepSampler", "SaveAudio"} <= set(report["missing_classes"]),
        "the combined list still names the workflow's absent classes",
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

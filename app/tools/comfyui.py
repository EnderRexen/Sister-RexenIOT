"""ComfyUI integration.

ComfyUI exposes a prompt-queue API rather than a single generation call, so this
module:

1. builds a minimal graph from the configured workflow template, or from a
   built-in text-to-image graph when none is supplied;
2. POSTs it to ``/prompt``;
3. polls ``/history/{prompt_id}`` until the job finishes;
4. fetches the produced image through ``/view``.

The graph is built server-side so the model only has to supply a prompt string.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

from .. import config
from .base import ToolError, registry

log = logging.getLogger("rexeniot.tools.comfyui")

#: Output keys ComfyUI nodes report generated files under. ``images`` is the
#: core SaveImage node; the rest come from the audio, video and 3D nodes and
#: packs (SaveAudio, VHS_VideoCombine, ...).
_FILE_KEYS = ("images", "audio", "gifs", "video", "videos", "3d", "files")


def _base_url() -> str:
    url = config.load().tools.resolved_comfyui_url()
    if not url:
        raise ToolError(
            "ComfyUI is not configured. Add its address in the admin panel under Model Backend."
        )
    return url.rstrip("/")


def _default_workflow(prompt: str, negative: str, width: int, height: int, steps: int) -> dict[str, Any]:
    """A conventional SD1.5/SDXL checkpoint graph.

    Node ids are fixed strings so the caller can override individual nodes.
    This is the shape ComfyUI's default text-to-image workflow exports to.
    """
    seed = int(time.time() * 1000) % (2**31)
    return {
        "4": {
            "class_type": "CheckpointLoaderSimple",
            "inputs": {"ckpt_name": "model.safetensors"},
        },
        "5": {
            "class_type": "EmptyLatentImage",
            "inputs": {"width": width, "height": height, "batch_size": 1},
        },
        "6": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": prompt, "clip": ["4", 1]},
        },
        "7": {
            "class_type": "CLIPTextEncode",
            "inputs": {"text": negative, "clip": ["4", 1]},
        },
        "3": {
            "class_type": "KSampler",
            "inputs": {
                "seed": seed,
                "steps": steps,
                "cfg": 7.0,
                "sampler_name": "euler",
                "scheduler": "normal",
                "denoise": 1.0,
                "model": ["4", 0],
                "positive": ["6", 0],
                "negative": ["7", 0],
                "latent_image": ["5", 0],
            },
        },
        "8": {
            "class_type": "VAEDecode",
            "inputs": {"samples": ["3", 0], "vae": ["4", 2]},
        },
        "9": {
            "class_type": "SaveImage",
            "inputs": {"filename_prefix": "rexeniot", "images": ["8", 0]},
        },
    }


async def _submit(base: str, graph: dict[str, Any]) -> str:
    client_id = uuid.uuid4().hex
    async with httpx.AsyncClient(timeout=30.0) as client:
        resp = await client.post(
            f"{base}/prompt", json={"prompt": graph, "client_id": client_id}
        )
    if resp.status_code >= 400:
        raise ToolError(f"ComfyUI rejected the workflow (HTTP {resp.status_code}): {resp.text[:300]}")
    payload = resp.json()
    prompt_id = payload.get("prompt_id")
    if not prompt_id:
        raise ToolError(f"ComfyUI did not return a prompt id: {str(payload)[:200]}")
    return prompt_id


async def _wait(base: str, prompt_id: str, timeout: float) -> dict[str, Any]:
    deadline = time.time() + timeout
    async with httpx.AsyncClient(timeout=15.0) as client:
        while time.time() < deadline:
            resp = await client.get(f"{base}/history/{prompt_id}")
            if resp.status_code == 200:
                history = resp.json()
                entry = history.get(prompt_id)
                if entry:
                    status = (entry.get("status") or {}).get("status_str")
                    if status == "error":
                        messages = (entry.get("status") or {}).get("messages", [])
                        raise ToolError(f"ComfyUI workflow failed: {str(messages)[:300]}")
                    outputs = entry.get("outputs") or {}
                    if outputs:
                        return outputs
            await asyncio.sleep(1.5)
    raise ToolError(f"ComfyUI did not finish within {int(timeout)} seconds.")


def _collect_outputs(outputs: dict[str, Any]) -> list[dict[str, str]]:
    """Every generated file ComfyUI reported, whatever kind it is.

    A workflow can end in SaveImage, SaveAudio, VHS_VideoCombine or a 3D export,
    and each reports its result under a different key (``images``, ``audio``,
    ``gifs``, ``video``, ``3d``). Reading only ``images`` meant a music or video
    workflow finished successfully and then handed back an empty list, which
    reads as "nothing was generated". The keys are collected generically so a
    node pack that invents a new one still works.
    """
    found: list[dict[str, str]] = []
    for node_id, node in (outputs or {}).items():
        if not isinstance(node, dict):
            continue
        for key, value in node.items():
            if key not in _FILE_KEYS or not isinstance(value, list):
                continue
            for item in value:
                if not isinstance(item, dict):
                    continue
                filename = item.get("filename") or item.get("file") or ""
                if not filename:
                    continue
                found.append(
                    {
                        "node": node_id,
                        "kind": key,
                        "filename": filename,
                        "subfolder": item.get("subfolder", ""),
                        "type": item.get("type", "output"),
                    }
                )
    return found


@registry.add(
    "generate_media",
    "Generate an image, or audio, video or 3D output, through ComfyUI. Use for "
    "'draw me', 'make a picture of', or a song when a music workflow is installed. "
    "Images use a built-in graph; other kinds need a workflow file. Results are "
    "shown in the chat.",
    {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "What to generate."},
            "kind": {
                "type": "string",
                "enum": ["image", "audio", "video", "3d"],
                "default": "image",
                "description": "What to produce. 'audio' covers music and speech.",
            },
            "negative_prompt": {"type": "string", "description": "What to avoid.", "default": "blurry, low quality, watermark, text"},
            "width": {"type": "integer", "default": 1024},
            "height": {"type": "integer", "default": 1024},
            "steps": {"type": "integer", "default": 25},
        },
        "required": ["prompt"],
    },
    requires="ComfyUI base URL",
    tags=["generation"],
)
async def generate_media(
    prompt: str,
    kind: str = "image",
    negative_prompt: str = "blurry, low quality, watermark, text",
    width: int = 1024,
    height: int = 1024,
    steps: int = 25,
) -> Any:
    base = _base_url()
    if not prompt.strip():
        raise ToolError("A prompt is required to generate something.")

    cfg = config.load()
    # A workflow file lets the operator pin their own checkpoint and nodes. One
    # named after the requested kind is preferred, so a music or video graph can
    # be dropped in as data/workflows/audio.json without disturbing the default
    # image workflow.
    graph = None
    for name in (kind, cfg.tools.comfyui_workflow):
        path = config.DATA_DIR / "workflows" / f"{name}.json"
        if not path.exists():
            continue
        try:
            graph = _inject_prompt(
                json.loads(path.read_text(encoding="utf-8")), prompt, negative_prompt, kind
            )
        except ToolError:
            # Already a clear explanation of what the workflow is missing.
            raise
        except Exception as exc:
            raise ToolError(f"Could not read workflow '{path.name}': {exc}") from exc
        break

    if graph is None:
        if kind != "image":
            # Saying so beats submitting an image graph and returning a picture
            # of the request, or nothing at all.
            raise ToolError(
                f"Generating {kind} needs a ComfyUI workflow file. Save one as "
                f"data/workflows/{kind}.json -- an ACE-Step or Stable Audio graph "
                "for music -- and ask again."
            )
        width = max(256, min(int(width), 2048))
        height = max(256, min(int(height), 2048))
        steps = max(4, min(int(steps), 80))
        graph = _default_workflow(prompt, negative_prompt, width, height, steps)

    prompt_id = await _submit(base, graph)
    outputs = await _wait(base, prompt_id, timeout=cfg.tools.request_timeout_s * 15)
    files = _collect_outputs(outputs)
    shared = _share_from_comfyui(base, files)
    first = files[0] if files else None
    return {
        "ok": True,
        "prompt_id": prompt_id,
        "kind": kind,
        "files": files,
        "shared": shared,
        "view_url": (
            f"{base}/view?"
            + urlencode(
                {
                    "filename": first["filename"],
                    "subfolder": first["subfolder"],
                    "type": first["type"],
                }
            )
            if first
            else None
        ),
        "note": (
            f"The {kind} is displayed in the chat."
            if shared
            else f"Nothing was returned; the {kind} workflow has no output node."
            if not files
            else f"The {kind} is stored by ComfyUI; open the view_url to see it."
        ),
    }


def _share_from_comfyui(base: str, files: list[dict[str, str]]) -> list[str]:
    """Pull generated files off ComfyUI and register them for the chat.

    ComfyUI only hands back filenames, so the bytes are fetched from its /view
    endpoint and registered as artifacts. A failure here must not fail the
    generation itself -- the caller still has the filenames to fall back on.
    """
    from .. import uploads as uploads_mod

    shared: list[str] = []
    for item in files[:4]:
        # Filenames can contain spaces or '&', which would truncate the query.
        query = urlencode(
            {
                "filename": item["filename"],
                "subfolder": item.get("subfolder", ""),
                "type": item.get("type", "output"),
            }
        )
        url = f"{base}/view?{query}"
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.get(url)
                resp.raise_for_status()
            staged = config.DATA_DIR / "cache" / f"comfyui-{uuid.uuid4().hex[:8]}-{Path(item['filename']).name}"
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_bytes(resp.content)
            registered = uploads_mod.register_artifact(staged, name=Path(item["filename"]).name)
            shared.append(registered.id)
            staged.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001 - generation already succeeded
            log.warning("could not share generated file %s: %s", item.get("filename"), exc)
    return shared


def _text_nodes(graph: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Map node id -> node for every CLIPTextEncode node in the graph."""
    return {
        node_id: node
        for node_id, node in graph.items()
        if isinstance(node, dict) and node.get("class_type") == "CLIPTextEncode"
    }


def _wired_text_nodes(graph: dict[str, Any]) -> tuple[str | None, str | None]:
    """Find the text nodes the sampler actually reads as positive and negative.

    ComfyUI wires a KSampler to two CLIPTextEncode nodes through
    ``inputs.positive`` / ``inputs.negative``, each a ``[node_id, slot]`` pair.
    Following those links is the only reliable way to tell the slots apart: a
    graph exported before the user typed anything has both text fields empty, so
    ordering alone cannot distinguish them.
    """
    positive = negative = None
    for node in graph.values():
        if not isinstance(node, dict) or node.get("class_type") != "KSampler":
            continue
        inputs = node.get("inputs") or {}
        for key, target in (("positive", "positive"), ("negative", "negative")):
            link = inputs.get(key)
            if not isinstance(link, (list, tuple)) or not link:
                continue
            node_id = str(link[0])
            if node_id not in _text_nodes(graph):
                continue
            if target == "positive" and positive is None:
                positive = node_id
            elif target == "negative" and negative is None:
                negative = node_id
    return positive, negative


def _inject_prompt(
    graph: dict[str, Any], prompt: str, negative: str, kind: str = "image"
) -> dict[str, Any]:
    """Replace the positive and negative text nodes in a user workflow.

    A workflow with no text node at all can only be the built-in graph's job: the
    fallback builds an image graph, so it is only safe for ``kind == "image"``.
    Substituting it for a music workflow would submit an image graph and hand
    back a picture of the song request, so for anything else the caller's own
    missing-workflow error is the honest answer.
    """
    nodes = _text_nodes(graph)
    if not nodes:
        if kind != "image":
            raise ToolError(
                f"The {kind} workflow has no text node to put the prompt in. Add a "
                "CLIPTextEncode (or equivalent) node, or generate with kind='image'."
            )
        return _default_workflow(prompt, negative, 1024, 1024, 25)

    positive_id, negative_id = _wired_text_nodes(graph)

    # Fall back to declaration order only when the sampler does not say which
    # node is which. JSON object order is insertion order, which is how ComfyUI
    # exports a workflow, so the first node is the conventional positive slot.
    if positive_id is None:
        remaining = [nid for nid in nodes if nid != negative_id]
        positive_id = remaining[0] if remaining else None
    if negative_id is None:
        remaining = [nid for nid in nodes if nid != positive_id]
        negative_id = remaining[0] if remaining else None

    if positive_id is not None:
        nodes[positive_id].setdefault("inputs", {})["text"] = prompt
    if negative_id is not None and negative_id != positive_id:
        nodes[negative_id].setdefault("inputs", {})["text"] = negative
    return graph


@registry.add(
    "comfyui_status",
    "Check whether the ComfyUI backend is reachable and what it reports.",
    {"type": "object", "properties": {}},
    tags=["generation"],
)
async def comfyui_status() -> Any:
    try:
        base = _base_url()
    except ToolError as exc:
        return {"ok": False, "detail": str(exc)}
    try:
        async with httpx.AsyncClient(timeout=8.0) as client:
            resp = await client.get(f"{base}/system_stats")
        if resp.status_code >= 400:
            return {"ok": False, "detail": f"HTTP {resp.status_code}"}
        return {"ok": True, "detail": "reachable", "stats": resp.json()}
    except Exception as exc:
        return {"ok": False, "detail": f"{type(exc).__name__}: {exc}"}

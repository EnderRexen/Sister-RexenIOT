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
from typing import Any

import httpx

from .. import config
from .base import ToolError, registry

log = logging.getLogger("rexeniot.tools.comfyui")


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


def _collect_images(outputs: dict[str, Any]) -> list[dict[str, str]]:
    images: list[dict[str, str]] = []
    for node_id, node in outputs.items():
        for img in node.get("images", []) or []:
            images.append(
                {
                    "node": node_id,
                    "filename": img.get("filename", ""),
                    "subfolder": img.get("subfolder", ""),
                    "type": img.get("type", "output"),
                }
            )
    return images


@registry.add(
    "generate_image",
    "Generate an image from a text description using ComfyUI. Returns the saved "
    "filenames. Use for 'draw me', 'make a picture of' and similar requests.",
    {
        "type": "object",
        "properties": {
            "prompt": {"type": "string", "description": "What the image should show."},
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
async def generate_image(
    prompt: str,
    negative_prompt: str = "blurry, low quality, watermark, text",
    width: int = 1024,
    height: int = 1024,
    steps: int = 25,
) -> Any:
    base = _base_url()
    if not prompt.strip():
        raise ToolError("A prompt is required to generate an image.")

    cfg = config.load()
    # A workflow file lets the operator pin their own checkpoint and nodes.
    workflow_path = config.DATA_DIR / "workflows" / f"{cfg.tools.comfyui_workflow}.json"
    if workflow_path.exists():
        try:
            graph = json.loads(workflow_path.read_text(encoding="utf-8"))
            graph = _inject_prompt(graph, prompt, negative_prompt)
        except Exception as exc:
            raise ToolError(f"Could not read workflow '{cfg.tools.comfyui_workflow}': {exc}") from exc
    else:
        width = max(256, min(int(width), 2048))
        height = max(256, min(int(height), 2048))
        steps = max(4, min(int(steps), 80))
        graph = _default_workflow(prompt, negative_prompt, width, height, steps)

    prompt_id = await _submit(base, graph)
    outputs = await _wait(base, prompt_id, timeout=cfg.tools.request_timeout_s * 15)
    images = _collect_images(outputs)
    shared = _share_from_comfyui(base, images)
    return {
        "ok": True,
        "prompt_id": prompt_id,
        "images": images,
        "shared": shared,
        "view_url": (
            f"{base}/view?filename={images[0]['filename']}&subfolder={images[0]['subfolder']}"
            f"&type={images[0]['type']}" if images else None
        ),
        "note": (
            "The image is displayed in the chat." if shared
            else "Images are stored by ComfyUI; open the view_url to see them."
        ),
    }


def _share_from_comfyui(base: str, images: list[dict[str, str]]) -> list[str]:
    """Pull generated images off ComfyUI and register them for the chat.

    ComfyUI only hands back filenames, so the bytes are fetched from its /view
    endpoint and registered as artifacts. A failure here must not fail the
    generation itself -- the caller still has the filenames to fall back on.
    """
    from .. import uploads as uploads_mod

    shared: list[str] = []
    for img in images[:4]:
        url = f"{base}/view?filename={img['filename']}&subfolder={img.get('subfolder', '')}&type={img.get('type', 'output')}"
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.get(url)
                resp.raise_for_status()
            staged = config.DATA_DIR / "cache" / f"comfyui-{uuid.uuid4().hex[:8]}-{img['filename']}"
            staged.parent.mkdir(parents=True, exist_ok=True)
            staged.write_bytes(resp.content)
            item = uploads_mod.register_artifact(staged, name=img["filename"])
            shared.append(item.id)
            staged.unlink(missing_ok=True)
        except Exception as exc:  # noqa: BLE001 - generation already succeeded
            log.warning("could not share generated image %s: %s", img.get("filename"), exc)
    return shared


def _inject_prompt(graph: dict[str, Any], prompt: str, negative: str) -> dict[str, Any]:
    """Replace the first positive and negative text nodes in a user workflow."""
    positive_done = negative_done = False
    for node in graph.values():
        if not isinstance(node, dict):
            continue
        if node.get("class_type") != "CLIPTextEncode":
            continue
        inputs = node.setdefault("inputs", {})
        text = str(inputs.get("text", ""))
        # Heuristic: an empty or obviously placeholder text is the positive slot.
        if not positive_done and text.strip() not in ("", "positive"):
            inputs["text"] = prompt
            positive_done = True
        elif not negative_done:
            inputs["text"] = negative
            negative_done = True
    if not positive_done:
        # No usable node found: fall back to the built-in graph.
        return _default_workflow(prompt, negative, 1024, 1024, 25)
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

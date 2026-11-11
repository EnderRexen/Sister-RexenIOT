"""Provision ComfyUI with the node packs and model files a workflow needs.

A workflow file on its own is not enough to generate anything. The graph names
node classes (``SaveAudio``, ``ACEStepSampler``, ...) that only exist once the
node pack is installed, and it loads checkpoints by filename that have to be on
disk. Dropping in a music workflow without either produced a ComfyUI error the
operator had to go and read in ComfyUI's own log.

This module closes that gap. At startup it can, when asked:

* clone each configured node pack into ``custom_nodes/`` and install its
  ``requirements.txt``;
* download each configured model file into the right ``models/`` subdirectory;
* report what is still missing, so the chat can say so plainly.

It is deliberately quiet and never fatal. ComfyUI is an optional integration
that usually runs as its own service, so a failure here must not stop the app
from booting -- the report is logged and the admin panel can show it.

Everything is opt-in. ``comfyui_nodes`` and ``comfyui_models`` are empty by
default because provisioning fetches gigabytes from the network; that is a
decision for the operator, not something to do on first boot.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import httpx

from . import config

log = logging.getLogger("rexeniot.comfyui")

#: Model files worth knowing by a short name, so the config stays readable.
#: Each entry is (huggingface repo, filename, ComfyUI models/ subdirectory).
#: A filename of "" means the entry is a *directory* of weights, fetched whole
#: into that subdirectory -- which is how ACE-Step ships.
KNOWN_MODELS: dict[str, tuple[str, str, str]] = {
    # ACE-Step is the music model with the widest ComfyUI node support, and it
    # is the one that runs fully offline. The repo is a directory tree of four
    # component folders (the DCAE, the vocoder, the transformer and the text
    # encoder), not a single checkpoint: the node pack loads each folder
    # separately, so downloading one .safetensors file would leave it broken.
    "ace-step": ("ACE-Step/ACE-Step-v1-3.5B", "", "TTS/ACE-Step-v1-3.5B"),
    "stable-audio-open": (
        "stabilityai/stable-audio-open-1.0",
        "model.safetensors",
        "checkpoints",
    ),
}

#: Node packs by short name, as git URLs.
#: ``ace-step`` is billwuhao's pack, which runs the model locally. The similarly
#: named ``ace-step/ACE-Step-ComfyUI`` is NOT interchangeable: it is an API
#: proxy that posts to a hosted service, so it generates nothing offline.
KNOWN_NODES: dict[str, str] = {
    "manager": "https://github.com/ltdrdata/ComfyUI-Manager",
    "ace-step": "https://github.com/billwuhao/ComfyUI_ACE-Step",
}

#: Component folders a directory-shaped model repo must contain, by repo id.
#: Used by the offline "is it already downloaded?" check. Kept as a table
#: rather than a live Hugging Face listing so a startup check never needs the
#: network, and so a half-finished download is caught instead of assumed good.
DIR_MODEL_COMPONENTS: dict[str, tuple[str, ...]] = {
    "ACE-Step/ACE-Step-v1-3.5B": (
        "music_dcae_f8c8",
        "music_vocoder",
        "ace_step_transformer",
        "umt5-base",
    ),
}


def _cfg() -> Any:
    return config.load().tools


def resolve_root(cfg: Any | None = None) -> Path | None:
    """ComfyUI's own directory, the one holding ``custom_nodes`` and ``models``.

    Taken from config when set, since ComfyUI may run on another machine and
    share the path. Otherwise it is read from the running ComfyUI process: the
    app often runs as the same user, and its command line carries the directory
    it was started from. A ComfyUI started from a portable or container path is
    simply not found, which is why the report says "unknown" rather than
    guessing.
    """
    tools = cfg or _cfg()
    explicit = (getattr(tools, "comfyui_root", "") or "").strip()
    if explicit:
        path = Path(explicit).expanduser()
        return path if path.is_dir() else None

    for pid in _candidate_pids():
        cwd = Path(f"/proc/{pid}/cwd")
        try:
            root = cwd.resolve()
        except Exception:
            continue
        if (root / "custom_nodes").is_dir() and (root / "models").is_dir():
            return root
    return None


def _candidate_pids() -> list[int]:
    """PIDs whose command line looks like a ComfyUI server."""
    pids: list[int] = []
    proc = Path("/proc")
    if not proc.is_dir():
        return pids
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            cmdline = (entry / "cmdline").read_bytes().replace(b"\x00", b" ").decode(
                "utf-8", "replace"
            )
        except Exception:
            continue
        if "main.py" in cmdline and "comfy" in cmdline.lower():
            pids.append(int(entry.name))
    return pids


def missing(cfg: Any | None = None) -> dict[str, Any]:
    """What is configured but not yet on disk. Cheap; touches no network."""
    tools = cfg or _cfg()
    wanted_nodes = list(getattr(tools, "comfyui_nodes", []) or [])
    wanted_models = list(getattr(tools, "comfyui_models", []) or [])
    report: dict[str, Any] = {
        "root": None,
        "nodes_missing": [],
        "models_missing": [],
    }
    if not wanted_nodes and not wanted_models:
        return report

    root = resolve_root(tools)
    report["root"] = str(root) if root else None
    if root is None:
        # Without the root there is nothing to check against, and the honest
        # answer is that it is unknown, not that everything is missing.
        report["detail"] = (
            "ComfyUI's directory could not be found. Set tools.comfyui_root to "
            "the directory holding its custom_nodes and models folders."
        )
        report["nodes_missing"] = wanted_nodes
        report["models_missing"] = wanted_models
        return report

    for spec in wanted_nodes:
        name = _node_name(spec)
        if not (root / "custom_nodes" / name).is_dir():
            report["nodes_missing"].append(spec)
    for spec in wanted_models:
        try:
            repo, filename, subdir = _parse_model_spec(spec)
        except ValueError:
            report["models_missing"].append(spec)
            continue
        if filename:
            if not (root / "models" / subdir / filename).is_file():
                report["models_missing"].append(spec)
        elif not _dir_model_present(root, repo, subdir):
            report["models_missing"].append(spec)
    return report


def _dir_model_present(root: Path, repo: str, subdir: str) -> bool:
    """Whether a directory-shaped model repo has been fetched already.

    Deliberately offline: this runs at startup and in the admin panel, so it
    must not depend on the network. For a model whose layout is known, every
    component folder has to be present and non-empty, which is what catches the
    usual failure -- a multi-gigabyte download that stopped half way. An unknown
    repo can only be checked for "something is there".
    """
    base = root / "models" / subdir
    if not base.is_dir():
        return False
    components = DIR_MODEL_COMPONENTS.get(repo)
    if not components:
        return any(base.iterdir())
    return all((base / name).is_dir() and any((base / name).iterdir()) for name in components)


def _node_name(spec: str) -> str:
    """Directory name a node pack clones into."""
    known = KNOWN_NODES.get(spec)
    url = known or spec
    name = url.rstrip("/").split("/")[-1]
    return name[:-4] if name.endswith(".git") else name


def _parse_model_spec(spec: str) -> tuple[str, str, str]:
    """Resolve a model spec to (hf repo, filename, models subdirectory).

    Accepts a short name from :data:`KNOWN_MODELS`, or an explicit
    ``repo/filename=subdir`` so an operator can add anything else without a
    code change. An empty filename means the whole repo is a directory tree.
    """
    if spec in KNOWN_MODELS:
        return KNOWN_MODELS[spec]
    if "=" in spec and "/" in spec:
        left, subdir = spec.rsplit("=", 1)
        repo, _, filename = left.rpartition("/")
        if repo and filename:
            return repo, filename, subdir.strip("/")
    raise ValueError(
        f"Unrecognised model '{spec}'. Use a known name "
        f"({', '.join(sorted(KNOWN_MODELS))}) or 'repo/filename=subdir'."
    )


def _run(args: list[str], cwd: Path | None = None, timeout: float = 900.0) -> str:
    proc = subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "").strip()[:400])
    return proc.stdout


def _install_node(spec: str, root: Path) -> str:
    """Clone a node pack and install its requirements."""
    known = KNOWN_NODES.get(spec)
    url = known or spec
    if not url.startswith(("http://", "https://", "git@")):
        raise RuntimeError(f"'{spec}' is not a known pack or a git URL.")
    dest = root / "custom_nodes" / _node_name(spec)
    if dest.is_dir():
        return "already present"

    if shutil.which("git") is None:
        raise RuntimeError("git is not installed, so node packs cannot be fetched.")
    _run(["git", "clone", "--depth=1", url, str(dest)])

    requirements = dest / "requirements.txt"
    if requirements.is_file():
        # Best effort: a pack that needs no extra packages, or whose packages
        # are already installed, must not be reported as a failure. The
        # interpreter running the app is the right one to install into -- a
        # hardcoded .venv path does not exist on a Nix or system install.
        try:
            _run(
                [sys.executable, "-m", "pip", "install", "-q", "-r", str(requirements)],
                timeout=1200.0,
            )
        except Exception as exc:
            log.warning("node pack %s installed but its requirements failed: %s", spec, exc)
    return "installed"


def _install_model(spec: str, root: Path) -> str:
    """Download one model into ComfyUI's models tree.

    A spec with a filename fetches that one file. A spec without one fetches the
    whole repo, for models like ACE-Step that ship as a tree of component
    folders rather than a single checkpoint.
    """
    repo, filename, subdir = _parse_model_spec(spec)
    dest = root / "models" / subdir
    dest.mkdir(parents=True, exist_ok=True)

    if filename:
        if (dest / filename).is_file():
            return "already present"
        from huggingface_hub import hf_hub_download

        hf_hub_download(
            repo_id=repo,
            filename=filename,
            local_dir=str(dest),
            cache_dir=str(config.MODELS_DIR),
        )
        return "downloaded"

    if _dir_model_present(root, repo, subdir):
        return "already present"
    from huggingface_hub import snapshot_download

    snapshot_download(
        repo_id=repo,
        local_dir=str(dest),
        cache_dir=str(config.MODELS_DIR),
    )
    return "downloaded"


def provision(cfg: Any | None = None) -> dict[str, Any]:
    """Install whatever is configured and missing. Blocking; run in a thread.

    Never raises. Every failure is collected into the returned report so the
    caller can log it and the admin panel can show it.
    """
    tools = cfg or _cfg()
    wanted_nodes = list(getattr(tools, "comfyui_nodes", []) or [])
    wanted_models = list(getattr(tools, "comfyui_models", []) or [])
    report: dict[str, Any] = {
        "root": None,
        "nodes": {},
        "models": {},
        "errors": [],
    }
    if not wanted_nodes and not wanted_models:
        report["detail"] = "nothing configured"
        return report

    root = resolve_root(tools)
    report["root"] = str(root) if root else None
    if root is None:
        report["errors"].append(
            "ComfyUI's directory could not be found; set tools.comfyui_root."
        )
        return report

    for spec in wanted_nodes:
        try:
            report["nodes"][spec] = _install_node(spec, root)
        except Exception as exc:
            report["nodes"][spec] = "failed"
            report["errors"].append(f"node '{spec}': {exc}")
    for spec in wanted_models:
        try:
            report["models"][spec] = _install_model(spec, root)
        except Exception as exc:
            report["models"][spec] = "failed"
            report["errors"].append(f"model '{spec}': {exc}")

    installed = [k for k, v in report["nodes"].items() if v == "installed"]
    if installed:
        # ComfyUI reads custom_nodes at startup, so a new pack needs a restart
        # before its nodes exist. Saying so avoids a confusing "node not found".
        report["restart_required"] = True
    return report


def manager_present(root: Path | None = None) -> bool:
    """Whether ComfyUI-Manager is installed. Only used for the report."""
    root = root or resolve_root()
    if root is None:
        return False
    return (root / "custom_nodes" / "ComfyUI-Manager").is_dir()


async def status(base_url: str) -> dict[str, Any]:
    """ComfyUI reachability plus what provisioning still needs. Non-blocking."""
    tools = _cfg()
    out: dict[str, Any] = {"url": base_url or None, "reachable": False}
    if not base_url:
        out["detail"] = "ComfyUI is not configured."
        return out
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{base_url.rstrip('/')}/system_stats")
        out["reachable"] = resp.status_code == 200
    except Exception as exc:
        out["detail"] = f"{type(exc).__name__}: {exc}"

    root = await asyncio.to_thread(resolve_root, tools)
    out["root"] = str(root) if root else None
    out["manager"] = await asyncio.to_thread(manager_present, root)
    out["missing"] = await asyncio.to_thread(missing, tools)
    return out


# ------------------------------------------------------------------- verifying
#
# A node pack directory being present does not mean its nodes load: a pack whose
# requirements failed to install, or that targets a different ComfyUI version,
# is on disk and still provides nothing. The only dependable check is to ask the
# running server which classes it actually has and compare that against the
# classes the installed workflows name.


def _workflow_names() -> list[str]:
    """Workflow names in data/workflows, without the extension."""
    folder = config.DATA_DIR / "workflows"
    if not folder.is_dir():
        return []
    return sorted(p.stem for p in folder.glob("*.json"))


def _read_graph(name: str) -> dict[str, Any] | None:
    path = config.DATA_DIR / "workflows" / f"{name}.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001 - a bad file is reported, not raised
        log.warning("workflow '%s' is not readable JSON: %s", name, exc)
        return None


def required_classes(graph: dict[str, Any]) -> set[str]:
    """Every node class a workflow graph names."""
    classes: set[str] = set()
    for node in (graph or {}).values():
        if isinstance(node, dict):
            class_type = node.get("class_type")
            if isinstance(class_type, str) and class_type:
                classes.add(class_type)
    return classes


async def _object_info() -> dict[str, Any] | None:
    """Every node class the running ComfyUI offers, or None if unreachable."""
    base = (config.load().tools.resolved_comfyui_url() or "").rstrip("/")
    if not base:
        return None
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.get(f"{base}/object_info")
            resp.raise_for_status()
            return resp.json()
    except Exception as exc:  # noqa: BLE001 - reachability is reported, not raised
        log.debug("ComfyUI object_info unavailable: %s", exc)
        return None


def _builtin_classes() -> dict[str, set[str]]:
    """Classes each built-in graph needs, keyed by a label for the report.

    Checked as well as the workflow files, because these two paths work with no
    workflow installed at all. Image is the baseline -- if its core nodes are
    absent, ComfyUI is not usable for anything. Music needs the ACE-Step pack,
    which is a separate install, so it is reported separately rather than
    counted against ComfyUI as a whole.
    """
    out: dict[str, set[str]] = {}
    try:
        from .tools.comfyui import _default_workflow, _music_workflow

        out["builtin:image"] = required_classes(_default_workflow("x", "y", 512, 512, 4))
        out["builtin:music"] = required_classes(
            _music_workflow("x", "y", "", 60.0, 20)
        )
    except Exception:  # noqa: BLE001 - only a diagnostic aid
        return out
    return out


async def verify() -> dict[str, Any]:
    """What the installed workflows need versus what ComfyUI actually offers.

    Every branch is a normal state -- unconfigured, unreachable, or missing a
    pack the operator has not installed -- so this reports rather than raises.
    """
    report: dict[str, Any] = {
        "configured": bool(config.load().tools.resolved_comfyui_url()),
        "reachable": False,
        "manager": False,
        "root": None,
        "workflows": {},
        "missing_classes": [],
    }
    if not report["configured"]:
        return report

    info = await _object_info()
    if info is None:
        return report
    report["reachable"] = True

    root = await asyncio.to_thread(resolve_root)
    report["root"] = str(root) if root else None
    report["manager"] = await asyncio.to_thread(manager_present, root)

    available = set(info)
    missing: set[str] = set()

    for name in _workflow_names():
        graph = _read_graph(name)
        if graph is None:
            continue
        needed = required_classes(graph)
        absent = sorted(needed - available)
        report["workflows"][name] = {
            "classes": len(needed),
            "missing": absent,
            "ready": not absent,
        }
        missing.update(absent)

    for label, needed in _builtin_classes().items():
        if not needed:
            continue
        absent = sorted(needed - available)
        report["workflows"][label] = {
            "classes": len(needed),
            "missing": absent,
            "ready": not absent,
        }
        missing.update(absent)

    report["missing_classes"] = sorted(missing)
    return report


async def verify_and_provision() -> dict[str, Any]:
    """The startup hook. Verifies, then installs what is configured.

    ``provision`` shells out to git and pip, so it runs in a worker thread: this
    is called from the event loop, and blocking it would stall every request
    until a multi-gigabyte download finished. Never raises, so a ComfyUI problem
    cannot stop the app from booting.
    """
    out: dict[str, Any] = {"verify": {}, "provision": {}}
    try:
        out["verify"] = await verify()
    except Exception as exc:  # noqa: BLE001
        log.warning("ComfyUI verification failed: %s", exc)
        out["verify"] = {"error": f"{type(exc).__name__}: {exc}"}
    try:
        out["provision"] = await asyncio.to_thread(provision)
    except Exception as exc:  # noqa: BLE001
        log.warning("ComfyUI provisioning failed: %s", exc)
        out["provision"] = {"error": f"{type(exc).__name__}: {exc}"}
    return out

#!/usr/bin/env python3
"""The startup pipeline shared by all four launchers.

Each launcher does the same thing and differs only in how it obtains a Python:
``start.cmd`` / ``start.sh`` use the system one, the portable pair fetch a
standalone build. Resolving the interpreter has to stay in the launcher, because
a machine with no Python cannot run this file at all. Everything after that --
the virtual environment, the dependency hash, the GPU note, the config check, the
speech warm-up and the server itself -- is identical everywhere, so it lives here.

That matters because the logic used to be copied into each launcher and drifted.
The GPU advice reached ``start.cmd`` and ``start.sh`` but never the portable pair,
so a portable install on a machine with a discrete GPU was told nothing about how
to use it. The advice is now read from ``app.hardware``, which the admin panel
also calls, so there is one copy of the rule instead of three.

Phase A runs on the launcher's interpreter and may use only the standard library,
because no packages are installed yet. It prepares the venv, then re-executes
this file with the venv's interpreter for phase B, where ``app`` is importable.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import socket
import subprocess
import sys
from pathlib import Path

#: The floor every launcher enforces. `match` and modern typing are the reason.
MIN_PYTHON = (3, 10)

BANNER = """\
 ==========================================================
   S I S T E R   R E X E N I O T{portable}
   keeper of the house
 =========================================================="""


def _say(message: str = "") -> None:
    print(message, flush=True)


def _fail(message: str, *hints: str) -> int:
    _say()
    _say(f"  ERROR: {message}")
    for hint in hints:
        _say(f"  {hint}")
    _say()
    return 1


def _venv_python(venv: Path) -> Path:
    """The interpreter inside *venv*, which differs by platform."""
    if os.name == "nt":
        return venv / "Scripts" / "python.exe"
    return venv / "bin" / "python"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def _reinstall_needed(req: Path, mark: Path) -> bool:
    """Whether the requirements changed since the last successful install.

    Hashing the file rather than tracking it by mtime keeps a `git checkout` or a
    restored backup from triggering a reinstall of an identical set.
    """
    if not mark.is_file():
        return True
    try:
        return mark.read_text(encoding="utf-8").strip() != _sha256(req)
    except OSError:
        return True


# ------------------------------------------------------------------ phase A


def _phase_setup(args: argparse.Namespace) -> int:
    venv = Path(args.venv).resolve()
    req = Path(args.req).resolve()
    mark = Path(args.mark).resolve()

    _say()
    _say(BANNER.format(portable="   (portable)" if args.portable else ""))
    if args.portable:
        _say(f"   folder: {args.folder or venv.parent}")
    _say()

    # ------------------------------------------------------------ python ---
    # The launcher already announced and performed this step -- it had to, since
    # a machine with no Python cannot run this file. It is re-checked rather than
    # trusted because a launcher could hand off to a too-old interpreter.
    _say("[1/6] Checking for Python...")
    if sys.version_info[:2] < MIN_PYTHON:
        return _fail(
            f"Python {MIN_PYTHON[0]}.{MIN_PYTHON[1]} or newer is required, "
            f"this is {sys.version.split()[0]}.",
            f"Found at: {sys.executable}",
            "Download a current version from https://www.python.org/downloads/",
        )
    _say(f"      found: Python {sys.version.split()[0]}")

    # -------------------------------------------------------------- venv ---
    _say("[2/6] Preparing the virtual environment...")
    py = _venv_python(venv)
    if py.is_file():
        _say(f"      reusing existing {venv.name}")
    else:
        _say(f"      creating {venv.name} ...")
        result = subprocess.run([sys.executable, "-m", "venv", str(venv)])
        if result.returncode != 0 or not py.is_file():
            # Standalone python-build-standalone archives ship without
            # ensurepip on some platforms, so a plain `-m venv` can fail or
            # produce an environment with no pip. Retry without pip and add it
            # below instead.
            subprocess.run(
                [sys.executable, "-m", "venv", "--without-pip", str(venv)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        if not py.is_file():
            return _fail(
                "Could not create the virtual environment.",
                "On Debian/Ubuntu run: sudo apt install python3-venv",
            )

    if subprocess.run(
        [str(py), "-m", "pip", "--version"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode != 0:
        if args.offline:
            return _fail(
                "pip is missing from the environment and REXENIOT_OFFLINE=1.",
                "Run once with a network connection.",
            )
        _say("      bootstrapping pip ...")
        if subprocess.run(
            [str(py), "-m", "ensurepip", "--upgrade"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        ).returncode != 0:
            return _fail(
                "Could not install pip into the environment.",
                f"Run: {py} -m ensurepip --upgrade",
            )

    # ------------------------------------------------------ dependencies ---
    _say("[3/6] Checking dependencies...")
    if not _reinstall_needed(req, mark):
        _say("      dependencies unchanged, skipping install")
    else:
        if args.offline:
            return _fail(
                "packages need installing but REXENIOT_OFFLINE=1.",
                "Run once with a network connection.",
            )
        _say("      installing packages (this is slow the first time) ...")
        if subprocess.run([str(py), "-m", "pip", "install", "--upgrade", "pip", "--quiet"]).returncode != 0:
            return _fail("Could not upgrade pip.", f"Run: {py} -m pip install --upgrade pip")
        if subprocess.run([str(py), "-m", "pip", "install", "-r", str(req), "--quiet"]).returncode != 0:
            return _fail(
                "Dependency installation failed.",
                "Run this to see the full output:",
                f"    {py} -m pip install -r {req}",
            )
        # kittenml declares torch as a hard dependency, but the ONNX speech
        # backend never imports it. Installing without dependencies keeps the
        # footprint near 200 MB instead of 2.5 GB; the ONNX packages the engine
        # does need are listed explicitly in the requirements file.
        _say("      installing the speech package (ONNX only, no PyTorch) ...")
        if subprocess.run([str(py), "-m", "pip", "install", "--no-deps", "kittenml==0.9.4", "--quiet"]).returncode != 0:
            return _fail(
                "Could not install the speech package.",
                f"Run: {py} -m pip install --no-deps kittenml==0.9.4",
            )
        mark.parent.mkdir(parents=True, exist_ok=True)
        mark.write_text(_sha256(req), encoding="utf-8")
        _say("      dependencies ready")

    # Re-enter on the venv interpreter, where `app` is importable. Phase A must
    # not import it: pydantic is not installed until the step above.
    argv = [str(py), str(Path(__file__).resolve()), *args.forwarded, "--phase", "run"]
    os.execv(str(py), argv)
    return 1  # unreachable; execv only returns on failure


# ------------------------------------------------------------------ phase B


def _gpu_note(py: Path) -> None:
    """Tell the reader how to put a present-but-unused GPU to work.

    The rule lives in ``app.hardware`` because the admin panel prints the same
    advice; asking it here keeps the launcher from being a second copy that can
    fall out of date. Only NVIDIA is probed -- a full ``hardware.scan()`` takes
    about five seconds, which is too slow to run on every start.
    """
    from app import hardware

    profile = hardware.light_profile()
    if not profile.get("gpu"):
        return

    _say("      detected GPU:")
    for gpu in profile["gpu"]:
        vram = f"{gpu.get('vram_gb')} GB" if gpu.get("vram_gb") else "unknown VRAM"
        _say(f"        {gpu.get('name', 'GPU')} ({vram})")

    def render(advice: dict[str, str]) -> None:
        """Print each advice command as a runnable line.

        The advice comes from app.hardware, where a follow-up step is written
        after a ``#`` on the same string. That reads well in the admin panel's
        HTML but not here: ``#`` is not a comment to cmd.exe, so printing it
        inline would hand the reader a command that cannot run. Split it out.
        """
        for raw_line in (advice.get("command") or "").splitlines():
            for part in raw_line.split("#"):
                command = part.strip().removeprefix("and:").strip()
                if not command:
                    continue
                _say(f"                {py} -m {command}"
                     if command.startswith("pip ")
                     else f"                {py} -c \"{command}\"")

    torch_advice = hardware.torch_gpu_advice(profile)
    directml = hardware.directml_advice(profile)

    # A CUDA build that cannot reach the card is a driver or wheel-index problem,
    # not a missing install, so the two cases get different advice.
    if not torch_advice and not directml:
        return

    _say()
    _say("      NOTE: a GPU is present but the speech runtime cannot use it yet.")
    if directml:
        _say("            DirectML reaches any Windows GPU with no CUDA toolkit:")
        render(directml)
    if torch_advice:
        _say("            For the 1.7B tier on GPU, torch must come from the CUDA index")
        _say("            rather than PyPI. Run both commands, in this order:")
        render(torch_advice)
    _say()


def _phase_run(args: argparse.Namespace) -> int:
    py = Path(sys.executable)

    # Running `python scripts/bootstrap.py` puts *scripts/* on sys.path, not the
    # app folder, so `import app` would fail. The launchers always run from the
    # app folder, and this file always lives one level below it.
    root = Path(__file__).resolve().parent.parent
    if str(root) not in sys.path:
        sys.path.insert(0, str(root))

    _say("[4/6] Checking configuration...")
    for folder in ("data", "data/models", "data/cache", "data/voices"):
        (Path(args.folder or ".") / folder).mkdir(parents=True, exist_ok=True)

    try:
        import app.config as app_config

        app_config.load()
    except Exception as exc:  # noqa: BLE001 - any failure here is fatal and printed
        return _fail(
            "The configuration could not be loaded.",
            f"{type(exc).__name__}: {exc}",
            f"Run: {py} -c \"import app.config\"",
        )
    _say("      configuration ready")

    _gpu_note(py)

    # Only needed when the Alexa skill is driven by alexacli. The admin panel
    # reports the same thing, so a miss here is a hint rather than a failure.
    try:
        if app_config.load().tools.resolved_alexa_provider() == "alexacli":
            import shutil

            if not shutil.which("alexacli"):
                _say()
                _say("      NOTE: the Alexa skill uses the alexacli provider, but the")
                _say("            'alexacli' command is not on PATH. Install it with:")
                _say("                brew install buddyh/tap/alexacli")
                _say("            or set its full path in the admin panel.")
                _say()
    except Exception:
        pass

    _say("[5/6] Preparing speech engine...")
    if args.offline:
        _say("      offline mode: using the voice model already in data/models")
    else:
        _say("      the first run downloads the voice model (~80 MB, one time only)")
    _say("      loading it and speaking a test line (this can take a minute) ...")
    # Loading is not "ready": the weights can load while speech is still broken,
    # so this speaks the active persona's greeting. A failure is reported but not
    # fatal, because the app starts either way and the panel can repair it later.
    if subprocess.run([str(py), "-m", "app.main", "--prepare-tts"]).returncode != 0:
        _say("      NOTE: speech is not ready; the app will still start.")

    _say("[6/6] Starting Sister RexenIOT...")
    if _port_in_use(args.bind, args.port):
        _say()
        _say(f"  NOTE: port {args.port} is already in use, so RexenIOT may fail to start.")
        _say("  Close the other process or change the port in the admin panel.")
        _say()

    _say()
    _say("  ----------------------------------------------------------")
    _say(f"   Open:        http://{args.host}:{args.port}/")
    _say(f"   Admin panel: http://{args.host}:{args.port}/admin")
    _say()
    if args.portable:
        _say("   Everything is stored inside this folder.")
    else:
        _say("   Keep this window open while RexenIOT is running.")
    _say("   Press Ctrl+C to stop.")
    _say("  ----------------------------------------------------------")
    _say()

    result = subprocess.run(
        [str(py), "-m", "app.main", "--host", args.bind, "--port", str(args.port)]
    )
    _say()
    _say("  Sister RexenIOT has stopped.")
    return result.returncode


def _port_in_use(host: str, port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


# -------------------------------------------------------------------- entry


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Shared startup pipeline for the Sister RexenIOT launchers."
    )
    parser.add_argument("--venv", required=True, help="virtual environment folder")
    parser.add_argument("--req", required=True, help="requirements file to hash")
    parser.add_argument("--mark", required=True, help="file holding the installed hash")
    parser.add_argument("--port", type=int, default=8060)
    parser.add_argument("--host", default="localhost", help="host the browser opens")
    parser.add_argument("--bind", default="127.0.0.1", help="address the server binds")
    parser.add_argument("--folder", default="", help="the app folder, for display")
    parser.add_argument("--portable", action="store_true")
    parser.add_argument(
        "--offline",
        action="store_true",
        default=bool(os.environ.get("REXENIOT_OFFLINE", "").strip()),
        help="never touch the network (default: from REXENIOT_OFFLINE)",
    )
    parser.add_argument("--phase", choices=("setup", "run"), default="setup")
    args = parser.parse_args()

    # Re-echoed verbatim on the phase-B re-exec, so the launcher only has to pass
    # its own flags once.
    args.forwarded = [
        "--venv", args.venv,
        "--req", args.req,
        "--mark", args.mark,
        "--port", str(args.port),
        "--host", args.host,
        "--bind", args.bind,
        "--folder", args.folder,
        *( ["--portable"] if args.portable else [] ),
        *( ["--offline"] if args.offline else [] ),
    ]

    if args.phase == "run":
        return _phase_run(args)
    return _phase_setup(args)


if __name__ == "__main__":
    sys.exit(main())

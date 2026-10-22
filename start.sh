#!/usr/bin/env bash
# ============================================================================
#  Sister RexenIOT - Linux / macOS launcher
#
#  Everything lives inside this folder: the Python runtime, the packages, the
#  voice models, your settings and your chat history. Copy the folder to a USB
#  stick and it runs on any Linux or macOS machine with a network connection.
#
#  The first run uses a Python 3.10+ already on the machine if there is one,
#  and otherwise downloads a standalone runtime (~30 MB), then installs the
#  packages (~300 MB). After that the folder is self-contained: set
#  REXENIOT_OFFLINE=1 to run with no network at all, provided the voice model
#  was already fetched.
#
#  Only the part that obtains a Python is here, because a machine with no
#  Python cannot run bootstrap.py at all. The startup pipeline itself is
#  scripts/bootstrap.py, shared with the other launcher, so the two cannot
#  drift apart.
# ============================================================================

set -u

cd "$(dirname "$0")" || exit 1
APP_DIR="$(pwd)"

RUNTIME="$APP_DIR/runtime"
PYVER="3.12.7"
PYROOT="$RUNTIME/python"
PY="$PYROOT/bin/python3"
VENV="$RUNTIME/venv"
MARK="$RUNTIME/.deps-hash"
PORT="${REXENIOT_PORT:-8060}"
OFFLINE="${REXENIOT_OFFLINE:-0}"

# Keep every byte of state inside the folder, including Hugging Face caches,
# so nothing leaks into the host machine's home directory.
export REXENIOT_HOME="$APP_DIR"
export REXENIOT_PORTABLE=1
export HF_HOME="$APP_DIR/data/models/hf"
export HF_HUB_CACHE="$APP_DIR/data/models/hf/hub"
export XDG_CACHE_HOME="$APP_DIR/data/cache"
export PIP_CACHE_DIR="$APP_DIR/runtime/pip-cache"
export PYTHONDONTWRITEBYTECODE=1

mkdir -p "$RUNTIME" "$APP_DIR/data/models" "$APP_DIR/data/cache"

# ---------------------------------------------------------------- platform ---
case "$(uname -s)" in
  Linux)  OS="linux" ;;
  Darwin) OS="macos" ;;
  *) echo "  ERROR: unsupported platform $(uname -s)."; exit 1 ;;
esac
case "$(uname -m)" in
  x86_64|amd64) ARCH="x86_64" ;;
  arm64|aarch64) ARCH="aarch64" ;;
  *) echo "  ERROR: unsupported architecture $(uname -m)."; exit 1 ;;
esac

if [ "$OS" = "macos" ] && [ "$ARCH" = "aarch64" ]; then
  echo "  NOTE: on Apple Silicon the bundled runtime is x86_64 emulated."
  echo "        A native python3 already on this Mac will be used if present."
fi

# ------------------------------------------------------------ find python ---
# Prefer an already-installed interpreter when it is new enough, because it is
# faster and avoids the download entirely. Fall back to the bundled runtime.
find_system_python() {
  for candidate in python3.13 python3.12 python3.11 python3.10 python3 python; do
    if command -v "$candidate" >/dev/null 2>&1; then
      if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3,10) else 1)' >/dev/null 2>&1; then
        command -v "$candidate"
        return 0
      fi
    fi
  done
  return 1
}

USE_PY=""

echo "Locating a Python runtime..."

if [ -x "$PY" ]; then
  USE_PY="$PY"
  echo "      using the bundled runtime: $("$PY" --version 2>&1)"
else
  SYS_PY="$(find_system_python || true)"
  if [ -n "$SYS_PY" ] && [ "$OFFLINE" != "1" ]; then
    # A system interpreter keeps the folder small, but the venv must still live
    # here so the install stays self-contained.
    echo "      found system python: $SYS_PY ($("$SYS_PY" --version 2>&1))"
    USE_PY="$SYS_PY"
  else
    if [ "$OFFLINE" = "1" ]; then
      echo "      ERROR: no suitable Python found and REXENIOT_OFFLINE=1."
      echo "      Run once with a network connection, or install Python 3.10+."
      exit 1
    fi
    echo "      no suitable Python on this machine; fetching a portable one"
    echo "      (~30 MB, one time only) ..."

    case "$OS-$ARCH" in
      linux-x86_64)  TRIPLE="x86_64-unknown-linux-gnu" ;;
      linux-aarch64) TRIPLE="aarch64-unknown-linux-gnu" ;;
      macos-x86_64)  TRIPLE="x86_64-apple-darwin" ;;
      macos-aarch64) TRIPLE="aarch64-apple-darwin" ;;
    esac

    TARBALL="cpython-$PYVER+20241016-$TRIPLE-install_only.tar.gz"
    URL="https://github.com/astral-sh/python-build-standalone/releases/download/20241016/$TARBALL"
    PYTAR="$RUNTIME/$TARBALL"

    if command -v curl >/dev/null 2>&1; then
      curl -fL --retry 3 -o "$PYTAR" "$URL" || { echo "  ERROR: download failed."; exit 1; }
    elif command -v wget >/dev/null 2>&1; then
      wget -O "$PYTAR" "$URL" || { echo "  ERROR: download failed."; exit 1; }
    else
      echo "  ERROR: neither curl nor wget is available to fetch Python."
      exit 1
    fi

    tar -xzf "$PYTAR" -C "$RUNTIME" || { echo "  ERROR: could not unpack Python."; exit 1; }
    rm -f "$PYTAR"
    # The archive unpacks to runtime/python.
    if [ ! -x "$PY" ]; then
      echo "  ERROR: unexpected archive layout; $PY is missing."
      exit 1
    fi
    USE_PY="$PY"
    echo "      installed: $("$PY" --version 2>&1)"
  fi
fi

# -------------------------------------------------------------------- hand ---
# Steps 2 to 6 are bootstrap.py's: the environment, the packages, the GPU note,
# the speech warm-up and the server. It re-prints the banner itself.
# REXENIOT_OFFLINE is already exported above, so it needs no flag here.
exec "$USE_PY" "$APP_DIR/scripts/bootstrap.py" \
  --venv "$VENV" \
  --req "$APP_DIR/requirements.txt" \
  --mark "$MARK" \
  --port "$PORT" \
  --host localhost \
  --bind 127.0.0.1 \
  --folder "$APP_DIR"

#!/usr/bin/env bash
# ============================================================================
#  Sister RexenIOT - portable launcher (Linux / macOS)
#
#  Everything lives inside this folder: the Python runtime, the packages, the
#  voice models, your settings and your chat history. Copy the folder to a USB
#  stick and it runs on any Linux or macOS machine with a network connection.
#
#  The first run downloads a standalone Python (~30 MB) and the packages
#  (~300 MB). After that the folder is self-contained: set
#  REXENIOT_OFFLINE=1 to run with no network at all, provided the voice model
#  was already fetched.
# ============================================================================

set -u

cd "$(dirname "$0")" || exit 1
APP_DIR="$(pwd)"

RUNTIME="$APP_DIR/runtime"
PYVER="3.12.7"
PYROOT="$RUNTIME/python"
PY="$PYROOT/bin/python3"
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

echo
echo " =========================================================="
echo "   S I S T E R   R E X E N I O T   (portable)"
echo "   keeper of the house"
echo " =========================================================="
echo "   folder: $APP_DIR"
echo

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

echo "[1/5] Locating a Python runtime..."

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

# ------------------------------------------------------------------ venv ----
echo "[2/5] Preparing the environment..."

VENV="$RUNTIME/venv"
if [ ! -x "$VENV/bin/python" ]; then
  "$USE_PY" -m venv "$VENV" 2>/dev/null || {
    # Standalone builds ship without ensurepip on some platforms.
    "$USE_PY" -m venv --without-pip "$VENV" 2>/dev/null || true
  }
fi

if [ ! -x "$VENV/bin/python" ]; then
  echo "  ERROR: could not create a virtual environment."
  echo "  On Debian/Ubuntu: sudo apt install python3-venv"
  exit 1
fi
VPY="$VENV/bin/python"

# ------------------------------------------------------------------ pip -----
if ! "$VPY" -m pip --version >/dev/null 2>&1; then
  if [ "$OFFLINE" = "1" ]; then
    echo "  ERROR: pip is missing from the environment and REXENIOT_OFFLINE=1."
    exit 1
  fi
  echo "      bootstrapping pip ..."
  "$VPY" -m ensurepip --upgrade >/dev/null 2>&1 || \
    "$VPY" "$RUNTIME/get-pip.py" >/dev/null 2>&1 || {
      curl -fsSL https://bootstrap.pypa.io/get-pip.py -o "$RUNTIME/get-pip.py" && \
      "$VPY" "$RUNTIME/get-pip.py" >/dev/null 2>&1
    }
fi

# ------------------------------------------------------------ dependencies --
echo "[3/5] Checking packages..."

REQ="$APP_DIR/requirements.txt"
if command -v sha256sum >/dev/null 2>&1; then
  HASH=$(sha256sum "$REQ" | awk '{print $1}')
elif command -v shasum >/dev/null 2>&1; then
  HASH=$(shasum -a 256 "$REQ" | awk '{print $1}')
else
  HASH=$(wc -c < "$REQ")
fi

if [ -f "$MARK" ] && [ "$(cat "$MARK")" = "$HASH" ]; then
  echo "      packages unchanged, skipping install"
else
  if [ "$OFFLINE" = "1" ]; then
    echo "      ERROR: packages need installing but REXENIOT_OFFLINE=1."
    exit 1
  fi
  echo "      installing packages (~300 MB, one time only) ..."
  "$VPY" -m pip install --upgrade pip --quiet
  if ! "$VPY" -m pip install -r "$REQ" --quiet; then
    echo "  ERROR: package installation failed. Run without --quiet to see why:"
    echo "      $VPY -m pip install -r $REQ"
    exit 1
  fi
  # kittenml pulls in PyTorch, which the ONNX speech backend never uses.
  # Skipping its dependencies keeps the folder near 300 MB, not 2.5 GB.
  echo "      installing the speech package (ONNX only, no PyTorch) ..."
  if ! "$VPY" -m pip install --no-deps kittenml==0.9.4 --quiet; then
    echo "  ERROR: could not install the speech package."
    exit 1
  fi
  printf '%s' "$HASH" > "$MARK"
  echo "      packages ready"
fi

# --------------------------------------------------------------- verify -----
echo "[4/5] Verifying the application..."

if ! "$VPY" -c "import app.main; print('      import ok')" 2>&1 | tail -5; then
  echo
  echo "  ERROR: the application failed to import."
  exit 1
fi

# ------------------------------------------------------------ voice model ---
if [ "$OFFLINE" != "1" ]; then
  echo "      the first spoken reply downloads the voice model (~80 MB)"
else
  echo "      offline mode: using the voice model already in data/models"
fi

# ------------------------------------------------------------ voice model ----
echo "[5/5] Preparing the voice..."
echo "      the first run downloads the voice model (~80 MB, one time only)"
echo "      loading it and speaking a test line (this can take a minute) ..."
if ! "$VPY" -m app.main --prepare-tts; then
  echo "      NOTE: speech is not ready; the app will still start."
fi

# ------------------------------------------------------------------ run -----
echo "Starting Sister RexenIOT..."
echo
echo "  ----------------------------------------------------------"
echo "   Open:        http://localhost:$PORT/"
echo "   Admin panel: http://localhost:$PORT/admin"
echo
echo "   Everything is stored inside this folder."
echo "   Press Ctrl+C to stop."
echo "  ----------------------------------------------------------"
echo

exec "$VPY" -m app.main --host 127.0.0.1 --port "$PORT"

#!/usr/bin/env bash
# ============================================================================
#  Sister RexenIOT - Linux / macOS launcher
#
#  The counterpart to start.cmd: checks prerequisites, prepares the virtual
#  environment, then starts the app. Safe to run repeatedly.
# ============================================================================

set -u

cd "$(dirname "$0")" || exit 1

VENV=".venv"
REQ="requirements.txt"
MARK="$VENV/.req-hash"
PORT="${REXENIOT_PORT:-8060}"
HOST_URL="localhost"        # what the browser opens
BIND="127.0.0.1"           # what the server binds to (IPv4 is always present)

echo
echo " =========================================================="
echo "   S I S T E R   R E X E N I O T"
echo "   keeper of the house"
echo " =========================================================="
echo

# ------------------------------------------------------------------ Python ---
echo "[1/6] Checking for Python..."

PYEXE=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1; then
    if "$candidate" -c 'import sys; sys.exit(0 if sys.version_info[:2] >= (3,10) else 1)' >/dev/null 2>&1; then
      PYEXE="$candidate"
      break
    fi
  fi
done

if [ -z "$PYEXE" ]; then
  echo
  echo "  ERROR: Python 3.10 or newer was not found."
  echo "  Install it, then run this script again:"
  echo "    Debian/Ubuntu : sudo apt install python3 python3-venv python3-pip"
  echo "    Fedora        : sudo dnf install python3 python3-pip"
  echo "    macOS         : brew install python"
  echo
  exit 1
fi
echo "      found: $($PYEXE --version 2>&1)"

# -------------------------------------------------------------------- venv ---
echo "[2/6] Preparing the virtual environment..."

if [ ! -x "$VENV/bin/python" ]; then
  echo "      creating $VENV ..."
  if ! "$PYEXE" -m venv "$VENV"; then
    echo
    echo "  ERROR: Could not create the virtual environment."
    echo "  On Debian/Ubuntu run: sudo apt install python3-venv"
    echo
    exit 1
  fi
else
  echo "      reusing existing $VENV"
fi
PY="$VENV/bin/python"

# ------------------------------------------------------------ dependencies ---
echo "[3/6] Checking dependencies..."

HASH=""
if command -v sha256sum >/dev/null 2>&1; then
  HASH=$(sha256sum "$REQ" | awk '{print $1}')
elif command -v shasum >/dev/null 2>&1; then
  HASH=$(shasum -a 256 "$REQ" | awk '{print $1}')
else
  HASH=$(wc -c < "$REQ")
fi

NEED_INSTALL=1
if [ -f "$MARK" ] && [ "$(cat "$MARK")" = "$HASH" ]; then
  NEED_INSTALL=0
fi

# espeak-ng is normally bundled by the espeakng_loader wheel, so this is only a
# heads-up for the rare platform where the bundled library cannot load.
if ! ldconfig -p 2>/dev/null | grep -q libespeak; then
  if ! command -v espeak-ng >/dev/null 2>&1 && ! command -v espeak >/dev/null 2>&1; then
    echo "      note: no system espeak-ng found; using the bundled library"
    echo "            (install espeak-ng only if speech synthesis fails)"
  fi
fi

# --------------------------------------------------------------- gpu note ----
# The default install is CPU-only on purpose: onnxruntime is small and works
# everywhere. A GPU only helps if the runtime can reach it, so this detects the
# card and points at the right requirements file rather than guessing.
GPU_FOUND=0
if command -v nvidia-smi >/dev/null 2>&1 && nvidia-smi -L >/dev/null 2>&1; then
  GPU_FOUND=1
  echo "      detected NVIDIA GPU:"
  nvidia-smi --query-gpu=name,memory.total --format=csv,noheader 2>/dev/null | sed 's/^/        /'
fi

if [ "$NEED_INSTALL" = "1" ]; then
  echo "      installing packages (slow the first time) ..."
  "$PY" -m pip install --upgrade pip --quiet
  if ! "$PY" -m pip install -r "$REQ" --quiet; then
    echo
    echo "  ERROR: Dependency installation failed."
    echo "  Run this to see the full output:"
    echo "      $PY -m pip install -r $REQ"
    echo
    exit 1
  fi
  # kittenml declares torch as a hard dependency, but the ONNX backend does not
  # use it. Installing without dependencies keeps the footprint near 200 MB
  # instead of 2.5 GB; the ONNX requirements are listed in requirements.txt.
  echo "      installing the speech package (ONNX only, no PyTorch) ..."
  if ! "$PY" -m pip install --no-deps kittenml==0.9.4 --quiet; then
    echo
    echo "  ERROR: Could not install the speech package."
    echo "  Run: $PY -m pip install --no-deps kittenml==0.9.4"
    echo
    exit 1
  fi
  printf '%s' "$HASH" > "$MARK"
  echo "      dependencies ready"
else
  echo "      dependencies unchanged, skipping install"
fi

if [ "$GPU_FOUND" = "1" ]; then
  if ! "$PY" -c "import onnxruntime as o; raise SystemExit(0 if 'CUDAExecutionProvider' in o.get_available_providers() else 1)" 2>/dev/null; then
    echo
    echo "      NOTE: a GPU is present but the speech runtime cannot use it yet."
    echo "            For faster speech, install the GPU build of onnxruntime:"
    echo "                $PY -m pip uninstall -y onnxruntime"
    echo "                $PY -m pip install -r requirements-gpu.txt"
    echo "            For the 1.7B tier on GPU, also install:"
    echo "                $PY -m pip install -r requirements-full.txt"
    echo
  fi
fi

# ---------------------------------------------------------------- config ----
echo "[4/6] Checking configuration..."

mkdir -p data/models data/cache data/voices

if ! "$PY" -c "import app.config as c; c.load(); print('      configuration ready')"; then
  echo
  echo "  ERROR: The application could not be imported."
  echo "  Run this to see the traceback:"
  echo "      $PY -c 'import app.main'"
  echo
  exit 1
fi

# ------------------------------------------------------- optional: Alexa ----
# Only needed when the Alexa skill is driven by alexacli. The admin panel
# reports the same thing, so a miss here is a hint rather than a failure.
if "$PY" -c "import app.config as c; raise SystemExit(0 if c.load().tools.resolved_alexa_provider()=='alexacli' else 1)" 2>/dev/null; then
  if ! command -v alexacli >/dev/null 2>&1; then
    echo
    echo "      NOTE: the Alexa skill is set to the alexacli provider, but the"
    echo "            'alexacli' command is not on PATH. Install it with:"
    echo "                brew install buddyh/tap/alexacli"
    echo "            or set its full path in the admin panel."
    echo
  fi
fi

# ---------------------------------------------------------------- speech ----
echo "[5/6] Preparing speech engine..."
echo "      the first run downloads the voice model (~80 MB, one time only)"
echo "      loading it and speaking a test line (this can take a minute) ..."
# Loading alone is not "ready": the weights can load while speech is still
# broken. A failure is reported but not fatal, so the app still starts.
if ! "$PY" -m app.main --prepare-tts; then
  echo "      NOTE: speech is not ready; the app will still start."
fi

# ------------------------------------------------------------------- run ----
echo "[6/6] Starting Sister RexenIOT..."

echo
echo "  ----------------------------------------------------------"
echo "   Opening http://$HOST_URL:$PORT/"
echo "   Admin panel:  http://$HOST_URL:$PORT/admin"
echo
echo "   Press Ctrl+C to stop."
echo "  ----------------------------------------------------------"
echo

exec "$PY" -m app.main --host "$BIND" --port "$PORT"

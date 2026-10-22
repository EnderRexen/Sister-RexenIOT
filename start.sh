#!/usr/bin/env bash
# ============================================================================
#  Sister RexenIOT - Linux / macOS launcher
#
#  The counterpart to start.cmd: finds a Python 3.10+ interpreter, then hands
#  the rest of the startup to scripts/bootstrap.py. Everything after "which
#  Python" -- the virtual environment, the dependency check, the GPU note, the
#  speech warm-up and the server -- is shared with the other three launchers and
#  lives in that file, so this one cannot drift out of step with them.
# ============================================================================

set -u

cd "$(dirname "$0")" || exit 1

PORT="${REXENIOT_PORT:-8060}"

# ------------------------------------------------------------------ Python ---
echo "Locating Python..."

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
  echo "  Or use ./start-portable.sh, which needs no Python at all."
  echo
  exit 1
fi
echo "      found: $($PYEXE --version 2>&1)"

# -------------------------------------------------------------------- hand ---
# bootstrap.py prints the banner and the remaining steps.
exec "$PYEXE" "scripts/bootstrap.py" \
  --venv ".venv" \
  --req "requirements.txt" \
  --mark ".venv/.req-hash" \
  --port "$PORT" \
  --host localhost \
  --bind 127.0.0.1 \
  --folder "$(pwd)"

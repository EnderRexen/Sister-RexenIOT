#!/usr/bin/env bash
# Bring the whole demo stack up after a sandbox recycle: local LLM + app.
#
# The runtime kills child processes and wipes /tmp on recycle, so the model and
# binaries live in /workspace/.llm (outside the repo) and this script is the one
# thing to re-run. It is idempotent -- already-running pieces are left alone.
#
# Usage: bash scripts/dev-stack.sh
set -u

REPO="$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)"
LLM_DIR="${LLM_DIR:-/workspace/.llm}"
LLM_PORT=8080
APP_PORT="${REXENIOT_PORT:-12000}"
MODEL_ALIAS="qwen3:8"

up() { curl -s -o /dev/null --max-time 4 "$1"; }

# --- local LLM -----------------------------------------------------------------
if up "http://127.0.0.1:${LLM_PORT}/v1/models"; then
  echo "llm    : already up on ${LLM_PORT}"
elif [ -x "${LLM_DIR}/llama-b11711/llama-server" ] && [ -f "${LLM_DIR}/qwen.gguf" ]; then
  echo "llm    : starting on ${LLM_PORT} ..."
  cd "${LLM_DIR}" || exit 1
  nohup ./llama-b11711/llama-server -m qwen.gguf --host 127.0.0.1 --port "${LLM_PORT}" \
    -c 4096 -t 4 --alias "${MODEL_ALIAS}" > "${LLM_DIR}/server.log" 2>&1 &
  for _ in $(seq 1 40); do
    up "http://127.0.0.1:${LLM_PORT}/v1/models" && break
    sleep 2
  done
  up "http://127.0.0.1:${LLM_PORT}/v1/models" \
    && echo "llm    : ready (alias ${MODEL_ALIAS})" \
    || echo "llm    : FAILED, see ${LLM_DIR}/server.log"
else
  echo "llm    : no model at ${LLM_DIR}, skipping (app runs in offline mode)"
fi

# --- app + watchdog ------------------------------------------------------------
if up "http://127.0.0.1:${APP_PORT}/api/health"; then
  echo "app    : already up on ${APP_PORT}"
else
  echo "app    : starting on ${APP_PORT} ..."
  cd "${REPO}" || exit 1
  REXENIOT_PORT="${APP_PORT}" nohup bash ./scripts/keep-alive.sh > /dev/null 2>&1 &
  # Cold start loads KittenTTS; allow a generous window before declaring failure.
  for _ in $(seq 1 60); do
    up "http://127.0.0.1:${APP_PORT}/api/health" && break
    sleep 3
  done
fi

if up "http://127.0.0.1:${APP_PORT}/api/health"; then
  echo "app    : ready on ${APP_PORT}"
  echo "url    : https://work-1-dsytpcehntqnlxzx.prod-runtime.all-hands.dev/"
else
  echo "app    : still cold (TTS warm-up can take a minute); re-run to confirm"
fi

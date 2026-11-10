#!/usr/bin/env bash
# Keep Sister RexenIOT alive across sandbox recycles.
#
# The runtime periodically kills child processes. This loop restarts the server
# whenever it genuinely dies, so the preview link stays usable without manual
# intervention.
#
# Two failure modes this guards against:
#   1. Startup is slow -- the TTS warm-up downloads ~107 model files and starves
#      the event loop, so HTTP can be unresponsive for a minute or more. A naive
#      short health check reads that as "dead" and starts a duplicate, which then
#      dies on "address already in use". So the server PID is authoritative and a
#      health check alone never triggers a restart.
#   2. Two watchdogs racing each other. A flock keeps exactly one running.
#
# Logs: /tmp/rexeniot_live.log (server), /tmp/rexeniot_watchdog.log (loop).

cd "$(dirname "$(readlink -f "$0")")/.." || exit 1

PORT="${REXENIOT_PORT:-12000}"
LOG=/tmp/rexeniot_live.log
WLOG=/tmp/rexeniot_watchdog.log
PIDFILE=/tmp/rexeniot_server.pid
LOCKFILE=/tmp/rexeniot_watchdog.lock
PY="$PWD/.venv/bin/python"
TOKENFILE=/tmp/rexeniot_api_token

# The preview proxy connects from outside this container, so the server has to
# listen on 0.0.0.0 here -- loopback would be unreachable. That is safe because
# the container's network is isolated, but it is *not* safe on a home LAN, so
# the server now refuses a non-loopback bind without a token. Mint one if the
# environment does not already provide it; it is written to $TOKENFILE and
# printed in the log so the UI gate can be answered.
if [ -z "${REXENIOT_API_TOKEN:-}" ]; then
  if [ -f "$TOKENFILE" ]; then
    REXENIOT_API_TOKEN="$(cat "$TOKENFILE")"
  else
    REXENIOT_API_TOKEN="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
    printf '%s' "$REXENIOT_API_TOKEN" > "$TOKENFILE"
    chmod 600 "$TOKENFILE"
  fi
  export REXENIOT_API_TOKEN
fi

#: How long to leave a freshly started server alone, in seconds. Must exceed the
#: worst-case TTS warm-up or the loop will restart a healthy-but-busy server.
STARTUP_GRACE=120
#: Consecutive failed health checks tolerated before the server is treated as
#: wedged (not merely busy).
FAIL_LIMIT=6

if [ ! -x "$PY" ]; then
  echo "[$(date +%H:%M:%S)] FATAL: interpreter not found at $PY" >> "$WLOG"
  exit 1
fi

# Single instance only.
exec 9>"$LOCKFILE"
if ! flock -n 9; then
  echo "[$(date +%H:%M:%S)] another watchdog holds the lock; exiting" >> "$WLOG"
  exit 0
fi

log() { echo "[$(date +%H:%M:%S)] $*" >> "$WLOG"; }

#: PID of the server we started, or empty if it is not running.
server_pid() {
  [ -f "$PIDFILE" ] || return 1
  local pid
  pid=$(cat "$PIDFILE" 2>/dev/null)
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  echo "$pid"
}

#: True when something is listening and answering on the port.
healthy() {
  curl -sf -o /dev/null --max-time 8 "http://127.0.0.1:$PORT/" 2>/dev/null
}

start_server() {
  log "starting server on port $PORT"
  nohup "$PY" -m app.main --host 0.0.0.0 --port "$PORT" --no-browser >> "$LOG" 2>&1 &
  echo $! > "$PIDFILE"
  log "server pid $(cat "$PIDFILE")"
  STARTED_AT=$(date +%s)
}

log "watchdog started (port $PORT, grace ${STARTUP_GRACE}s)"
fails=0
STARTED_AT=0

while true; do
  pid=$(server_pid)

  if [ -z "$pid" ]; then
    # Only start when the port is genuinely free; a leftover process still
    # holding the socket would make the new one die instantly.
    if ss -ltn 2>/dev/null | grep -q ":$PORT "; then
      log "pid file stale but port $PORT is held; waiting"
      sleep 10
      continue
    fi
    fails=0
    start_server
    sleep 10
    continue
  fi

  # The process is alive, so the port is ours. A slow response during warm-up is
  # expected; only a sustained run of failures means it is wedged.
  if healthy; then
    fails=0
  else
    age=$(( $(date +%s) - STARTED_AT ))
    if [ "$age" -lt "$STARTUP_GRACE" ]; then
      fails=0
    else
      fails=$(( fails + 1 ))
      log "health check failed ($fails/$FAIL_LIMIT, pid $pid, age ${age}s)"
      if [ "$fails" -ge "$FAIL_LIMIT" ]; then
        log "server pid $pid unresponsive; restarting"
        kill "$pid" 2>/dev/null
        sleep 5
        kill -9 "$pid" 2>/dev/null
        rm -f "$PIDFILE"
        fails=0
      fi
    fi
  fi

  sleep 15
done

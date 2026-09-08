#!/usr/bin/env bash
# Start, stop and inspect the maps2cad web app (scripts/serve.py).
#
#   ./scripts/app.sh start            # background, on 127.0.0.1:8765
#   ./scripts/app.sh start 9000       # another port
#   ./scripts/app.sh stop
#   ./scripts/app.sh restart
#   ./scripts/app.sh status
#   ./scripts/app.sh logs             # tail -f the server log
#
# Environment:
#   PORT           port to listen on (default 8765; the argument wins)
#   HOST           bind address (default 127.0.0.1; 0.0.0.0 to expose it)
#   MAPS2CAD_DATA  passed through as --data-dir when set
#
# The pid and log live under output/ (gitignored): output/serve.pid and
# output/server.log. Ownership of the pid is checked against the command
# line before anything is killed, so a stale pidfile never stops a process
# that merely reused the number.
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

CMD="${1:-}"
PORT="${2:-${PORT:-8765}}"
HOST="${HOST:-127.0.0.1}"
PIDFILE="$ROOT/output/serve.pid"
LOGFILE="$ROOT/output/server.log"
URL="http://$HOST:$PORT"
HEALTH="http://127.0.0.1:$PORT/health"
[ "$HOST" = "0.0.0.0" ] || HEALTH="$URL/health"

# Pick the interpreter the way the rest of the repo does: uv installs the
# PEP 723 dependencies of every script serve.py shells out to; without uv
# the .venv from requirements.txt is the fallback, then whatever python3 is.
runner() {
  if command -v uv >/dev/null 2>&1; then
    echo "uv run scripts/serve.py"
  elif [ -x .venv/bin/python ]; then
    echo ".venv/bin/python -u scripts/serve.py"
  else
    echo "python3 -u scripts/serve.py"
  fi
}

# Print the pid from the pidfile if that process is alive and is serve.py.
live_pid() {
  [ -f "$PIDFILE" ] || return 1
  local pid
  pid="$(cat "$PIDFILE" 2>/dev/null || true)"
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  ps -o command= -p "$pid" 2>/dev/null | grep -q 'serve\.py' || return 1
  echo "$pid"
}

healthy() { curl -fsS --max-time 2 "$HEALTH" >/dev/null 2>&1; }

do_start() {
  if pid="$(live_pid)"; then
    echo "maps2cad already running (pid $pid) — $URL"
    return 0
  fi
  if healthy; then
    echo "Something already answers on $HEALTH but was not started by this" >&2
    echo "script. Stop it first (pkill -f serve.py) or pick another port." >&2
    return 1
  fi
  mkdir -p output
  local args=(--host "$HOST" --port "$PORT")
  [ -n "${MAPS2CAD_DATA:-}" ] && args+=(--data-dir "$MAPS2CAD_DATA")

  echo "Starting maps2cad on $URL ..."
  # shellcheck disable=SC2046
  PYTHONUNBUFFERED=1 nohup $(runner) "${args[@]}" >>"$LOGFILE" 2>&1 &
  echo $! >"$PIDFILE"

  # uv may have to install dependencies on the first run, so allow a while.
  for _ in $(seq 1 120); do
    if healthy; then
      echo "maps2cad is up: $URL   (pid $(cat "$PIDFILE"), log $LOGFILE)"
      return 0
    fi
    if ! kill -0 "$(cat "$PIDFILE")" 2>/dev/null; then
      echo "ERROR: server exited during start — last lines of $LOGFILE:" >&2
      tail -n 20 "$LOGFILE" >&2 || true
      rm -f "$PIDFILE"
      return 1
    fi
    sleep 0.5
  done
  echo "ERROR: no answer on $HEALTH after 60 s — see $LOGFILE" >&2
  return 1
}

do_stop() {
  local pid
  if ! pid="$(live_pid)"; then
    rm -f "$PIDFILE"
    echo "maps2cad is not running"
    return 0
  fi
  echo "Stopping maps2cad (pid $pid) ..."
  # `uv run` may stay as a parent of the real server, so signal its
  # children too. Not the process group: nohup leaves the child in this
  # shell's own group, and killing that would take the script with it.
  local kids
  kids="$(pgrep -P "$pid" 2>/dev/null || true)"
  kill -TERM "$pid" $kids 2>/dev/null || true
  for _ in $(seq 1 20); do
    kill -0 "$pid" 2>/dev/null || break
    sleep 0.5
  done
  if kill -0 "$pid" 2>/dev/null; then
    echo "Still running after 10 s, sending SIGKILL"
    kill -KILL "$pid" $kids 2>/dev/null || true
  fi
  rm -f "$PIDFILE"
  echo "Stopped"
}

do_status() {
  if pid="$(live_pid)"; then
    if healthy; then
      echo "maps2cad running (pid $pid), healthy at $URL"
    else
      echo "maps2cad running (pid $pid) but $HEALTH is not answering"
      return 1
    fi
  elif healthy; then
    echo "$HEALTH answers, but not from a process this script started"
  else
    echo "maps2cad is not running"
    return 3
  fi
}

case "$CMD" in
  start)   do_start ;;
  stop)    do_stop ;;
  restart) do_stop; do_start ;;
  status)  do_status ;;
  logs)    touch "$LOGFILE"; exec tail -n 50 -f "$LOGFILE" ;;
  *)
    sed -n '2,15p' "$0" | sed 's/^# \{0,1\}//'
    exit 2 ;;
esac

#!/usr/bin/env bash
# Start the maps2cad web app and expose it through ngrok.
#
#   ./scripts/tunnel.sh          # app on :8765 + public https URL
#   ./scripts/tunnel.sh 9000     # different local port
#
# Ctrl-C stops the tunnel. The app keeps running; stop it with:
#   pkill -f 'serve.py'
set -euo pipefail

PORT="${1:-8765}"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
GLOBAL_CFG="$HOME/Library/Application Support/ngrok/ngrok.yml"
cd "$ROOT"

if [ ! -f ngrok.yml ]; then
  echo "ERROR: ngrok.yml not found in $ROOT" >&2
  exit 1
fi

# Start the app only if it is not already answering
if ! curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
  echo "Starting maps2cad on 127.0.0.1:$PORT ..."
  if [ -x .venv/bin/python ]; then PY=.venv/bin/python; else PY=python3; fi
  "$PY" -u scripts/serve.py --port "$PORT" > output/server.log 2>&1 &
  for _ in $(seq 1 20); do
    curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 && break
    sleep 0.5
  done
fi
curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null || {
  echo "ERROR: app did not start — see output/server.log" >&2; exit 1; }
echo "App is up: http://127.0.0.1:$PORT"

# Report the public URL once ngrok has registered the tunnel
( for _ in $(seq 1 30); do
    sleep 1
    URL=$(curl -fsS http://127.0.0.1:4040/api/tunnels 2>/dev/null \
          | python3 -c 'import json,sys; t=json.load(sys.stdin)["tunnels"]; print(t[0]["public_url"]) if t else ""' 2>/dev/null || true)
    if [ -n "${URL:-}" ]; then
      echo
      echo "  Public URL: $URL"
      echo "  Open access — anyone with this link can generate maps, edit"
      echo "  staged names, and download outputs. Ctrl-C when finished."
      echo
      break
    fi
  done ) &

exec ngrok start --all --config "$GLOBAL_CFG" --config ngrok.yml

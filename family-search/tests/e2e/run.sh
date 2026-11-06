#!/usr/bin/env bash
# Dev-only: start the app with fakes and run the Chromium check at 360 px.
#   PYTHON=/path/to/venv/python tests/e2e/run.sh
set -euo pipefail
cd "$(dirname "$0")/../.."
PY=${PYTHON:-python3}
PORT=${PORT:-8765}
SHOTS=${SHOTS:-tests/e2e/shots}
WORK=$(mktemp -d)
mkdir -p "$SHOTS"
"$PY" tests/e2e/serve.py "$PORT" "$WORK/tokens.json" 2>"$WORK/server.log" &
SERVER=$!
stop() { kill "$SERVER" 2>/dev/null || true; }
trap stop EXIT
for _ in $(seq 1 100); do
  if [ -s "$WORK/tokens.json" ] && curl -s -o /dev/null "http://127.0.0.1:$PORT/"; then break; fi
  sleep 0.2
done
NODE_PATH=${NODE_PATH:-$(npm root -g)} node tests/e2e/check.mjs "$PORT" "$WORK/tokens.json" "$SHOTS" \
  || { echo "--- server log"; tail -20 "$WORK/server.log"; exit 1; }

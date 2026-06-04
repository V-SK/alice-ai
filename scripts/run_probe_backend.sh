#!/usr/bin/env bash
# Launch the backend on an ephemeral loopback port with the real Simple-mode
# env (+ a dev-override small MLX model so generation actually runs), wait for
# /healthz, then leave it running. Prints "PORT=<n>" and "TOKEN=<t>".
#
# Env in:  ALICE_SIMPLE_MODE (default 1), ALICE_LOCAL_TOKEN (default random)
# Used by the security probe; torn down by the caller via the printed PID.
set -euo pipefail

REPO=/Users/v/Alice/alice-ai
PY="$REPO/.venv/bin/python"
ODY="$REPO/backend/odysseus"

# Small already-resident MLX model for a fast, real on-device generation.
SNAP=$(find /Users/v/.cache/huggingface/hub/models--mlx-community--Qwen3-0.6B-4bit/snapshots -maxdepth 1 -mindepth 1 -type d 2>/dev/null | head -1)

PORT=$("$PY" - <<'PYEOF'
import socket
s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1]); s.close()
PYEOF
)

TOKEN="${ALICE_LOCAL_TOKEN:-$("$PY" -c 'import secrets;print(secrets.token_urlsafe(32))')}"
LOG="$REPO/scripts/.probe-backend.log"

cd "$ODY"
AUTH_ENABLED=false \
LOCALHOST_BYPASS=true \
ALICE_BACKEND_PORT="$PORT" \
ALICE_SIMPLE_MODE="${ALICE_SIMPLE_MODE:-1}" \
ALICE_LOCAL_TOKEN="$TOKEN" \
ODYSSEUS_INPROCESS_TASKS=0 \
PYTHONPATH="$REPO/backend" \
ALICE_AI_MODEL_DIR="$SNAP" \
ALICE_AI_RUNTIME=mlx \
PYTHONUNBUFFERED=1 \
nohup "$PY" -m uvicorn app:app --host 127.0.0.1 --port "$PORT" --no-access-log >"$LOG" 2>&1 &
PID=$!

echo "PORT=$PORT"
echo "TOKEN=$TOKEN"
echo "PID=$PID"
echo "LOG=$LOG"

# Wait for health (up to ~40s).
for i in $(seq 1 80); do
  if curl -fsS "http://127.0.0.1:$PORT/healthz" >/dev/null 2>&1; then
    echo "READY=1"
    exit 0
  fi
  if ! kill -0 "$PID" 2>/dev/null; then
    echo "READY=0 (process died)"; tail -30 "$LOG"; exit 1
  fi
  sleep 0.5
done
echo "READY=0 (timeout)"; tail -30 "$LOG"; exit 1

#!/usr/bin/env bash
#
# dev_run.sh — run the shell + backend from source (no freeze, no package).
#
# PLACEHOLDER (M0). The full implementation lands in M1 (PLAN §5 acceptance):
# it will boot the forked odysseus FastAPI from source on an ephemeral loopback
# port, wait on `GET /healthz`, and open the PyWebView shell. For now it points
# the way and runs the import smoke.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV="${REPO_ROOT}/.venv"

if [ ! -x "${VENV}/bin/python" ]; then
  echo "No venv at ${VENV}. Run scripts/setup_dev_env.sh first." >&2
  exit 1
fi

echo "dev_run.sh: M0 placeholder. The shell+backend boot lands in M1 (PLAN §5)."
echo "Smoke instead:"
"${VENV}/bin/python" -c "import alice_acp.local_inference; print('  alice_acp.local_inference OK')"
# M1 will replace the above with, roughly:
#   exec "${VENV}/bin/python" -m alice_shell      # which spawns the backend

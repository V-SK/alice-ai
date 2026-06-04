#!/usr/bin/env bash
#
# dev_run.sh — run the Alice AI spine (shell + backend) from source (M1).
#
# The PyWebView shell (shell/alice_shell) claims an ephemeral loopback port,
# spawns the vendored odysseus FastAPI backend (uvicorn, AUTH off + loopback,
# inference in-process via alice_provider.py), waits on GET /healthz, and opens
# the native chat window. No terminal, no visible port (PLAN §2.2).
#
# Flags / env:
#   --no-window           boot backend + wait health + print URL, no GUI window
#                         (headless verification path; chat then provable via curl)
#   ALICE_AI_MODEL_DIR    point at an already-resident MLX snapshot dir to prove
#                         the chat without the multi-GB Alice Lite download
#                         (the wired default is still Alice Lite).
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV="${REPO_ROOT}/.venv"

if [ ! -x "${VENV}/bin/python" ]; then
  echo "No venv at ${VENV}. Run scripts/setup_dev_env.sh first." >&2
  exit 1
fi

# The shell lives under shell/; make alice_shell importable.
export PYTHONPATH="${REPO_ROOT}/shell:${PYTHONPATH:-}"

exec "${VENV}/bin/python" -m alice_shell "$@"

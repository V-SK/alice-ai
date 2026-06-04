#!/usr/bin/env bash
#
# setup_dev_env.sh — create the in-repo dev venv (gitignored) and install the
# M0 dependency subset so `from alice_acp.local_inference import ...` resolves
# and the odysseus FastAPI app imports.
#
# Heavy inference wheels (llama-cpp-python / mlx) and odysseus's Advanced-only
# deps are DEFERRED (see backend/requirements.txt) — M0 only needs imports to
# resolve.
#
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "${SCRIPT_DIR}/.." && pwd)"
VENV="${REPO_ROOT}/.venv"
PY="${PYTHON:-python3}"

echo "==> Creating venv at ${VENV} (using ${PY})"
"${PY}" -m venv "${VENV}"

# shellcheck disable=SC1091
"${VENV}/bin/python" -m pip install --quiet --upgrade pip setuptools wheel

echo "==> Installing M0 deps from backend/requirements.txt"
# Install from inside backend/ so the editable path dep `-e ../../alice-acp`
# (relative to the requirements file) resolves to the sibling alice-acp repo.
# pip resolves relative requirement paths against its CWD, not the -r file's dir.
( cd "${REPO_ROOT}/backend" && "${VENV}/bin/python" -m pip install -r requirements.txt )

echo "==> Smoke: import alice_acp.local_inference"
"${VENV}/bin/python" -c "import alice_acp.local_inference as li; print('  OK', li.__file__)"

echo "==> Done. Activate with:  source ${VENV}/bin/activate"

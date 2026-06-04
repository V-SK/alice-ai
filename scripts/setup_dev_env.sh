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

# HIGH-3 (deep-security-audit): a SIGNED RELEASE build must install from the
# hash-pinned lock with enforcement, so the bundle can only contain the exact
# audited artifacts. Opt in with ALICE_LOCKED_INSTALL=1 (set by the release
# build); plain dev iteration uses the loose requirements.txt for speed.
if [ "${ALICE_LOCKED_INSTALL:-0}" = "1" ]; then
  echo "==> Installing PINNED deps (--require-hashes) from backend/requirements.lock.txt"
  # The editable first-party alice-acp checkout is not in the (PyPI-only) lock;
  # install it separately (pin it by git commit in the real build).
  "${VENV}/bin/python" -m pip install --require-hashes -r "${REPO_ROOT}/backend/requirements.lock.txt"
  ( cd "${REPO_ROOT}/backend" && "${VENV}/bin/python" -m pip install --no-deps -e ../../alice-acp )
else
  echo "==> Installing M0 deps from backend/requirements.txt (loose dev env)"
  # Install from inside backend/ so the editable path dep `-e ../../alice-acp`
  # (relative to the requirements file) resolves to the sibling alice-acp repo.
  # pip resolves relative requirement paths against CWD, not the -r file's dir.
  ( cd "${REPO_ROOT}/backend" && "${VENV}/bin/python" -m pip install -r requirements.txt )
fi

echo "==> Smoke: import alice_acp.local_inference"
"${VENV}/bin/python" -c "import alice_acp.local_inference as li; print('  OK', li.__file__)"

echo "==> Done. Activate with:  source ${VENV}/bin/activate"

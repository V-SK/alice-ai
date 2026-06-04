"""Filesystem paths the shell needs to locate the vendored backend + venv.

In dev (``python -m alice_shell`` / ``scripts/dev_run.sh``) the backend runs
from source under ``backend/odysseus``; in a frozen build (M2+) these resolve
inside the PyInstaller bundle. Kept tiny + logic-free so the PyWebView → Tauri
swap stays shell-only (PLAN §2.1).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def repo_root() -> Path:
    """Repo root = three levels up from this file (shell/alice_shell/paths.py)."""
    return Path(__file__).resolve().parents[2]


def backend_dir() -> Path:
    """The vendored odysseus backend dir (cwd for the uvicorn child).

    Override with ``$ALICE_BACKEND_DIR`` for a frozen/relocated layout.
    """
    env = os.getenv("ALICE_BACKEND_DIR")
    if env:
        return Path(env).resolve()
    return repo_root() / "backend" / "odysseus"


def venv_python() -> Path:
    """The interpreter that runs the backend child.

    Prefer the in-repo dev venv; fall back to the current interpreter (covers a
    frozen build or an already-activated venv).
    """
    env = os.getenv("ALICE_BACKEND_PYTHON")
    if env:
        return Path(env)
    candidate = repo_root() / ".venv" / "bin" / "python"
    if candidate.exists():
        return candidate
    return Path(sys.executable)


def models_dir() -> Path:
    """User model cache (PLAN §2.5: ~/.alice/models)."""
    env = os.getenv("ALICE_AI_MODELS_DIR")
    if env:
        return Path(env)
    return Path.home() / ".alice" / "models"


def backend_log_path() -> Path:
    """Where the shell writes the backend's stdout/stderr log.

    In dev this sits in the repo venv (``.venv/alice-backend.log``). In a FROZEN
    .app, ``repo_root()`` resolves INSIDE the bundle (Contents/Resources), which
    is read-only when the app is installed to /Applications — so redirect the
    log to a user-owned dir (``~/.alice/logs``). Honours ``$ALICE_AI_LOG_DIR``.
    """
    env = os.getenv("ALICE_AI_LOG_DIR")
    if env:
        d = Path(env)
    elif getattr(sys, "frozen", False):
        d = Path.home() / ".alice" / "logs"
    else:
        d = repo_root() / ".venv"
    d.mkdir(parents=True, exist_ok=True)
    return d / "alice-backend.log"

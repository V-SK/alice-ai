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


def data_root() -> Path:
    """The AI client's PRIVATE, USER-OWNED, WRITABLE data root (OS-native).

    This is NOT the shared cross-client ``~/.alice`` identity contract (that
    lives at ``~/.alice/identity.json`` via ``$ALICE_IDENTITY_DIR`` and is read
    by the Wallet/Miner — see earn/identity_reader.py). This is where Alice AI
    writes its OWN private state: model cache, the sqlite DB, search cache, logs.

    Resolution (override always wins):
      1. ``$ALICE_AI_DATA_DIR`` — explicit override (CI, dev, advanced users).
      2. Windows → ``%LOCALAPPDATA%\\Alice``  (the Windows convention; falls back
         to ``~/.alice`` if LOCALAPPDATA is somehow unset).
      3. macOS / Linux → ``~/.alice`` — UNCHANGED from M2 (proven), and keeps the
         AI's data next to the shared contract dir as the design intends.
    XDG note: on Linux ``~/.alice`` is deliberate (co-located with the contract);
    set ``$ALICE_AI_DATA_DIR=$XDG_DATA_HOME/alice`` to follow XDG strictly.
    """
    env = os.getenv("ALICE_AI_DATA_DIR")
    if env:
        return Path(env)
    if os.name == "nt":
        local = os.getenv("LOCALAPPDATA")
        if local:
            return Path(local) / "Alice"
    return Path.home() / ".alice"


def models_dir() -> Path:
    """User model cache (PLAN §2.5). ``ALICE_AI_MODELS_DIR`` overrides; else
    ``<data_root>/models`` (``~/.alice/models`` on mac/Linux, ``%LOCALAPPDATA%\\
    Alice\\models`` on Windows)."""
    env = os.getenv("ALICE_AI_MODELS_DIR")
    if env:
        return Path(env)
    return data_root() / "models"


def backend_log_path() -> Path:
    """Where the shell writes the backend's stdout/stderr log.

    In dev this sits in the repo venv (``.venv/alice-backend.log``). In a FROZEN
    build ``repo_root()`` resolves INSIDE the read-only bundle, so redirect the
    log to the user-owned ``<data_root>/logs`` (``~/.alice/logs`` on mac/Linux,
    ``%LOCALAPPDATA%\\Alice\\logs`` on Windows). Honours ``$ALICE_AI_LOG_DIR``.
    """
    env = os.getenv("ALICE_AI_LOG_DIR")
    if env:
        d = Path(env)
    elif getattr(sys, "frozen", False):
        d = data_root() / "logs"
    else:
        d = repo_root() / ".venv"
    d.mkdir(parents=True, exist_ok=True)
    return d / "alice-backend.log"

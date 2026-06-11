"""Frozen single-binary entry point for AliceAI.app (PyInstaller one-dir, M2).

The packaged app is ONE frozen executable that plays two roles (PLAN §2.2's
two-process model, realized without a second frozen tree):

  * **shell role** (default, what the .app launches): run the existing
    ``alice_shell`` logic — claim an ephemeral loopback port, spawn the backend
    child, wait on ``/healthz``, open the PyWebView chat window, tear the child
    down on quit.
  * **backend role** (``--alice-backend``, how the shell spawns its child): be
    the uvicorn FastAPI backend — ``chdir`` into the bundled ``backend/odysseus``
    so its by-cwd imports (``app``/``core``/``src``/``routes``/``alice_provider``
    /``alice_routes``) resolve, then ``uvicorn.run(app)`` on the handed port.

Why one binary, re-exec'd: PyInstaller freezes a single Python interpreter +
module graph. Spawning ``sys.executable --alice-backend`` reuses that exact
frozen interpreter for the child (no system Python, no venv) — the shell's
supervisor already spawns ``sys.executable`` when frozen (see
``shell/alice_shell/supervisor.py``). ``sys._MEIPASS`` is the bundle resource
root; the spec lays the backend tree + our ``alice_ai`` package + the vendored
``alice_acp`` sources there so both roles import cleanly with NO Python install.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _meipass() -> Path:
    """Bundle resource root (``sys._MEIPASS`` when frozen, else the repo)."""
    mp = getattr(sys, "_MEIPASS", None)
    if mp:
        return Path(mp)
    # Not frozen (running this file directly from source): repo root.
    return Path(__file__).resolve().parents[2]


def _default_data_root() -> Path:
    """OS-native AI-private data root (mirrors alice_shell.paths.data_root).

    Inlined here so the backend role doesn't depend on the shell package being
    importable at this point. Windows → ``%LOCALAPPDATA%\\Alice``; mac/Linux →
    ``~/.alice`` (unchanged from M2). ``$ALICE_AI_DATA_DIR`` overrides upstream.
    """
    if os.name == "nt":
        local = os.environ.get("LOCALAPPDATA")
        if local:
            return Path(local) / "Alice"
    return Path.home() / ".alice"


def _prepare_import_roots(base: Path) -> Path:
    """Put the bundled source roots on sys.path so by-cwd + package imports work.

    Layout the spec creates under the bundle root:
      <base>/backend/odysseus/   — the FastAPI app (imported by cwd)
      <base>/backend/            — our ``alice_ai`` package (PYTHONPATH root)
      <base>/_alice_src/         — vendored ``alice_acp`` sources (editable dep)

    Returns the backend working directory (``backend/odysseus``).
    """
    backend_root = base / "backend"
    backend_dir = backend_root / "odysseus"
    acp_src = base / "_alice_src"

    for p in (str(backend_dir), str(backend_root), str(acp_src)):
        if p not in sys.path:
            sys.path.insert(0, p)
    return backend_dir


def _run_backend(base: Path) -> int:
    """Backend role: become the uvicorn FastAPI server (the shell's child).

    The forked odysseus app makes MANY writes RELATIVE TO CWD (``./data/...``,
    ``./services/*.log``, RAG/chroma dirs, the sqlite DB), and a few relative to
    ``__file__``. In a frozen .app the bundle is READ-ONLY when installed to
    /Applications, so we do NOT run with cwd inside the bundle. Instead we build
    a USER-OWNED runtime dir, SYMLINK the read-only frontend assets (``static``,
    ``config``) into it from the bundle, and ``chdir`` there — so every
    cwd-relative write lands on a writable disk while ``directory="static"``
    still resolves (through the symlink). The ``__file__``-relative writes are
    redirected via ``ALICE_AI_DATA_DIR`` (search cache/log) + ``DATABASE_URL``.
    """
    backend_dir = _prepare_import_roots(base)

    # AI-private writable data dir (OS-native). NOT the shared ~/.alice identity
    # contract — that is separate ($ALICE_IDENTITY_DIR). On Windows this lands
    # under %LOCALAPPDATA%\Alice; on mac/Linux it stays ~/.alice (unchanged from
    # M2). The shell role normally sets ALICE_AI_DATA_DIR before spawning us, so
    # this default only fires if the backend is launched standalone.
    data_dir = Path(os.environ.get("ALICE_AI_DATA_DIR") or _default_data_root() / "ai-data")
    data_dir.mkdir(parents=True, exist_ok=True)
    os.environ["ALICE_AI_DATA_DIR"] = str(data_dir)

    # The writable runtime dir we chdir into (sibling of ai-data).
    runtime_dir = data_dir.parent / "ai-runtime"
    runtime_dir.mkdir(parents=True, exist_ok=True)
    # Symlink the READ-ONLY frontend assets from the bundle so cwd-relative
    # reads (directory="static", config/) resolve, while writes go to disk.
    for asset in ("static", "config"):
        src = backend_dir / asset
        link = runtime_dir / asset
        if not src.exists():
            continue
        # ALWAYS (re)point the symlink at the CURRENT bundle. A stale link left by
        # a previous install/version (different bundle path, or an older build
        # missing files) would otherwise keep serving outdated frontend assets —
        # e.g. a 404 on an ES module aborts app.js's import graph → blank window.
        try:
            if link.is_symlink() or link.exists():
                link.unlink()
            link.symlink_to(src, target_is_directory=True)
        except (OSError, NotImplementedError):
            pass
    os.chdir(str(runtime_dir))

    # core/database.py reads DATABASE_URL (default sqlite:///./data/app.db, now
    # cwd-relative to the WRITABLE runtime dir — but pin it explicitly to the
    # user data dir so the DB is stable regardless of cwd).
    os.environ.setdefault("DATABASE_URL", f"sqlite:///{data_dir / 'app.db'}")

    # UTF-8 + unbuffered so llama.cpp/mlx progress + logs are clean on every OS.
    os.environ.setdefault("PYTHONUTF8", "1")
    os.environ.setdefault("PYTHONUNBUFFERED", "1")

    port = int(os.environ.get("ALICE_BACKEND_PORT", "0") or "0")
    if port <= 0:
        print("[alice-entry] ALICE_BACKEND_PORT not set for backend role", file=sys.stderr)
        return 2

    import uvicorn  # noqa: WPS433 — frozen import

    # Import the FastAPI app from the bundled backend (now on sys.path + cwd).
    from app import app  # type: ignore  # noqa: WPS433

    # Access logging is off by default (noise); enable with ALICE_AI_ACCESS_LOG=1 to debug.
    _access_log = os.environ.get("ALICE_AI_ACCESS_LOG", "0").strip().lower() in ("1", "true", "yes", "on")
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="info",
                access_log=_access_log)
    return 0


def _run_shell(base: Path) -> int:
    """Shell role (default): the PyWebView double-click target."""
    # Make ``alice_shell`` importable from the bundle, then defer to its main.
    # The spec bundles the shell package under <base>/shell.
    shell_root = base / "shell"
    if shell_root.exists() and str(shell_root) not in sys.path:
        sys.path.insert(0, str(shell_root))

    # Tell the shell where the bundled backend lives + how to spawn it. In a
    # frozen build the backend is THIS executable re-exec'd with --alice-backend
    # (ALICE_BACKEND_PYTHON is unused on the frozen path; supervisor branches on
    # sys.frozen). ALICE_BACKEND_DIR points paths.backend_dir() at the bundle.
    os.environ.setdefault("ALICE_BACKEND_DIR", str(base / "backend" / "odysseus"))
    # Also expose the bundle root so the supervisor can set the child's
    # PYTHONPATH to the bundled source roots (backend/ + _alice_src/).
    os.environ.setdefault("ALICE_BUNDLE_ROOT", str(base))

    from alice_shell.__main__ import main as shell_main  # noqa: WPS433

    return shell_main(sys.argv[1:])


def main() -> int:
    base = _meipass()

    argv = sys.argv[1:]
    is_backend = (
        ("--alice-backend" in argv)
        or os.environ.get("ALICE_ROLE", "").lower() == "backend"
    )
    if is_backend:
        return _run_backend(base)
    return _run_shell(base)


if __name__ == "__main__":
    raise SystemExit(main())

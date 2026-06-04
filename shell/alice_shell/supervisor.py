"""Spawn + supervise the odysseus FastAPI backend as a child process (PLAN §2.2).

The backend runs ``uvicorn app:app`` in its OWN process group so that on quit we
can SIGTERM-then-SIGKILL the entire group (uvicorn + any reload/worker kids) and
never leave a stale child holding the port (R5). The child runs with:

  * ``AUTH_ENABLED=false`` + ``LOCALHOST_BYPASS=true`` — Simple mode, no login
    (PLAN §2.3 / D6); the admin-password prompt is never reached.
  * ``ALICE_BACKEND_PORT`` — the ephemeral loopback port (so alice_provider can
    seed its default endpoint at the right URL).
  * ``ALICE_AI_MODELS_DIR`` — where Alice Lite caches/loads (~/.alice/models).
  * any ``ALICE_AI_MODEL_DIR`` / ``ALICE_AI_RUNTIME`` dev override, passed
    through verbatim.

No shell, no terminal window — stdout/stderr are piped to a log the shell can
surface. Exactly two OS processes total: this shell + the backend.
"""

from __future__ import annotations

import os
import secrets
import signal
import subprocess
import sys
from pathlib import Path

from alice_shell import paths


class BackendProcess:
    """A supervised uvicorn child bound to a loopback port."""

    def __init__(self, port: int, *, log_path: Path | None = None,
                 local_token: str | None = None) -> None:
        self.port = port
        self._proc: subprocess.Popen | None = None
        self._log_path = log_path
        self._log_fh = None
        # Per-launch anti-pivot shared secret (deep-security-audit CRIT-2 b):
        # generated once per process, handed to the backend via env AND injected
        # into the served UI (cookie). A random web page / DNS-rebound origin
        # can't read or set it, so its requests to the guarded API are rejected.
        self.local_token = local_token or secrets.token_urlsafe(32)

    def _child_env(self) -> dict[str, str]:
        env = dict(os.environ)
        env["AUTH_ENABLED"] = "false"
        env["LOCALHOST_BYPASS"] = "true"
        # Simple-mode security boundary ON by default (deep-security-audit):
        # hard-blocks the dangerous agent tools at dispatch, unmounts the
        # shell/cookbook/MCP/codex/vault routers, forces chat mode, and requires
        # the per-launch token + loopback Host on the API. Advanced (which
        # re-enables that surface) is a separate, admin-account-gated build.
        env.setdefault("ALICE_SIMPLE_MODE", "1")
        env["ALICE_LOCAL_TOKEN"] = self.local_token
        # The backend binds this and alice_provider seeds its endpoint here.
        env["ALICE_BACKEND_PORT"] = str(self.port)
        env.setdefault("ALICE_AI_MODELS_DIR", str(paths.models_dir()))
        # The backend runs from backend/odysseus/ (so its sibling alice_provider
        # / alice_routes import by cwd), but those import OUR alice_ai.* package
        # which lives one level up at backend/. Put backend/ on PYTHONPATH so the
        # Model Manager (M4) + earn glue (M7) resolve. alice_acp is a pip
        # editable install, so it does not need a path entry.
        backend_root = str(paths.backend_dir().parent)
        path_parts = [backend_root]
        # Frozen build (M2): the backend is THIS executable re-exec'd, so it has
        # no venv site-packages. Add the bundled vendored alice_acp source root
        # (<bundle>/_alice_src) so ``import alice_acp`` resolves with no Python
        # install. ALICE_BUNDLE_ROOT is set by the frozen entry's shell role.
        bundle_root = os.getenv("ALICE_BUNDLE_ROOT")
        if bundle_root:
            path_parts.append(os.path.join(bundle_root, "_alice_src"))
        existing = env.get("PYTHONPATH", "")
        if existing:
            path_parts.append(existing)
        env["PYTHONPATH"] = os.pathsep.join(path_parts)
        # Quiet odysseus's background pollers that are useless for a local chat
        # spine (they only add log noise + cold-start pings).
        env.setdefault("ODYSSEUS_INPROCESS_TASKS", "0")
        env.setdefault("PYTHONUNBUFFERED", "1")
        env.setdefault("PYTHONUTF8", "1")
        return env

    def start(self) -> None:
        bdir = paths.backend_dir()
        if not (bdir / "app.py").exists():
            raise FileNotFoundError(f"backend app.py not found under {bdir}")
        # Ensure the backend's relative data dir (sqlite at ./data/app.db) exists.
        (bdir / "data").mkdir(parents=True, exist_ok=True)

        if getattr(sys, "frozen", False):
            # Frozen build (M2): re-exec THIS bundled executable in its backend
            # role (alice_entry --alice-backend → uvicorn.run(app)). Reuses the
            # exact frozen interpreter + module graph for the child; no venv, no
            # system Python. The port is passed via ALICE_BACKEND_PORT (env).
            cmd = [sys.executable, "--alice-backend"]
        else:
            # Dev: spawn the venv interpreter running uvicorn against the source
            # backend (scripts/dev_run.sh path).
            py = str(paths.venv_python())
            cmd = [
                py, "-m", "uvicorn", "app:app",
                "--host", "127.0.0.1",
                "--port", str(self.port),
                "--no-access-log",
            ]
        if self._log_path is not None:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_fh = open(self._log_path, "w", encoding="utf-8")
            stdout = self._log_fh
            stderr = subprocess.STDOUT
        else:
            stdout = stderr = None

        # New session/process-group so we can kill the whole tree on quit.
        kwargs: dict = {"cwd": str(bdir), "env": self._child_env(),
                        "stdout": stdout, "stderr": stderr}
        if os.name == "posix":
            kwargs["start_new_session"] = True  # setsid → own process group
        else:  # Windows: new process group for CTRL_BREAK / taskkill /T
            kwargs["creationflags"] = getattr(
                subprocess, "CREATE_NEW_PROCESS_GROUP", 0
            )

        self._proc = subprocess.Popen(cmd, **kwargs)

    def is_alive(self) -> bool:
        return self._proc is not None and self._proc.poll() is None

    def returncode(self):
        return None if self._proc is None else self._proc.poll()

    def stop(self, *, term_grace_s: float = 5.0) -> None:
        """SIGTERM the process group, then SIGKILL if it lingers (R5)."""
        if self._proc is None:
            return
        if self._proc.poll() is None:
            try:
                if os.name == "posix":
                    os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
                else:
                    self._proc.send_signal(getattr(signal, "CTRL_BREAK_EVENT", signal.SIGTERM))
            except (ProcessLookupError, PermissionError, OSError):
                pass
            try:
                self._proc.wait(timeout=term_grace_s)
            except subprocess.TimeoutExpired:
                try:
                    if os.name == "posix":
                        os.killpg(os.getpgid(self._proc.pid), signal.SIGKILL)
                    else:
                        self._proc.kill()
                except (ProcessLookupError, PermissionError, OSError):
                    pass
                try:
                    self._proc.wait(timeout=term_grace_s)
                except subprocess.TimeoutExpired:
                    pass
        if self._log_fh is not None:
            try:
                self._log_fh.flush()
                self._log_fh.close()
            except Exception:  # noqa: BLE001
                pass
            self._log_fh = None

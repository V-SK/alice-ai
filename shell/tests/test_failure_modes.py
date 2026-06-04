"""Shell failure-mode tests (M8) — port-conflict retry + backend restart.

Locks the F6 (backend-not-up / port race → re-claim a fresh port) and F7
(backend crash mid-chat → bounded restart, same port + token) behaviours so they
can't silently regress. Pure-stdlib unittest; no GUI, no real uvicorn — the
BackendProcess child command is stubbed to a trivial sleeper so start/restart/
stop are exercised against real OS process semantics (process group, kill).

Run (from the repo root):
  PYTHONPATH=shell .venv/bin/python -m unittest shell.tests.test_failure_modes -v
"""

from __future__ import annotations

import socket
import sys
import time
import unittest
from pathlib import Path

_SHELL = Path(__file__).resolve().parents[1]
if str(_SHELL) not in sys.path:
    sys.path.insert(0, str(_SHELL))


class PortTests(unittest.TestCase):
    def test_claim_returns_free_port(self):
        from alice_shell.port import claim_ephemeral_port, is_port_free

        p = claim_ephemeral_port()
        self.assertTrue(1024 <= p <= 65535)
        # Nothing bound it yet → free.
        self.assertTrue(is_port_free(p))

    def test_is_port_free_detects_held_port(self):
        from alice_shell.port import is_port_free

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        s.listen(1)
        held = s.getsockname()[1]
        try:
            # While we hold a LISTEN socket, the port is NOT free.
            self.assertFalse(is_port_free(held))
        finally:
            s.close()
        # Released → free again (give the OS a beat).
        for _ in range(20):
            if is_port_free(held):
                break
            time.sleep(0.05)
        self.assertTrue(is_port_free(held))


class _StubBackend:
    """A BackendProcess whose child is a trivial python sleeper, so we exercise
    real start/restart/stop (process group + kill) without uvicorn/odysseus."""

    def __init__(self):
        from alice_shell.supervisor import BackendProcess

        self.bp = BackendProcess(port=0, log_path=None, local_token="fixed-token-123")
        # Replace start() with a stub that spawns a short sleeper in its own group.
        import os
        import subprocess

        def _start():
            self.bp._proc = subprocess.Popen(  # noqa: SLF001
                [sys.executable, "-c", "import time; time.sleep(30)"],
                start_new_session=(os.name == "posix"),
            )

        self.bp.start = _start  # type: ignore[method-assign]


class RestartTests(unittest.TestCase):
    def test_restart_relaunches_a_dead_child(self):
        sb = _StubBackend()
        bp = sb.bp
        bp.start()
        self.assertTrue(bp.is_alive())
        pid1 = bp._proc.pid  # noqa: SLF001

        # Simulate a crash: hard-stop the child.
        bp.stop(term_grace_s=2.0)
        self.assertFalse(bp.is_alive())

        # Restart should bring up a NEW child (different pid), same token.
        bp.restart()
        self.assertTrue(bp.is_alive())
        pid2 = bp._proc.pid  # noqa: SLF001
        self.assertNotEqual(pid1, pid2)
        self.assertEqual(bp.local_token, "fixed-token-123")

        bp.stop(term_grace_s=2.0)
        self.assertFalse(bp.is_alive())

    def test_token_is_stable_across_a_new_process_for_reclaim(self):
        """The boot-time port re-claim creates a NEW BackendProcess reusing the
        prior token (so the shell-injected cookie stays valid). Assert the token
        can be threaded through the constructor."""
        from alice_shell.supervisor import BackendProcess

        a = BackendProcess(port=0, local_token="tok-A")
        b = BackendProcess(port=1, local_token=a.local_token)
        self.assertEqual(a.local_token, b.local_token)
        self.assertEqual(b.local_token, "tok-A")


if __name__ == "__main__":
    unittest.main(verbosity=2)

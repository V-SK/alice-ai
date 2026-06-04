"""Claim an ephemeral loopback port (PLAN §2.2, R4).

odysseus's stock fixed port 7000 collides with macOS AirPlay; we instead bind
``127.0.0.1:0`` so the OS assigns a free port, then hand it to the backend via
env. Binding ephemerally also makes a leaked child process harmless.
"""

from __future__ import annotations

import socket


def claim_ephemeral_port() -> int:
    """Return a currently-free loopback TCP port.

    There is an unavoidable tiny race between closing this socket and the
    backend binding it; on loopback with an immediate hand-off this is
    effectively never hit, and the ``/healthz`` gate catches any failure.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]

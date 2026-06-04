"""Claim an ephemeral loopback port (PLAN §2.2, R4).

odysseus's stock fixed port 7000 collides with macOS AirPlay; we instead bind
``127.0.0.1:0`` so the OS assigns a free port, then hand it to the backend via
env. Binding ephemerally also makes a leaked child process harmless.
"""

from __future__ import annotations

import socket


def claim_ephemeral_port() -> int:
    """Return a currently-free loopback TCP port.

    Binding ``127.0.0.1:0`` lets the OS hand back a port it knows is free, so a
    conflict at claim time is effectively impossible. There is an unavoidable
    tiny race between closing this socket and the backend binding it; on loopback
    with an immediate hand-off this is effectively never hit, and the
    ``/healthz`` gate (plus :func:`is_port_free` re-claim on a failed boot)
    catches any failure (R4 / F6 — odysseus's old fixed port 7000 = macOS
    AirPlay; ephemeral side-steps that class of conflict entirely).
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def is_port_free(port: int) -> bool:
    """True iff ``127.0.0.1:<port>`` can be bound right now (nothing else holds
    it). Used to detect the rare race where the claimed port got taken before
    the backend bound it, so the shell can re-claim a fresh one (F6)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
            return True
        except OSError:
            return False

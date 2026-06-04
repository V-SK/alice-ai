"""Wait on the backend's ``GET /healthz`` before showing the window (PLAN §2.2).

stdlib-only (urllib) so the shell has no extra runtime dep beyond pywebview.
"""

from __future__ import annotations

import time
import urllib.error
import urllib.request


def base_url(port: int) -> str:
    return f"http://127.0.0.1:{port}"


def wait_for_health(
    port: int,
    *,
    timeout_s: float = 120.0,
    interval_s: float = 0.4,
    is_alive=None,
) -> bool:
    """Poll ``/healthz`` until it returns 200 or ``timeout_s`` elapses.

    ``is_alive`` is an optional callable; if it returns False (the child died),
    we stop early. Returns True on a healthy backend, False on timeout/death.
    """
    url = base_url(port) + "/healthz"
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if is_alive is not None and not is_alive():
            return False
        try:
            with urllib.request.urlopen(url, timeout=2.0) as resp:
                if resp.status == 200:
                    return True
        except (urllib.error.URLError, ConnectionError, OSError):
            pass
        time.sleep(interval_s)
    return False

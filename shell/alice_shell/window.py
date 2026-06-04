"""The PyWebView native window (PLAN §2.1–§2.2).

A thin wrapper around ``webview.create_window`` + ``webview.start``. The window
points at the backend's loopback chat URL; it carries zero business logic so a
PyWebView → Tauri swap is shell-only (PLAN §2.1). Brand/skin is M3 — odysseus's
default look is fine for the M1 spine.
"""

from __future__ import annotations

WINDOW_TITLE = "Alice"

# The single active window, kept so a watchdog thread can destroy it to unblock
# ``webview.start()`` on a quit signal / backend death.
_active_window = None


def open_chat_window(url: str, *, on_closed=None) -> None:
    """Create the chat WebView at ``url`` and run the GUI loop (blocks).

    ``on_closed`` (if given) is invoked when the window closes, so the caller
    can tear down the backend child.
    """
    global _active_window
    import webview  # imported lazily so headless/no-window verification needs no GUI

    _active_window = webview.create_window(
        WINDOW_TITLE,
        url=url,
        width=1100,
        height=760,
        min_size=(420, 560),
    )
    if on_closed is not None:
        _active_window.events.closed += on_closed
    # http_server=False: we serve our own loopback backend; don't spin pywebview's.
    webview.start()


def close_all_windows() -> None:
    """Destroy the active PyWebView window (dispatched to the GUI thread).

    Safe to call from a watchdog thread; used to unblock ``webview.start()`` on
    a quit signal or a backend death so the shell can exit + tear down cleanly.
    """
    win = _active_window
    if win is None:
        return
    try:
        win.destroy()
    except Exception:  # noqa: BLE001 — GUI may already be torn down
        pass

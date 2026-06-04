"""Entry point for the Alice AI native shell — ``python -m alice_shell``.

The double-click target (PLAN §2.2). Exactly two OS processes: this shell
(parent — owns the window + lifecycle) and the odysseus FastAPI backend (child
— uvicorn, inference in-process). The shell:

  1. claims an ephemeral loopback port (``bind(127.0.0.1, 0)``),
  2. spawns + supervises the backend (uvicorn, AUTH off + loopback, port via
     env) as a child in its own process group,
  3. waits on ``GET /healthz`` before showing any window,
  4. opens the chat WebView at the localhost URL (system WebView via PyWebView),
  5. kills the child process-group on quit.

``--no-window`` (or ``ALICE_SHELL_NO_WINDOW=1``) runs everything EXCEPT creating
the GUI window — it boots the backend, waits health, prints the URL, and stays
alive until Ctrl-C. This is the headless verification path (a GUI window can't
be driven by an automated check); the chat is then proven over the loopback
endpoint with curl.
"""

from __future__ import annotations

import atexit
import os
import signal
import sys
import threading
import time

from alice_shell import health, paths
from alice_shell.port import claim_ephemeral_port
from alice_shell.supervisor import BackendProcess


def _log(msg: str) -> None:
    print(f"[alice-shell] {msg}", file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    no_window = ("--no-window" in argv) or (
        os.getenv("ALICE_SHELL_NO_WINDOW", "").lower() in ("1", "true", "yes")
    )

    port = claim_ephemeral_port()
    log_path = paths.repo_root() / ".venv" / "alice-backend.log"
    backend = BackendProcess(port, log_path=log_path)

    _log(f"claimed loopback port {port}")
    _log(f"backend dir: {paths.backend_dir()}")
    _log(f"backend python: {paths.venv_python()}")
    _log(f"backend log: {log_path}")

    backend.start()
    _log(f"backend pid {backend._proc.pid if backend._proc else '?'} starting…")  # noqa: SLF001

    # Tear down the child on any exit path. ``atexit`` is the backstop that fires
    # even when ``webview.start()`` returns or the interpreter is exiting; the
    # signal handler + watchdog handle SIGTERM/SIGINT while the Cocoa/GTK event
    # loop owns the main thread (where Python signal handlers don't run).
    _stopped = threading.Event()
    _want_quit = threading.Event()

    def _shutdown(*_a) -> None:
        if _stopped.is_set():
            return
        _stopped.set()
        _log("shutting down backend…")
        backend.stop()
        _log("backend stopped.")

    atexit.register(_shutdown)

    # Signal delivery under a native GUI loop (macOS NSApp.run / GTK main) does
    # NOT reach a Python main-thread handler — the main thread is blocked in C.
    # ``signal.set_wakeup_fd`` writes the signal number to a socket from the
    # C-level handler (no Python main thread needed); a watchdog thread reads it.
    import socket as _socket

    _wake_r, _wake_w = _socket.socketpair()
    _wake_w.setblocking(False)
    _wake_r.setblocking(True)
    try:
        signal.set_wakeup_fd(_wake_w.fileno())
    except (ValueError, OSError):
        pass

    def _noop_handler(*_a) -> None:
        # The real work happens in the watchdog reading _wake_r; we still install
        # a Python handler so the default (terminate) action is replaced.
        _want_quit.set()

    for _sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(_sig, _noop_handler)
        except (ValueError, OSError):
            pass  # not in main thread / unsupported — best effort

    def _signal_watch() -> None:
        """Block on the wakeup socket; flag a quit when any signal arrives.

        Works even while a native GUI loop owns the main thread, because the
        bytes are written by the C-level wakeup handler, not a Python handler.
        """
        while not _stopped.is_set():
            try:
                data = _wake_r.recv(1)
            except OSError:
                return
            if data:
                _log("quit signal received.")
                _want_quit.set()
                return

    threading.Thread(target=_signal_watch, name="alice-signal-watch", daemon=True).start()

    try:
        ok = health.wait_for_health(port, timeout_s=120.0, is_alive=backend.is_alive)
        if not ok:
            rc = backend.returncode()
            _log(
                f"backend did NOT become healthy (alive={backend.is_alive()}, "
                f"returncode={rc}). See log: {log_path}"
            )
            _shutdown()
            return 1

        url = health.base_url(port)
        _log(f"backend HEALTHY at {url}/healthz")
        _log(f"chat URL: {url}/")

        if no_window:
            _log("no-window mode: backend up; press Ctrl-C to stop.")
            try:
                while backend.is_alive() and not _want_quit.is_set():
                    time.sleep(0.4)
            except KeyboardInterrupt:
                pass
            _log(f"backend exited (returncode={backend.returncode()}).")
            _shutdown()
            return 0

        # Open the native chat window (blocks until closed). A daemon watchdog
        # destroys the window when (a) a quit signal arrives or (b) the backend
        # dies, so neither leaves a zombie window or a stale backend.
        from alice_shell.window import close_all_windows, open_chat_window

        def _watchdog() -> None:
            while not _stopped.is_set():
                if _want_quit.is_set() or not backend.is_alive():
                    if not backend.is_alive():
                        _log("backend exited unexpectedly — closing window.")
                    close_all_windows()
                    return
                time.sleep(0.4)

        threading.Thread(target=_watchdog, name="alice-watchdog", daemon=True).start()

        _log("opening chat window…")
        open_chat_window(url, on_closed=_shutdown)
        _log("window closed.")
        _shutdown()
        return 0
    finally:
        _shutdown()


if __name__ == "__main__":
    raise SystemExit(main())

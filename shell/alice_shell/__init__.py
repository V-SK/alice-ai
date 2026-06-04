"""Alice AI native shell (PyWebView).

The double-click target (PLAN §2.1–§2.2): a thin, logic-free parent process that
claims an ephemeral loopback port, spawns + supervises the PyInstaller backend,
waits on ``GET /healthz``, opens the chat WebView, and kills the child process
group on quit.

M0 is scaffold only — the supervisor/port/health/window/tray modules and the
PyInstaller spec land in M1+ (PLAN §5). This package intentionally holds zero
business logic so a PyWebView -> Tauri swap is shell-only (PLAN §2.1).
"""

__all__: list[str] = []

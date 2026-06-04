"""Alice AI native shell (PyWebView).

The double-click target (PLAN §2.1–§2.2): a thin, logic-free parent process that
claims an ephemeral loopback port, spawns + supervises the odysseus FastAPI
backend (inference in-process), waits on ``GET /healthz``, opens the chat
WebView, and kills the child process group on quit.

This package holds zero business logic so a PyWebView -> Tauri swap is
shell-only (PLAN §2.1). Submodules: ``paths``, ``port``, ``health``,
``supervisor``, ``window``, and ``__main__`` (the orchestrator).
"""

__all__ = ["paths", "port", "health", "supervisor", "window"]

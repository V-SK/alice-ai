"""Entry point for the Alice AI native shell — ``python -m alice_shell``.

PLACEHOLDER (M0). The real shell (M1, PLAN §2.2) will:
  1. claim an ephemeral loopback port (``bind(127.0.0.1, 0)``),
  2. spawn + supervise the PyInstaller backend (uvicorn FastAPI, in-proc
     inference), passing the port via env,
  3. wait on ``GET /healthz`` before showing any window,
  4. open the chat WebView (system WebView via PyWebView),
  5. kill the child process-group / Job-Object on quit.

For now this only proves the package is importable + runnable.
"""

from __future__ import annotations

import sys


def main() -> int:
    print(
        "alice-ai shell: M0 scaffold placeholder. "
        "The PyWebView supervisor lands in M1 (see docs/PLAN.md §2.2, §5).",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

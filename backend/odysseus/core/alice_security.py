"""Alice Simple-mode security boundary (security + privacy hardening pass).

This module is the single source of truth for the Simple-mode posture that the
deep-security-audit (`docs/audit/deep-security-audit.md`) requires before the
NO-GO verdict flips to GO. It is imported by both the FastAPI app (router
gating + middleware) and the agent tool dispatcher, so the policy is defined
once and enforced identically server-side.

Why this exists (audit context):
  * CRIT-1 — odysseus gates the dangerous agent tools on
    ``owner_is_admin_or_single_user(owner)``, which **fails OPEN** when no users
    exist (Alice's shipping config). So every chat is an all-powerful admin and
    ``python``/``bash``/``read_file``/``write_file``/``api_call``/``app_api``/
    ``mcp__*`` are live. Simple mode must hard-block these at *dispatch*,
    treating the session as the LEAST-privileged — not depend on the owner gate.
  * HIGH-1 — the privileged routers (shell/cookbook/MCP/codex/vault/...) must
    not be *mounted* in Simple mode, so they 404 rather than 403.
  * CRIT-2 / HIGH-2 — the loopback API must reject DNS-rebind ``Host`` headers,
    cross-origin requests, and any request that does not carry the per-launch
    shared secret token (which only the legit served UI has). Advanced is a
    real server-side boundary, not a CSS class.

Env contract (set by ``shell/alice_shell/supervisor.py``):
  * ``ALICE_SIMPLE_MODE``    — "1" (default ON) ⇒ enforce the Simple boundary.
                               "0" only for an explicit Advanced/dev build.
  * ``ALICE_LOCAL_TOKEN``    — per-launch random secret the shell generates and
                               hands to the backend; the backend injects it into
                               the served UI (cookie) and requires it on the
                               state-changing/stream API routes.
  * ``ALICE_ADVANCED``       — "1" ⇒ Advanced build; gates the tool/router
                               re-enable. MUST also require an admin account to
                               exist (so odysseus's real non-admin model
                               engages) — see ``advanced_enabled()``.
"""

from __future__ import annotations

import os
from typing import Optional

# Tools that expose local code execution, the filesystem, the loopback admin
# API, or the MCP namespace. Blocked UNCONDITIONALLY in Simple mode at the
# dispatch layer (CRIT-1) — independent of the owner/admin gate.
#
# This is intentionally the union of odysseus's own ``NON_ADMIN_BLOCKED_TOOLS``
# code-exec subset plus the loopback/api tools the audit calls out. ``mcp__*``
# is matched by prefix in ``is_simple_mode_blocked_tool``.
SIMPLE_MODE_BLOCKED_TOOLS = frozenset({
    "python",
    "bash",
    "read_file",
    "write_file",
    "api_call",
    "app_api",
    # MCP management + the served MCP tools (mcp__* handled by prefix below).
    "manage_mcp",
    # The remaining odysseus admin/persistent-state + messaging tools that the
    # audit groups with the agent surface. Harmless to a plain chat (chat mode
    # passes tools=None), but blocked at dispatch as defense-in-depth so a
    # forced/attacker agent turn can't reach them either.
    "manage_endpoints",
    "manage_webhooks",
    "manage_tokens",
    "manage_settings",
    "serve_model",
    "serve_preset",
    "stop_served_model",
    "download_model",
    "cancel_download",
    "adopt_served_model",
    "send_email",
    "reply_to_email",
    "vault_search",
    "vault_get",
    "vault_unlock",
})


def simple_mode() -> bool:
    """True when the Simple-mode security boundary is active (default ON).

    Reads the env on every call so a test/dev process can flip it without an
    import-time freeze. Anything other than an explicit "0"/"false"/"no" is ON.
    """
    val = os.getenv("ALICE_SIMPLE_MODE", "1").strip().lower()
    return val not in ("0", "false", "no", "off", "")


def advanced_enabled() -> bool:
    """True only for a genuine Advanced build (HIGH-2).

    Advanced re-enables the tool/router/MCP surface, so it MUST be a real
    server-side decision — not a ``localStorage``/``?adv=1`` toggle. We require
    BOTH an explicit ``ALICE_ADVANCED=1`` env AND that an admin account exists,
    so odysseus's own non-admin authorization model is actually engaged (a
    password gates the dangerous surface). If auth isn't configured, Advanced
    stays OFF regardless of the env — fail closed.
    """
    if os.getenv("ALICE_ADVANCED", "0").strip().lower() not in ("1", "true", "yes", "on"):
        return False
    try:
        from core.auth import AuthManager

        return bool(AuthManager().is_configured)
    except Exception:
        return False


def simple_boundary_active() -> bool:
    """The Simple boundary is enforced unless we're a real Advanced build.

    This is what the tool dispatcher + router gating key off: Simple mode ON and
    NOT a genuine (admin-backed) Advanced build.
    """
    return simple_mode() and not advanced_enabled()


def is_simple_mode_blocked_tool(tool_name: Optional[str]) -> bool:
    """Return True if *tool_name* must be hard-blocked under the Simple boundary.

    Fails CLOSED for a non-string tool name (treated as blocked), matching
    ``tool_security.is_public_blocked_tool``. ``None``/"" = no tool to gate.
    """
    if not simple_boundary_active():
        return False
    if tool_name is None or tool_name == "":
        return False
    if not isinstance(tool_name, str):
        return True
    return tool_name in SIMPLE_MODE_BLOCKED_TOOLS or tool_name.startswith("mcp__")


def local_token() -> str:
    """The per-launch shared secret (empty string if unset).

    Empty ⇒ the anti-pivot token guard is disabled (e.g. a dev run with no
    shell). The shell always sets it, so the shipped app always enforces it.
    """
    return os.getenv("ALICE_LOCAL_TOKEN", "").strip()


# Cookie + header names for the per-launch anti-pivot token (CRIT-2 b).
LOCAL_TOKEN_COOKIE = "alice_local_token"
LOCAL_TOKEN_HEADER = "X-Alice-Local"

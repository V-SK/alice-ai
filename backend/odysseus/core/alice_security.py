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
    shared secret token (which only the legit served UI has). The "agent can run
    tools" capability is a real server-side boundary, not a CSS class.

The Agent-mode toggle (HIGH-2 resolution — informed consent, not an admin gate)
---------------------------------------------------------------------------------
The app ships in the safe chat-only **Simple mode** (default). The full odysseus
agent framework (code-exec / file / tool / MCP) is a **user-controlled toggle,
DEFAULT OFF**, that only turns ON after the user reads an explicit RISK warning
and confirms. This replaces the older "Advanced requires an admin account" gate:
the owner has decided the right boundary for a local single-user desktop app is
**the user's informed consent**, persisted server-side, not a password.

CRITICAL — what the toggle does and does NOT control:
  * The toggle ONLY controls the **agent-tools-at-dispatch** block
    (``SIMPLE_MODE_BLOCKED_TOOLS``) and whether the **agent/MCP/tool routers**
    are mounted + the forced chat mode. These are the "the model can run tools
    on your machine" capability — the user's informed choice.
  * The toggle does NOT control the **network-attack-surface protections**:
    the per-launch ``ALICE_LOCAL_TOKEN`` requirement, ``TrustedHostMiddleware``,
    and the Origin/``Sec-Fetch-Site`` guard. Those defend against EXTERNAL
    attackers (browser-pivot / DNS-rebind) which the user did NOT opt into, so
    they are **ALWAYS ON** whenever ``simple_mode()`` is true — independent of
    the toggle. Even with Agent mode ON, a malicious web page still cannot drive
    the tools because it can't obtain the per-launch token.

The choice is **server-side persisted** (a real boundary, not CSS): a JSON flag
in the app's writable data dir that the backend reads on every gate check, so it
survives a restart and the backend — not the client — decides whether the agent
surface is live.

Env contract (set by ``shell/alice_shell/supervisor.py``):
  * ``ALICE_SIMPLE_MODE``    — "1" (default ON) ⇒ enforce the network boundary
                               (token + Host + Origin) ALWAYS. "0" only for an
                               explicit dev build with no shell.
  * ``ALICE_LOCAL_TOKEN``    — per-launch random secret the shell generates and
                               hands to the backend; the backend injects it into
                               the served UI (cookie) and requires it on the
                               state-changing/stream API routes.
  * ``ALICE_AGENT_MODE``     — optional hard override for the persisted toggle:
                               "1"/"on" force Agent mode ON, "0"/"off" force it
                               OFF (ignoring the persisted flag). Unset ⇒ the
                               persisted user choice decides. Lets CI/dev pin a
                               state without touching the on-disk flag.
  * ``ALICE_AGENT_MODE_LOCKED`` — "1" ⇒ refuse to ever enable Agent mode
                               (a kill-switch for a locked-down deployment); the
                               toggle endpoint returns it as ``locked: true``.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger("alice_security")

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


# --------------------------------------------------------------------------- #
# Server-side persistence of the Agent-mode toggle.
# --------------------------------------------------------------------------- #
# A tiny JSON flag in the app's writable data dir. The backend is the authority:
# the client asks to flip it (with the risk acknowledged), the backend persists
# it, and every gate check reads it. This is the "real boundary, not CSS" the
# audit (HIGH-2) requires — a foreign page can't change what the server reads.

_AGENT_FLAG_FILENAME = "agent_mode.json"
_flag_lock = threading.RLock()
# Process-level cache so the gate (hit on every tool dispatch / request) doesn't
# stat+read the file constantly. Invalidated whenever WE write it; the file is
# only written by this process via set_agent_mode(), so the cache is coherent.
_flag_cache: Optional[bool] = None
_flag_cache_path: Optional[str] = None


def _data_dir() -> Path:
    """The app's writable data dir (OS-native, set by the shell supervisor).

    Mirrors the rest of the odysseus tree: ``ALICE_AI_DATA_DIR`` wins (the shell
    points it at ~/.alice/ai-data on mac/Linux, %LOCALAPPDATA%\\Alice on Win);
    otherwise fall back to the backend-local ``./data`` (dev/tests run from
    backend/odysseus/, where the sqlite app.db already lives).
    """
    d = os.getenv("ALICE_AI_DATA_DIR", "").strip()
    if d:
        return Path(d)
    return Path("data")


def _flag_path() -> Path:
    return _data_dir() / _AGENT_FLAG_FILENAME


def _read_persisted_flag() -> bool:
    """Read the persisted Agent-mode flag (False if absent/unreadable).

    Fails CLOSED: any missing file, parse error, or unexpected shape ⇒ OFF
    (the safe chat-only mode). The flag is only ever True if a confirmed toggle
    wrote ``{"agent_mode": true, "risk_acknowledged": true}``.
    """
    global _flag_cache, _flag_cache_path
    p = _flag_path()
    sp = str(p)
    with _flag_lock:
        if _flag_cache is not None and _flag_cache_path == sp:
            return _flag_cache
        value = False
        try:
            raw = p.read_text(encoding="utf-8")
            data = json.loads(raw)
            # Require BOTH the on flag and the explicit risk acknowledgement so a
            # stray/partial file can never silently enable the agent surface.
            value = bool(data.get("agent_mode")) and bool(data.get("risk_acknowledged"))
        except FileNotFoundError:
            value = False
        except Exception as exc:  # noqa: BLE001
            logger.warning("agent_mode flag unreadable (%s) — defaulting OFF: %s",
                           sp, exc)
            value = False
        _flag_cache = value
        _flag_cache_path = sp
        return value


def _invalidate_flag_cache() -> None:
    global _flag_cache, _flag_cache_path
    with _flag_lock:
        _flag_cache = None
        _flag_cache_path = None


def agent_mode_locked() -> bool:
    """True if Agent mode is hard-locked OFF for this deployment (kill-switch).

    A locked deployment can never enable the agent surface regardless of the
    user toggle (e.g. a managed/enterprise build). Default unlocked.
    """
    return os.getenv("ALICE_AGENT_MODE_LOCKED", "0").strip().lower() in ("1", "true", "yes", "on")


def agent_mode_enabled() -> bool:
    """True when the powerful Agent mode is ON (informed-consent toggle).

    Resolution order (the backend is the authority, fail-closed):
      1. If locked OFF (``ALICE_AGENT_MODE_LOCKED``) ⇒ always False.
      2. If ``ALICE_AGENT_MODE`` is set ⇒ that explicit override wins (lets
         CI/dev pin a state without touching the on-disk flag).
      3. Otherwise ⇒ the server-side persisted user choice (default OFF).

    When this is False the dangerous agent/tool/MCP/shell surface is blocked at
    dispatch + unmounted (via ``simple_boundary_active``); when True the full
    odysseus agent framework is live. This NEVER affects the network guards.
    """
    if agent_mode_locked():
        return False
    override = os.getenv("ALICE_AGENT_MODE")
    if override is not None:
        return override.strip().lower() in ("1", "true", "yes", "on")
    return _read_persisted_flag()


def set_agent_mode(enabled: bool, *, risk_acknowledged: bool) -> bool:
    """Persist the Agent-mode toggle server-side. Returns the new effective state.

    Turning it ON REQUIRES ``risk_acknowledged=True`` (the explicit user confirm
    from the risk-warning modal) — a request without it cannot enable the agent
    surface. Turning it OFF always succeeds (the easy "turn it back off" path).
    A locked deployment refuses to enable (returns the locked-off state).

    The flag is written atomically (temp + ``os.replace``) so a crashed write
    never leaves a half-file that the fail-closed reader would treat as OFF
    anyway. Invalidates the process cache so the next gate check sees the change.
    """
    enabled = bool(enabled)
    if enabled and agent_mode_locked():
        logger.warning("set_agent_mode(on) refused — Agent mode is locked OFF")
        return False
    if enabled and not risk_acknowledged:
        # Defense in depth: the route already requires the ack, but never let an
        # un-acknowledged call flip the surface on.
        logger.warning("set_agent_mode(on) refused — risk not acknowledged")
        return False

    p = _flag_path()
    payload = {
        "agent_mode": enabled,
        # Persist the acknowledgement alongside so the reader's both-true gate is
        # satisfied; turning OFF clears it (no lingering "acknowledged" state).
        "risk_acknowledged": bool(enabled and risk_acknowledged),
        "version": 1,
    }
    with _flag_lock:
        try:
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, p)
        except Exception as exc:  # noqa: BLE001
            logger.error("failed to persist agent_mode flag at %s: %s", p, exc)
            raise
        _invalidate_flag_cache()
    logger.info("Agent mode %s (persisted at %s)",
                "ENABLED" if enabled else "disabled", p)
    return enabled


def simple_mode() -> bool:
    """True when the Simple-mode NETWORK boundary is active (default ON).

    This gates the ALWAYS-ON external-attack protections (TrustedHost +
    per-launch local token + Origin/Sec-Fetch guard). It is INDEPENDENT of the
    Agent-mode toggle: those middlewares defend against browser-pivot /
    DNS-rebind, which the user never opted into, so they stay on even when the
    user has turned Agent mode ON.

    Reads the env on every call so a test/dev process can flip it without an
    import-time freeze. Anything other than an explicit "0"/"false"/"no" is ON.
    """
    val = os.getenv("ALICE_SIMPLE_MODE", "1").strip().lower()
    return val not in ("0", "false", "no", "off", "")


# Backwards-compatible alias. The old name meant "a genuine Advanced build
# (admin-gated)"; the new model is the informed-consent Agent-mode toggle. Kept
# so any external caller of ``advanced_enabled()`` keeps working with the new
# semantics (the codebase itself now calls ``agent_mode_enabled``).
def advanced_enabled() -> bool:
    """Deprecated alias for :func:`agent_mode_enabled` (HIGH-2 resolution).

    The Advanced/agent surface is now gated by the user's risk-acknowledged
    toggle, not an admin account.
    """
    return agent_mode_enabled()


def simple_boundary_active() -> bool:
    """The Simple (chat-only) boundary is enforced unless Agent mode is ON.

    This is what the tool dispatcher + router gating + forced-chat-mode key off:
    the network boundary is active AND the user has NOT turned on the powerful
    Agent mode. When the user confirms Agent mode, this flips False and the full
    agent/tool/MCP surface is un-gated — while the network guards (keyed off
    ``simple_mode`` alone) stay ON.
    """
    return simple_mode() and not agent_mode_enabled()


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


# --------------------------------------------------------------------------- #
# Import-time mount snapshot.
# --------------------------------------------------------------------------- #
# The privileged HTTP routers (shell/cookbook/MCP/codex/vault) + the MCP server
# autostart are decided ONCE at app import (you can't un-mount a FastAPI router
# at runtime). The per-turn chat agent tools (python/bash/read_file/...) DO flip
# live with the toggle, because the dispatcher + agent loop read
# ``simple_boundary_active()`` on every call. So when the user turns Agent mode
# ON in a running process, the chat agent tools work immediately, but the
# heavier MCP/privileged-router surface only mounts on the NEXT launch. app.py
# records the import-time decision here so the /alice/mode payload can honestly
# tell the UI when a restart is needed to apply the *full* surface.
_PRIVILEGED_MOUNTED_AT_STARTUP: Optional[bool] = None


def record_privileged_mounted(mounted: bool) -> None:
    """Called once by app.py with the import-time privileged-router decision."""
    global _PRIVILEGED_MOUNTED_AT_STARTUP
    _PRIVILEGED_MOUNTED_AT_STARTUP = bool(mounted)


def restart_required_for_full_agent() -> bool:
    """True if Agent mode is ON now but the privileged surface wasn't mounted at
    startup (so the chat agent tools are live, but MCP/shell routers need a
    relaunch to fully apply). False when the two already agree, or unknown."""
    if _PRIVILEGED_MOUNTED_AT_STARTUP is None:
        return False
    return agent_mode_enabled() and not _PRIVILEGED_MOUNTED_AT_STARTUP

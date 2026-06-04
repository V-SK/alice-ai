"""Simple-mode security boundary — offline unit tests (stdlib unittest).

Locks the deep-security-audit fixes so they can't silently regress:

  * CRIT-1 — the dangerous tools (python/bash/read_file/write_file/api_call/
    app_api/mcp__*) are HARD-BLOCKED at dispatch by default (Agent mode OFF),
    regardless of the (open) owner gate; and LIVE when Agent mode is ON.
  * HIGH-2 — the agent surface is gated by a risk-acknowledged, server-persisted
    USER TOGGLE (not an admin account): turning it on requires the explicit
    risk ack, the choice persists, and it un-gates the tools — while the
    ALWAYS-ON network guards (token/Host/Origin) do NOT depend on the toggle.
  * the policy fails CLOSED for the code-exec set on a malformed tool name.

Run (from backend/odysseus/, so the sqlite ./data/app.db CWD resolves):
  cd backend/odysseus && AUTH_ENABLED=false PYTHONPATH=$(cd ../ && pwd) \
      ../../.venv/bin/python -m unittest tests.test_simple_mode_security -v
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

_ODY = Path(__file__).resolve().parents[1] / "odysseus"
import sys

if str(_ODY) not in sys.path:
    sys.path.insert(0, str(_ODY))


def _set_env(**kw):
    for k, v in kw.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


def _fresh_data_dir():
    """A throwaway ALICE_AI_DATA_DIR (no persisted flag) + drop the cache, so a
    test's default is always Agent-mode OFF regardless of the dev box state."""
    d = tempfile.mkdtemp(prefix="alice-agent-test-")
    os.environ["ALICE_AI_DATA_DIR"] = d
    from core import alice_security as s
    s._invalidate_flag_cache()
    return d


class SimpleModePolicyTests(unittest.TestCase):
    def setUp(self):
        # Default Simple posture; Agent mode OFF (no persisted flag, no override).
        _set_env(ALICE_SIMPLE_MODE="1", ALICE_AGENT_MODE=None,
                 ALICE_AGENT_MODE_LOCKED=None, AUTH_ENABLED="false")
        _fresh_data_dir()

    def test_boundary_active_by_default(self):
        from core import alice_security as s
        self.assertTrue(s.simple_mode())
        self.assertFalse(s.agent_mode_enabled())     # default OFF
        self.assertTrue(s.simple_boundary_active())

    def test_dangerous_tools_flagged_blocked(self):
        from core import alice_security as s
        for t in ("python", "bash", "read_file", "write_file", "api_call",
                  "app_api", "manage_mcp", "mcp__rag__search"):
            self.assertTrue(s.is_simple_mode_blocked_tool(t), t)

    def test_benign_tools_not_flagged(self):
        from core import alice_security as s
        # web_search is gated by the chat toggle, NOT the Simple hard-block;
        # plain create_document etc. stay available.
        for t in ("web_search", "create_document", "manage_notes", "manage_calendar"):
            self.assertFalse(s.is_simple_mode_blocked_tool(t), t)

    def test_malformed_tool_name_fails_closed(self):
        from core import alice_security as s
        self.assertTrue(s.is_simple_mode_blocked_tool(123))  # non-string → blocked
        # empty/None means "no tool", not a block
        self.assertFalse(s.is_simple_mode_blocked_tool(None))
        self.assertFalse(s.is_simple_mode_blocked_tool(""))

    def test_default_off_blocks_tools(self):
        """The shipping default (no toggle confirmed) hard-blocks the agent
        surface — this is the safe chat-only Simple mode."""
        from core import alice_security as s
        self.assertFalse(s.agent_mode_enabled())
        self.assertTrue(s.simple_boundary_active())
        for t in ("python", "bash", "read_file", "write_file", "mcp__x__y"):
            self.assertTrue(s.is_simple_mode_blocked_tool(t), t)

    def test_toggle_on_requires_confirm_and_persists(self):
        """Turning Agent mode ON without the risk ack is refused; with the ack it
        persists server-side (survives a 'restart' = a cache drop)."""
        from core import alice_security as s
        d = os.environ["ALICE_AI_DATA_DIR"]
        # no-ack enable refused
        self.assertFalse(s.set_agent_mode(True, risk_acknowledged=False))
        self.assertFalse(s.agent_mode_enabled())
        self.assertFalse((Path(d) / "agent_mode.json").exists())
        # acknowledged enable persists
        self.assertTrue(s.set_agent_mode(True, risk_acknowledged=True))
        self.assertTrue(s.agent_mode_enabled())
        self.assertTrue((Path(d) / "agent_mode.json").exists())
        # a fresh read (simulated restart) still sees it ON
        s._invalidate_flag_cache()
        self.assertTrue(s.agent_mode_enabled())
        # turning it back off is easy (no ack needed) + clears the flag
        self.assertFalse(s.set_agent_mode(False, risk_acknowledged=False))
        s._invalidate_flag_cache()
        self.assertFalse(s.agent_mode_enabled())

    def test_toggle_on_unblocks_tools(self):
        """With Agent mode confirmed ON, the boundary lifts + tools un-gate."""
        from core import alice_security as s
        s.set_agent_mode(True, risk_acknowledged=True)
        self.assertFalse(s.simple_boundary_active())
        self.assertFalse(s.is_simple_mode_blocked_tool("python"))

    def test_network_guard_independent_of_toggle(self):
        """KEY SAFETY PROPERTY: simple_mode() (the always-on token/Host/Origin
        guard) is independent of the Agent-mode toggle — ON or OFF, it's ON."""
        from core import alice_security as s
        self.assertTrue(s.simple_mode())          # toggle OFF
        s.set_agent_mode(True, risk_acknowledged=True)
        self.assertTrue(s.agent_mode_enabled())
        self.assertTrue(s.simple_mode())          # STILL on with toggle ON

    def test_locked_deployment_cannot_enable(self):
        from core import alice_security as s
        _set_env(ALICE_AGENT_MODE_LOCKED="1")
        try:
            self.assertTrue(s.agent_mode_locked())
            self.assertFalse(s.set_agent_mode(True, risk_acknowledged=True))
            self.assertFalse(s.agent_mode_enabled())
            self.assertTrue(s.simple_boundary_active())
        finally:
            _set_env(ALICE_AGENT_MODE_LOCKED=None)

    def test_env_override_pins_state(self):
        """ALICE_AGENT_MODE pins the state without touching the on-disk flag
        (used by CI/dev to force a posture)."""
        from core import alice_security as s
        _set_env(ALICE_AGENT_MODE="1")
        try:
            self.assertTrue(s.agent_mode_enabled())
            self.assertFalse(s.simple_boundary_active())
        finally:
            _set_env(ALICE_AGENT_MODE="0")
            self.assertFalse(s.agent_mode_enabled())
            _set_env(ALICE_AGENT_MODE=None)

    def test_boundary_off_when_simple_disabled(self):
        from core import alice_security as s
        _set_env(ALICE_SIMPLE_MODE="0")
        try:
            self.assertFalse(s.simple_boundary_active())
            self.assertFalse(s.is_simple_mode_blocked_tool("python"))
        finally:
            _set_env(ALICE_SIMPLE_MODE="1")


class DispatchBlockTests(unittest.TestCase):
    """The dispatcher refuses the dangerous tools when Agent mode is OFF (even
    with the open owner gate, owner=None), and runs them when Agent mode is ON.
    Simple mode (the network guard) stays ON throughout — only the Agent-mode
    toggle moves, proving the toggle alone controls the tool boundary."""

    def _dispatch(self, tool, content, *, agent_on):
        # Network boundary always ON; the Agent-mode env override pins the toggle.
        _set_env(ALICE_SIMPLE_MODE="1", ALICE_AGENT_MODE="1" if agent_on else "0",
                 ALICE_ADVANCED=None, AUTH_ENABLED="false")
        from core import alice_security as s
        s._invalidate_flag_cache()
        from src.tool_execution import execute_tool_block
        block = SimpleNamespace(tool_type=tool, content=content)
        return asyncio.run(execute_tool_block(block, session_id="t", owner=None))

    def tearDown(self):
        _set_env(ALICE_AGENT_MODE=None)

    def test_python_blocked_when_agent_off(self):
        desc, res = self._dispatch("python", "print(2+2)", agent_on=False)
        self.assertEqual(res.get("exit_code"), 1)
        self.assertIn("disabled in Simple mode", res.get("error", ""))

    def test_write_file_blocked_and_no_side_effect(self):
        target = "/tmp/alice_simplemode_pwn_test.txt"
        try:
            os.remove(target)
        except OSError:
            pass
        desc, res = self._dispatch("write_file", f"{target}\nowned", agent_on=False)
        self.assertEqual(res.get("exit_code"), 1)
        self.assertFalse(os.path.exists(target), "write_file executed despite block")

    def test_mcp_namespaced_blocked_when_agent_off(self):
        desc, res = self._dispatch("mcp__email__send", "x", agent_on=False)
        self.assertEqual(res.get("exit_code"), 1)

    def test_python_runs_when_agent_on(self):
        desc, res = self._dispatch("python", "print(2+2)", agent_on=True)
        # owner=None → open single-user gate → tool runs once Agent mode is on.
        self.assertEqual(res.get("exit_code"), 0)
        self.assertIn("4", res.get("output", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)

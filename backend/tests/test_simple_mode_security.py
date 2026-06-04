"""Simple-mode security boundary — offline unit tests (stdlib unittest).

Locks the deep-security-audit fixes so they can't silently regress:

  * CRIT-1 — the dangerous tools (python/bash/read_file/write_file/api_call/
    app_api/mcp__*) are HARD-BLOCKED at dispatch in Simple mode, regardless of
    the (open) owner gate; and LIVE again when the Simple boundary is off.
  * HIGH-2 — Advanced requires BOTH ALICE_ADVANCED=1 AND an admin account;
    ALICE_ADVANCED=1 alone (no admin) does NOT lift the boundary.
  * the policy fails CLOSED for the code-exec set on a malformed tool name.

Run (from backend/odysseus/, so the sqlite ./data/app.db CWD resolves):
  cd backend/odysseus && AUTH_ENABLED=false PYTHONPATH=$(cd ../ && pwd) \
      ../../.venv/bin/python -m unittest tests.test_simple_mode_security -v
"""

from __future__ import annotations

import asyncio
import os
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


class SimpleModePolicyTests(unittest.TestCase):
    def setUp(self):
        # Default Simple posture; no admin account, no Advanced.
        _set_env(ALICE_SIMPLE_MODE="1", ALICE_ADVANCED="0", AUTH_ENABLED="false")

    def test_boundary_active_by_default(self):
        from core import alice_security as s
        self.assertTrue(s.simple_mode())
        self.assertFalse(s.advanced_enabled())
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

    def test_advanced_needs_admin_account(self):
        from core import alice_security as s
        # ALICE_ADVANCED=1 but auth NOT configured (no admin) → stays Simple.
        _set_env(ALICE_ADVANCED="1")
        self.assertFalse(s.advanced_enabled())
        self.assertTrue(s.simple_boundary_active())

    def test_boundary_off_when_simple_disabled(self):
        from core import alice_security as s
        _set_env(ALICE_SIMPLE_MODE="0")
        self.assertFalse(s.simple_boundary_active())
        self.assertFalse(s.is_simple_mode_blocked_tool("python"))


class DispatchBlockTests(unittest.TestCase):
    """The dispatcher refuses the dangerous tools in Simple mode even with the
    open owner gate (owner=None), and runs them when the boundary is off."""

    def _dispatch(self, tool, content, *, simple):
        _set_env(ALICE_SIMPLE_MODE="1" if simple else "0",
                 ALICE_ADVANCED="0", AUTH_ENABLED="false")
        from src.tool_execution import execute_tool_block
        block = SimpleNamespace(tool_type=tool, content=content)
        return asyncio.run(execute_tool_block(block, session_id="t", owner=None))

    def test_python_blocked_in_simple(self):
        desc, res = self._dispatch("python", "print(2+2)", simple=True)
        self.assertEqual(res.get("exit_code"), 1)
        self.assertIn("disabled in Simple mode", res.get("error", ""))

    def test_write_file_blocked_and_no_side_effect(self):
        target = "/tmp/alice_simplemode_pwn_test.txt"
        try:
            os.remove(target)
        except OSError:
            pass
        desc, res = self._dispatch("write_file", f"{target}\nowned", simple=True)
        self.assertEqual(res.get("exit_code"), 1)
        self.assertFalse(os.path.exists(target), "write_file executed despite block")

    def test_mcp_namespaced_blocked_in_simple(self):
        desc, res = self._dispatch("mcp__email__send", "x", simple=True)
        self.assertEqual(res.get("exit_code"), 1)

    def test_python_runs_when_boundary_off(self):
        desc, res = self._dispatch("python", "print(2+2)", simple=False)
        # owner=None → open single-user gate → tool runs in Advanced.
        self.assertEqual(res.get("exit_code"), 0)
        self.assertIn("4", res.get("output", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)

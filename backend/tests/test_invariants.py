"""Alice AI — cross-cutting invariant GATES (M8).

The three ship-blocking invariants from the two audits, locked as offline tests
so a regression FAILS the suite / CI (PLAN §M8 + R7/R8). These are deliberately
STRUCTURAL (no live server, no network) so they're fast, deterministic, and
catch the regression at the source — not just at probe time.

  1. PRIVACY (privacy-audit) — a chat generation makes NO network call:
       * the served UI's two CDN libs (KaTeX/Mermaid) are vendored, not jsdelivr;
       * the inference glue (alice_provider) imports no socket/http/requests and
         stamps the honest ``alice_local`` block (network_calls_made:false …);
       * the only egress in our code is the opt-in HF model download.
  2. HONESTY (privacy + earn audits) — across the WHOLE served Alice UI:
       * model names are Alice-only (no qwen / GGUF / MLX / param-count leak);
       * no ``$``/fiat/credit-as-cash anywhere; the credit-only "待发放" framing
         is present; ``paid_acu`` is "0"; no "no logging" overclaim.
  3. SECURITY (deep-security-audit) — the Simple-mode boundary policy holds:
       * dangerous tools are blocked by default (Agent mode OFF), and Agent mode
         only turns ON via a risk-acknowledged, server-persisted toggle
         (re-asserted here so the policy can't silently weaken);
       * the ALWAYS-ON network guards (TrustedHost + per-launch local token +
         Origin guard) do NOT depend on the Agent-mode toggle;
       * the app source wires those middlewares + gates the privileged routers
         behind the Simple boundary.

A companion ``scripts/security_probe.py`` re-checks 1+3 LIVE against a running
instance (28 checks); this file is the build-time gate that runs without one.

Run (from backend/, so alice_ai + the odysseus tree import):
  cd backend && PYTHONPATH="$(pwd):$(pwd)/odysseus" \
      ../.venv/bin/python -m pytest tests/test_invariants.py -q
"""

from __future__ import annotations

import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1]
_ODY = _BACKEND / "odysseus"
_STATIC = _ODY / "static"
_ALICE = _STATIC / "alice"
for _p in (str(_BACKEND), str(_ODY)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

# Point odysseus's SQLite + auth/data at a throwaway dir BEFORE any core.* import
# (core.database reads DATABASE_URL at import). This makes the SecurityInvariant
# tests (which touch AuthManager via alice_security.advanced_enabled) CWD-robust,
# so the whole suite runs from backend/ under one pytest invocation.
_TMP = tempfile.mkdtemp(prefix="alice-invariant-")
os.environ.setdefault("DATABASE_URL", f"sqlite:///{_TMP}/app.db")
os.environ.setdefault("ALICE_AI_DATA_DIR", _TMP)


def _read(p: Path) -> str:
    return p.read_text(encoding="utf-8", errors="replace")


def _strip_comments_js(src: str) -> str:
    """Drop /* block */ + // line comments so an honesty scan sees only live
    code/strings (the source intentionally NAMES forbidden tokens in comments to
    forbid them — those must not trip the guard)."""
    src = re.sub(r"/\*[\s\S]*?\*/", "", src)
    src = re.sub(r"(?m)//.*$", "", src)
    return src


def _strip_comments_py(src: str) -> str:
    return re.sub(r"(?m)#.*$", "", src)


# =========================================================================== #
# 1. PRIVACY — chat generation is on-device, zero network.
# =========================================================================== #
class PrivacyInvariant(unittest.TestCase):
    def test_no_cdn_libs_in_served_html(self):
        """The served index.html must reference NO CDN — every script/style it
        loads is local under /static (privacy-P1). The lean chat UI ships its
        own /static/lean/* bundle + one vendored lib (highlight.js); it renders
        neither LaTeX nor diagrams, so it pulls no KaTeX/Mermaid/CDN at all.
        (The old odysseus frontend vendored KaTeX/Mermaid; the lean rewrite
        dropped them, so this asserts the *general* no-CDN invariant instead.)"""
        html = _read(_STATIC / "index.html")
        self.assertNotIn("jsdelivr", html, "a jsdelivr CDN ref leaked back into index.html")
        self.assertNotIn("cdnjs", html)
        self.assertNotIn("unpkg", html)
        # The lean frontend loads its own controller locally (no ES-module/CDN graph).
        self.assertIn("/static/lean/alice-lean.js", html)
        # Every fetched <script src>/<link href> must be a LOCAL path, never an
        # external origin (the inline data: SVG favicon and in-page #anchors are
        # not network fetches). This is the real no-CDN privacy guarantee.
        for _m in re.finditer(r'(?:src|href)\s*=\s*"([^"]+)"', html):
            url = _m.group(1)
            if url.startswith("data:") or url.startswith("#"):
                continue
            self.assertFalse(
                url.startswith("http://") or url.startswith("https://"),
                f"served HTML fetches an external URL (privacy-P1): {url}",
            )

    def test_csp_is_self_only(self):
        """The Simple-mode CSP must be 'self'-only for scripts (no CDN), so any
        future CDN regression fails closed at the browser (privacy-P1)."""
        mw = _read(_ODY / "core" / "middleware.py")
        # The hardened branch builds script-src 'self' with a nonce and NO jsdelivr.
        self.assertIn("script-src 'self'", mw)
        # jsdelivr must not appear in any CSP directive.
        for line in mw.splitlines():
            if "script-src" in line or "style-src" in line or "connect-src" in line or "font-src" in line:
                self.assertNotIn("jsdelivr", line, f"CSP directive still allows jsdelivr: {line.strip()}")

    def test_inference_glue_imports_no_network(self):
        """alice_provider (the in-proc chat path) must not import a network
        client — the generation path provably has no socket primitive, which is
        WHY ``network_calls_made:false`` is honest (privacy-audit §1)."""
        src = _strip_comments_py(_read(_ODY / "alice_provider.py"))
        for bad in ("import requests", "import httpx", "import aiohttp",
                    "import socket", "urllib.request", "http.client",
                    "websocket", "import grpc"):
            self.assertNotIn(bad, src, f"chat glue imports a network client: {bad!r}")

    def test_alice_local_privacy_block_present_and_honest(self):
        """The non-stream chat body must stamp the honest privacy block on BOTH
        the real-engine and dev-override paths (network_calls_made:false,
        credit_ledger_touched:false, side_channel_used:false, paid_acu:'0')."""
        src = _read(_ODY / "alice_provider.py")
        # Both occurrences (real backend + dev override).
        self.assertGreaterEqual(src.count('"network_calls_made": False'), 2)
        self.assertGreaterEqual(src.count('"credit_ledger_touched": False'), 2)
        self.assertGreaterEqual(src.count('"side_channel_used": False'), 2)
        self.assertGreaterEqual(src.count('"paid_acu": "0"'), 2)
        # And NO honest field is ever set to a truthy/networked value.
        self.assertNotIn('"network_calls_made": True', src)
        self.assertNotIn('"credit_ledger_touched": True', src)

    def test_only_egress_in_our_code_is_hf_download(self):
        """Our own packages (alice_ai/, the shell) must contain no outbound
        network call other than the opt-in HF model download + the loopback
        health/port primitives (privacy-audit §2 'our wiring')."""
        # The model_manager downloader is the ONLY place huggingface_hub is used.
        dl = _read(_BACKEND / "alice_ai" / "model_manager" / "downloader.py")
        self.assertIn("huggingface_hub", dl)  # the allowed egress lives here
        # No HF endpoint override / TLS-disable anywhere under alice_ai/.
        for py in (_BACKEND / "alice_ai").rglob("*.py"):
            body = _read(py)
            self.assertNotIn("HF_ENDPOINT", body, f"HF endpoint override in {py}")
            self.assertNotIn("verify=False", body, f"TLS verify disabled in {py}")
            self.assertNotIn("http://", body.replace("http://127.0.0.1", "").replace("http://localhost", ""),
                             f"plain-http egress in {py}")


# =========================================================================== #
# 2. HONESTY — the whole served Alice UI: Alice-only names, no $/fiat, 待发放.
# =========================================================================== #
class HonestyInvariant(unittest.TestCase):
    # The five Alice display tiers are the ONLY model names the UI may show.
    ALLOWED_NAMES = {"Alice", "Alice Lite", "Alice Pro", "Alice RP"}
    # Tokens that must NEVER appear in served UI strings (param-count / engine).
    LEAK_TOKENS = ["qwen", "qwopus", "bluestar", "safetensors", ".gguf", ".mlx",
                   "llama.cpp", "mlx_lm", "0.6b", "1.7b", "4b", "9b", "27b",
                   "35b", "parameter", "param-count", "billion param"]
    # Currency / cash-as-credit forbiddens (the credit-only contract, R8).
    CASH_TOKENS = ["income", "profit", "make money", "cash out", "usd", "fiat",
                   "per hour", "/hour", "payout in", "cash reward", "earnings of"]

    def _alice_ui_blob(self) -> str:
        """Every Alice-authored served UI file, comments stripped, lowercased.

        We scan the i18n table, the skin, the model-picker client, the earn
        surface, and the new failure module — the strings a 小白 can actually
        see. odysseus's own deep files are out of scope (Advanced surface)."""
        parts = []
        for name in ("alice-i18n.js", "alice-skin.js", "alice-models.js",
                     "alice-earn.js", "alice-failure.js"):
            p = _ALICE / name
            if p.exists():
                parts.append(_strip_comments_js(_read(p)))
        return "\n".join(parts).lower()

    def test_no_engine_or_paramcount_leak_in_ui(self):
        blob = self._alice_ui_blob()
        for tok in self.LEAK_TOKENS:
            self.assertNotIn(tok, blob, f"engine/param leak token {tok!r} in served Alice UI")

    def test_no_cash_or_fiat_in_ui(self):
        blob = self._alice_ui_blob()
        for tok in self.CASH_TOKENS:
            self.assertNotIn(tok, blob, f"cash/fiat token {tok!r} in served Alice UI")
        # No CURRENCY `$` (a `$` next to a digit or money word). Bare `$` in code
        # (template literals / regex) is fine.
        self.assertNotRegex(blob, r"(?:us)?\$\s*\d|\d\s*\$|\bus\$", "currency $ in served Alice UI")

    def test_credit_only_pending_framing_present(self):
        """The credit-only 待发放 / pending framing must be present in the served
        UI (not silently dropped) — earn rewards are shown pending-until-
        distributed — and the chat path stamps paid_acu='0' server-side."""
        blob = self._alice_ui_blob()
        self.assertIn("待发放", blob)
        # paid_acu='0' is the honest server-side value the chat body carries.
        provider = _read(_ODY / "alice_provider.py")
        self.assertIn('"paid_acu": "0"', provider)
        self.assertNotRegex(provider, r'"paid_acu":\s*"[^0]')  # never non-zero

    def test_no_logging_overclaim_fixed(self):
        """privacy-P2: the chat note must NOT claim 'no logging' (chat history is
        persisted to a LOCAL SQLite DB). The honest phrasing is 'no telemetry, no
        cloud — saved only on this device' (EN) / '无遥测、无云端' (ZH)."""
        i18n = _read(_ALICE / "alice-i18n.js")
        # Pull the chat.note strings (EN + ZH).
        m = re.search(r"'chat\.note':\s*\{(.*?)\}", i18n, re.S)
        self.assertIsNotNone(m, "chat.note key missing")
        note = m.group(1)
        self.assertNotIn("no logging", note.lower(), "the 'no logging' overclaim is back")
        self.assertIn("no telemetry", note.lower())
        self.assertIn("无遥测", note)

    def test_display_guard_only_passes_alice_names(self):
        """The catalog display guard must reject any non-Alice name + accept the
        five Alice tiers (the 3-layer display rule, design 03)."""
        from alice_ai.model_manager.catalog import assert_displayable
        for ok in self.ALLOWED_NAMES:
            self.assertEqual(assert_displayable(ok), ok)
        for leak in ("Qwen3 9B", "Alice (27B)", "alice-lite.gguf", "BlueStar 4B"):
            with self.assertRaises(Exception):
                assert_displayable(leak)

    def test_earn_status_payload_is_credit_only(self):
        """The earn status route's honesty block is paid_acu='0', credit_only,
        and the GPU-earn lane is inert (coming_soon, disabled)."""
        from alice_ai.earn import gpu_earn
        st = gpu_earn.gpu_earn_status()
        self.assertFalse(st["enabled"])
        self.assertEqual(st["status"], "coming_soon")
        # the routes module's honesty constant (read its source, no live server)
        routes = _read(_BACKEND / "alice_ai" / "earn" / "routes.py")
        self.assertIn('"paid_acu": "0"', routes)
        self.assertIn('"credit_only": True', routes)


# =========================================================================== #
# 3. SECURITY — the Simple-mode boundary policy + app wiring hold.
# =========================================================================== #
class SecurityInvariant(unittest.TestCase):
    def setUp(self):
        os.environ["ALICE_SIMPLE_MODE"] = "1"
        os.environ.pop("ALICE_AGENT_MODE", None)        # persisted flag decides
        os.environ.pop("ALICE_AGENT_MODE_LOCKED", None)
        os.environ["AUTH_ENABLED"] = "false"
        # Point the persisted Agent-mode flag at a throwaway dir with NO flag, so
        # the default (Agent mode OFF) holds regardless of the dev box's state.
        self._tmp = tempfile.mkdtemp(prefix="alice-sec-inv-")
        os.environ["ALICE_AI_DATA_DIR"] = self._tmp
        from core import alice_security as s
        s._invalidate_flag_cache()

    def test_dangerous_tools_blocked_by_default(self):
        """Default OFF (no toggle): the agent surface is hard-blocked."""
        from core import alice_security as s
        self.assertFalse(s.agent_mode_enabled())       # default OFF
        self.assertTrue(s.simple_boundary_active())
        for t in ("python", "bash", "read_file", "write_file", "api_call",
                  "app_api", "manage_mcp", "mcp__rag__search"):
            self.assertTrue(s.is_simple_mode_blocked_tool(t), t)

    def test_agent_mode_requires_risk_ack_and_persists(self):
        """The HIGH-2 resolution: Agent mode is a risk-acknowledged, server-
        persisted toggle (NOT an admin account). Turning it on without the
        explicit ack is refused; with the ack it persists + un-gates the tools."""
        from core import alice_security as s
        # an un-acknowledged enable is refused → boundary still active
        self.assertFalse(s.set_agent_mode(True, risk_acknowledged=False))
        self.assertTrue(s.simple_boundary_active())
        self.assertTrue(s.is_simple_mode_blocked_tool("python"))
        # the acknowledged enable persists + un-gates the tools
        self.assertTrue(s.set_agent_mode(True, risk_acknowledged=True))
        self.assertTrue(s.agent_mode_enabled())
        self.assertFalse(s.simple_boundary_active())
        self.assertFalse(s.is_simple_mode_blocked_tool("python"))
        # it is persisted server-side (survives a cache drop = a "restart")
        s._invalidate_flag_cache()
        self.assertTrue(s.agent_mode_enabled())
        self.assertTrue((Path(self._tmp) / "agent_mode.json").exists())

    def test_network_guards_independent_of_agent_toggle(self):
        """The key safety property: the ALWAYS-ON network guards key off
        simple_mode() ALONE, so turning Agent mode ON does NOT disable them."""
        from core import alice_security as s
        # OFF → network guard on
        self.assertTrue(s.simple_mode())
        # turn Agent mode ON
        s.set_agent_mode(True, risk_acknowledged=True)
        self.assertTrue(s.agent_mode_enabled())
        # network guard is STILL on (independent of the toggle)
        self.assertTrue(s.simple_mode())
        # and a locked deployment can never enable the agent surface
        os.environ["ALICE_AGENT_MODE_LOCKED"] = "1"
        try:
            self.assertFalse(s.agent_mode_enabled())
            self.assertFalse(s.set_agent_mode(True, risk_acknowledged=True))
            self.assertTrue(s.simple_mode())  # guard unaffected by the lock too
        finally:
            os.environ.pop("ALICE_AGENT_MODE_LOCKED", None)

    def test_app_source_wires_the_network_boundary(self):
        """app.py must mount TrustedHost + the local-token/Origin guard keyed off
        simple_mode() (ALWAYS-ON, independent of the toggle), and gate the
        privileged routers behind the Simple boundary (CRIT-2/HIGH-1). Source-
        level so we don't need a live server."""
        app = _read(_ODY / "app.py")
        self.assertIn("TrustedHostMiddleware", app)
        self.assertIn("AliceLocalTokenMiddleware", app)
        self.assertIn("CROSS_ORIGIN_BLOCKED", app)
        self.assertIn("LOCAL_TOKEN_REQUIRED", app)
        # the network boundary is installed under simple_mode() (NOT the toggle):
        self.assertIn("if _alice_sec.simple_mode():", app)
        # privileged routers gated behind the Simple boundary (toggle-controlled)
        self.assertIn("_ALICE_MOUNT_PRIVILEGED = not _alice_sec.simple_boundary_active()", app)
        for guarded in ("setup_shell_routes", "setup_cookbook_routes",
                        "setup_mcp_routes", "setup_vault_routes"):
            self.assertIn(guarded, app)

    def test_toggle_route_present_and_token_guarded(self):
        """The POST /alice/mode toggle exists, requires the risk ack server-side,
        and rides the /alice/* token guard (so a foreign page can't flip it)."""
        routes = _read(_ODY / "alice_routes.py")
        self.assertIn('@router.post("/alice/mode")', routes)
        self.assertIn("RISK_NOT_ACKNOWLEDGED", routes)
        self.assertIn("set_agent_mode", routes)
        # /alice/* is in the middleware's guarded prefixes (token + Origin).
        app = _read(_ODY / "app.py")
        self.assertIn('"/alice/"', app)

    def test_no_native_js_bridge_in_shell(self):
        """The PyWebView window must expose NO js_api (a page can't call native
        Python) and run with no debug bridge (deep-security-audit §4 GOOD)."""
        win = _read(_BACKEND.parent / "shell" / "alice_shell" / "window.py")
        self.assertNotIn("js_api=", win)
        self.assertNotIn("debug=True", win)


if __name__ == "__main__":
    unittest.main(verbosity=2)

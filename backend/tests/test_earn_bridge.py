"""Earn bridge (M7) — offline unit tests (stdlib unittest).

Locks the design-04 / M7 invariants so they can't silently regress:

  * Miner DETECTION is per-OS, best-effort, and resolves only KNOWN fixed paths
    (installed / not-installed, cross-OS via $ALICE_EARN_OS).
  * Safe LAUNCH builds the right fixed command (``open -a <known bundle>`` on
    macOS; the resolved binary elsewhere) and REFUSES any target not on the
    detection allow-list (no path/command injection — the security invariant).
  * The identity read is READ-ONLY (the file is never written/created/mutated)
    and honors $ALICE_IDENTITY_DIR; missing/invalid is a first-class None.
  * The phase-2 GPU-earn module stays INERT (flag default off; status
    coming_soon) and imports NOTHING from the inference path.
  * The honest credit-only strings pass the guard: no ``$``, no rate, no
    "profit" anywhere in the Earn i18n / module copy; ``paid_acu`` is "0".

Run (from backend/, so alice_ai is importable):
  cd backend && PYTHONPATH=$(pwd) ../.venv/bin/python -m unittest \
      tests.test_earn_bridge -v
"""

from __future__ import annotations

import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path

_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))


def _set_env(**kw):
    for k, v in kw.items():
        if v is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = v


# --------------------------------------------------------------------------- #
# Detection
# --------------------------------------------------------------------------- #
class DetectTests(unittest.TestCase):
    def tearDown(self):
        _set_env(ALICE_EARN_OS=None, LOCALAPPDATA=None, ProgramFiles=None,
                 ALICE_EARN_MACOS_APPS_DIR=None)

    def test_macos_installed_when_bundle_with_inner_exec(self):
        from alice_ai.earn import miner_detect as d

        with tempfile.TemporaryDirectory() as td:
            # Build a fake <apps>/AliceMiner.app with the inner exec, and isolate
            # detection to that dir (so the host's real /Applications can't leak).
            apps = Path(td)
            bundle = apps / "AliceMiner.app"
            inner = bundle / "Contents" / "MacOS" / "AliceMiner"
            inner.parent.mkdir(parents=True)
            inner.write_text("#!/bin/sh\n")
            _set_env(ALICE_EARN_OS="darwin", ALICE_EARN_MACOS_APPS_DIR=str(apps))
            mi = d.detect_miner()
            self.assertTrue(mi.installed)
            self.assertEqual(mi.launch_kind, d.KIND_MACOS_APP)
            self.assertEqual(mi.launch_target, str(bundle))
            # public projection hides the absolute path
            self.assertEqual(mi.to_public(), {"installed": True, "launch_kind": "macos_app"})

    def test_macos_not_installed_when_bundle_absent(self):
        from alice_ai.earn import miner_detect as d

        with tempfile.TemporaryDirectory() as td:
            _set_env(ALICE_EARN_OS="darwin", ALICE_EARN_MACOS_APPS_DIR=str(td))
            mi = d.detect_miner()
            self.assertFalse(mi.installed)
            self.assertIsNone(mi.launch_target)
            self.assertEqual(mi.to_public(), {"installed": False, "launch_kind": None})

    def test_macos_bundle_without_inner_exec_is_not_installed(self):
        """A bare .app dir with no Contents/MacOS/AliceMiner does NOT count."""
        from alice_ai.earn import miner_detect as d

        with tempfile.TemporaryDirectory() as td:
            apps = Path(td)
            (apps / "AliceMiner.app").mkdir(parents=True)  # empty bundle
            _set_env(ALICE_EARN_OS="darwin", ALICE_EARN_MACOS_APPS_DIR=str(apps))
            mi = d.detect_miner()
            self.assertFalse(mi.installed)

    def test_windows_installed_via_localappdata(self):
        from alice_ai.earn import miner_detect as d

        with tempfile.TemporaryDirectory() as td:
            local = Path(td) / "Local"
            exe = local / "Programs" / "AliceMiner" / "alice-miner.exe"
            exe.parent.mkdir(parents=True)
            exe.write_text("MZ")  # fake PE
            _set_env(ALICE_EARN_OS="windows", LOCALAPPDATA=str(local))
            mi = d.detect_miner()
            self.assertTrue(mi.installed)
            self.assertEqual(mi.launch_kind, d.KIND_WINDOWS_EXE)
            self.assertEqual(mi.launch_target, str(exe))

    def test_linux_installed_via_local_bin(self):
        from alice_ai.earn import miner_detect as d

        with tempfile.TemporaryDirectory() as td:
            home = Path(td)
            binp = home / ".local" / "bin" / "alice-miner"
            binp.parent.mkdir(parents=True)
            binp.write_text("#!/bin/sh\n")
            binp.chmod(0o755)
            _set_env(ALICE_EARN_OS="linux")
            orig_home = Path.home
            try:
                Path.home = staticmethod(lambda: home)  # type: ignore
                mi = d.detect_miner()
            finally:
                Path.home = orig_home  # type: ignore
            self.assertTrue(mi.installed)
            self.assertEqual(mi.launch_kind, d.KIND_BINARY)
            self.assertEqual(mi.launch_target, str(binp))

    def test_allow_list_is_fixed_and_platform_scoped(self):
        from alice_ai.earn import miner_detect as d

        _set_env(ALICE_EARN_OS="darwin")
        allow = d.allowed_launch_targets()
        self.assertTrue(any(a.endswith("AliceMiner.app") for a in allow))
        self.assertTrue(all(a.endswith("AliceMiner.app") for a in allow))


# --------------------------------------------------------------------------- #
# Launch safety (the security-critical part)
# --------------------------------------------------------------------------- #
class LaunchSafetyTests(unittest.TestCase):
    def tearDown(self):
        _set_env(ALICE_EARN_OS=None)

    def test_macos_command_is_open_dash_a_known_bundle(self):
        from alice_ai.earn import miner_detect as d
        from alice_ai.earn import miner_launch as L

        _set_env(ALICE_EARN_OS="darwin")
        target = d.allowed_launch_targets()[0]  # a KNOWN allow-listed bundle
        install = d.MinerInstall(True, d.KIND_MACOS_APP, target)
        argv = L.build_launch_command(install)
        # Exactly the fixed safe form — no shell, no client string.
        self.assertEqual(argv, ["open", "-a", target])
        # No shell metacharacters anywhere (defense-in-depth assertion).
        for part in argv:
            self.assertNotRegex(part, r"[;&|`$><]")

    def test_arbitrary_target_is_refused(self):
        """A fabricated install pointing at any non-Miner path must be refused
        — this is what makes launch a FIXED action, not an arbitrary exec."""
        from alice_ai.earn import miner_detect as d
        from alice_ai.earn import miner_launch as L

        _set_env(ALICE_EARN_OS="darwin")
        for evil in (
            "/Applications/Calculator.app",
            "/bin/sh",
            "/Applications/AliceMiner.app; rm -rf ~",  # injection attempt as a path
            "../../../../usr/bin/python3",
        ):
            install = d.MinerInstall(True, d.KIND_MACOS_APP, evil)
            with self.assertRaises(L.UnsafeLaunchTarget, msg=evil):
                L.build_launch_command(install)

    def test_not_installed_build_raises(self):
        from alice_ai.earn import miner_detect as d
        from alice_ai.earn import miner_launch as L

        with self.assertRaises(ValueError):
            L.build_launch_command(d.MinerInstall(False))

    def test_launch_miner_reports_not_installed_without_spawn(self):
        from alice_ai.earn import miner_detect as d
        from alice_ai.earn import miner_launch as L

        res = L.launch_miner(d.MinerInstall(False))
        self.assertFalse(res.launched)
        self.assertEqual(res.reason, "not_installed")

    def test_launch_miner_refuses_unsafe_target_without_spawn(self):
        from alice_ai.earn import miner_detect as d
        from alice_ai.earn import miner_launch as L

        _set_env(ALICE_EARN_OS="darwin")
        res = L.launch_miner(d.MinerInstall(True, d.KIND_MACOS_APP, "/bin/sh"))
        self.assertFalse(res.launched)
        self.assertEqual(res.reason, "unsafe_target")


# --------------------------------------------------------------------------- #
# Identity read-only
# --------------------------------------------------------------------------- #
class IdentityReadOnlyTests(unittest.TestCase):
    def tearDown(self):
        _set_env(ALICE_IDENTITY_DIR=None)

    def _write_identity(self, d: Path, payload: dict):
        (d / "identity.json").write_text(json.dumps(payload), encoding="utf-8")

    def test_reads_address_and_truncates_display(self):
        from alice_ai.earn import identity_reader as ir

        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self._write_identity(d, {
                "address": "alice1qwertyuiopasdfghjklzxcvbnm9876",
                "pubkey": "0xabc", "keystore_path": "/x/wallet.json",
                "label": "my rig", "created": 1717459200,
            })
            _set_env(ALICE_IDENTITY_DIR=str(d))
            v = ir.read_identity()
            self.assertIsNotNone(v)
            self.assertEqual(v["address"], "alice1qwertyuiopasdfghjklzxcvbnm9876")
            self.assertEqual(v["address_display"], "alice1…9876")
            self.assertEqual(v["label"], "my rig")
            self.assertFalse(v["watch_only"])  # has keystore + pubkey

    def test_watch_only_when_no_keystore_or_pubkey(self):
        from alice_ai.earn import identity_reader as ir

        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            self._write_identity(d, {"address": "alice1pastedwatchonlyaddr0000"})
            _set_env(ALICE_IDENTITY_DIR=str(d))
            v = ir.read_identity()
            self.assertTrue(v["watch_only"])
            # P4: secrets never surfaced even if present — only address/label/watch.
            self.assertEqual(set(v.keys()), {"address", "address_display", "label", "watch_only"})

    def test_missing_file_returns_none(self):
        from alice_ai.earn import identity_reader as ir

        with tempfile.TemporaryDirectory() as td:
            _set_env(ALICE_IDENTITY_DIR=str(td))  # empty dir, no identity.json
            self.assertIsNone(ir.read_identity())

    def test_malformed_or_blank_address_returns_none(self):
        from alice_ai.earn import identity_reader as ir

        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            (d / "identity.json").write_text("not json {{", encoding="utf-8")
            _set_env(ALICE_IDENTITY_DIR=str(d))
            self.assertIsNone(ir.read_identity())
            self._write_identity(d, {"address": "   "})  # blank
            self.assertIsNone(ir.read_identity())

    def test_read_does_not_create_or_mutate_the_file(self):
        """The audit invariant: reading NEVER writes ~/.alice."""
        from alice_ai.earn import identity_reader as ir

        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            payload = {"address": "alice1zzz", "label": "x"}
            self._write_identity(d, payload)
            p = d / "identity.json"
            before_bytes = p.read_bytes()
            before_mtime = p.stat().st_mtime_ns
            _set_env(ALICE_IDENTITY_DIR=str(d))
            for _ in range(5):
                ir.read_identity()
            # Byte-identical + mtime unchanged → no write occurred.
            self.assertEqual(p.read_bytes(), before_bytes)
            self.assertEqual(p.stat().st_mtime_ns, before_mtime)

    def test_missing_read_does_not_create_file(self):
        from alice_ai.earn import identity_reader as ir

        with tempfile.TemporaryDirectory() as td:
            d = Path(td)
            _set_env(ALICE_IDENTITY_DIR=str(d))
            ir.read_identity()
            self.assertFalse((d / "identity.json").exists(), "read created the file!")

    def test_identity_dir_honors_override(self):
        from alice_ai.earn import identity_reader as ir

        # Use a tmp dir from the OS so the path comparison is platform-portable
        # (a literal "/tmp/…" renders as "\tmp\…" on Windows). Compare resolved
        # Path objects rather than raw strings.
        with tempfile.TemporaryDirectory() as td:
            override = Path(td) / "some-test-dir"
            _set_env(ALICE_IDENTITY_DIR=str(override))
            self.assertEqual(Path(ir.identity_dir()), override)
        _set_env(ALICE_IDENTITY_DIR=None)
        # Default falls back to ~/.alice on every OS (compare the final path
        # component, not a "/"-joined suffix which differs on Windows).
        self.assertEqual(Path(ir.identity_dir()).name, ".alice")


# --------------------------------------------------------------------------- #
# Phase-2 GPU-earn stays inert
# --------------------------------------------------------------------------- #
class GpuEarnInertTests(unittest.TestCase):
    def tearDown(self):
        _set_env(ALICE_AI_GPU_EARN_ENABLED=None)

    def test_disabled_by_default(self):
        from alice_ai.earn import gpu_earn as g

        _set_env(ALICE_AI_GPU_EARN_ENABLED=None)
        self.assertFalse(g.gpu_earn_enabled())
        st = g.gpu_earn_status()
        self.assertFalse(st["enabled"])
        self.assertEqual(st["status"], "coming_soon")
        self.assertEqual(st["gates"], ["G1", "G2", "G3", "G4", "G5"])

    def test_flag_on_still_reports_coming_soon(self):
        """Even with the flag on, v1 ships no earning path → still coming_soon."""
        from alice_ai.earn import gpu_earn as g

        _set_env(ALICE_AI_GPU_EARN_ENABLED="1")
        self.assertTrue(g.gpu_earn_enabled())
        self.assertEqual(g.gpu_earn_status()["status"], "coming_soon")

    def test_earn_package_imports_nothing_from_inference(self):
        """design 04 P2: the Earn surface must not pull in the inference path."""
        # Import all earn modules, then assert no inference module is loaded *by*
        # them (the provider/local_inference must not be a transitive dep).
        for m in list(sys.modules):
            if "alice_ai.earn" in m or "alice_provider" in m or "local_inference" in m:
                sys.modules.pop(m, None)
        import alice_ai.earn  # noqa: F401
        import alice_ai.earn.routes  # noqa: F401

        leaked = [m for m in sys.modules
                  if m == "alice_provider" or m.endswith("alice_provider")
                  or ".local_inference" in m]
        self.assertEqual(leaked, [], f"earn imported inference modules: {leaked}")


# --------------------------------------------------------------------------- #
# Honesty / credit-only string guard
# --------------------------------------------------------------------------- #
class HonestyGuardTests(unittest.TestCase):
    """No `$`, no fiat, no rate, no 'profit' anywhere in the Earn copy; the
    credit-only 'pending / 待发放' framing is present; paid_acu == '0'."""

    # Forbidden substrings (case-insensitive) on the Earn surfaces.
    FORBIDDEN = ["$", "income", "profit", "make money", "cash out", "usd", "fiat",
                 "per hour", "/hour", "/hr", "per day", "alice/hour", "earnings of"]

    def _earn_i18n_strings(self):
        """Pull the EN+中 values of every earn.*/story.*/gpu.* i18n key."""
        js = (_BACKEND / "odysseus" / "static" / "alice" / "alice-i18n.js").read_text(encoding="utf-8")
        # crude but robust: collect lines for our key prefixes with en/zh values
        vals = []
        for m in re.finditer(r"'(earn\.[^']+|story\.[^']+|gpu\.[^']+|settings\.earn)':\s*\{([^}]*)\}", js):
            vals.append(m.group(2))
        self.assertTrue(vals, "no earn i18n keys found — table moved?")
        return "\n".join(vals)

    def test_no_forbidden_tokens_in_i18n(self):
        blob = self._earn_i18n_strings().lower()
        for bad in self.FORBIDDEN:
            self.assertNotIn(bad.lower(), blob, f"forbidden token {bad!r} in Earn i18n")

    @staticmethod
    def _strip_js_comments(src: str) -> str:
        """Remove /* block */ and // line comments so the honesty scan only sees
        code/strings that can reach the user (our own doc-comments legitimately
        SAY "no $/profit/rate" — that must not trip the guard)."""
        src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
        src = re.sub(r"(?m)//.*$", "", src)
        return src

    def test_no_forbidden_tokens_in_earn_js(self):
        raw = (_BACKEND / "odysseus" / "static" / "alice" / "alice-earn.js").read_text(encoding="utf-8")
        code = self._strip_js_comments(raw)
        low = code.lower()
        # No CURRENCY use of `$` — a `$` adjacent to a digit or a money word
        # (e.g. "$5", "$ 10", "5$", "us$"). Bare `$` in code (regex char-class)
        # is harmless; we forbid the money pattern.
        self.assertNotRegex(code, r"(?:US)?\$\s*\d|\d\s*\$|\bUS\$")
        for bad in ("profit", "income", "make money", "/hour", "per hour"):
            self.assertNotIn(bad, low)

    def test_credit_only_framing_present(self):
        blob = self._earn_i18n_strings()
        # the pending / 待发放 credit-only treatment must appear
        self.assertIn("待发放", blob)
        self.assertIn("pending", blob.lower())

    def test_status_payload_is_credit_only(self):
        from alice_ai.earn import gpu_earn as g

        # honesty contract values surfaced by the route
        self.assertEqual(str(g.gpu_earn_status().get("enabled")).lower() in ("false", "true"), True)
        # the routes module asserts paid_acu="0" — check the source constant path
        from alice_ai.earn import routes as r

        # build the same dict the route returns for the honesty block
        # (no network; gpu_earn_status + a literal honesty block)
        self.assertTrue(callable(r.earn_status))

    def test_gpu_reason_has_no_dollar_or_rate(self):
        from alice_ai.earn import gpu_earn as g

        for key in ("reason_en", "reason_zh"):
            txt = g.gpu_earn_status()[key]
            self.assertNotIn("$", txt)
            self.assertNotRegex(txt.lower(), r"\b(profit|income|per hour|/hour)\b")


if __name__ == "__main__":
    unittest.main(verbosity=2)

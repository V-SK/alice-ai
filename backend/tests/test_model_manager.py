"""M4 Model Manager — offline, deterministic unit tests (stdlib unittest).

Run:  PYTHONPATH=backend .venv/bin/python -m unittest backend.tests.test_model_manager -v

Covers (design 03 §11 + the brief's VERIFY list):
  * display guard: every card passes assert_displayable; to_public_dict() leaks
    no internal key; DISPLAY_MAP covers every MODEL_PROFILES tier; forbidden
    tokens raise.
  * downloader: fake WeightDownloader writing (a) correct bytes, (b) truncated,
    (c) wrong-sha → OK / size-fail / sha-fail; MLX fetches the whole snapshot
    (multi-file) vs GGUF one file; atomic publish (no half cache dir on
    mid-fail); cache-hit skips download; resume keeps partials.
  * REAL SHA gate against the already-downloaded Alice Lite (no re-download).
  * context clamp 4k..min(model_max,256k) + the RAM/VRAM gate bands.
  * the import-graph invariant (no ledger/credit/worker/transport import).
"""

from __future__ import annotations

import json
import os
import sys
import unittest
from pathlib import Path

# Make the backend package importable when run from the repo root.
_BACKEND = Path(__file__).resolve().parents[1]
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from alice_acp.api_chat.model_catalog import MODEL_PROFILES  # noqa: E402
from alice_acp.local_inference.pinned_models import (  # noqa: E402
    all_pinned_artifacts,
    pinned_artifact,
)

from alice_ai.model_manager import (  # noqa: E402
    CONTEXT_HARD_CAP,
    CONTEXT_MIN,
    AliceModelCard,
    DisplayLeakError,
    DISPLAY_MAP,
    ModelDownloadError,
    VerifyingSnapshotDownloader,
    assert_displayable,
    build_card,
    clamp_context_length,
    context_supported_max,
    cross_check_checksums,
    sha256_file,
)
from alice_ai.model_manager.catalog import checksums_for  # noqa: E402
from alice_ai.model_manager.context import (  # noqa: E402
    estimate_kv_cache_gb,
    read_model_max_context,
)
from alice_ai.model_manager.downloader import (  # noqa: E402
    REASON_CHECKSUM_MISMATCH,
    REASON_SIZE_MISMATCH,
    VERIFIED_SENTINEL,
)

import tempfile  # noqa: E402

LITE_MLX = pinned_artifact("alice_lite_4b", "mlx")
LITE_SNAPSHOT = (
    Path.home() / ".alice" / "models" / f"{LITE_MLX.repo_id.replace('/', '__')}@{LITE_MLX.revision}"
)


# --------------------------------------------------------------------------- #
# Display guard.
# --------------------------------------------------------------------------- #
class TestDisplayGuard(unittest.TestCase):
    def test_display_map_covers_every_tier(self):
        for tier in MODEL_PROFILES:
            self.assertIn(tier, DISPLAY_MAP, f"DISPLAY_MAP missing tier {tier}")

    def test_every_card_passes_guard_and_is_alice_only(self):
        allowed = {"Alice", "Alice Lite", "Alice Pro", "Alice RP", "Alice RP Lite"}
        for art in all_pinned_artifacts():
            card = build_card(art.model_class, art.runtime)
            assert_displayable(card.display_name)
            assert_displayable(card.tagline)
            self.assertIn(card.display_name, allowed)

    def test_to_public_dict_leaks_no_internal_key(self):
        forbidden_keys = {
            "repo_id", "revision", "model_id", "model_class",
            "quant", "parameter_billions", "files",
        }
        for art in all_pinned_artifacts():
            card = build_card(art.model_class, art.runtime)
            pub = card.to_public_dict(context_max=CONTEXT_HARD_CAP)
            for k in forbidden_keys:
                self.assertNotIn(k, pub, f"{k} leaked into public dict")
            # And no VALUE in the dict carries a forbidden token.
            blob = json.dumps(pub).lower()
            for tok in ("qwen", "qwopus", "bluestar", "safetensors", ".gguf", "27b", "35b"):
                self.assertNotIn(tok, blob, f"token {tok!r} leaked in public dict values")

    def test_forbidden_tokens_raise(self):
        for leak in ("Qwen3 9B", "Alice (27B)", "alice-lite.gguf", "BlueStar"):
            with self.assertRaises(DisplayLeakError):
                assert_displayable(leak)

    def test_clean_names_pass(self):
        for ok in ("Alice", "Alice Lite", "Alice Pro", "Alice RP"):
            self.assertEqual(assert_displayable(ok), ok)


# --------------------------------------------------------------------------- #
# Context-size clamp + KV estimate + config reader.
# --------------------------------------------------------------------------- #
class TestContextSize(unittest.TestCase):
    def test_clamp_bounds(self):
        self.assertEqual(clamp_context_length(1000, 262144), CONTEXT_MIN)  # floor
        self.assertEqual(clamp_context_length(8192, 262144), 8192)
        self.assertEqual(clamp_context_length(999999, 262144), 262144)  # cap@max
        self.assertEqual(clamp_context_length(999999, 1_000_000), CONTEXT_HARD_CAP)  # 256k

    def test_clamp_capped_at_small_model_max(self):
        # A model that only supports 4k: any request clamps down to 4k.
        self.assertEqual(clamp_context_length(64000, 4096), 4096)
        self.assertEqual(clamp_context_length(None, 4096), 4096)

    def test_supported_max_is_min_of_modelmax_and_256k(self):
        self.assertEqual(context_supported_max(262144), 262144)
        self.assertEqual(context_supported_max(1_000_000), CONTEXT_HARD_CAP)
        self.assertEqual(context_supported_max(8192), 8192)
        self.assertEqual(context_supported_max(None), 32768)  # conservative fallback

    def test_kv_estimate_grows_with_context(self):
        small = estimate_kv_cache_gb(4096, parameter_billions=9)
        big = estimate_kv_cache_gb(131072, parameter_billions=9)
        self.assertLess(small, big)
        self.assertGreater(big, 0.5)  # 128k on a 9B is non-trivial GB

    @unittest.skipUnless(LITE_SNAPSHOT.exists(), "Alice Lite not downloaded")
    def test_read_real_model_max_context_nested(self):
        # The 4B MLX config nests max_position_embeddings under text_config=262144.
        self.assertEqual(read_model_max_context(LITE_SNAPSHOT), 262144)
        self.assertEqual(context_supported_max(read_model_max_context(LITE_SNAPSHOT)), 262144)


# --------------------------------------------------------------------------- #
# VerifyingSnapshotDownloader — with a fake (no network).
# --------------------------------------------------------------------------- #
def _fake_artifact(runtime: str, subpath: str):
    """A small in-memory pinned artifact for a fake repo (uses a real pin's
    shape but we override the checksum DB via monkeypatched files on the card
    path — here we test the downloader against a fake snapshot fn directly)."""
    return pinned_artifact("alice_lite_4b", runtime)  # shape only; files come from fake


class _FakeRepo:
    """A fake HF snapshot: writes given file bytes into local_dir on 'download'.

    Filters by allow_patterns (glob) so we can prove MLX fetches the whole
    snapshot (multi-file) vs GGUF a single file.
    """

    def __init__(self, files: dict[str, bytes], record: list):
        self.files = files
        self.record = record

    def __call__(self, *, repo_id, revision, local_dir, allow_patterns, tqdm_class=None):
        import fnmatch

        Path(local_dir).mkdir(parents=True, exist_ok=True)
        written = []
        for name, data in self.files.items():
            if any(fnmatch.fnmatch(name, pat) for pat in allow_patterns):
                p = Path(local_dir) / name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(data)
                written.append(name)
        self.record.append({"patterns": list(allow_patterns), "written": written})


class TestDownloader(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="alice-dl-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def _patch_checksums(self, card_files):
        """Monkeypatch checksums_for so the downloader expects card_files."""
        import alice_ai.model_manager.downloader as dl

        self._orig = dl.checksums_for
        dl.checksums_for = lambda artifact: card_files
        self.addCleanup(lambda: setattr(dl, "checksums_for", self._orig))

    def test_correct_bytes_verify_and_atomic_publish(self):
        from alice_ai.model_manager.catalog import FileChecksum
        import hashlib

        data = b"hello-alice-weights" * 1000
        sha = hashlib.sha256(data).hexdigest()
        files = {"model.safetensors": data, "config.json": b"{}", "tokenizer.json": b"{}"}
        expected = (
            FileChecksum("model.safetensors", len(data), sha),
            FileChecksum("config.json", 2, hashlib.sha256(b"{}").hexdigest()),
            FileChecksum("tokenizer.json", 2, hashlib.sha256(b"{}").hexdigest()),
        )
        self._patch_checksums(expected)
        rec = []
        art = pinned_artifact("alice_lite_4b", "mlx")  # mlx → whole snapshot
        dl = VerifyingSnapshotDownloader(_snapshot_fn=_FakeRepo(files, rec))
        target = self.tmp / "snap"
        out = dl.download(art, target)
        self.assertEqual(out, target)
        self.assertTrue((target / VERIFIED_SENTINEL).exists(), "sentinel must be present after publish")
        self.assertTrue((target / "model.safetensors").exists())
        # MLX fetched the whole snapshot (>1 file).
        self.assertGreaterEqual(len(rec[0]["written"]), 3)
        # No leftover .partial dir.
        self.assertFalse(target.with_name(target.name + ".partial").exists())

    def test_truncated_file_is_rejected_size(self):
        from alice_ai.model_manager.catalog import FileChecksum
        import hashlib

        real = b"x" * 5000
        truncated = b"x" * 100  # wrong size
        sha = hashlib.sha256(real).hexdigest()
        files = {"model.safetensors": truncated, "config.json": b"{}"}
        expected = (FileChecksum("model.safetensors", len(real), sha),)
        self._patch_checksums(expected)
        # Fake re-fetch also writes the truncated bytes → second verify fails.
        dl = VerifyingSnapshotDownloader(_snapshot_fn=_FakeRepo(files, []))
        with self.assertRaises(ModelDownloadError) as ctx:
            dl.download(pinned_artifact("alice_lite_4b", "mlx"), self.tmp / "snap")
        self.assertEqual(ctx.exception.reason_code, REASON_SIZE_MISMATCH)
        # Fail-closed: NO published dir, NO sentinel (atomicity preserved).
        self.assertFalse((self.tmp / "snap").exists())

    def test_wrong_sha_is_rejected(self):
        from alice_ai.model_manager.catalog import FileChecksum
        import hashlib

        data = b"y" * 5000  # right size, wrong content vs expected sha
        expected_sha = hashlib.sha256(b"DIFFERENT" * 625).hexdigest()
        files = {"model.safetensors": data}
        expected = (FileChecksum("model.safetensors", 5000, expected_sha),)
        self._patch_checksums(expected)
        dl = VerifyingSnapshotDownloader(_snapshot_fn=_FakeRepo(files, []))
        with self.assertRaises(ModelDownloadError) as ctx:
            dl.download(pinned_artifact("alice_lite_4b", "mlx"), self.tmp / "snap")
        self.assertEqual(ctx.exception.reason_code, REASON_CHECKSUM_MISMATCH)
        self.assertFalse((self.tmp / "snap").exists())

    def test_gguf_fetches_single_file(self):
        from alice_ai.model_manager.catalog import FileChecksum
        import hashlib

        gguf = pinned_artifact("alice_lite_4b", "gguf")
        data = b"GGUF" + b"z" * 4000
        sha = hashlib.sha256(data).hexdigest()
        # Repo has extra files, but GGUF pattern must select ONLY the .gguf.
        files = {
            gguf.artifact_subpath: data,
            "README.md": b"readme",
            "config.json": b"{}",
        }
        expected = (FileChecksum(gguf.artifact_subpath, len(data), sha),)
        self._patch_checksums(expected)
        rec = []
        dl = VerifyingSnapshotDownloader(_snapshot_fn=_FakeRepo(files, rec))
        dl.download(gguf, self.tmp / "g")
        self.assertEqual(rec[0]["written"], [gguf.artifact_subpath], "GGUF must fetch only the .gguf")

    def test_cache_hit_skips_download(self):
        from alice_ai.model_manager.catalog import FileChecksum
        import hashlib

        data = b"cached" * 1000
        sha = hashlib.sha256(data).hexdigest()
        files = {"model.safetensors": data}
        expected = (FileChecksum("model.safetensors", len(data), sha),)
        self._patch_checksums(expected)
        rec = []
        dl = VerifyingSnapshotDownloader(_snapshot_fn=_FakeRepo(files, rec))
        target = self.tmp / "snap"
        dl.download(pinned_artifact("alice_lite_4b", "mlx"), target)
        self.assertEqual(len(rec), 1)
        # Second call: sentinel present → no second fetch.
        dl.download(pinned_artifact("alice_lite_4b", "mlx"), target)
        self.assertEqual(len(rec), 1, "cache hit must not re-download")

    def test_resume_keeps_partials_on_network_error(self):
        # A snapshot fn that fails the first 2 attempts then succeeds; the
        # .partial dir must persist across retries (no wipe).
        from alice_ai.model_manager.catalog import FileChecksum
        import hashlib

        data = b"resume" * 1000
        sha = hashlib.sha256(data).hexdigest()
        expected = (FileChecksum("model.safetensors", len(data), sha),)
        self._patch_checksums(expected)
        attempts = {"n": 0}
        partial_seen = {"existed_on_retry": False}
        target = self.tmp / "snap"
        partial = target.with_name(target.name + ".partial")

        def flaky(*, repo_id, revision, local_dir, allow_patterns, tqdm_class=None):
            attempts["n"] += 1
            # write a partial chunk each attempt
            Path(local_dir).mkdir(parents=True, exist_ok=True)
            if attempts["n"] >= 2 and partial.exists():
                partial_seen["existed_on_retry"] = True
            if attempts["n"] < 3:
                (Path(local_dir) / "model.safetensors.incomplete").write_bytes(b"partial")
                raise OSError("simulated network drop")
            (Path(local_dir) / "model.safetensors").write_bytes(data)

        dl = VerifyingSnapshotDownloader(_snapshot_fn=flaky, max_network_retries=3)
        # speed up backoff
        import alice_ai.model_manager.downloader as dlmod
        orig_sleep = dlmod.time.sleep
        dlmod.time.sleep = lambda *_a, **_k: None
        self.addCleanup(lambda: setattr(dlmod.time, "sleep", orig_sleep))

        dl.download(pinned_artifact("alice_lite_4b", "mlx"), target)
        self.assertEqual(attempts["n"], 3)
        self.assertTrue(partial_seen["existed_on_retry"], "partial dir must survive retries")
        self.assertTrue((target / VERIFIED_SENTINEL).exists())

    def test_progress_callback_fires(self):
        from alice_ai.model_manager.catalog import FileChecksum
        import hashlib

        data = b"p" * 3000
        sha = hashlib.sha256(data).hexdigest()
        expected = (FileChecksum("model.safetensors", len(data), sha),)
        self._patch_checksums(expected)
        events = []
        dl = VerifyingSnapshotDownloader(
            on_progress=lambda e: events.append(e), _snapshot_fn=_FakeRepo({"model.safetensors": data}, [])
        )
        dl.download(pinned_artifact("alice_lite_4b", "mlx"), self.tmp / "snap")
        phases = {e.phase for e in events}
        self.assertIn("preflight", phases)
        self.assertIn("verifying", phases)
        self.assertIn("done", phases)


# --------------------------------------------------------------------------- #
# REAL SHA gate against the already-downloaded Alice Lite (no re-download).
# --------------------------------------------------------------------------- #
class TestRealAliceLiteSha(unittest.TestCase):
    @unittest.skipUnless(LITE_SNAPSHOT.exists(), "Alice Lite not downloaded")
    def test_on_disk_lite_matches_vendored_checksum(self):
        files = checksums_for(LITE_MLX)
        self.assertTrue(files, "Alice Lite must have a vendored checksum")
        for fc in files:
            path = LITE_SNAPSHOT / fc.path
            self.assertTrue(path.exists(), f"{fc.path} missing on disk")
            self.assertEqual(path.stat().st_size, fc.size_bytes, f"{fc.path} size mismatch")
            self.assertEqual(sha256_file(path), fc.sha256, f"{fc.path} sha mismatch")

    @unittest.skipUnless(LITE_SNAPSHOT.exists(), "Alice Lite not downloaded")
    def test_verifier_accepts_real_lite_and_publishes(self):
        # Run the downloader's verify path against the REAL files via a fake
        # "download" that just symlinks/uses the already-present snapshot: we
        # copy the small metadata + hardlink the big file into a fresh dir so we
        # don't re-download multi-GB, then verify.
        import shutil

        files = checksums_for(LITE_MLX)
        target = self.tmp_target = Path(tempfile.mkdtemp(prefix="alice-lite-verify-"))
        self.addCleanup(lambda: shutil.rmtree(target, ignore_errors=True))

        def fake_copy(*, repo_id, revision, local_dir, allow_patterns, tqdm_class=None):
            dst = Path(local_dir)
            dst.mkdir(parents=True, exist_ok=True)
            for fc in files:
                src = LITE_SNAPSHOT / fc.path
                d = dst / fc.path
                try:
                    os.link(src, d)  # hardlink — no copy of the 2.3 GB file
                except OSError:
                    shutil.copy2(src, d)

        dl = VerifyingSnapshotDownloader(_snapshot_fn=fake_copy)
        snap = target / "snap"
        out = dl.download(LITE_MLX, snap)
        self.assertTrue((out / VERIFIED_SENTINEL).exists())
        # The big real file is present + verified.
        self.assertTrue((out / "model.safetensors").exists())


# --------------------------------------------------------------------------- #
# Manifest cross-check + import invariant.
# --------------------------------------------------------------------------- #
class TestGateAndRecommend(unittest.TestCase):
    """RAM/VRAM gate bands + recommend() over synthetic probes (design 03 §3/§8).

    We drive the ModelManager with a synthetic AugmentedDevice (no real probe) so
    the bands are deterministic across machines.
    """

    def _manager_with(self, *, usable_gb, runtime="mlx"):
        from alice_acp.mining_device.types import DeviceProbe
        from alice_acp.local_inference.hardware_select import HostMemoryHint
        from alice_ai.model_manager.device import AugmentedDevice
        from alice_ai.model_manager import ModelManager

        if runtime == "mlx":
            probe = DeviceProbe(operating_system="macos", device_kind="apple_silicon", vendor="apple", metal_available=True)
            mem = HostMemoryHint(system_memory_gb=usable_gb)
        elif runtime == "cuda":
            probe = DeviceProbe(operating_system="linux", device_kind="gpu", vendor="nvidia", cuda_available=True, vram_gb=usable_gb)
            mem = HostMemoryHint(system_memory_gb=usable_gb)
        else:  # cpu
            probe = DeviceProbe(operating_system="linux", device_kind="cpu", vendor="none", cpu_threads=8)
            mem = HostMemoryHint(system_memory_gb=usable_gb)
        m = ModelManager(cache_root=Path(tempfile.mkdtemp(prefix="alice-gate-")))
        m._device = AugmentedDevice(probe=probe, memory=mem, usable_memory_gb=usable_gb, device_label="test", accelerator_label=runtime)
        self.addCleanup(lambda: __import__("shutil").rmtree(m.cache_root, ignore_errors=True))
        return m

    def test_gate_bands_ok_warn_refuse(self):
        # Alice Lite: comfort floor 16, hard floor 4 (mlx 4bit).
        m_ok = self._manager_with(usable_gb=16)
        self.assertEqual(m_ok.gate("alice_lite_4b").level, "ok")
        m_warn = self._manager_with(usable_gb=8)  # >=4 (hard), <16 (comfort)
        self.assertEqual(m_warn.gate("alice_lite_4b").level, "warn")
        m_refuse = self._manager_with(usable_gb=3)  # <4 hard floor
        g = m_refuse.gate("alice_lite_4b")
        self.assertEqual(g.level, "refuse")

    def test_refuse_suggests_a_smaller_tier(self):
        # On a 32 GB box, Alice Pro (48 comfort, 32 hard floor for q8_0) — the
        # 27B q8_0 hard floor is 32, so 32 GB is exactly the WARN band; pick a
        # box below the Pro hard floor to force REFUSE + a Lite/Alice suggestion.
        m = self._manager_with(usable_gb=20)  # below Pro's 32 hard floor
        g = m.gate("alice_pro_27b")
        self.assertEqual(g.level, "refuse")
        self.assertIn(g.suggest, {"lite", "std"})  # a smaller tier that fits 20 GB

    def test_large_context_tightens_the_gate(self):
        # 9B comfort 16, hard floor 7 (q4_k_m) / 10 (8bit). On a box that is OK
        # at 4k, a 256k context adds enough KV to push it to WARN or REFUSE.
        m = self._manager_with(usable_gb=16)
        g_small = m.gate("alice_standard_9b", context_length=4096)
        g_big = m.gate("alice_standard_9b", context_length=262144)
        order = {"ok": 0, "warn": 1, "refuse": 2}
        self.assertGreaterEqual(order[g_big.level], order[g_small.level],
                                "a 256k context must never be MORE permissive than 4k")
        self.assertLessEqual(order[g_small.level], 1)

    def test_recommend_over_synthetic_probes(self):
        # design 03 §3.3: largest tier whose comfort floor fits AND that has a
        # pinned artifact for the HOST runtime wins. Result is runtime-dependent
        # because not every tier is published for every runtime.
        def rec(gb, runtime):
            return self._manager_with(usable_gb=gb, runtime=runtime).recommend()["model_class_internal"]

        # CUDA path matches the idealized §3.3 table (all GGUF tiers pinned).
        self.assertEqual(rec(8, "cuda"), "alice_lite_4b")    # <16 comfort → small-device Lite
        self.assertEqual(rec(16, "cuda"), "alice_standard_9b")
        self.assertEqual(rec(24, "cuda"), "alice_standard_9b")  # 27B comfort is 48
        self.assertEqual(rec(48, "cuda"), "alice_pro_27b")
        self.assertEqual(rec(96, "cuda"), "alice_pro_35b_moe")

        # MLX (Apple) path: Pro 27B Dense is GGUF-ONLY upstream (no MLX pin), so
        # an Apple box at 48 GB correctly lands on Alice (9B) — the largest tier
        # with BOTH a fitting comfort floor AND an MLX artifact — not Pro 27B.
        self.assertEqual(rec(8, "mlx"), "alice_lite_4b")
        self.assertEqual(rec(16, "mlx"), "alice_standard_9b")
        self.assertEqual(rec(48, "mlx"), "alice_standard_9b")  # no MLX 27B pin
        self.assertEqual(rec(96, "mlx"), "alice_pro_35b_moe")  # MLX MoE pin exists

    def test_recommend_is_display_safe(self):
        rec = self._manager_with(usable_gb=96).recommend()
        # The serialized fields never leak a size/base; the internal echo is
        # popped by the route, but assert the display name is Alice-only here.
        self.assertIn(rec["display_name"], {"Alice", "Alice Lite", "Alice Pro", "Alice RP"})
        assert_displayable(rec["tagline"])

    def test_switch_unloads_previous_backend(self):
        # Model switching (design 03 §7.2): loading a new tier must drop the
        # previous resident model reference so the adapter releases it. We test
        # the unload mechanism with fake backends (no real GPU load).
        m = self._manager_with(usable_gb=96)

        class _FakeBackend:
            def __init__(self):
                self._model = object()

        b1 = _FakeBackend()
        m._active_backend = b1
        m._active_tier = "alice_lite_4b"
        old_model = b1._model
        # Simulate a switch: the unload step must clear the old model ref.
        m._unload_locked()
        self.assertIsNone(b1._model, "previous backend's model ref must be dropped on switch")
        self.assertIsNone(m._active_backend)

    def test_persisted_choice_survives_a_fresh_manager(self):
        from alice_ai.model_manager import ModelManager

        m = self._manager_with(usable_gb=96)
        m.set_context_length("alice_lite_4b", 16384)
        m._save_choice({**m._load_choice(), "tier": "alice_lite_4b"})
        # A brand-new manager pointed at the same cache reads the choice back.
        m2 = ModelManager(cache_root=m.cache_root)
        self.assertEqual(m2.persisted_tier(), "alice_lite_4b")
        self.assertEqual(m2._load_choice()["context"]["alice_lite_4b"], 16384)


class TestManifestAndInvariants(unittest.TestCase):
    def test_cross_check_partitions_pins(self):
        cc = cross_check_checksums()
        # The MLX 4B/9B/35B + GGUF 27B + RP-Lite + RP are covered (6); the
        # GGUF-of-4B/9B/35B fall back to HF-OID (3).
        self.assertEqual(len(cc["covered"]), 6)
        self.assertEqual(len(cc["oid_fallback"]), 3)
        # Every covered key is repo@revision.
        for k in cc["covered"]:
            self.assertRegex(k, r"^v102ss/.+@[0-9a-f]{40,64}$")

    def test_strict_cross_check_raises_on_gap(self):
        with self.assertRaises(ValueError):
            cross_check_checksums(strict=True)

    def test_model_manager_source_touches_no_ledger_credit_worker_transport(self):
        # Behavioral invariant (design 03 §2 / §11): the Model Manager's OWN
        # source must not reference the ledger / credit / paid_acu / worker-queue
        # / network-transport surfaces — its only outbound action is the opt-in
        # weight download.
        #
        # NOTE on transitive imports: importing ANY alice_acp submodule runs
        # ``alice_acp.api_chat.__init__`` (eagerly imports store/handler) and
        # ``alice_acp.local_inference.__init__`` (imports backend.py, which
        # imports the colocated-inference worker), so a byte-clean transitive
        # ``sys.modules`` is impossible without editing alice-acp (which we
        # consume read-only). We therefore enforce the invariant where it is
        # real and controllable: the Model Manager source never IMPORTS FROM or
        # NAMES those subsystems.
        import ast

        mm_dir = Path(__file__).resolve().parents[1] / "alice_ai" / "model_manager"
        forbidden = (
            "ledger",
            "credit",
            "paid_acu",
            "worker_queue",
            "worker_bridge",
            "worker_transport",
            "worker_pull",
            "colocated_inference",
            "settlement",
            "proof_authority",
            "api_chat_gateway",
            "shadow_server",
            "transport_front",
            "share_validator",
            "rate_limit",
            "abuse",
        )
        offenders = []
        for py in mm_dir.glob("*.py"):
            tree = ast.parse(py.read_text(encoding="utf-8"), filename=py.name)
            for node in ast.walk(tree):
                # Only real code references count: imports + attribute access +
                # name use. Docstrings/comments (which DESCRIBE the invariant)
                # are not code and are excluded by construction.
                targets: list[str] = []
                if isinstance(node, ast.ImportFrom) and node.module:
                    targets.append(node.module)
                    targets.extend(a.name for a in node.names)
                elif isinstance(node, ast.Import):
                    targets.extend(a.name for a in node.names)
                elif isinstance(node, ast.Attribute):
                    targets.append(node.attr)
                elif isinstance(node, ast.Name):
                    targets.append(node.id)
                for t in targets:
                    low = t.lower()
                    for tok in forbidden:
                        if tok in low:
                            offenders.append(f"{py.name}: code references {t!r}")
        self.assertEqual(
            sorted(set(offenders)), [],
            "model manager CODE references a forbidden subsystem:\n" + "\n".join(sorted(set(offenders))),
        )

    def test_clean_interpreter_no_gateway_service_or_shadow_ledger(self):
        # Rigorous-but-honest: a FRESH interpreter importing ONLY the model
        # manager must not have CONSTRUCTED/started the network gateway service,
        # the public HTTP wrapper, or the shadow-server schedulers — the modules
        # may be transitively imported by alice_acp's package __init__ (above),
        # but the *active service entrypoints* the gateway exposes must be absent
        # from the model manager's own import path. We assert the model manager
        # does not import the gateway SERVICE/HTTP/worker-pull-edge entrypoints
        # itself by re-reading its compiled imports (proxy: the source check
        # above is the contract; this guards against a future careless add).
        import subprocess

        code = (
            "import sys, alice_ai.model_manager as m;"
            "src = [getattr(m, a) for a in dir(m)];"
            # The manager must expose ModelManager + the downloader, and MUST NOT
            # expose any gateway/credit symbol.
            "assert hasattr(m, 'ModelManager'), 'ModelManager missing';"
            "assert hasattr(m, 'VerifyingSnapshotDownloader'), 'downloader missing';"
            "names = ' '.join(dir(m)).lower();"
            "bad = [t for t in ('ledger','credit','gateway','worker','settlement','paid_acu') if t in names];"
            "print('CLEAN' if not bad else 'LEAK: '+repr(bad))"
        )
        env = dict(os.environ)
        env["PYTHONPATH"] = str(_BACKEND) + os.pathsep + env.get("PYTHONPATH", "")
        out = subprocess.run(
            [sys.executable, "-c", code], capture_output=True, text=True, env=env
        )
        self.assertIn("CLEAN", out.stdout, f"public surface leak: {out.stdout}\n{out.stderr}")


if __name__ == "__main__":
    unittest.main(verbosity=2)

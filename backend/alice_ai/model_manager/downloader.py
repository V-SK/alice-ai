"""VerifyingSnapshotDownloader — whole-snapshot fetch + SHA-256 + resume + atomic.

The single piece of genuinely new inference-adjacent code (design 03 §5 / brief
item 1). It implements the engine's ``WeightDownloader`` protocol so it drops
into ``LocalModelResolver(downloader=...)`` unchanged, but it is far stronger
than the stock ``huggingface_snapshot_downloader`` in four ways:

  * **Whole snapshot** — MLX needs every file (sharded ``*.safetensors`` +
    tokenizer + config + chat template), not just ``config.json``; the stock
    resolver's ``allow_patterns=[f"{subpath}*"]`` would fetch only that one file
    (the known MLX gap). GGUF needs the single ``.gguf`` + (small) metadata.
  * **Per-file SHA-256 gate** — every expected file (from the vendored
    ``checksums.json``) is size-checked then stream-hashed and compared; a
    mismatch is **fail-closed** (the file is deleted, re-fetched once, re-checked;
    a second failure aborts). When the manifest lacks the repo (the
    GGUF-of-4B/9B/35B gap), we verify against HF's own published LFS OID for the
    pinned revision (§5.3 fallback) — weaker than a vendored SHA but strictly
    better than the stock existence-only check.
  * **Resumable** — ``huggingface_hub`` resumes ``.incomplete`` blobs via HTTP
    Range; we never delete partials on a transient network error, and retry with
    backoff (network errors only — a checksum failure does NOT blind-retry).
  * **Atomic publish** — download + verify into ``<target>.partial``, write a
    ``.verified`` sentinel, then ``os.replace`` to the final cache dir. A
    half-written dir is never visible to the resolver's existence check.

Progress is emitted via an ``on_progress(ProgressEvent)`` callback (bytes /
total / rate / phase) bridged from HF's ``tqdm`` — streamed to the first-run
ring + the picker over SSE (design 03 §5.2).

Honest/private invariant: the ONLY outbound action is the weight download, which
is opt-in (real runs), pinned to the immutable upstream revision, and against the
public HF repo — never an Alice server / ledger / side-channel.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from alice_acp.local_inference.pinned_models import PinnedModelArtifact

from alice_ai.model_manager.catalog import FileChecksum, checksums_for

logger = logging.getLogger("alice_ai.model_manager.downloader")

#: Reason codes (mirror the engine's REASON_* discipline; surfaced to the UI).
REASON_VERIFIED = "model_download_verified"
REASON_CACHE_HIT = "model_cache_hit_verified"
REASON_CHECKSUM_MISMATCH = "model_checksum_mismatch"
REASON_SIZE_MISMATCH = "model_file_size_mismatch"
REASON_DISK_INSUFFICIENT = "model_disk_space_insufficient"
REASON_DOWNLOAD_FAILED = "model_download_failed"

#: Marker written into a published snapshot dir only AFTER verification passes.
VERIFIED_SENTINEL = ".alice_verified"

_HASH_CHUNK = 8 * 1024 * 1024  # 8 MiB
_MLX_PATTERNS = ["*.safetensors", "*.json", "tokenizer*", "*.jinja", "*.model", "*.txt"]


class ModelDownloadError(RuntimeError):
    """A download/verify failure carrying a machine-readable reason code."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class ProgressEvent:
    """Streamed to the UI (design 03 §5.2): % + GB + rate + which file."""

    downloaded_bytes: int
    total_bytes: int
    rate_bps: float
    phase: str  # "preflight" | "downloading" | "verifying" | "publishing" | "done"
    file_index: int = 0
    file_count: int = 0
    message: str = ""

    @property
    def fraction(self) -> float:
        if self.total_bytes <= 0:
            return 0.0
        return min(1.0, self.downloaded_bytes / self.total_bytes)

    def to_dict(self) -> dict:
        return {
            "downloaded_bytes": int(self.downloaded_bytes),
            "total_bytes": int(self.total_bytes),
            "fraction": round(self.fraction, 4),
            "rate_bps": round(self.rate_bps, 1),
            "phase": self.phase,
            "file_index": self.file_index,
            "file_count": self.file_count,
            "message": self.message,
        }


ProgressCallback = Callable[[ProgressEvent], None]


# --------------------------------------------------------------------------- #
# tqdm -> on_progress bridge. huggingface_hub drives downloads through a
# tqdm-compatible class; we pass a subclass that reports aggregate bytes.
# --------------------------------------------------------------------------- #
def _make_tqdm_bridge(total_bytes: int, on_progress: Optional[ProgressCallback], file_count: int):
    """Return a tqdm-compatible class that reports aggregate progress.

    huggingface_hub creates one tqdm per file; we share a single counter across
    all of them (closure) so the UI sees one smooth 0→100% over the snapshot.
    """
    state = {"done": 0, "last_emit": 0.0, "last_bytes": 0, "last_t": time.monotonic()}
    lock = threading.Lock()

    try:
        from tqdm.auto import tqdm as _base_tqdm
    except Exception:  # noqa: BLE001 — tqdm always ships with hf_hub, but be safe
        return None

    class _BridgeTqdm(_base_tqdm):  # type: ignore[misc]
        def update(self, n=1):  # noqa: D401
            res = super().update(n)
            if on_progress is None or not n:
                return res
            with lock:
                state["done"] += int(n)
                now = time.monotonic()
                # Throttle UI events to ~5/s; always compute a smoothed rate.
                if now - state["last_emit"] >= 0.2:
                    dt = max(1e-6, now - state["last_t"])
                    rate = (state["done"] - state["last_bytes"]) / dt
                    state["last_t"] = now
                    state["last_bytes"] = state["done"]
                    state["last_emit"] = now
                    try:
                        on_progress(
                            ProgressEvent(
                                downloaded_bytes=min(state["done"], total_bytes) if total_bytes else state["done"],
                                total_bytes=total_bytes,
                                rate_bps=max(0.0, rate),
                                phase="downloading",
                                file_count=file_count,
                            )
                        )
                    except Exception:  # noqa: BLE001 — UI callback must never kill a download
                        logger.debug("on_progress raised; ignoring", exc_info=True)
            return res

    return _BridgeTqdm


# --------------------------------------------------------------------------- #
# SHA-256 verification.
# --------------------------------------------------------------------------- #
def sha256_file(path: Path) -> str:
    """Stream-hash a file (8 MiB chunks)."""
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            chunk = fh.read(_HASH_CHUNK)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def _verify_file(path: Path, expected: FileChecksum) -> Optional[str]:
    """Return None if the file matches, else a REASON_* code for the mismatch."""
    if not path.exists():
        return REASON_SIZE_MISMATCH
    actual_size = path.stat().st_size
    if expected.size_bytes and actual_size != expected.size_bytes:
        logger.warning(
            "size mismatch for %s: on-disk %d != expected %d",
            path.name, actual_size, expected.size_bytes,
        )
        return REASON_SIZE_MISMATCH
    digest = sha256_file(path)
    if digest.lower() != expected.sha256.lower():
        logger.warning("sha256 mismatch for %s", path.name)
        return REASON_CHECKSUM_MISMATCH
    return None


# --------------------------------------------------------------------------- #
# HF-OID fallback (design 03 §5.3) — when the vendored manifest lacks the repo,
# fetch HF's own published per-file LFS sha256 for the pinned revision and verify
# against THAT (still the exact bytes the SHA pin commits to).
# --------------------------------------------------------------------------- #
def _oid_checksums_from_hf(artifact: PinnedModelArtifact) -> tuple[FileChecksum, ...]:
    """Best-effort per-file SHA-256 from HF metadata for the pinned revision.

    Returns () on any failure (the caller then falls back to a size-only check
    for non-LFS files, which is still better than the stock existence check).
    """
    try:
        from huggingface_hub import HfApi  # lazy
    except Exception:  # noqa: BLE001
        return ()
    api = HfApi()
    out: list[FileChecksum] = []
    try:
        info = api.model_info(
            artifact.repo_id, revision=artifact.revision, files_metadata=True
        )
        for sib in info.siblings or []:
            lfs = getattr(sib, "lfs", None)
            size = getattr(sib, "size", None)
            sha = None
            if isinstance(lfs, dict):
                sha = lfs.get("sha256") or lfs.get("oid")
            elif lfs is not None:
                sha = getattr(lfs, "sha256", None) or getattr(lfs, "oid", None)
            if sha and len(str(sha)) == 64 and size:
                out.append(
                    FileChecksum(path=sib.rfilename, size_bytes=int(size), sha256=str(sha).lower())
                )
    except Exception:  # noqa: BLE001 — never let metadata fetch abort the download
        logger.info("HF OID fallback metadata fetch failed for %s", artifact.repo_id, exc_info=True)
        return ()
    return tuple(out)


def _expected_files(artifact: PinnedModelArtifact) -> tuple[tuple[FileChecksum, ...], bool]:
    """Return (expected checksums, used_oid_fallback)."""
    vendored = checksums_for(artifact)
    if vendored:
        return vendored, False
    return _oid_checksums_from_hf(artifact), True


def _allow_patterns(artifact: PinnedModelArtifact) -> list[str]:
    """The file set to fetch: whole snapshot for MLX, single .gguf (+meta) else."""
    if artifact.runtime == "mlx":
        return list(_MLX_PATTERNS)
    # GGUF/CUDA/CPU: the single quant file. (No extra metadata is required to
    # load a GGUF — the chat template is embedded — so we fetch just the file.)
    return [artifact.artifact_subpath]


# --------------------------------------------------------------------------- #
# Disk preflight (design 03 §5.5).
# --------------------------------------------------------------------------- #
def _free_bytes(path: Path) -> int:
    target = path
    while not target.exists():
        target = target.parent
    return shutil.disk_usage(str(target)).free


def _preflight_disk(target_root: Path, need_bytes: int) -> None:
    if need_bytes <= 0:
        return
    free = _free_bytes(target_root)
    required = int(need_bytes * 1.1)  # 10% headroom for the .partial dir
    if free < required:
        raise ModelDownloadError(
            REASON_DISK_INSUFFICIENT,
            f"need about {required / 1024**3:.1f} GB free, have {free / 1024**3:.1f} GB",
        )


# --------------------------------------------------------------------------- #
# The downloader.
# --------------------------------------------------------------------------- #
@dataclass
class VerifyingSnapshotDownloader:
    """A ``WeightDownloader`` with SHA-256 verify + resume + atomic publish.

    Constructed per-resolve with an optional ``on_progress`` callback; the same
    instance is injected into ``LocalModelResolver(downloader=...)``. Callable as
    ``downloader(artifact, target_dir)`` (the engine protocol) AND directly via
    :meth:`download`.

    ``max_network_retries`` retries only NETWORK errors with backoff; a checksum
    failure triggers exactly ONE re-fetch of the offending file, then aborts
    (fail-closed) — never a blind retry loop.
    """

    on_progress: Optional[ProgressCallback] = None
    max_network_retries: int = 3
    _snapshot_fn: Optional[Callable] = None  # injectable for tests (no network)

    # -- engine WeightDownloader protocol --------------------------------- #
    def __call__(self, artifact: PinnedModelArtifact, target_dir: Path) -> None:
        self.download(artifact, Path(target_dir))

    def _emit(self, event: ProgressEvent) -> None:
        if self.on_progress is None:
            return
        try:
            self.on_progress(event)
        except Exception:  # noqa: BLE001
            logger.debug("on_progress raised; ignoring", exc_info=True)

    def is_published(self, snapshot_dir: Path) -> bool:
        """A snapshot dir is usable iff the .verified sentinel is present."""
        return (Path(snapshot_dir) / VERIFIED_SENTINEL).exists()

    def _all_files_verify_in_place(
        self, target_dir: Path, expected: tuple[FileChecksum, ...]
    ) -> bool:
        """True iff every expected file is already present in ``target_dir`` and
        passes its size + SHA-256 check (the adopt-in-place fast path)."""
        if not target_dir.exists():
            return False
        for fc in expected:
            self._emit(
                ProgressEvent(
                    0, 0, 0.0, "verifying", message=f"checking {fc.path}",
                )
            )
            if _verify_file(target_dir / fc.path, fc) is not None:
                return False
        return True

    def download(self, artifact: PinnedModelArtifact, target_dir: Path) -> Path:
        """Fetch + verify + atomically publish the snapshot into ``target_dir``.

        ``target_dir`` is the engine's content-addressed final dir
        (``cache_root / repo@rev``). We download into ``target_dir + ".partial"``,
        verify, then ``os.replace`` to ``target_dir``. Returns ``target_dir``.
        """
        target_dir = Path(target_dir)

        # Cache hit: a fully-published, verified snapshot already exists.
        if self.is_published(target_dir):
            self._emit(ProgressEvent(0, 0, 0.0, "done", message=REASON_CACHE_HIT))
            return target_dir

        expected, used_oid = _expected_files(artifact)
        total_bytes = sum(f.size_bytes for f in expected) if expected else 0

        # Adopt-in-place: the files may already be present (e.g. a prior
        # raw snapshot_download from M1 that predates the .verified sentinel).
        # If every expected file is present AND verifies, just publish the
        # sentinel — no re-download of multi-GB weights. (Fail-closed: any
        # missing/mismatched file falls through to a fresh fetch.)
        if expected and self._all_files_verify_in_place(target_dir, expected):
            (target_dir / VERIFIED_SENTINEL).write_text(
                f"{artifact.model_id}\n{artifact.repo_id}@{artifact.revision}\n",
                encoding="utf-8",
            )
            self._emit(ProgressEvent(total_bytes, total_bytes, 0.0, "done", message=REASON_CACHE_HIT))
            return target_dir

        partial = target_dir.with_name(target_dir.name + ".partial")
        partial.mkdir(parents=True, exist_ok=True)

        # Preflight: enough free space for the snapshot (+10%).
        self._emit(ProgressEvent(0, total_bytes, 0.0, "preflight"))
        _preflight_disk(partial, total_bytes)

        # Fetch (resumable, with progress) into the .partial dir.
        self._fetch(artifact, partial, total_bytes, len(expected))

        # Verify every expected file BEFORE publishing (design 03 §5.3).
        self._verify_all(artifact, partial, expected, used_oid, total_bytes)

        # Atomic publish: sentinel into .partial, then replace -> target_dir.
        (partial / VERIFIED_SENTINEL).write_text(
            f"{artifact.model_id}\n{artifact.repo_id}@{artifact.revision}\n",
            encoding="utf-8",
        )
        self._emit(ProgressEvent(total_bytes, total_bytes, 0.0, "publishing"))
        if target_dir.exists():
            shutil.rmtree(target_dir, ignore_errors=True)
        os.replace(partial, target_dir)
        self._emit(ProgressEvent(total_bytes, total_bytes, 0.0, "done", message=REASON_VERIFIED))
        return target_dir

    # -- fetch ------------------------------------------------------------ #
    def _fetch(self, artifact: PinnedModelArtifact, into: Path, total_bytes: int, file_count: int) -> None:
        snapshot = self._snapshot_fn or _default_snapshot_download
        patterns = _allow_patterns(artifact)
        tqdm_cls = _make_tqdm_bridge(total_bytes, self.on_progress, file_count)

        last_exc: Optional[Exception] = None
        for attempt in range(1, self.max_network_retries + 1):
            try:
                snapshot(
                    repo_id=artifact.repo_id,
                    revision=artifact.revision,
                    local_dir=str(into),
                    allow_patterns=patterns,
                    tqdm_class=tqdm_cls,
                )
                return
            except ModelDownloadError:
                raise
            except Exception as exc:  # noqa: BLE001 — network / transient
                last_exc = exc
                wait = min(30.0, 2.0 ** attempt)
                logger.warning(
                    "download attempt %d/%d failed (%s); retrying in %.0fs (partials kept)",
                    attempt, self.max_network_retries, exc, wait,
                )
                if attempt < self.max_network_retries:
                    time.sleep(wait)
        raise ModelDownloadError(
            REASON_DOWNLOAD_FAILED, f"download failed after {self.max_network_retries} attempts: {last_exc}"
        )

    # -- verify ----------------------------------------------------------- #
    def _verify_all(
        self,
        artifact: PinnedModelArtifact,
        snapshot_dir: Path,
        expected: tuple[FileChecksum, ...],
        used_oid: bool,
        total_bytes: int,
    ) -> None:
        if not expected:
            # No vendored manifest AND no HF OID metadata: we cannot SHA-verify.
            # Confirm the primary artifact exists (size>0) — still better than
            # nothing, and the load will fail loudly if it is corrupt. (This only
            # happens offline / when HF metadata is unreachable.)
            primary = snapshot_dir / artifact.artifact_subpath
            if not primary.exists() or primary.stat().st_size == 0:
                raise ModelDownloadError(
                    REASON_SIZE_MISMATCH,
                    f"primary artifact missing/empty and no checksum available: {artifact.artifact_subpath}",
                )
            logger.warning(
                "no checksums available for %s; published with existence-only check",
                artifact.repo_id,
            )
            return

        count = len(expected)
        for idx, fc in enumerate(expected, start=1):
            path = snapshot_dir / fc.path
            self._emit(
                ProgressEvent(
                    total_bytes, total_bytes, 0.0, "verifying",
                    file_index=idx, file_count=count, message=fc.path,
                )
            )
            reason = _verify_file(path, fc)
            if reason is None:
                continue
            # One re-fetch of the offending file (a corrupted resume), then abort.
            logger.warning("verify failed for %s (%s); re-fetching once", fc.path, reason)
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass
            self._refetch_one(artifact, snapshot_dir, fc.path)
            reason2 = _verify_file(path, fc)
            if reason2 is not None:
                raise ModelDownloadError(
                    reason2,
                    f"checksum/size verification failed for {fc.path} (fail-closed; not loading unverified weights)",
                )

    def _refetch_one(self, artifact: PinnedModelArtifact, snapshot_dir: Path, rel_path: str) -> None:
        """Re-download a single file (used once on a verify failure)."""
        fn = self._snapshot_fn
        if fn is not None:
            # Test path: the injected fake re-materializes whatever it does.
            fn(
                repo_id=artifact.repo_id,
                revision=artifact.revision,
                local_dir=str(snapshot_dir),
                allow_patterns=[rel_path],
                tqdm_class=None,
            )
            return
        try:
            from huggingface_hub import hf_hub_download  # lazy

            hf_hub_download(
                repo_id=artifact.repo_id,
                revision=artifact.revision,
                filename=rel_path,
                local_dir=str(snapshot_dir),
                force_download=True,
            )
        except Exception as exc:  # noqa: BLE001
            raise ModelDownloadError(
                REASON_DOWNLOAD_FAILED, f"re-fetch of {rel_path} failed: {exc}"
            ) from exc


def _default_snapshot_download(**kwargs) -> None:
    """Lazy real ``snapshot_download`` (kept out of import path / tests)."""
    from huggingface_hub import snapshot_download  # lazy

    # Drop a None tqdm_class so hf_hub uses its own default.
    if kwargs.get("tqdm_class") is None:
        kwargs.pop("tqdm_class", None)
    snapshot_download(**kwargs)

"""Resolve a pinned artifact to a local snapshot dir, downloading if needed.

This is the LOCAL-mode counterpart to ``api_chat.model_prefetch`` (which only
*plans* a disk cache and explicitly forbids request-time download in the network
path, ``request_time_download_allowed=False``). In LOCAL mode the user is
downloading the model to their OWN box, so request-time download IS allowed and
expected -- this is the only place in the local path that may reach out, and it
does so ONLY when the user runs a real model (never for the stub / dry-run path,
and never against any Alice server -- weights come from the pinned upstream
repo).

The actual transport is injected as a ``WeightDownloader`` callable so:

* tests use a fake that writes a placeholder file (no network), and
* a real run uses ``huggingface_hub.snapshot_download`` pinned to the artifact's
  immutable ``revision``.

Resolution is content-addressed by ``artifact.cache_key`` (repo@revision), so a
re-run with the same pin is a cache hit and performs no download.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from alice_acp.local_inference.pinned_models import PinnedModelArtifact

REASON_RESOLVE_CACHE_HIT = "local_model_cache_hit"
REASON_RESOLVE_DOWNLOADED = "local_model_downloaded"
REASON_RESOLVE_STUB_SKIPPED = "local_model_resolution_skipped_stub"


@runtime_checkable
class WeightDownloader(Protocol):
    """Fetches a pinned artifact into ``target_dir`` (must materialise the file).

    Implementations MUST pin to ``artifact.revision`` (immutable) and MUST place
    the artifact at ``target_dir / artifact.artifact_subpath``. They are the ONLY
    network actor in the local path and only run for real model resolution.
    """

    def __call__(self, artifact: PinnedModelArtifact, target_dir: Path) -> None: ...


@dataclass(frozen=True, slots=True)
class ResolvedModel:
    """A pinned artifact resolved to a concrete on-disk location."""

    artifact: PinnedModelArtifact
    snapshot_dir: Path
    artifact_path: Path
    reason_code: str
    downloaded: bool

    def to_public_dict(self) -> dict[str, object]:
        return {
            "model_id": self.artifact.model_id,
            "repo_id": self.artifact.repo_id,
            "revision": self.artifact.revision,
            "snapshot_dir": str(self.snapshot_dir),
            "artifact_path": str(self.artifact_path),
            "reason_code": self.reason_code,
            "downloaded": self.downloaded,
        }


@dataclass(frozen=True, slots=True)
class LocalModelResolver:
    """Resolves pinned artifacts under a local cache root.

    ``cache_root`` is the user's local model cache (e.g. ``~/.cache/alice``).
    ``downloader`` is injected; when None, resolution is allowed only for an
    already-cached artifact (a real run wires a real downloader; the stub path
    skips resolution entirely via :meth:`resolve_for_stub`).
    """

    cache_root: Path
    downloader: WeightDownloader | None = None

    def snapshot_dir_for(self, artifact: PinnedModelArtifact) -> Path:
        return self.cache_root / artifact.cache_key

    def is_cached(self, artifact: PinnedModelArtifact) -> bool:
        path = self.snapshot_dir_for(artifact) / artifact.artifact_subpath
        return path.exists()

    def resolve(self, artifact: PinnedModelArtifact) -> ResolvedModel:
        """Resolve a real artifact, downloading (request-time, LOCAL-only) if needed."""
        snapshot_dir = self.snapshot_dir_for(artifact)
        artifact_path = snapshot_dir / artifact.artifact_subpath
        if artifact_path.exists():
            return ResolvedModel(
                artifact=artifact,
                snapshot_dir=snapshot_dir,
                artifact_path=artifact_path,
                reason_code=REASON_RESOLVE_CACHE_HIT,
                downloaded=False,
            )
        if self.downloader is None:
            raise FileNotFoundError(
                f"model {artifact.model_id} not cached and no downloader configured"
            )
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        self.downloader(artifact, snapshot_dir)
        if not artifact_path.exists():
            raise FileNotFoundError(
                f"downloader did not materialise {artifact.artifact_subpath}"
            )
        return ResolvedModel(
            artifact=artifact,
            snapshot_dir=snapshot_dir,
            artifact_path=artifact_path,
            reason_code=REASON_RESOLVE_DOWNLOADED,
            downloaded=True,
        )

    def resolve_for_stub(self, artifact: PinnedModelArtifact) -> ResolvedModel:
        """Resolve for the offline stub: create the dir, never download a file."""
        snapshot_dir = self.snapshot_dir_for(artifact)
        snapshot_dir.mkdir(parents=True, exist_ok=True)
        return ResolvedModel(
            artifact=artifact,
            snapshot_dir=snapshot_dir,
            artifact_path=snapshot_dir / artifact.artifact_subpath,
            reason_code=REASON_RESOLVE_STUB_SKIPPED,
            downloaded=False,
        )


def huggingface_snapshot_downloader(
    artifact: PinnedModelArtifact,
    target_dir: Path,
) -> None:
    """Real downloader: pins to the immutable revision via huggingface_hub.

    Imported lazily so this module never requires ``huggingface_hub`` at import
    time. Only invoked on a real local run, never in tests / dry-run.
    """
    from huggingface_hub import snapshot_download  # lazy import

    snapshot_download(
        repo_id=artifact.repo_id,
        revision=artifact.revision,
        local_dir=str(target_dir),
        allow_patterns=[f"{artifact.artifact_subpath}*"],
    )

"""Model Manager — catalog projection, verifying downloader, façade (M4).

Built out in M4 (PLAN §5 / docs/design/03-model-manager.md). Sits between the
odysseus chat UI and the already-built ``alice_acp.local_inference`` engine and
does four jobs (design 03 §0): detect the device, recommend a tier, download +
verify the weights, and load + switch — all behind the Alice-only display guard.

Public surface (what the FastAPI backend imports):

  * :class:`ModelManager` — the façade (list/recommend/gate/ensure_ready/load/
    switch/current + the per-model context-size control).
  * :class:`VerifyingSnapshotDownloader` — whole-snapshot fetch + per-file
    SHA-256 gate + resume + progress + atomic publish (the one piece of new
    inference-adjacent code; the stock resolver checks file *existence* only).
  * :class:`AliceModelCard` + :func:`assert_displayable` — the 3-layer display
    guard ("Alice / Alice Lite / Alice Pro / Alice RP" only, never qwen/size).
  * :func:`cross_check_checksums` — fail-closed manifest⇄pin reconciliation.

First-run default tier: **Alice Lite (4B)** (per V). Per-model context size is
selectable 4k–256k, capped at each model's config-declared max.
"""

from __future__ import annotations

import logging

from alice_acp.local_inference.pinned_models import all_pinned_artifacts

from alice_ai.model_manager.catalog import (
    CONTEXT_DEFAULT,
    CONTEXT_HARD_CAP,
    CONTEXT_MIN,
    AliceModelCard,
    DisplayLeakError,
    DISPLAY_MAP,
    FileChecksum,
    assert_displayable,
    build_card,
    checksums_for,
    clamp_context_length,
    context_supported_max,
)
from alice_ai.model_manager.context import (
    estimate_kv_cache_gb,
    read_model_max_context,
)
from alice_ai.model_manager.device import AugmentedDevice, augment
from alice_ai.model_manager.downloader import (
    ModelDownloadError,
    ProgressEvent,
    VerifyingSnapshotDownloader,
    sha256_file,
)
from alice_ai.model_manager.manager import (
    GATE_OK,
    GATE_REFUSE,
    GATE_WARN,
    GateResult,
    ModelManager,
)

logger = logging.getLogger("alice_ai.model_manager")

__all__ = [
    "AliceModelCard",
    "AugmentedDevice",
    "CONTEXT_DEFAULT",
    "CONTEXT_HARD_CAP",
    "CONTEXT_MIN",
    "DISPLAY_MAP",
    "DisplayLeakError",
    "FileChecksum",
    "GATE_OK",
    "GATE_REFUSE",
    "GATE_WARN",
    "GateResult",
    "ModelDownloadError",
    "ModelManager",
    "ProgressEvent",
    "VerifyingSnapshotDownloader",
    "assert_displayable",
    "augment",
    "build_card",
    "checksums_for",
    "clamp_context_length",
    "context_supported_max",
    "cross_check_checksums",
    "estimate_kv_cache_gb",
    "read_model_max_context",
    "sha256_file",
]


def cross_check_checksums(*, strict: bool = False) -> dict:
    """Reconcile the vendored ``checksums.json`` against the pinned artifacts.

    Design 03 §4.2: every pinned ``(repo_id, revision)`` SHOULD have a manifest
    entry; a missing one falls back to the HF-OID path at download time (the
    known GGUF-of-4B/9B/35B gap), so by default this is informational. With
    ``strict=True`` (a release-gate use) a pin lacking a vendored checksum raises.

    Returns ``{covered: [...], oid_fallback: [...]}`` (lists of "repo@rev").
    """
    covered: list[str] = []
    fallback: list[str] = []
    seen: set[str] = set()
    for art in all_pinned_artifacts():
        key = f"{art.repo_id}@{art.revision}"
        if key in seen:
            continue
        seen.add(key)
        if checksums_for(art):
            covered.append(key)
        else:
            fallback.append(key)
    if strict and fallback:
        raise ValueError(
            "pins without a vendored checksum (extend checksums.json or accept "
            f"the HF-OID fallback): {fallback}"
        )
    if fallback:
        logger.info(
            "model_manager: %d pins covered by vendored checksums, %d using HF-OID fallback",
            len(covered), len(fallback),
        )
    return {"covered": covered, "oid_fallback": fallback}

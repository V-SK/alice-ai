"""VPS-side model PIN + download LOGIC (no real download here).

The CPU verification VPS scores a served completion under the REAL model the
worker CLAIMED (``model_ref``). To do that at runtime it must load that exact
pinned model. This module resolves the worker-claimed ``model_ref`` /
(tier, runtime) to the SAME immutable pin Track A uses (``pinned_artifact``) and
expresses the DOWNLOAD LOGIC (repo + immutable revision SHA + content-addressed
cache key) WITHOUT performing any I/O.

HARD CONSTRAINT (do NOT weaken): nothing here downloads multi-GB weights. The
real fetch happens at VPS RUNTIME via the SAME ``WeightDownloader`` seam the
local shell uses (``local_inference.model_resolver``), pinned to
``artifact.revision``. This module only *describes* what to fetch (the pin) and
hands back a plan; the SHA is the pin, fetching it is free until runtime.
"""

from __future__ import annotations

from dataclasses import dataclass

from alice_acp.api_chat.model_catalog import ModelRuntimeFamily
from alice_acp.api_chat.types import ApiChatModelClass
from alice_acp.local_inference.pinned_models import (
    PinnedModelArtifact,
    PinnedModelLookupError,
    all_pinned_artifacts,
    pinned_artifact,
)

VERIFICATION_VPS_MODEL_PIN_CONTRACT_VERSION = "alice-verification-vps-model-pin-contract-v1"


@dataclass(frozen=True, slots=True)
class VpsModelDownloadPlan:
    """What the VPS must fetch to score under a model (the PIN, not the bytes).

    ``repo_id`` + ``revision`` are the immutable upstream pin (a public HF repo +
    commit SHA); ``cache_key`` is the content-addressed on-disk dir
    (``repo@revision``). ``download_performed`` is ALWAYS ``False`` here -- the
    real fetch is a runtime step. The VPS scorer loads from the resolved
    ``cache_key`` dir once the runtime downloader has populated it.
    """

    model_ref: str
    model_class: ApiChatModelClass
    runtime: ModelRuntimeFamily
    repo_id: str
    revision: str
    cache_key: str
    download_performed: bool = False

    def to_public_dict(self) -> dict[str, object]:
        return {
            "contract_version": VERIFICATION_VPS_MODEL_PIN_CONTRACT_VERSION,
            "model_ref": self.model_ref,
            "model_class": self.model_class,
            "runtime": self.runtime,
            "repo_id": self.repo_id,
            "revision": self.revision,
            "cache_key": self.cache_key,
            # No multi-GB download happens at plan time -- runtime only.
            "download_performed": False,
        }


def _plan_from_artifact(artifact: PinnedModelArtifact) -> VpsModelDownloadPlan:
    return VpsModelDownloadPlan(
        model_ref=artifact.model_id,
        model_class=artifact.model_class,
        runtime=artifact.runtime,
        repo_id=artifact.repo_id,
        revision=artifact.revision,
        cache_key=artifact.cache_key,
    )


def download_plan_for_tier(
    model_class: ApiChatModelClass,
    runtime: ModelRuntimeFamily,
) -> VpsModelDownloadPlan:
    """Resolve the VPS download plan for a (tier, runtime) from the immutable pin.

    Raises :class:`PinnedModelLookupError` (fail-closed) when (tier, runtime) has
    no pinned artifact -- the VPS will not guess a model to score under.
    """
    return _plan_from_artifact(pinned_artifact(model_class, runtime))


def download_plan_for_model_ref(model_ref: str) -> VpsModelDownloadPlan:
    """Resolve the VPS download plan from the worker-claimed ``model_ref``.

    The worker submits the pinned ``alice-...@quant`` id it claims it ran; the VPS
    must score under THAT exact pin. We map the ref back to its pinned artifact so
    the VPS fetches the same immutable repo@revision. Raises
    :class:`PinnedModelLookupError` for an unknown ref (a worker cannot make the
    VPS load an unpinned model).
    """
    for artifact in all_pinned_artifacts():
        if artifact.model_id == model_ref:
            return _plan_from_artifact(artifact)
    raise PinnedModelLookupError(f"no pinned artifact for model_ref {model_ref!r}")


VPS_DOWNLOAD_RUNTIME_TODO = (
    "RUNTIME (CPU VPS): fetch the planned repo@revision via the SAME WeightDownloader "
    "seam the local shell uses (local_inference.model_resolver.huggingface_snapshot_"
    "downloader), pinned to artifact.revision, into the cache_key dir; then load ONLY "
    "what the logprob forward pass needs. The SHA is pinned here; the multi-GB fetch "
    "happens at VPS runtime, never in this repo / tests."
)

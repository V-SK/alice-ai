"""Worker auto-select-and-download by free VRAM (M0 task #3, V directive).

On startup / first AI job a GPU worker self-provisions WITHOUT any server push:

1. Detect its GPU class + free VRAM (``probe_local_host`` already gives the
   class; the free-VRAM number is injected -- the host probe leaves ``vram_gb``
   ``None`` for a discrete GPU because querying it accurately is a follow-up, so
   the worker passes the measured free VRAM in explicitly, e.g. from
   ``nvidia-smi --query-gpu=memory.free``).
2. Pick the runtime FORMAT for its class: Apple -> MLX, NVIDIA -> GGUF (CUDA
   llama.cpp), AMD -> GGUF (ROCm), CPU -> GGUF (CPU); the GGUF family is the
   "fallback GGUF/Metal" path for everything that is not Apple-MLX.
3. Pick the LARGEST catalog tier whose CHOSEN quant fits free VRAM (the
   per-(tier, quant) floor in ``model_catalog.QUANT_VRAM_FLOOR_GB``) AND that has
   a REAL pinned artifact for that runtime (``pinned_models``).
4. Trigger the download of that one artifact via the existing
   ``LocalModelResolver`` (request-time download is allowed in worker/LOCAL mode;
   it is the ONLY network actor and only runs for a real selected model).
5. Advertise EXACTLY the tiers it can serve -- every tier that fits its VRAM on
   its runtime (so a 24 GB GPU advertises 9B + 4B, a 48 GB GPU adds 27B, etc.).

Nothing here downloads multi-GB weights at import or plan time: planning only
reads the pinned SHA/floor; :func:`provision_worker` performs the single real
download for the SELECTED tier (and is the seam tests stub out).

Credit-only: this module never touches payout/reward/paid_acu. It only decides
*what model to run* and *what tiers to advertise*.
"""

from __future__ import annotations

from dataclasses import dataclass

from alice_acp.api_chat.model_catalog import (
    DEFAULT_GENERAL_LADDER,
    FALLBACK_RUNTIME,
    GPU_CLASS_RUNTIME,
    MODEL_PROFILES,
    GpuClass,
    ModelRuntimeFamily,
    canonical_model_class,
    quant_vram_floor_gb,
)
from alice_acp.api_chat.types import ApiChatModelClass
from alice_acp.local_inference.model_resolver import LocalModelResolver, ResolvedModel
from alice_acp.local_inference.pinned_models import (
    PinnedModelArtifact,
    pinned_artifact,
    runtime_can_serve,
)
from alice_acp.mining_device.detector import detect_backend_capability
from alice_acp.mining_device.types import DeviceProbe

REASON_VRAM_SELECT_OK = "worker_vram_tier_selected"
REASON_VRAM_SELECT_NO_GPU_CLASS = "worker_vram_no_gpu_class"
REASON_VRAM_SELECT_NO_TIER_FITS = "worker_vram_no_tier_fits"


class WorkerVramSelectionError(RuntimeError):
    """No tier could be auto-selected for the worker's GPU class + free VRAM."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def gpu_class_for_probe(probe: DeviceProbe) -> GpuClass | None:
    """Map a hardware probe to the GPU class the catalog keys runtimes on.

    Returns None when the host has no AI-capable runtime at all (so the caller
    fails closed rather than advertising a tier it cannot serve). A cpu host is
    a valid class (smallest tier on a llama.cpp CPU build).
    """
    capability = detect_backend_capability(probe)
    if probe.vendor == "apple" or probe.device_kind == "apple_silicon":
        return "apple" if capability.ai_supported else None
    if probe.device_kind == "gpu" and probe.vendor == "nvidia":
        return "nvidia" if capability.ai_supported else None
    if probe.device_kind == "gpu" and probe.vendor == "amd":
        return "amd" if capability.ai_supported else None
    if probe.device_kind == "cpu":
        return "cpu"
    return None


def runtime_for_gpu_class_with_fallback(
    gpu_class: GpuClass,
    *,
    prefer_fallback: bool = False,
) -> ModelRuntimeFamily:
    """Runtime FORMAT for a class; ``prefer_fallback`` forces GGUF/Metal.

    Apple normally runs MLX, but ``prefer_fallback=True`` (e.g. a Mac without a
    usable MLX install) routes it to the GGUF/Metal llama.cpp path instead --
    the V directive's "fallback GGUF/Metal".
    """
    if prefer_fallback:
        return FALLBACK_RUNTIME
    return GPU_CLASS_RUNTIME[gpu_class]


@dataclass(frozen=True, slots=True)
class WorkerProvisionPlan:
    """The chosen tier/runtime/artifact + the full advertised capability set.

    ``selected_*`` is the LARGEST tier that fits (the one to download + warm);
    ``advertised_tiers`` is every tier the worker can serve on this runtime given
    its free VRAM (the capability the worker registers). ``runtime`` is the
    catalog runtime family the worker advertises.
    """

    gpu_class: GpuClass
    runtime: ModelRuntimeFamily
    free_vram_gb: int
    selected_model_class: ApiChatModelClass
    selected_artifact: PinnedModelArtifact
    advertised_tiers: tuple[ApiChatModelClass, ...]
    reason_code: str = REASON_VRAM_SELECT_OK

    def to_public_dict(self) -> dict[str, object]:
        return {
            "gpu_class": self.gpu_class,
            "runtime": self.runtime,
            "free_vram_gb": self.free_vram_gb,
            "selected_model_class": self.selected_model_class,
            "selected_model_id": self.selected_artifact.model_id,
            "selected_repo_id": self.selected_artifact.repo_id,
            "selected_revision": self.selected_artifact.revision,
            "selected_quant": self.selected_artifact.quant,
            "selected_min_vram_gb": self.selected_artifact.min_vram_gb,
            "advertised_tiers": list(self.advertised_tiers),
            "reason_code": self.reason_code,
        }


def _fits(model_class: ApiChatModelClass, runtime: ModelRuntimeFamily, free_vram_gb: int) -> bool:
    """True if (tier, runtime) is pinned AND its quant's VRAM floor fits."""
    canonical = canonical_model_class(model_class)
    if not runtime_can_serve(canonical, runtime):
        return False
    artifact = pinned_artifact(canonical, runtime)
    floor = quant_vram_floor_gb(canonical, artifact.quant)
    if floor is None:
        return False
    return free_vram_gb >= floor


def select_tier_for_vram(
    gpu_class: GpuClass,
    free_vram_gb: int,
    *,
    candidate_tiers: tuple[ApiChatModelClass, ...] = DEFAULT_GENERAL_LADDER,
    prefer_fallback: bool = False,
) -> WorkerProvisionPlan:
    """Pick the largest tier that fits free VRAM on this class's runtime.

    ``candidate_tiers`` is the ladder to choose from (best first); the default is
    the four general tiers. Raises :class:`WorkerVramSelectionError` (fail-closed)
    when no candidate tier has a pinned artifact whose quant fits the free VRAM.
    """
    if free_vram_gb < 0:
        raise ValueError("free_vram_gb must be non-negative")
    if not candidate_tiers:
        raise ValueError("candidate_tiers must not be empty")

    runtime = runtime_for_gpu_class_with_fallback(gpu_class, prefer_fallback=prefer_fallback)

    fitting: list[ApiChatModelClass] = []
    selected: ApiChatModelClass | None = None
    for tier in candidate_tiers:
        canonical = canonical_model_class(tier)
        if canonical not in MODEL_PROFILES:
            continue
        if not _fits(canonical, runtime, free_vram_gb):
            continue
        if selected is None:
            selected = canonical  # ladder is best-first, so the first hit is largest
        if canonical not in fitting:
            fitting.append(canonical)

    if selected is None:
        raise WorkerVramSelectionError(
            REASON_VRAM_SELECT_NO_TIER_FITS,
            f"no candidate tier fits {free_vram_gb} GB free VRAM on runtime {runtime!r} "
            f"(gpu_class={gpu_class})",
        )

    return WorkerProvisionPlan(
        gpu_class=gpu_class,
        runtime=runtime,
        free_vram_gb=free_vram_gb,
        selected_model_class=selected,
        selected_artifact=pinned_artifact(selected, runtime),
        advertised_tiers=tuple(fitting),
    )


def plan_worker_provision(
    probe: DeviceProbe,
    free_vram_gb: int,
    *,
    candidate_tiers: tuple[ApiChatModelClass, ...] = DEFAULT_GENERAL_LADDER,
    prefer_fallback: bool = False,
) -> WorkerProvisionPlan:
    """Probe -> GPU class -> VRAM tier plan (no download). Fail-closed.

    Convenience wrapper: classify the host, then delegate to
    :func:`select_tier_for_vram`. Raises if the host has no AI runtime.
    """
    gpu_class = gpu_class_for_probe(probe)
    if gpu_class is None:
        raise WorkerVramSelectionError(
            REASON_VRAM_SELECT_NO_GPU_CLASS,
            "host has no AI-capable GPU class (cannot auto-provision a worker)",
        )
    return select_tier_for_vram(
        gpu_class,
        free_vram_gb,
        candidate_tiers=candidate_tiers,
        prefer_fallback=prefer_fallback,
    )


def provision_worker(
    plan: WorkerProvisionPlan,
    resolver: LocalModelResolver,
) -> ResolvedModel:
    """Download (or cache-hit) the SELECTED artifact via the resolver.

    This is the one place a real, multi-GB download is triggered -- and only for
    the single selected tier. ``resolver`` is injected so tests stub the
    download; a real worker wires a resolver with the HF snapshot downloader.
    Returns the resolved on-disk model for the runtime adapter to load.
    """
    return resolver.resolve(plan.selected_artifact)

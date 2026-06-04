"""Map detected hardware -> (model tier, runtime, pinned artifact) for LOCAL run.

This is the bridge from ``mining_device.detector`` (which classifies a host into
nvidia / amd-ROCm / apple-Metal / cpu) to a concrete local-inference plan: the
runtime family to use, the best catalog tier the host's memory can serve, and
the pinned artifact to load. It REUSES ``detect_backend_capability`` rather than
re-implementing hardware detection.

Selection is deterministic and offline. It does NOT download anything and does
NOT touch the network or any ledger -- it only decides *what* the local shell
should resolve + load.

Runtime mapping (from the detected backend):

* apple Metal  -> ``mlx``   (MLX on Apple silicon)
* nvidia CUDA  -> ``cuda``  (CUDA-accelerated llama.cpp / GGUF)
* amd ROCm     -> ``gguf``  (llama.cpp ROCm/HIP build; the catalog runtime
                             family is ``gguf`` -- there is no distinct ``rocm``
                             ``ModelRuntimeFamily`` value)
* cpu          -> ``cpu``   (llama.cpp CPU)

Tier choice: among the candidate tiers (default = the four general tiers, best
first), pick the largest whose ``minimum_memory_gb`` fits the host's usable
memory AND that has a pinned artifact for the chosen runtime. This mirrors the
catalog's fallback ladders without involving the network scheduler.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from alice_acp.api_chat.model_catalog import (
    DEFAULT_GENERAL_LADDER,
    DEFAULT_ROLEPLAY_LADDER,
    MODEL_PROFILES,
    ModelRuntimeFamily,
    canonical_model_class,
)
from alice_acp.api_chat.types import ApiChatModelClass
from alice_acp.local_inference.pinned_models import (
    PinnedModelArtifact,
    pinned_artifact,
    runtime_can_serve,
)
from alice_acp.mining_device.detector import detect_backend_capability
from alice_acp.mining_device.types import DeviceProbe

REASON_LOCAL_SELECT_OK = "local_inference_hardware_selected"
REASON_LOCAL_SELECT_NO_RUNTIME = "local_inference_no_inference_runtime"
REASON_LOCAL_SELECT_NO_TIER = "local_inference_no_tier_fits_memory"

# Best-first tier ladders are defined in model_catalog (the tier source of
# truth) and re-exported here for backward compatibility with existing callers
# that import them from this module.
__all__ = [
    "DEFAULT_GENERAL_LADDER",
    "DEFAULT_ROLEPLAY_LADDER",
    "HostMemoryHint",
    "LocalHardwareSelectionError",
    "LocalRuntimePlan",
    "plan_for_explicit_tier",
    "runtime_for_probe",
    "select_local_runtime",
    "usable_memory_gb",
]


class LocalHardwareSelectionError(RuntimeError):
    """The detected hardware cannot run any pinned local model."""

    def __init__(self, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


@dataclass(frozen=True, slots=True)
class LocalRuntimePlan:
    """A concrete, offline plan for running one tier locally on this host."""

    runtime: ModelRuntimeFamily
    model_class: ApiChatModelClass
    artifact: PinnedModelArtifact
    usable_memory_gb: int
    detected_backend: str
    reason_code: str = REASON_LOCAL_SELECT_OK

    def to_public_dict(self) -> dict[str, object]:
        return {
            "runtime": self.runtime,
            "model_class": self.model_class,
            "model_id": self.artifact.model_id,
            "repo_id": self.artifact.repo_id,
            "revision": self.artifact.revision,
            "quant": self.artifact.quant,
            "is_moe": self.artifact.is_moe,
            "usable_memory_gb": self.usable_memory_gb,
            "detected_backend": self.detected_backend,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True, slots=True)
class HostMemoryHint:
    """Usable inference memory for the host.

    For a GPU runtime this is VRAM; for Apple unified memory / cpu it is system
    RAM. The probe carries ``vram_gb``; system RAM is supplied explicitly
    because ``DeviceProbe`` does not model it.
    """

    system_memory_gb: int = 0
    field_note: str = field(default="", repr=False)

    def __post_init__(self) -> None:
        if self.system_memory_gb < 0:
            raise ValueError("system_memory_gb must be non-negative")


def runtime_for_probe(probe: DeviceProbe) -> ModelRuntimeFamily | None:
    """Return the local inference runtime for a probe, or None if unsupported.

    Delegates hardware classification to ``detect_backend_capability`` and maps
    its result to a ``ModelRuntimeFamily``. Returns None when the host has no
    AI-capable inference runtime (e.g. cpu-only is mining-idle capable but the
    detector marks ``ai_supported=False`` -- we still allow a cpu *llama.cpp*
    fallback here because LOCAL private inference is the whole point, but only
    when the device kind is genuinely cpu).
    """
    capability = detect_backend_capability(probe)
    if probe.vendor == "apple" or probe.device_kind == "apple_silicon":
        return "mlx" if capability.ai_supported else None
    if probe.device_kind == "gpu" and probe.vendor == "nvidia":
        return "cuda" if capability.ai_supported else None
    if probe.device_kind == "gpu" and probe.vendor == "amd":
        return "gguf" if capability.ai_supported else None
    if probe.device_kind == "cpu":
        # CPU llama.cpp is slow but valid for the smallest tier; allow it for
        # the LOCAL private path even though it is not a *mining* AI lane.
        return "cpu"
    return None


def usable_memory_gb(probe: DeviceProbe, memory: HostMemoryHint) -> int:
    """Usable inference memory: VRAM for discrete GPUs, else system RAM."""
    if probe.device_kind == "gpu" and probe.vram_gb is not None:
        return probe.vram_gb
    return memory.system_memory_gb


def select_local_runtime(
    probe: DeviceProbe,
    memory: HostMemoryHint,
    *,
    candidate_tiers: tuple[ApiChatModelClass, ...] = DEFAULT_GENERAL_LADDER,
) -> LocalRuntimePlan:
    """Pick a runtime + best-fitting pinned tier for this host.

    Raises :class:`LocalHardwareSelectionError` (fail-closed) when there is no
    inference runtime or no tier fits memory + has a pin for the runtime.
    """
    if not candidate_tiers:
        raise ValueError("candidate_tiers must not be empty")

    runtime = runtime_for_probe(probe)
    capability = detect_backend_capability(probe)
    if runtime is None:
        raise LocalHardwareSelectionError(
            REASON_LOCAL_SELECT_NO_RUNTIME,
            f"no local inference runtime for backend {capability.backend!r}",
        )

    mem = usable_memory_gb(probe, memory)
    for tier in candidate_tiers:
        canonical = canonical_model_class(tier)
        profile = MODEL_PROFILES.get(canonical)
        if profile is None:
            continue
        if mem < profile.minimum_memory_gb:
            continue
        if not runtime_can_serve(canonical, runtime):
            continue
        return LocalRuntimePlan(
            runtime=runtime,
            model_class=canonical,
            artifact=pinned_artifact(canonical, runtime),
            usable_memory_gb=mem,
            detected_backend=capability.backend,
        )

    raise LocalHardwareSelectionError(
        REASON_LOCAL_SELECT_NO_TIER,
        f"no candidate tier fits {mem} GB usable memory on runtime {runtime!r}",
    )


def plan_for_explicit_tier(
    probe: DeviceProbe,
    memory: HostMemoryHint,
    model_class: ApiChatModelClass,
) -> LocalRuntimePlan:
    """Plan for a user-pinned tier (honours the host's runtime + memory floor)."""
    return select_local_runtime(probe, memory, candidate_tiers=(model_class,))

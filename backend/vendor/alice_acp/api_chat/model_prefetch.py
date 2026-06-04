from __future__ import annotations

from dataclasses import dataclass

from alice_acp.api_chat.model_catalog import (
    MODEL_PROFILES,
    canonical_model_class,
)
from alice_acp.api_chat.scheduler import _runtime_for_device, _supports_lane
from alice_acp.api_chat.types import ApiChatInferenceDevice, ApiChatModelClass

REASON_PREFETCH_NOT_ELIGIBLE = "api_chat_model_prefetch_not_eligible"
REASON_PREFETCH_PLANNED = "api_chat_model_prefetch_planned"
REASON_WARM_LOAD_DISABLED = "api_chat_resident_warm_load_disabled"
REASON_WARM_LOAD_CONFLICTS_WITH_GPU_MINING = "api_chat_warm_load_conflicts_with_gpu_mining"
REASON_WARM_LOAD_PLANNED = "api_chat_resident_warm_load_planned"


@dataclass(frozen=True, slots=True)
class ApiChatModelPrefetchRequest:
    device: ApiChatInferenceDevice
    candidate_model_classes: tuple[ApiChatModelClass, ...]
    resident_warm_load_allowed: bool = False
    gpu_mining_active: bool = False
    memory_headroom_gb: int = 0

    def __post_init__(self) -> None:
        if not self.candidate_model_classes:
            raise ValueError("candidate_model_classes must not be empty")
        if self.memory_headroom_gb < 0:
            raise ValueError("memory_headroom_gb must be non-negative")


@dataclass(frozen=True, slots=True)
class ApiChatModelPrefetchPlan:
    device_id: str
    disk_cache_model_classes: tuple[ApiChatModelClass, ...]
    resident_warm_load_model_classes: tuple[ApiChatModelClass, ...]
    disk_cache_reason_code: str
    resident_warm_load_reason_code: str
    request_time_download_allowed: bool = False


def plan_model_prefetch(request: ApiChatModelPrefetchRequest) -> ApiChatModelPrefetchPlan:
    device = request.device
    disk_models = _eligible_disk_models(device, request.candidate_model_classes)
    if not device.prefetch_eligible or not disk_models:
        return ApiChatModelPrefetchPlan(
            device_id=device.device_id,
            disk_cache_model_classes=(),
            resident_warm_load_model_classes=(),
            disk_cache_reason_code=REASON_PREFETCH_NOT_ELIGIBLE,
            resident_warm_load_reason_code=REASON_WARM_LOAD_DISABLED,
        )

    resident_models = _resident_warm_load_models(request, disk_models)
    if not request.resident_warm_load_allowed:
        warm_reason = REASON_WARM_LOAD_DISABLED
    elif request.gpu_mining_active and device.platform == "cuda":
        warm_reason = REASON_WARM_LOAD_CONFLICTS_WITH_GPU_MINING
    elif resident_models:
        warm_reason = REASON_WARM_LOAD_PLANNED
    else:
        warm_reason = REASON_WARM_LOAD_DISABLED

    return ApiChatModelPrefetchPlan(
        device_id=device.device_id,
        disk_cache_model_classes=disk_models,
        resident_warm_load_model_classes=resident_models,
        disk_cache_reason_code=REASON_PREFETCH_PLANNED,
        resident_warm_load_reason_code=warm_reason,
        request_time_download_allowed=False,
    )


def _eligible_disk_models(
    device: ApiChatInferenceDevice,
    candidate_model_classes: tuple[ApiChatModelClass, ...],
) -> tuple[ApiChatModelClass, ...]:
    seen: set[ApiChatModelClass] = set()
    eligible: list[ApiChatModelClass] = []
    for model_class in candidate_model_classes:
        canonical = canonical_model_class(model_class)
        if canonical in seen:
            continue
        if _supports_lane(device, canonical):
            eligible.append(canonical)
            seen.add(canonical)
    return tuple(eligible)


def _resident_warm_load_models(
    request: ApiChatModelPrefetchRequest,
    disk_models: tuple[ApiChatModelClass, ...],
) -> tuple[ApiChatModelClass, ...]:
    if not request.resident_warm_load_allowed:
        return ()
    if request.gpu_mining_active and request.device.platform == "cuda":
        return ()
    runtime = _runtime_for_device(request.device)
    warmed: list[ApiChatModelClass] = []
    for model_class in disk_models:
        profile = MODEL_PROFILES[model_class]
        if runtime not in profile.preferred_runtime_order:
            continue
        if request.memory_headroom_gb >= profile.minimum_memory_gb:
            warmed.append(model_class)
    return tuple(warmed)

from __future__ import annotations

from dataclasses import dataclass

from alice_acp.api_chat.model_catalog import (
    ALICE_LITE_4B,
    ALICE_PRO_35B_MOE,
    ALICE_STANDARD_9B,
    LEGACY_MODEL_CLASS_ALIASES,
    MODEL_PROFILES,
    RP_LITE_9B,
    RP_PRO_27B,
    canonical_model_class,
    fallback_model_classes,
)
from alice_acp.api_chat.types import (
    ApiChatInferenceDevice,
    ApiChatModelClass,
    ApiChatSchedulerDecision,
    ApiChatSchedulerRequest,
    ApiChatSchedulerRuntime,
)

REASON_SCHEDULER_ADMITTED = "api_chat_scheduler_admitted"
REASON_SCHEDULER_QUEUED_BUSY = "api_chat_scheduler_queued_busy"
REASON_SCHEDULER_REJECTED_NO_DEVICE = "api_chat_scheduler_no_eligible_device"
REASON_SCHEDULER_IDLE = "api_chat_scheduler_idle_no_background_inference"


@dataclass(frozen=True, slots=True)
class ApiChatInferenceScheduler:
    devices: tuple[ApiChatInferenceDevice, ...] = ()

    def idle_decision(self) -> ApiChatSchedulerDecision:
        return ApiChatSchedulerDecision(
            status="rejected",
            reason_code=REASON_SCHEDULER_IDLE,
            demand_driven=False,
            should_throttle_mining=False,
            background_inference_started=False,
        )

    def schedule(self, request: ApiChatSchedulerRequest) -> ApiChatSchedulerDecision:
        target_lane = model_lane_for_request(
            mode=request.mode,
            requested_model_class=request.requested_model_class,
        )
        lanes = _fallback_lanes(target_lane)

        for lane in lanes:
            eligible = [device for device in self.devices if _supports_lane(device, lane)]
            available = [device for device in eligible if not device.busy]
            if available:
                selected = _best_device(available, lane)
                return ApiChatSchedulerDecision(
                    status="admitted",
                    reason_code=REASON_SCHEDULER_ADMITTED,
                    request_id=request.request_id,
                    selected_device_id=selected.device_id,
                    selected_miner_id=selected.miner_id,
                    model_lane=lane,
                    runtime=_runtime_for_device(selected),
                    fallback_from=target_lane if lane != target_lane else None,
                    downgrade_applied=lane != target_lane,
                    demand_driven=True,
                    should_throttle_mining=True,
                    background_inference_started=False,
                )
            if eligible:
                best_queued = _best_device(eligible, lane)
                return ApiChatSchedulerDecision(
                    status="queued",
                    reason_code=REASON_SCHEDULER_QUEUED_BUSY,
                    request_id=request.request_id,
                    selected_device_id=best_queued.device_id,
                    selected_miner_id=best_queued.miner_id,
                    model_lane=lane,
                    runtime=_runtime_for_device(best_queued),
                    fallback_from=target_lane if lane != target_lane else None,
                    downgrade_applied=lane != target_lane,
                    demand_driven=True,
                    should_throttle_mining=False,
                    background_inference_started=False,
                )

        return ApiChatSchedulerDecision(
            status="rejected",
            reason_code=REASON_SCHEDULER_REJECTED_NO_DEVICE,
            request_id=request.request_id,
            fallback_from=target_lane,
            demand_driven=True,
            should_throttle_mining=False,
            background_inference_started=False,
        )


def model_lane_for_request(
    *,
    mode: str,
    requested_model_class: ApiChatModelClass,
) -> ApiChatModelClass:
    if requested_model_class != "auto":
        return canonical_model_class(requested_model_class)
    if mode == "rp_pro":
        return RP_PRO_27B
    if mode in {"roleplay", "rp_lite"}:
        return RP_LITE_9B
    if mode == "best":
        return ALICE_PRO_35B_MOE
    if mode == "fast":
        return ALICE_LITE_4B
    if mode == "standard":
        return ALICE_STANDARD_9B
    return ALICE_STANDARD_9B


def _fallback_lanes(target_lane: ApiChatModelClass) -> tuple[ApiChatModelClass, ...]:
    return fallback_model_classes(target_lane)


def _supports_lane(device: ApiChatInferenceDevice, lane: ApiChatModelClass) -> bool:
    if not device.inference_eligible:
        return False
    canonical_lane = canonical_model_class(lane)
    profile = MODEL_PROFILES.get(canonical_lane)
    if profile is None:
        return False
    if device.memory_gb < profile.minimum_memory_gb:
        return False
    if canonical_lane == ALICE_PRO_35B_MOE and not (
        device.platform == "mac" and device.supports_mlx
    ):
        return False
    if canonical_lane in device.supported_model_classes:
        return True
    for legacy_class, mapped_class in LEGACY_MODEL_CLASS_ALIASES.items():
        if mapped_class == canonical_lane and legacy_class in device.supported_model_classes:
            return True
    return False


def _best_device(
    devices: list[ApiChatInferenceDevice],
    lane: ApiChatModelClass,
) -> ApiChatInferenceDevice:
    return max(devices, key=lambda device: _device_score(device, lane))


def _device_score(device: ApiChatInferenceDevice, lane: ApiChatModelClass) -> tuple[int, int, int]:
    runtime = _runtime_for_device(device)
    runtime_score = {"mlx": 4, "cuda": 3, "gguf": 2, "cpu": 1}[runtime]
    lane_bonus = (
        2 if canonical_model_class(lane) == ALICE_PRO_35B_MOE and device.memory_gb >= 96 else 0
    )
    return (runtime_score, lane_bonus, device.memory_gb)


def _runtime_for_device(device: ApiChatInferenceDevice) -> ApiChatSchedulerRuntime:
    if device.platform == "mac" and device.supports_mlx:
        return "mlx"
    if device.platform == "mac" and device.supports_gguf:
        return "gguf"
    if device.platform == "cuda":
        return "cuda"
    return "cpu"

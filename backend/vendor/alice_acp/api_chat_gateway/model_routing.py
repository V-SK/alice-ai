from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from alice_acp.api_chat.model_catalog import (
    MODEL_PROFILES,
    canonical_model_class,
    fallback_model_classes,
    is_roleplay_model_class,
)
from alice_acp.api_chat.scheduler import model_lane_for_request
from alice_acp.api_chat.types import (
    VALID_API_CHAT_MODEL_CLASSES,
    ApiChatGatewayMode,
    ApiChatModelClass,
    ApiChatSchedulerRuntime,
    utc_now,
    validate_public_identifier,
)
from alice_acp.api_chat.validators import validate_aware_timestamp, validate_sha256
from alice_acp.api_chat_gateway.public_contract import public_route_policy_contract
from alice_acp.api_chat_gateway.types import GatewayMode

MODEL_ROUTE_CONTRACT_VERSION = "api-chat-real-model-route-contract-v1"

REASON_MODEL_ROUTE_DISABLED = "api_chat_model_route_default_off"
REASON_MODEL_ROUTE_ADMITTED = "api_chat_model_route_admitted"
REASON_MODEL_ROUTE_FALLBACK_ADMITTED = "api_chat_model_route_fallback_admitted"
REASON_MODEL_ROUTE_QUEUED = "api_chat_model_route_queued"
REASON_MODEL_ROUTE_QUEUE_FULL = "api_chat_model_route_queue_full"
REASON_MODEL_ROUTE_NO_CACHED_MODEL = "api_chat_model_route_no_cached_or_loaded_model"
REASON_MODEL_ROUTE_ROLEPLAY_FAIL_CLOSED = "api_chat_model_route_roleplay_fail_closed"
REASON_MODEL_DISPATCH_FORBIDDEN = "api_chat_model_dispatch_forbidden"

ModelRouteStatus = Literal["admitted", "queued", "rejected"]
DevicePlatform = Literal["mac", "cuda", "cpu"]


@dataclass(frozen=True, slots=True)
class DeviceCapacityDTO:
    device_id: str
    miner_id: str
    platform: DevicePlatform
    memory_gb: int
    runtimes: tuple[ApiChatSchedulerRuntime, ...]
    loaded_model_classes: tuple[ApiChatModelClass, ...] = ()
    cached_model_classes: tuple[ApiChatModelClass, ...] = ()
    online: bool = True
    current_inference_jobs: int = 0
    max_concurrent_requests: int = 1
    queue_depth: int = 0
    supports_mining_throttle: bool = True
    observed_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        validate_public_identifier("device_id", self.device_id)
        validate_public_identifier("miner_id", self.miner_id)
        if self.platform not in ("mac", "cuda", "cpu"):
            raise ValueError("device platform is unsupported")
        if self.memory_gb <= 0:
            raise ValueError("memory_gb must be positive")
        if self.current_inference_jobs < 0:
            raise ValueError("current_inference_jobs must be non-negative")
        if self.max_concurrent_requests <= 0:
            raise ValueError("max_concurrent_requests must be positive")
        if self.queue_depth < 0:
            raise ValueError("queue_depth must be non-negative")
        if not self.runtimes:
            raise ValueError("runtimes must not be empty")
        for runtime in self.runtimes:
            if runtime not in ("mlx", "gguf", "cuda", "cpu"):
                raise ValueError("runtime is unsupported")
        loaded = tuple(
            canonical_model_class(model_class) for model_class in self.loaded_model_classes
        )
        cached = tuple(
            canonical_model_class(model_class) for model_class in self.cached_model_classes
        )
        object.__setattr__(self, "loaded_model_classes", loaded)
        object.__setattr__(self, "cached_model_classes", cached)
        for model_class in (*loaded, *cached):
            _validate_model_class(model_class)
        validate_aware_timestamp("observed_at", self.observed_at)

    @property
    def available_slots(self) -> int:
        return max(0, self.max_concurrent_requests - self.current_inference_jobs)

    def has_model(self, model_class: ApiChatModelClass) -> bool:
        canonical = canonical_model_class(model_class)
        return canonical in self.loaded_model_classes or canonical in self.cached_model_classes

    def runtime_for(self, model_class: ApiChatModelClass) -> ApiChatSchedulerRuntime | None:
        profile = MODEL_PROFILES.get(canonical_model_class(model_class))
        if profile is None:
            return None
        for runtime in profile.preferred_runtime_order:
            if runtime in self.runtimes:
                return runtime
        return None

    def to_public_dict(self) -> dict[str, object]:
        return {
            "device_id": self.device_id,
            "miner_id": self.miner_id,
            "platform": self.platform,
            "memory_gb": self.memory_gb,
            "runtimes": self.runtimes,
            "loaded_model_classes": self.loaded_model_classes,
            "cached_model_classes": self.cached_model_classes,
            "online": self.online,
            "current_inference_jobs": self.current_inference_jobs,
            "max_concurrent_requests": self.max_concurrent_requests,
            "available_slots": self.available_slots,
            "queue_depth": self.queue_depth,
            "supports_mining_throttle": self.supports_mining_throttle,
            "observed_at": self.observed_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class ModelRouteRequest:
    request_id: str
    mode: GatewayMode
    requested_model_class: ApiChatModelClass
    prompt_hash: str
    observed_at: datetime = field(default_factory=utc_now)
    identity_kind: Literal["anonymous", "api_key", "user_id"] = "anonymous"

    def __post_init__(self) -> None:
        validate_public_identifier("request_id", self.request_id)
        _gateway_mode_to_api_mode(self.mode)
        _validate_model_class(self.requested_model_class)
        validate_sha256(self.prompt_hash, field_name="prompt_hash")
        validate_aware_timestamp("observed_at", self.observed_at)
        if self.identity_kind not in ("anonymous", "api_key", "user_id"):
            raise ValueError("identity_kind is unsupported")


@dataclass(frozen=True, slots=True)
class ModelRouteRateLimitResult:
    admitted: bool
    reason_code: str
    identity_kind: str
    limit_requests_per_hour: int
    remaining_requests: int
    remaining_burst: int
    retry_after_seconds: int | None = None

    def __post_init__(self) -> None:
        validate_public_identifier("reason_code", self.reason_code)
        validate_public_identifier("identity_kind", self.identity_kind)
        for field_name, value in (
            ("limit_requests_per_hour", self.limit_requests_per_hour),
            ("remaining_requests", self.remaining_requests),
            ("remaining_burst", self.remaining_burst),
        ):
            if value < 0:
                raise ValueError(f"{field_name} must be non-negative")
        if self.retry_after_seconds is not None and self.retry_after_seconds < 0:
            raise ValueError("retry_after_seconds must be non-negative")

    @classmethod
    def from_metadata(
        cls,
        *,
        admitted: bool,
        reason_code: str,
        metadata: dict[str, object],
    ) -> ModelRouteRateLimitResult:
        return cls(
            admitted=admitted,
            reason_code=reason_code,
            identity_kind=str(metadata["identity_kind"]),
            limit_requests_per_hour=int(metadata["limit_requests_per_hour"]),
            remaining_requests=int(metadata["remaining_requests"]),
            remaining_burst=int(metadata["remaining_burst"]),
            retry_after_seconds=_optional_int(metadata.get("retry_after")),
        )

    def to_public_dict(self) -> dict[str, object]:
        return {
            "admitted": self.admitted,
            "reason_code": self.reason_code,
            "identity_kind": self.identity_kind,
            "limit_requests_per_hour": self.limit_requests_per_hour,
            "remaining_requests": self.remaining_requests,
            "remaining_burst": self.remaining_burst,
            "retry_after_seconds": self.retry_after_seconds,
        }


@dataclass(frozen=True, slots=True)
class ModelRouteDecision:
    status: ModelRouteStatus
    reason_code: str
    request_id: str
    route_mode: GatewayMode
    requested_model_class: ApiChatModelClass
    target_model_classes: tuple[ApiChatModelClass, ...]
    selected_model_class: ApiChatModelClass | None = None
    selected_device_id: str | None = None
    selected_miner_id: str | None = None
    selected_runtime: ApiChatSchedulerRuntime | None = None
    queue_position: int | None = None
    fallback_from: ApiChatModelClass | None = None
    downgrade_applied: bool = False
    fail_closed: bool = False
    request_time_model_download_allowed: bool = False
    model_dispatch_enabled: bool = False
    remote_model_call_performed: bool = False
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False

    def __post_init__(self) -> None:
        if self.status not in ("admitted", "queued", "rejected"):
            raise ValueError("model route status is unsupported")
        validate_public_identifier("reason_code", self.reason_code)
        validate_public_identifier("request_id", self.request_id)
        _gateway_mode_to_api_mode(self.route_mode)
        _validate_model_class(self.requested_model_class)
        for model_class in self.target_model_classes:
            _validate_model_class(model_class)
        for _, model_class in (
            ("selected_model_class", self.selected_model_class),
            ("fallback_from", self.fallback_from),
        ):
            if model_class is not None:
                _validate_model_class(model_class)
        if self.selected_device_id is not None:
            validate_public_identifier("selected_device_id", self.selected_device_id)
        if self.selected_miner_id is not None:
            validate_public_identifier("selected_miner_id", self.selected_miner_id)
        if self.selected_runtime is not None and self.selected_runtime not in (
            "mlx",
            "gguf",
            "cuda",
            "cpu",
        ):
            raise ValueError("selected_runtime is unsupported")
        if self.queue_position is not None and self.queue_position <= 0:
            raise ValueError("queue_position must be positive")
        if (
            self.request_time_model_download_allowed
            or self.model_dispatch_enabled
            or self.remote_model_call_performed
        ):
            raise ValueError(REASON_MODEL_DISPATCH_FORBIDDEN)
        if self.live_reward_enabled:
            raise ValueError("api_chat_live_reward_forbidden")
        if self.payout_executor_enabled:
            raise ValueError("api_chat_payout_executor_forbidden")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "contract_version": MODEL_ROUTE_CONTRACT_VERSION,
            "public_route_policy": public_route_policy_contract(),
            "status": self.status,
            "reason_code": self.reason_code,
            "request_id": self.request_id,
            "route_mode": self.route_mode,
            "requested_model_class": self.requested_model_class,
            "target_model_classes": self.target_model_classes,
            "selected_model_class": self.selected_model_class,
            "selected_device_id": self.selected_device_id,
            "selected_miner_id": self.selected_miner_id,
            "selected_runtime": self.selected_runtime,
            "queue_position": self.queue_position,
            "fallback_from": self.fallback_from,
            "downgrade_applied": self.downgrade_applied,
            "fail_closed": self.fail_closed,
            "request_time_model_download_allowed": False,
            "model_dispatch_enabled": False,
            "remote_model_call_performed": False,
            "live_reward_enabled": False,
            "payout_executor_enabled": False,
            "failure_fallback": {
                "policy": "general_routes_may_downgrade_rp_routes_fail_closed",
                "fallback_from": self.fallback_from,
                "target_model_classes": self.target_model_classes,
                "selected_model_class": self.selected_model_class,
                "fail_closed": self.fail_closed,
            },
        }


@dataclass(frozen=True, slots=True)
class ApiChatModelRouteScheduler:
    enabled: bool = False
    devices: tuple[DeviceCapacityDTO, ...] = ()
    queued_requests: int = 0
    queue_capacity: int = 128
    model_dispatch_enabled: bool = False

    def __post_init__(self) -> None:
        if self.queued_requests < 0:
            raise ValueError("queued_requests must be non-negative")
        if self.queue_capacity <= 0:
            raise ValueError("queue_capacity must be positive")
        if self.model_dispatch_enabled:
            raise ValueError(REASON_MODEL_DISPATCH_FORBIDDEN)

    def summary(self) -> dict[str, object]:
        return {
            "contract_version": MODEL_ROUTE_CONTRACT_VERSION,
            "public_route_policy": public_route_policy_contract(),
            "enabled": self.enabled,
            "model_dispatch_enabled": False,
            "remote_model_call_performed": False,
            "request_time_model_download_allowed": False,
            "queue_capacity": self.queue_capacity,
            "queued_requests": self.queued_requests,
            "device_count": len(self.devices),
        }

    def route(self, request: ModelRouteRequest) -> ModelRouteDecision:
        target = model_lane_for_request(
            mode=_gateway_mode_to_api_mode(request.mode),
            requested_model_class=request.requested_model_class,
        )
        targets = fallback_model_classes(target)
        if not self.enabled:
            return ModelRouteDecision(
                status="rejected",
                reason_code=REASON_MODEL_ROUTE_DISABLED,
                request_id=request.request_id,
                route_mode=request.mode,
                requested_model_class=request.requested_model_class,
                target_model_classes=targets,
                fallback_from=target,
                fail_closed=is_roleplay_model_class(target),
            )
        if self.queued_requests >= self.queue_capacity:
            return ModelRouteDecision(
                status="rejected",
                reason_code=REASON_MODEL_ROUTE_QUEUE_FULL,
                request_id=request.request_id,
                route_mode=request.mode,
                requested_model_class=request.requested_model_class,
                target_model_classes=targets,
                fallback_from=target,
                fail_closed=is_roleplay_model_class(target),
            )

        busy_candidate: tuple[DeviceCapacityDTO, ApiChatModelClass] | None = None
        for model_class in targets:
            eligible = tuple(
                device for device in self.devices if _device_eligible(device, model_class)
            )
            available = tuple(device for device in eligible if device.available_slots > 0)
            if available:
                selected = _best_device(available, model_class)
                fallback_applied = model_class != target
                return ModelRouteDecision(
                    status="admitted",
                    reason_code=(
                        REASON_MODEL_ROUTE_FALLBACK_ADMITTED
                        if fallback_applied
                        else REASON_MODEL_ROUTE_ADMITTED
                    ),
                    request_id=request.request_id,
                    route_mode=request.mode,
                    requested_model_class=request.requested_model_class,
                    target_model_classes=targets,
                    selected_model_class=model_class,
                    selected_device_id=selected.device_id,
                    selected_miner_id=selected.miner_id,
                    selected_runtime=selected.runtime_for(model_class),
                    fallback_from=target if fallback_applied else None,
                    downgrade_applied=fallback_applied,
                )
            if eligible and busy_candidate is None:
                busy_candidate = (_best_device(eligible, model_class), model_class)

        if busy_candidate is not None:
            selected, model_class = busy_candidate
            fallback_applied = model_class != target
            return ModelRouteDecision(
                status="queued",
                reason_code=REASON_MODEL_ROUTE_QUEUED,
                request_id=request.request_id,
                route_mode=request.mode,
                requested_model_class=request.requested_model_class,
                target_model_classes=targets,
                selected_model_class=model_class,
                selected_device_id=selected.device_id,
                selected_miner_id=selected.miner_id,
                selected_runtime=selected.runtime_for(model_class),
                queue_position=self.queued_requests + 1,
                fallback_from=target if fallback_applied else None,
                downgrade_applied=fallback_applied,
            )

        return ModelRouteDecision(
            status="rejected",
            reason_code=(
                REASON_MODEL_ROUTE_ROLEPLAY_FAIL_CLOSED
                if is_roleplay_model_class(target)
                else REASON_MODEL_ROUTE_NO_CACHED_MODEL
            ),
            request_id=request.request_id,
            route_mode=request.mode,
            requested_model_class=request.requested_model_class,
            target_model_classes=targets,
            fallback_from=target,
            fail_closed=is_roleplay_model_class(target),
        )


def model_route_request_for_chat(
    *,
    request_id: str,
    mode: GatewayMode,
    requested_model_class: ApiChatModelClass,
    prompt_hash: str,
    observed_at: datetime,
    identity_kind: Literal["anonymous", "api_key", "user_id"],
) -> ModelRouteRequest:
    return ModelRouteRequest(
        request_id=request_id,
        mode=mode,
        requested_model_class=requested_model_class,
        prompt_hash=prompt_hash,
        observed_at=observed_at,
        identity_kind=identity_kind,
    )


def _device_eligible(device: DeviceCapacityDTO, model_class: ApiChatModelClass) -> bool:
    profile = MODEL_PROFILES.get(canonical_model_class(model_class))
    if profile is None:
        return False
    return (
        device.online
        and device.memory_gb >= profile.minimum_memory_gb
        and device.has_model(model_class)
        and device.runtime_for(model_class) is not None
    )


def _best_device(
    devices: tuple[DeviceCapacityDTO, ...],
    model_class: ApiChatModelClass,
) -> DeviceCapacityDTO:
    return sorted(devices, key=lambda device: _device_sort_key(device, model_class))[0]


def _device_sort_key(
    device: DeviceCapacityDTO,
    model_class: ApiChatModelClass,
) -> tuple[int, int, int, int, int, str]:
    canonical = canonical_model_class(model_class)
    runtime = device.runtime_for(canonical)
    profile = MODEL_PROFILES[canonical]
    loaded_rank = 0 if canonical in device.loaded_model_classes else 1
    runtime_rank = (
        profile.preferred_runtime_order.index(runtime)
        if runtime in profile.preferred_runtime_order
        else len(profile.preferred_runtime_order)
    )
    return (
        loaded_rank,
        device.current_inference_jobs,
        device.queue_depth,
        runtime_rank,
        -device.memory_gb,
        device.device_id,
    )


def _gateway_mode_to_api_mode(mode: GatewayMode) -> ApiChatGatewayMode:
    return {
        "Auto": "auto",
        "Fast": "fast",
        "Standard": "standard",
        "Roleplay": "roleplay",
        "Best": "best",
        "RP Lite": "rp_lite",
        "RP Pro": "rp_pro",
    }[mode]


def _validate_model_class(model_class: ApiChatModelClass) -> None:
    if model_class not in VALID_API_CHAT_MODEL_CLASSES:
        raise ValueError("model_class is unsupported")


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)

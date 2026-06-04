from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Literal

FREE_CHAT_LIMIT_PER_HOUR = 10
API_PLAN_LIMITS_PER_HOUR = {
    "starter": 60,
    "pro": 600,
    "internal": 6_000,
}

MODEL_ALICE_LITE = "alice_lite_4b"
MODEL_ALICE_STANDARD = "alice_standard_9b"
MODEL_ALICE_PRO_27B = "alice_pro_27b"
MODEL_ALICE_PRO_35B_MOE = "alice_pro_35b_moe"
MODEL_RP_LITE = "rp_lite_9b"
MODEL_RP_PRO = "rp_pro_27b"

Status = Literal["accepted", "queued", "rejected"]
RouteMode = Literal["auto", "fast", "standard", "best", "roleplay", "rp_lite", "rp_pro"]
Runtime = Literal["mlx", "gguf", "cuda"]
MiningState = Literal["idle", "mining", "preemptible", "paused"]


class InferenceGatewayError(ValueError):
    pass


@dataclass(frozen=True)
class ModelProfile:
    model_id: str
    family: Literal["general", "roleplay"]
    product_lane: str
    min_memory_gb: int
    runtime_order: tuple[Runtime, ...]


MODEL_PROFILES: dict[str, ModelProfile] = {
    MODEL_ALICE_LITE: ModelProfile(
        model_id=MODEL_ALICE_LITE,
        family="general",
        product_lane="Alice Lite",
        min_memory_gb=16,
        runtime_order=("mlx", "cuda", "gguf"),
    ),
    MODEL_ALICE_STANDARD: ModelProfile(
        model_id=MODEL_ALICE_STANDARD,
        family="general",
        product_lane="Alice Standard",
        min_memory_gb=16,
        runtime_order=("mlx", "cuda", "gguf"),
    ),
    MODEL_ALICE_PRO_27B: ModelProfile(
        model_id=MODEL_ALICE_PRO_27B,
        family="general",
        product_lane="Alice Pro",
        min_memory_gb=32,
        runtime_order=("mlx", "cuda", "gguf"),
    ),
    MODEL_ALICE_PRO_35B_MOE: ModelProfile(
        model_id=MODEL_ALICE_PRO_35B_MOE,
        family="general",
        product_lane="Alice Pro",
        min_memory_gb=64,
        runtime_order=("mlx", "gguf"),
    ),
    MODEL_RP_LITE: ModelProfile(
        model_id=MODEL_RP_LITE,
        family="roleplay",
        product_lane="RP Lite",
        min_memory_gb=16,
        runtime_order=("mlx", "cuda", "gguf"),
    ),
    MODEL_RP_PRO: ModelProfile(
        model_id=MODEL_RP_PRO,
        family="roleplay",
        product_lane="RP Pro",
        min_memory_gb=32,
        runtime_order=("mlx", "cuda", "gguf"),
    ),
}


@dataclass(frozen=True)
class ApiKeyPlan:
    key_hash: str
    plan: str
    enabled: bool = True

    def validate(self) -> None:
        if not self.key_hash or not _looks_sha256(self.key_hash):
            raise InferenceGatewayError("invalid_api_key_hash")
        if self.plan not in API_PLAN_LIMITS_PER_HOUR:
            raise InferenceGatewayError("unsupported_api_key_plan")


@dataclass(frozen=True)
class GatewayPolicy:
    free_chat_limit_per_hour: int = FREE_CHAT_LIMIT_PER_HOUR
    ip_limit_per_hour: int = 120
    device_limit_per_hour: int = 80
    max_failed_requests_per_hour: int = 5
    max_replayed_requests_per_hour: int = 2
    queue_capacity: int = 128

    def validate(self) -> None:
        for name, value in (
            ("free_chat_limit_per_hour", self.free_chat_limit_per_hour),
            ("ip_limit_per_hour", self.ip_limit_per_hour),
            ("device_limit_per_hour", self.device_limit_per_hour),
            ("max_failed_requests_per_hour", self.max_failed_requests_per_hour),
            ("max_replayed_requests_per_hour", self.max_replayed_requests_per_hour),
            ("queue_capacity", self.queue_capacity),
        ):
            if value <= 0:
                raise InferenceGatewayError(f"invalid_{name}")


@dataclass(frozen=True)
class InferenceDeviceHeartbeat:
    device_id: str
    memory_gb: int
    platform: str
    runtimes: tuple[Runtime, ...]
    cached_models: tuple[str, ...] = ()
    loaded_models: tuple[str, ...] = ()
    current_mining_state: MiningState = "idle"
    latency_ms: int = 0
    online: bool = True
    current_inference_jobs: int = 0
    preemptible: bool = False
    observed_at: datetime | None = None

    def validate(self) -> None:
        if not self.device_id:
            raise InferenceGatewayError("missing_device_id")
        if self.memory_gb <= 0:
            raise InferenceGatewayError("invalid_device_memory")
        if not self.platform:
            raise InferenceGatewayError("missing_device_platform")
        if any(runtime not in {"mlx", "gguf", "cuda"} for runtime in self.runtimes):
            raise InferenceGatewayError("unsupported_runtime")
        if self.current_mining_state not in {"idle", "mining", "preemptible", "paused"}:
            raise InferenceGatewayError("unsupported_mining_state")
        if self.latency_ms < 0 or self.current_inference_jobs < 0:
            raise InferenceGatewayError("invalid_device_load")
        if self.observed_at is not None and self.observed_at.tzinfo is None:
            raise InferenceGatewayError("observed_at_must_be_timezone_aware")

    def has_model(self, model_id: str) -> bool:
        return model_id in self.loaded_models or model_id in self.cached_models


@dataclass(frozen=True)
class QueueState:
    queued_jobs: int = 0

    def validate(self) -> None:
        if self.queued_jobs < 0:
            raise InferenceGatewayError("invalid_queued_jobs")


@dataclass(frozen=True)
class InferenceGatewayRequest:
    request_id: str
    request_kind: Literal["free", "api"]
    route_mode: RouteMode
    identity_key: str
    ip_key: str
    requested_at: datetime
    api_key_hash: str | None = None
    device_key: str | None = None
    tampered: bool = False
    failed_auth: bool = False

    def validate(self) -> None:
        if not self.request_id:
            raise InferenceGatewayError("missing_request_id")
        if self.request_kind not in {"free", "api"}:
            raise InferenceGatewayError("unsupported_request_kind")
        if self.route_mode not in {
            "auto",
            "fast",
            "standard",
            "best",
            "roleplay",
            "rp_lite",
            "rp_pro",
        }:
            raise InferenceGatewayError("unsupported_route_mode")
        if not self.identity_key:
            raise InferenceGatewayError("missing_identity_key")
        if not self.ip_key:
            raise InferenceGatewayError("missing_ip_key")
        if self.requested_at.tzinfo is None:
            raise InferenceGatewayError("requested_at_must_be_timezone_aware")
        if self.request_kind == "api" and not self.api_key_hash:
            raise InferenceGatewayError("missing_api_key_hash")
        if self.api_key_hash is not None and not _looks_sha256(self.api_key_hash):
            raise InferenceGatewayError("invalid_api_key_hash")


@dataclass(frozen=True)
class RateLimitBucket:
    name: str
    key_hash: str
    limit_per_hour: int
    reason_code: str

    @property
    def storage_key(self) -> str:
        return f"{self.name}:{self.key_hash}"


@dataclass(frozen=True)
class SchedulerDecision:
    status: Status
    reason_code: str
    target_models: tuple[str, ...]
    selected_model_id: str | None = None
    selected_device_id: str | None = None
    selected_runtime: Runtime | None = None
    queue_position: int | None = None
    throttle_recommendation: Literal["none", "throttle", "pause"] = "none"
    request_time_model_download_allowed: bool = False
    external_model_call_planned: bool = False


@dataclass(frozen=True)
class GatewayAdmission:
    status: Status
    reason_code: str
    request_id: str
    rate_limit_bucket: str | None = None
    retry_after_seconds: int | None = None
    scheduler: SchedulerDecision | None = None
    raw_api_key_persisted: bool = False
    production_service_deployment: bool = False
    live_reward_enabled: bool = False
    payout_enabled: bool = False


@dataclass
class AbuseGuardState:
    failed_by_subject: dict[str, list[datetime]] = field(default_factory=dict)
    replayed_by_subject: dict[str, list[datetime]] = field(default_factory=dict)
    seen_request_ids: set[str] = field(default_factory=set)


@dataclass
class InferenceGatewayScheduler:
    devices: tuple[InferenceDeviceHeartbeat, ...] = ()
    queue_state: QueueState = field(default_factory=QueueState)
    policy: GatewayPolicy = field(default_factory=GatewayPolicy)

    def schedule(self, request: InferenceGatewayRequest) -> SchedulerDecision:
        request.validate()
        self.queue_state.validate()
        self.policy.validate()

        targets = _target_chain(request.route_mode)
        if self.queue_state.queued_jobs >= self.policy.queue_capacity:
            return SchedulerDecision(
                status="rejected",
                reason_code="q38_queue_full",
                target_models=targets,
            )

        busy_candidate: tuple[InferenceDeviceHeartbeat, str, Runtime] | None = None
        for model_id in targets:
            profile = MODEL_PROFILES[model_id]
            eligible = [
                (device, runtime)
                for device in self.devices
                if _eligible(device, profile)
                for runtime in _preferred_runtimes(device, profile)
            ]
            if not eligible:
                continue
            available = [
                (device, runtime)
                for device, runtime in eligible
                if device.current_inference_jobs == 0
                and device.current_mining_state in {"idle", "preemptible", "mining"}
            ]
            if available:
                device, runtime = sorted(
                    available,
                    key=lambda item: _device_sort_key(item[0], model_id),
                )[0]
                return SchedulerDecision(
                    status="accepted",
                    reason_code="q38_inference_accepted",
                    target_models=targets,
                    selected_model_id=model_id,
                    selected_device_id=device.device_id,
                    selected_runtime=runtime,
                    throttle_recommendation=_throttle_recommendation(device),
                    request_time_model_download_allowed=False,
                    external_model_call_planned=False,
                )
            if busy_candidate is None:
                device, runtime = sorted(
                    eligible,
                    key=lambda item: _device_sort_key(item[0], model_id),
                )[0]
                busy_candidate = (device, model_id, runtime)

        if busy_candidate is not None:
            device, model_id, runtime = busy_candidate
            return SchedulerDecision(
                status="queued",
                reason_code="q38_inference_queued_device_busy",
                target_models=targets,
                selected_model_id=model_id,
                selected_device_id=device.device_id,
                selected_runtime=runtime,
                queue_position=self.queue_state.queued_jobs + 1,
                request_time_model_download_allowed=False,
                external_model_call_planned=False,
            )

        return SchedulerDecision(
            status="rejected",
            reason_code=(
                "q38_roleplay_unavailable_fail_closed"
                if request.route_mode in {"roleplay", "rp_lite", "rp_pro"}
                else "q38_no_eligible_device"
            ),
            target_models=targets,
        )


@dataclass
class InMemoryInferenceGateway:
    api_keys: tuple[ApiKeyPlan, ...] = ()
    scheduler: InferenceGatewayScheduler = field(default_factory=InferenceGatewayScheduler)
    policy: GatewayPolicy = field(default_factory=GatewayPolicy)
    abuse: AbuseGuardState = field(default_factory=AbuseGuardState)
    _admissions_by_bucket: dict[str, list[datetime]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.policy.validate()
        for key in self.api_keys:
            key.validate()

    def handle(self, request: InferenceGatewayRequest) -> GatewayAdmission:
        request.validate()
        abuse = self._assess_abuse(request)
        if abuse is not None:
            return abuse

        api_key = None
        if request.request_kind == "api":
            api_key = self._api_key(request.api_key_hash or "")
            if api_key is None:
                return self._failed(request, "q38_unknown_api_key_hash")
            if not api_key.enabled:
                return self._failed(request, "q38_disabled_api_key_hash")

        limit = self._consume_rate_limits(request, api_key=api_key)
        if limit is not None:
            return limit

        decision = self.scheduler.schedule(request)
        return GatewayAdmission(
            status=decision.status,
            reason_code=decision.reason_code,
            request_id=request.request_id,
            scheduler=decision,
        )

    def _api_key(self, key_hash: str) -> ApiKeyPlan | None:
        return next((api_key for api_key in self.api_keys if api_key.key_hash == key_hash), None)

    def _assess_abuse(self, request: InferenceGatewayRequest) -> GatewayAdmission | None:
        subject = _hash_value("subject", request.identity_key)
        now = request.requested_at
        if request.request_id in self.abuse.seen_request_ids:
            replayed = _retained(self.abuse.replayed_by_subject, subject, now)
            replayed.append(now)
            self.abuse.replayed_by_subject[subject] = replayed
            if len(replayed) > self.policy.max_replayed_requests_per_hour:
                return self._failed(request, "q38_excessive_replayed_request")
            return self._failed(request, "q38_replayed_request")
        self.abuse.seen_request_ids.add(request.request_id)
        if request.tampered:
            return self._failed(request, "q38_tampered_request")
        if request.failed_auth:
            failed = _retained(self.abuse.failed_by_subject, subject, now)
            failed.append(now)
            self.abuse.failed_by_subject[subject] = failed
            if len(failed) > self.policy.max_failed_requests_per_hour:
                return self._failed(request, "q38_excessive_failed_request")
            return self._failed(request, "q38_failed_request")
        return None

    def _consume_rate_limits(
        self,
        request: InferenceGatewayRequest,
        *,
        api_key: ApiKeyPlan | None,
    ) -> GatewayAdmission | None:
        buckets = _rate_limit_buckets(request, self.policy, api_key=api_key)
        retained_by_key = {
            bucket.storage_key: _retained(
                self._admissions_by_bucket,
                bucket.storage_key,
                request.requested_at,
            )
            for bucket in buckets
        }
        for bucket in buckets:
            retained = retained_by_key[bucket.storage_key]
            if len(retained) >= bucket.limit_per_hour:
                return GatewayAdmission(
                    status="rejected",
                    reason_code=bucket.reason_code,
                    request_id=request.request_id,
                    rate_limit_bucket=bucket.name,
                    retry_after_seconds=_retry_after_seconds(retained[0], request.requested_at),
                )
        for bucket in buckets:
            retained = retained_by_key[bucket.storage_key]
            retained.append(request.requested_at)
            self._admissions_by_bucket[bucket.storage_key] = retained
        return None

    def _failed(self, request: InferenceGatewayRequest, reason_code: str) -> GatewayAdmission:
        return GatewayAdmission(
            status="rejected",
            reason_code=reason_code,
            request_id=request.request_id,
        )


def stable_api_key_hash(raw_key: str) -> str:
    if not raw_key:
        raise InferenceGatewayError("missing_raw_key")
    return hashlib.sha256(raw_key.encode()).hexdigest()


def _target_chain(route_mode: RouteMode) -> tuple[str, ...]:
    if route_mode == "fast":
        return (MODEL_ALICE_LITE,)
    if route_mode == "standard":
        return (MODEL_ALICE_STANDARD, MODEL_ALICE_LITE)
    if route_mode == "best":
        return (
            MODEL_ALICE_PRO_35B_MOE,
            MODEL_ALICE_PRO_27B,
            MODEL_ALICE_STANDARD,
            MODEL_ALICE_LITE,
        )
    if route_mode == "rp_lite":
        return (MODEL_RP_LITE,)
    if route_mode == "rp_pro":
        return (MODEL_RP_PRO, MODEL_RP_LITE)
    if route_mode == "roleplay":
        return (MODEL_RP_LITE, MODEL_RP_PRO)
    return (MODEL_ALICE_STANDARD, MODEL_ALICE_LITE)


def _eligible(device: InferenceDeviceHeartbeat, profile: ModelProfile) -> bool:
    device.validate()
    return (
        device.online
        and device.memory_gb >= profile.min_memory_gb
        and device.has_model(profile.model_id)
        and any(runtime in device.runtimes for runtime in profile.runtime_order)
    )


def _preferred_runtimes(
    device: InferenceDeviceHeartbeat,
    profile: ModelProfile,
) -> tuple[Runtime, ...]:
    return tuple(runtime for runtime in profile.runtime_order if runtime in device.runtimes)


def _device_sort_key(
    device: InferenceDeviceHeartbeat,
    model_id: str,
) -> tuple[int, int, int, int, int, str]:
    loaded_rank = 0 if model_id in device.loaded_models else 1
    cached_rank = 0 if model_id in device.cached_models else 1
    mining_rank = {
        "idle": 0,
        "preemptible": 1,
        "mining": 2,
        "paused": 3,
    }[device.current_mining_state]
    return (
        loaded_rank,
        cached_rank,
        mining_rank,
        device.current_inference_jobs,
        device.latency_ms,
        device.device_id,
    )


def _throttle_recommendation(
    device: InferenceDeviceHeartbeat,
) -> Literal["none", "throttle", "pause"]:
    if device.current_mining_state == "preemptible" or device.preemptible:
        return "pause"
    if device.current_mining_state == "mining":
        return "throttle"
    return "none"


def _rate_limit_buckets(
    request: InferenceGatewayRequest,
    policy: GatewayPolicy,
    *,
    api_key: ApiKeyPlan | None,
) -> tuple[RateLimitBucket, ...]:
    buckets = [
        RateLimitBucket(
            name="ip",
            key_hash=_hash_value("ip", request.ip_key),
            limit_per_hour=policy.ip_limit_per_hour,
            reason_code="q38_ip_rate_limit_exceeded",
        )
    ]
    if request.device_key:
        buckets.append(
            RateLimitBucket(
                name="device",
                key_hash=_hash_value("device", request.device_key),
                limit_per_hour=policy.device_limit_per_hour,
                reason_code="q38_device_rate_limit_exceeded",
            )
        )
    if request.request_kind == "free":
        buckets.append(
            RateLimitBucket(
                name="free_identity",
                key_hash=_hash_value("identity", request.identity_key),
                limit_per_hour=policy.free_chat_limit_per_hour,
                reason_code="q38_free_chat_hourly_limit_exceeded",
            )
        )
        buckets.append(
            RateLimitBucket(
                name="free_ip",
                key_hash=_hash_value("free_ip", request.ip_key),
                limit_per_hour=policy.free_chat_limit_per_hour,
                reason_code="q38_free_chat_ip_hourly_limit_exceeded",
            )
        )
    elif api_key is not None:
        buckets.append(
            RateLimitBucket(
                name="api_key",
                key_hash=api_key.key_hash,
                limit_per_hour=API_PLAN_LIMITS_PER_HOUR[api_key.plan],
                reason_code="q38_api_key_plan_limit_exceeded",
            )
        )
    return tuple(buckets)


def _retained(
    store: dict[str, list[datetime]],
    key: str,
    observed_at: datetime,
) -> list[datetime]:
    cutoff = observed_at - timedelta(hours=1)
    retained = [timestamp for timestamp in store.get(key, []) if timestamp > cutoff]
    store[key] = retained
    return retained


def _retry_after_seconds(oldest: datetime, observed_at: datetime) -> int:
    return max(1, math.ceil((oldest + timedelta(hours=1) - observed_at).total_seconds()))


def _hash_value(kind: str, value: str) -> str:
    if not kind or not value:
        raise InferenceGatewayError("hash_inputs_must_be_non_empty")
    return hashlib.sha256(f"{kind}:{value}".encode()).hexdigest()


def _looks_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in "0123456789abcdef" for char in value.lower())


def utc_now() -> datetime:
    return datetime.now(UTC)

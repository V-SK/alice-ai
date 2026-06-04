from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from alice_acp.api_chat.validators import (
    ensure_no_raw_secret,
    validate_aware_timestamp,
    validate_sha256,
)
from alice_acp.inference_gateway.contract import (
    FREE_CHAT_LIMIT_PER_HOUR,
    MODEL_ALICE_LITE,
    MODEL_ALICE_PRO_27B,
    MODEL_ALICE_PRO_35B_MOE,
    MODEL_ALICE_STANDARD,
    MODEL_PROFILES,
    MODEL_RP_LITE,
    MODEL_RP_PRO,
)

ApiKeyLifecycleState = Literal["requested", "issued", "revoked", "rotated"]
LaunchRouteMode = Literal["Auto", "Fast", "Standard", "Best", "RP Lite", "RP Pro"]
LaunchAdmissionStatus = Literal["accepted", "queued", "rejected"]

API_KEY_LIFECYCLE_STATES: tuple[ApiKeyLifecycleState, ...] = (
    "requested",
    "issued",
    "revoked",
    "rotated",
)
PUBLIC_LAUNCH_ROUTE_MODES: tuple[LaunchRouteMode, ...] = (
    "Auto",
    "Fast",
    "Standard",
    "Best",
    "RP Lite",
    "RP Pro",
)
PUBLIC_LAUNCH_ROUTE_CHAINS: dict[LaunchRouteMode, tuple[str, ...]] = {
    "Auto": (MODEL_ALICE_STANDARD, MODEL_ALICE_LITE),
    "Fast": (MODEL_ALICE_LITE,),
    "Standard": (MODEL_ALICE_STANDARD, MODEL_ALICE_LITE),
    "Best": (
        MODEL_ALICE_PRO_35B_MOE,
        MODEL_ALICE_PRO_27B,
        MODEL_ALICE_STANDARD,
        MODEL_ALICE_LITE,
    ),
    "RP Lite": (MODEL_RP_LITE,),
    "RP Pro": (MODEL_RP_PRO, MODEL_RP_LITE),
}
REQUIRED_RATE_LIMIT_BUCKETS = ("ip", "device", "api_key")
REQUIRED_ABUSE_GUARD_REASON_CODES = (
    "q38_tampered_request",
    "q38_replayed_request",
    "q38_excessive_replayed_request",
    "q38_failed_request",
    "q38_excessive_failed_request",
    "q38_unknown_api_key_hash",
    "q38_disabled_api_key_hash",
)

_PUBLIC_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._:@/-]{0,191}$")
_FORBIDDEN_RAW_KEY_FIELDS = frozenset(
    {
        "api_key",
        "api_key_value",
        "authorization",
        "bearer",
        "plaintext",
        "private_key",
        "raw_key",
        "secret",
        "seed",
        "token",
    }
)


@dataclass(frozen=True, slots=True)
class ApiKeyLifecycleRecord:
    key_id: str
    state: ApiKeyLifecycleState
    requested_at: datetime
    key_hash: str | None = None
    issued_at: datetime | None = None
    revoked_at: datetime | None = None
    rotated_at: datetime | None = None
    replacement_key_hash: str | None = None
    owner_ref: str = "owner-unassigned"
    raw_key_persisted: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "key_id", _canonical_key_id(self.key_id))
        object.__setattr__(self, "owner_ref", _canonical_public_id("owner_ref", self.owner_ref))
        if self.state not in API_KEY_LIFECYCLE_STATES:
            raise ValueError("api key lifecycle state is unsupported")
        validate_aware_timestamp("requested_at", self.requested_at)
        self._validate_optional_hash("key_hash", self.key_hash)
        self._validate_optional_hash("replacement_key_hash", self.replacement_key_hash)
        for name, timestamp in (
            ("issued_at", self.issued_at),
            ("revoked_at", self.revoked_at),
            ("rotated_at", self.rotated_at),
        ):
            if timestamp is not None:
                validate_aware_timestamp(name, timestamp)
                if timestamp < self.requested_at:
                    raise ValueError(f"{name} must not be before requested_at")
        if self.raw_key_persisted:
            raise ValueError("raw_api_key_persistence_forbidden")
        self._validate_state_shape()

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> ApiKeyLifecycleRecord:
        forbidden = _FORBIDDEN_RAW_KEY_FIELDS.intersection(payload)
        if forbidden:
            names = ", ".join(sorted(forbidden))
            raise ValueError(f"raw API key material is forbidden: {names}")
        return cls(
            key_id=_required_str(payload, "key_id"),
            state=_required_str(payload, "state"),  # type: ignore[arg-type]
            requested_at=_required_datetime(payload, "requested_at"),
            key_hash=_optional_str(payload, "key_hash"),
            issued_at=_optional_datetime(payload, "issued_at"),
            revoked_at=_optional_datetime(payload, "revoked_at"),
            rotated_at=_optional_datetime(payload, "rotated_at"),
            replacement_key_hash=_optional_str(payload, "replacement_key_hash"),
            owner_ref=_required_str(payload, "owner_ref", default="owner-unassigned"),
            raw_key_persisted=_required_bool(payload, "raw_key_persisted", default=False),
        )

    def public_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "key_id": self.key_id,
            "state": self.state,
            "requested_at": self.requested_at.isoformat(),
            "owner_ref": self.owner_ref,
            "raw_key_persisted": False,
        }
        if self.key_hash is not None:
            payload["key_hash"] = self.key_hash
        if self.issued_at is not None:
            payload["issued_at"] = self.issued_at.isoformat()
        if self.revoked_at is not None:
            payload["revoked_at"] = self.revoked_at.isoformat()
        if self.rotated_at is not None:
            payload["rotated_at"] = self.rotated_at.isoformat()
        if self.replacement_key_hash is not None:
            payload["replacement_key_hash"] = self.replacement_key_hash
        return payload

    def _validate_optional_hash(self, name: str, value: str | None) -> None:
        if value is None:
            return
        validate_sha256(value, field_name=name)

    def _validate_state_shape(self) -> None:
        if self.state == "requested":
            if any(
                value is not None
                for value in (
                    self.key_hash,
                    self.issued_at,
                    self.revoked_at,
                    self.rotated_at,
                    self.replacement_key_hash,
                )
            ):
                raise ValueError("requested keys must not carry issued key material")
            return
        if self.key_hash is None or self.issued_at is None:
            raise ValueError("issued key hash and issued_at are required")
        if self.state == "issued":
            if self.revoked_at is not None or self.rotated_at is not None:
                raise ValueError("issued keys must not carry revoke or rotate timestamps")
            if self.replacement_key_hash is not None:
                raise ValueError("issued keys must not carry replacement key hash")
            return
        if self.state == "revoked":
            if self.revoked_at is None:
                raise ValueError("revoked_at is required for revoked keys")
            if self.rotated_at is not None or self.replacement_key_hash is not None:
                raise ValueError("revoked keys must not carry rotation metadata")
            return
        if self.rotated_at is None or self.replacement_key_hash is None:
            raise ValueError("rotated_at and replacement_key_hash are required")
        if self.revoked_at is not None:
            raise ValueError("rotated keys must not carry revoked_at")
        if self.replacement_key_hash == self.key_hash:
            raise ValueError("replacement_key_hash must differ from key_hash")


@dataclass(frozen=True, slots=True)
class ApiChatLaunchRateLimitContract:
    free_chat_requests_per_hour: int = FREE_CHAT_LIMIT_PER_HOUR
    enforced_buckets: tuple[str, ...] = REQUIRED_RATE_LIMIT_BUCKETS
    api_key_hash_only: bool = True

    def __post_init__(self) -> None:
        if self.free_chat_requests_per_hour != FREE_CHAT_LIMIT_PER_HOUR:
            raise ValueError("free_chat_limit_must_remain_10_per_hour")
        missing = set(REQUIRED_RATE_LIMIT_BUCKETS).difference(self.enforced_buckets)
        if missing:
            names = ", ".join(sorted(missing))
            raise ValueError(f"missing_required_rate_limit_buckets: {names}")
        if not self.api_key_hash_only:
            raise ValueError("api_key_rate_limit_must_use_hash_only")
        for bucket in self.enforced_buckets:
            _canonical_public_id("rate_limit_bucket", bucket)


@dataclass(frozen=True, slots=True)
class ApiChatAbuseGuardContract:
    reason_codes: tuple[str, ...] = REQUIRED_ABUSE_GUARD_REASON_CODES

    def __post_init__(self) -> None:
        missing = set(REQUIRED_ABUSE_GUARD_REASON_CODES).difference(self.reason_codes)
        if missing:
            names = ", ".join(sorted(missing))
            raise ValueError(f"missing_required_abuse_reason_codes: {names}")
        if len(set(self.reason_codes)) != len(self.reason_codes):
            raise ValueError("abuse reason codes must be unique")
        for reason_code in self.reason_codes:
            _canonical_public_id("abuse_reason_code", reason_code)


@dataclass(frozen=True, slots=True)
class ModelRouteHealthInput:
    model_id: str
    healthy: bool
    capacity_available: bool
    observed_at: datetime
    latency_ms: int = 0
    queue_depth: int = 0
    request_time_model_download_required: bool = False

    def __post_init__(self) -> None:
        if self.model_id not in MODEL_PROFILES:
            raise ValueError("model_id is not part of the public launch route contract")
        validate_aware_timestamp("observed_at", self.observed_at)
        if self.latency_ms < 0:
            raise ValueError("latency_ms must be non-negative")
        if self.queue_depth < 0:
            raise ValueError("queue_depth must be non-negative")


@dataclass(frozen=True, slots=True)
class ModelRouteDecision:
    status: LaunchAdmissionStatus
    reason_code: str
    route_mode: LaunchRouteMode
    target_models: tuple[str, ...]
    selected_model_id: str | None = None
    downgrade_applied: bool = False
    fail_closed: bool = False
    request_time_model_download_allowed: bool = False

    def __post_init__(self) -> None:
        _canonical_public_id("reason_code", self.reason_code)
        if self.selected_model_id is not None and self.selected_model_id not in MODEL_PROFILES:
            raise ValueError("selected_model_id is unsupported")
        if self.request_time_model_download_allowed:
            raise ValueError("request_time_model_download_must_remain_forbidden")


@dataclass(frozen=True, slots=True)
class MinerDeviceAvailability:
    device_id: str
    online: bool
    memory_gb: int
    observed_at: datetime
    cached_models: tuple[str, ...] = ()
    loaded_models: tuple[str, ...] = ()
    current_inference_jobs: int = 0
    request_time_model_download_planned: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "device_id", _canonical_public_id("device_id", self.device_id))
        validate_aware_timestamp("observed_at", self.observed_at)
        if self.memory_gb <= 0:
            raise ValueError("memory_gb must be positive")
        if self.current_inference_jobs < 0:
            raise ValueError("current_inference_jobs must be non-negative")
        if self.request_time_model_download_planned:
            raise ValueError("request_time_model_download_must_not_be_planned")
        for model_id in (*self.cached_models, *self.loaded_models):
            if model_id not in MODEL_PROFILES:
                raise ValueError("device model is unsupported")

    def has_model(self, model_id: str) -> bool:
        return model_id in self.loaded_models or model_id in self.cached_models


@dataclass(frozen=True, slots=True)
class QueueAdmissionDecision:
    status: LaunchAdmissionStatus
    reason_code: str
    selected_device_id: str | None = None
    queue_position: int | None = None
    request_time_model_download_allowed: bool = False

    def __post_init__(self) -> None:
        _canonical_public_id("reason_code", self.reason_code)
        if self.selected_device_id is not None:
            _canonical_public_id("selected_device_id", self.selected_device_id)
        if self.queue_position is not None and self.queue_position <= 0:
            raise ValueError("queue_position must be positive")
        if self.request_time_model_download_allowed:
            raise ValueError("request_time_model_download_must_remain_forbidden")


@dataclass(frozen=True, slots=True)
class ApiChatLaunchReadinessContract:
    key_lifecycle_states: tuple[ApiKeyLifecycleState, ...] = API_KEY_LIFECYCLE_STATES
    rate_limits: ApiChatLaunchRateLimitContract = field(
        default_factory=ApiChatLaunchRateLimitContract
    )
    abuse_guard: ApiChatAbuseGuardContract = field(default_factory=ApiChatAbuseGuardContract)
    route_modes: tuple[LaunchRouteMode, ...] = PUBLIC_LAUNCH_ROUTE_MODES
    staging_ready: bool = True
    production_deployment_ready: bool = False
    production_service_deployment: bool = False
    live_reward_enabled: bool = False
    payout_enabled: bool = False
    request_time_model_download_allowed: bool = False
    fastapi_or_flask_required: bool = False

    def __post_init__(self) -> None:
        if self.key_lifecycle_states != API_KEY_LIFECYCLE_STATES:
            raise ValueError("api key lifecycle states must be requested/issued/revoked/rotated")
        if self.route_modes != PUBLIC_LAUNCH_ROUTE_MODES:
            raise ValueError("public launch route modes must match Q44")
        if self.production_deployment_ready or self.production_service_deployment:
            raise ValueError("production_deployment_must_remain_disabled")
        if self.live_reward_enabled:
            raise ValueError("live_reward_must_remain_disabled")
        if self.payout_enabled:
            raise ValueError("payout_must_remain_disabled")
        if self.request_time_model_download_allowed:
            raise ValueError("request_time_model_download_must_remain_forbidden")
        if self.fastapi_or_flask_required:
            raise ValueError("q44_contract_must_not_require_fastapi_or_flask")

    def public_summary(self) -> dict[str, object]:
        return {
            "staging_ready": self.staging_ready,
            "production_deployment_ready": False,
            "production_service_deployment": False,
            "live_reward_enabled": False,
            "payout_enabled": False,
            "key_lifecycle_states": self.key_lifecycle_states,
            "free_chat_requests_per_hour": self.rate_limits.free_chat_requests_per_hour,
            "rate_limit_buckets": self.rate_limits.enforced_buckets,
            "abuse_guard_reason_codes": self.abuse_guard.reason_codes,
            "route_modes": self.route_modes,
            "request_time_model_download_allowed": False,
            "fastapi_or_flask_required": False,
        }


def evaluate_model_route(
    mode: LaunchRouteMode,
    health_inputs: tuple[ModelRouteHealthInput, ...],
) -> ModelRouteDecision:
    if mode not in PUBLIC_LAUNCH_ROUTE_MODES:
        raise ValueError("launch route mode is unsupported")
    targets = PUBLIC_LAUNCH_ROUTE_CHAINS[mode]
    health_by_model = {health.model_id: health for health in health_inputs}

    blocked_by_request_download = False
    for model_id in targets:
        health = health_by_model.get(model_id)
        if health is None or not health.healthy or not health.capacity_available:
            continue
        if health.request_time_model_download_required:
            blocked_by_request_download = True
            continue
        downgrade = model_id != targets[0]
        return ModelRouteDecision(
            status="accepted",
            reason_code="q44_model_route_downgraded" if downgrade else "q44_model_route_healthy",
            route_mode=mode,
            target_models=targets,
            selected_model_id=model_id,
            downgrade_applied=downgrade,
        )

    fail_closed = mode in {"RP Lite", "RP Pro"}
    if blocked_by_request_download:
        reason_code = "q44_request_time_model_download_forbidden"
    elif fail_closed:
        reason_code = "q44_rp_route_unavailable_fail_closed"
    else:
        reason_code = "q44_no_healthy_model_route"
    return ModelRouteDecision(
        status="rejected",
        reason_code=reason_code,
        route_mode=mode,
        target_models=targets,
        fail_closed=fail_closed,
    )


def evaluate_queue_admission(
    *,
    model_id: str,
    devices: tuple[MinerDeviceAvailability, ...],
    queued_jobs: int,
    queue_capacity: int,
) -> QueueAdmissionDecision:
    if model_id not in MODEL_PROFILES:
        raise ValueError("model_id is unsupported")
    if queued_jobs < 0:
        raise ValueError("queued_jobs must be non-negative")
    if queue_capacity <= 0:
        raise ValueError("queue_capacity must be positive")
    if queued_jobs >= queue_capacity:
        return QueueAdmissionDecision(status="rejected", reason_code="q44_queue_full")

    candidates = tuple(device for device in devices if device.online and device.has_model(model_id))
    if not candidates:
        return QueueAdmissionDecision(
            status="rejected",
            reason_code="q44_no_cached_or_loaded_miner_model",
        )
    available = tuple(device for device in candidates if device.current_inference_jobs == 0)
    if available:
        selected = sorted(available, key=lambda device: _device_sort_key(device, model_id))[0]
        return QueueAdmissionDecision(
            status="accepted",
            reason_code="q44_queue_admitted",
            selected_device_id=selected.device_id,
        )
    selected = sorted(candidates, key=lambda device: _device_sort_key(device, model_id))[0]
    return QueueAdmissionDecision(
        status="queued",
        reason_code="q44_queue_waiting_for_cached_model",
        selected_device_id=selected.device_id,
        queue_position=queued_jobs + 1,
    )


def _device_sort_key(device: MinerDeviceAvailability, model_id: str) -> tuple[int, int, int, str]:
    loaded_rank = 0 if model_id in device.loaded_models else 1
    cached_rank = 0 if model_id in device.cached_models else 1
    return (loaded_rank, cached_rank, device.current_inference_jobs, device.device_id)


def _canonical_key_id(value: str) -> str:
    canonical = _canonical_public_id("key_id", value)
    if not canonical.startswith("ak_"):
        raise ValueError("key_id must start with ak_")
    return canonical


def _canonical_public_id(field_name: str, value: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be non-empty")
    canonical = value.strip().lower()
    ensure_no_raw_secret(canonical, field_name=field_name)
    if canonical != value.strip():
        raise ValueError(f"{field_name} must be canonical lowercase")
    if _PUBLIC_ID_PATTERN.fullmatch(canonical) is None:
        raise ValueError(f"{field_name} is malformed")
    return canonical


def _required_str(
    payload: Mapping[str, object],
    field_name: str,
    *,
    default: str | None = None,
) -> str:
    value = payload.get(field_name, default)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _optional_str(payload: Mapping[str, object], field_name: str) -> str | None:
    value = payload.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _required_datetime(payload: Mapping[str, object], field_name: str) -> datetime:
    value = payload.get(field_name)
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime")
    return value


def _optional_datetime(payload: Mapping[str, object], field_name: str) -> datetime | None:
    value = payload.get(field_name)
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime")
    return value


def _required_bool(
    payload: Mapping[str, object],
    field_name: str,
    *,
    default: bool,
) -> bool:
    value = payload.get(field_name, default)
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be a boolean")
    return value

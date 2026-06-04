from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from alice_acp.reward_backend.types import validate_public_identifier

LAUNCH_OPS_MONITORING_CONTRACT_VERSION = "q30-launch-ops-monitoring-contract-v1"

COMPONENT_PS = "ps"
COMPONENT_SHADOW = "shadow"
COMPONENT_REWARD_BACKEND = "reward_backend"
COMPONENT_MINER_PROOF = "miner_proof"
COMPONENT_API_CHAT = "api_chat"
COMPONENT_WEBSITE = "website"
SUPPORTED_COMPONENTS = (
    COMPONENT_PS,
    COMPONENT_SHADOW,
    COMPONENT_REWARD_BACKEND,
    COMPONENT_MINER_PROOF,
    COMPONENT_API_CHAT,
    COMPONENT_WEBSITE,
)

HEALTH_OK = "ok"
HEALTH_DEGRADED = "degraded"
HEALTH_BAD = "bad"
HEALTH_UNKNOWN = "unknown"

SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"

READINESS_GREEN = "green"
READINESS_DEGRADED = "degraded"
READINESS_BAD = "bad"

REASON_COMPONENT_DEGRADED = "launch_ops_component_degraded"
REASON_COMPONENT_BAD = "launch_ops_component_bad"
REASON_HEARTBEAT_STALE = "launch_ops_heartbeat_stale"
REASON_JSONL_GROWTH_STALLED = "launch_ops_jsonl_growth_stalled"
REASON_PROOF_REJECTION_SPIKE = "launch_ops_proof_rejection_spike"
REASON_MODEL_QUEUE_DEPTH_HIGH = "launch_ops_model_queue_depth_high"
REASON_RATE_LIMIT_SPIKE = "launch_ops_rate_limit_spike"
REASON_RESERVE_BUDGET_MISSING = "launch_ops_reserve_budget_missing"
REASON_RESERVE_BUDGET_LOW = "launch_ops_reserve_budget_low"
REASON_LIVE_REWARD_DEFAULT_OFF = "launch_ops_live_reward_default_off"
REASON_PAYOUT_EXECUTOR_DEFAULT_OFF = "launch_ops_payout_executor_default_off"
REASON_CHAIN_TRANSFER_FORBIDDEN = "launch_ops_chain_transfer_forbidden"
REASON_DIRECT_POOL_MODE_FORBIDDEN = "launch_ops_direct_pool_mode_forbidden"
REASON_MINER_PAYOUT_FORBIDDEN = "launch_ops_miner_payout_forbidden"

ZERO_DECIMAL = Decimal("0")
ONE_DECIMAL = Decimal("1")

LaunchOpsComponent = Literal[
    "ps",
    "shadow",
    "reward_backend",
    "miner_proof",
    "api_chat",
    "website",
]
HealthStatus = Literal["ok", "degraded", "bad", "unknown"]
AlertSeverity = Literal["info", "warning", "critical"]
ReadinessStatus = Literal["green", "degraded", "bad"]


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class LaunchOpsThresholds:
    max_heartbeat_age_seconds: int = 120
    max_jsonl_idle_seconds: int = 300
    warning_proof_rejection_rate: Decimal = Decimal("0.10")
    critical_proof_rejection_rate: Decimal = Decimal("0.25")
    warning_model_queue_depth: int = 25
    critical_model_queue_depth: int = 100
    warning_rate_limit_events: int = 50
    critical_rate_limit_events: int = 250
    required_reserve_ratio: Decimal = Decimal("0.20")

    def __post_init__(self) -> None:
        for field_name, value in (
            ("max_heartbeat_age_seconds", self.max_heartbeat_age_seconds),
            ("max_jsonl_idle_seconds", self.max_jsonl_idle_seconds),
            ("warning_model_queue_depth", self.warning_model_queue_depth),
            ("critical_model_queue_depth", self.critical_model_queue_depth),
            ("warning_rate_limit_events", self.warning_rate_limit_events),
            ("critical_rate_limit_events", self.critical_rate_limit_events),
        ):
            if value <= 0:
                raise ValueError(f"{field_name}_must_be_positive")
        if self.critical_model_queue_depth < self.warning_model_queue_depth:
            raise ValueError("critical_model_queue_depth_must_cover_warning_threshold")
        if self.critical_rate_limit_events < self.warning_rate_limit_events:
            raise ValueError("critical_rate_limit_events_must_cover_warning_threshold")
        for field_name, value in (
            ("warning_proof_rejection_rate", self.warning_proof_rejection_rate),
            ("critical_proof_rejection_rate", self.critical_proof_rejection_rate),
            ("required_reserve_ratio", self.required_reserve_ratio),
        ):
            _validate_ratio(field_name, value)
        if self.critical_proof_rejection_rate < self.warning_proof_rejection_rate:
            raise ValueError("critical_proof_rejection_rate_must_cover_warning_threshold")


@dataclass(frozen=True, slots=True)
class ComponentHealth:
    component: LaunchOpsComponent
    status: HealthStatus
    checked_at: datetime
    heartbeat_observed_at: datetime | None = None
    reason_code: str = "launch_ops_component_ok"

    def __post_init__(self) -> None:
        if self.component not in SUPPORTED_COMPONENTS:
            raise ValueError("launch_ops_component_is_unsupported")
        if self.status not in (HEALTH_OK, HEALTH_DEGRADED, HEALTH_BAD, HEALTH_UNKNOWN):
            raise ValueError("launch_ops_health_status_is_unsupported")
        validate_aware_timestamp("checked_at", self.checked_at)
        if self.heartbeat_observed_at is not None:
            validate_aware_timestamp("heartbeat_observed_at", self.heartbeat_observed_at)
            if self.heartbeat_observed_at > self.checked_at:
                raise ValueError("heartbeat_observed_at_must_not_be_after_checked_at")
        validate_public_identifier("reason_code", self.reason_code)

    def heartbeat_age_seconds(self, *, now: datetime) -> int | None:
        validate_aware_timestamp("now", now)
        if self.heartbeat_observed_at is None:
            return None
        return int((now - self.heartbeat_observed_at).total_seconds())


@dataclass(frozen=True, slots=True)
class JsonlGrowthMetric:
    stream_name: str
    previous_size_bytes: int
    current_size_bytes: int
    window_seconds: int
    observed_at: datetime
    expected_to_grow: bool = True

    def __post_init__(self) -> None:
        validate_public_identifier("stream_name", self.stream_name)
        if self.previous_size_bytes < 0 or self.current_size_bytes < 0:
            raise ValueError("jsonl_size_bytes_must_be_non_negative")
        if self.current_size_bytes < self.previous_size_bytes:
            raise ValueError("jsonl_current_size_must_not_shrink")
        if self.window_seconds <= 0:
            raise ValueError("jsonl_window_seconds_must_be_positive")
        validate_aware_timestamp("observed_at", self.observed_at)

    @property
    def growth_bytes(self) -> int:
        return self.current_size_bytes - self.previous_size_bytes


@dataclass(frozen=True, slots=True)
class ProofRejectionMetric:
    accepted_count: int = 0
    rejected_count: int = 0
    duplicate_count: int = 0
    tampered_count: int = 0

    def __post_init__(self) -> None:
        for field_name, value in (
            ("accepted_count", self.accepted_count),
            ("rejected_count", self.rejected_count),
            ("duplicate_count", self.duplicate_count),
            ("tampered_count", self.tampered_count),
        ):
            if value < 0:
                raise ValueError(f"{field_name}_must_be_non_negative")

    @property
    def total_count(self) -> int:
        return self.accepted_count + self.rejected_count

    @property
    def rejection_rate(self) -> Decimal:
        if self.total_count == 0:
            return ZERO_DECIMAL
        return Decimal(self.rejected_count) / Decimal(self.total_count)


@dataclass(frozen=True, slots=True)
class ApiChatQueueMetric:
    model_queue_depth: int = 0
    oldest_queued_seconds: int = 0

    def __post_init__(self) -> None:
        if self.model_queue_depth < 0:
            raise ValueError("model_queue_depth_must_be_non_negative")
        if self.oldest_queued_seconds < 0:
            raise ValueError("oldest_queued_seconds_must_be_non_negative")


@dataclass(frozen=True, slots=True)
class RateLimitCounters:
    free_chat_limited: int = 0
    api_key_limited: int = 0
    ip_limited: int = 0
    user_limited: int = 0

    def __post_init__(self) -> None:
        for field_name, value in (
            ("free_chat_limited", self.free_chat_limited),
            ("api_key_limited", self.api_key_limited),
            ("ip_limited", self.ip_limited),
            ("user_limited", self.user_limited),
        ):
            if value < 0:
                raise ValueError(f"{field_name}_must_be_non_negative")

    @property
    def total_limited(self) -> int:
        return (
            self.free_chat_limited
            + self.api_key_limited
            + self.ip_limited
            + self.user_limited
        )


@dataclass(frozen=True, slots=True)
class ReserveBudgetMetric:
    reserve_budget_confirmed: bool = False
    reserve_ratio: Decimal | None = None
    source_ref: str = "owner-input-required"

    def __post_init__(self) -> None:
        validate_public_identifier("source_ref", self.source_ref)
        if self.reserve_ratio is not None:
            _validate_ratio("reserve_ratio", self.reserve_ratio)


@dataclass(frozen=True, slots=True)
class LaunchOpsGateState:
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False
    chain_transfer_enabled: bool = False
    direct_pool_mode_supported: bool = False
    miner_payout_address_allowed: bool = False

    def __post_init__(self) -> None:
        if self.live_reward_enabled:
            raise ValueError(REASON_LIVE_REWARD_DEFAULT_OFF)
        if self.payout_executor_enabled:
            raise ValueError(REASON_PAYOUT_EXECUTOR_DEFAULT_OFF)
        if self.chain_transfer_enabled:
            raise ValueError(REASON_CHAIN_TRANSFER_FORBIDDEN)
        if self.direct_pool_mode_supported:
            raise ValueError(REASON_DIRECT_POOL_MODE_FORBIDDEN)
        if self.miner_payout_address_allowed:
            raise ValueError(REASON_MINER_PAYOUT_FORBIDDEN)


@dataclass(frozen=True, slots=True)
class LaunchOpsSnapshot:
    observed_at: datetime
    components: tuple[ComponentHealth, ...]
    proof_rejection: ProofRejectionMetric = field(default_factory=ProofRejectionMetric)
    jsonl_growth: tuple[JsonlGrowthMetric, ...] = ()
    api_chat_queue: ApiChatQueueMetric = field(default_factory=ApiChatQueueMetric)
    rate_limits: RateLimitCounters = field(default_factory=RateLimitCounters)
    reserve_budget: ReserveBudgetMetric = field(default_factory=ReserveBudgetMetric)
    gates: LaunchOpsGateState = field(default_factory=LaunchOpsGateState)
    contract_version: str = LAUNCH_OPS_MONITORING_CONTRACT_VERSION

    def __post_init__(self) -> None:
        validate_aware_timestamp("observed_at", self.observed_at)
        validate_public_identifier("contract_version", self.contract_version)
        component_names = {component.component for component in self.components}
        missing = set(SUPPORTED_COMPONENTS) - component_names
        if missing:
            raise ValueError("launch_ops_snapshot_missing_required_component")
        if len(component_names) != len(self.components):
            raise ValueError("launch_ops_snapshot_has_duplicate_component")


@dataclass(frozen=True, slots=True)
class LaunchOpsAlert:
    severity: AlertSeverity
    reason_code: str
    message: str
    component: LaunchOpsComponent | None = None

    def __post_init__(self) -> None:
        if self.severity not in (SEVERITY_INFO, SEVERITY_WARNING, SEVERITY_CRITICAL):
            raise ValueError("launch_ops_alert_severity_is_unsupported")
        validate_public_identifier("reason_code", self.reason_code)
        if self.component is not None and self.component not in SUPPORTED_COMPONENTS:
            raise ValueError("launch_ops_alert_component_is_unsupported")
        if not self.message or "\n" in self.message:
            raise ValueError("launch_ops_alert_message_must_be_single_line")


@dataclass(frozen=True, slots=True)
class LaunchOpsEvaluation:
    status: ReadinessStatus
    alerts: tuple[LaunchOpsAlert, ...]
    hard_blockers: tuple[str, ...]
    can_open_monitoring_observation: bool
    can_open_miner_public_entry: bool
    deferred_capability_gates: tuple[str, ...] = ()
    can_start_live_reward_window: bool = False
    can_start_payout_executor: bool = False
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False

    def __post_init__(self) -> None:
        if self.status not in (READINESS_GREEN, READINESS_DEGRADED, READINESS_BAD):
            raise ValueError("launch_ops_readiness_status_is_unsupported")
        for blocker in self.hard_blockers:
            validate_public_identifier("hard_blocker", blocker)
        for gate in self.deferred_capability_gates:
            validate_public_identifier("deferred_capability_gate", gate)
        if self.can_start_live_reward_window:
            raise ValueError(REASON_LIVE_REWARD_DEFAULT_OFF)
        if self.can_start_payout_executor:
            raise ValueError(REASON_PAYOUT_EXECUTOR_DEFAULT_OFF)
        if self.live_reward_enabled:
            raise ValueError(REASON_LIVE_REWARD_DEFAULT_OFF)
        if self.payout_executor_enabled:
            raise ValueError(REASON_PAYOUT_EXECUTOR_DEFAULT_OFF)


def validate_aware_timestamp(name: str, value: datetime) -> None:
    if value.tzinfo is None:
        raise ValueError(f"{name}_must_be_timezone_aware")


def _validate_ratio(field_name: str, value: Decimal) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(f"{field_name}_must_be_decimal")
    if value < ZERO_DECIMAL or value > ONE_DECIMAL:
        raise ValueError(f"{field_name}_must_be_between_zero_and_one")

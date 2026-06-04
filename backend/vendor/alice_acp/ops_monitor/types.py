from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from alice_acp.evidence.types import (
    ensure_no_production_alice_reference,
    ensure_no_raw_secret,
    validate_aware_timestamp,
)

OPS_MONITOR_CONTRACT_VERSION = "o1-ops-observability-anti-cheat-monitor-v1"

SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"

ACTION_ALLOW = "allow"
ACTION_CAP = "cap"
ACTION_UNDER_REVIEW = "under_review"
ACTION_REJECT = "reject"
ACTION_KILL_SWITCH_REVIEW = "kill_switch_review"

REASON_MINER_HEARTBEAT_STALE = "ops_monitor_miner_heartbeat_stale"
REASON_MINER_ONLINE_RATE_LOW = "ops_monitor_miner_online_rate_low"
REASON_PROOF_REJECTION_SPIKE = "ops_monitor_proof_rejection_spike"
REASON_DUPLICATE_PROOF_SPIKE = "ops_monitor_duplicate_proof_spike"
REASON_TAMPER_PROOF_SEEN = "ops_monitor_tamper_proof_seen"
REASON_STALE_PROOF_SPIKE = "ops_monitor_stale_proof_spike"
REASON_PAYOUT_MISMATCH_SEEN = "ops_monitor_payout_mismatch_seen"
REASON_POOL_EVIDENCE_MISSING_RATE_HIGH = (
    "ops_monitor_pool_evidence_missing_rate_high"
)
REASON_MODEL_INFERENCE_ERROR_SPIKE = "ops_monitor_model_inference_error_spike"
REASON_MODEL_INFERENCE_TIMEOUT_SPIKE = "ops_monitor_model_inference_timeout_spike"
REASON_MODEL_INFERENCE_QUEUE_DEPTH_HIGH = (
    "ops_monitor_model_inference_queue_depth_high"
)
REASON_KILL_SWITCH_REVIEW_RECOMMENDED = (
    "ops_monitor_kill_switch_review_recommended"
)

ZERO_DECIMAL = Decimal("0")
ONE_DECIMAL = Decimal("1")

AlertSeverity = Literal["info", "warning", "critical"]
RecommendedAction = Literal[
    "allow",
    "cap",
    "under_review",
    "reject",
    "kill_switch_review",
]


@dataclass(frozen=True, slots=True)
class OpsMonitorThresholds:
    max_heartbeat_age_seconds: int = 120
    warning_miner_online_rate: Decimal = Decimal("0.95")
    critical_miner_online_rate: Decimal = Decimal("0.80")
    warning_proof_rejection_rate: Decimal = Decimal("0.10")
    critical_proof_rejection_rate: Decimal = Decimal("0.25")
    warning_duplicate_rate: Decimal = Decimal("0.02")
    critical_duplicate_rate: Decimal = Decimal("0.10")
    warning_stale_rate: Decimal = Decimal("0.05")
    critical_stale_rate: Decimal = Decimal("0.20")
    warning_pool_evidence_missing_rate: Decimal = Decimal("0.05")
    critical_pool_evidence_missing_rate: Decimal = Decimal("0.20")
    warning_model_error_rate: Decimal = Decimal("0.05")
    critical_model_error_rate: Decimal = Decimal("0.15")
    warning_model_timeout_rate: Decimal = Decimal("0.02")
    critical_model_timeout_rate: Decimal = Decimal("0.10")
    warning_model_queue_depth: int = 25
    critical_model_queue_depth: int = 100
    kill_switch_review_critical_alerts: int = 2

    def __post_init__(self) -> None:
        if self.max_heartbeat_age_seconds <= 0:
            raise ValueError("max_heartbeat_age_seconds_must_be_positive")
        if self.warning_model_queue_depth <= 0 or self.critical_model_queue_depth <= 0:
            raise ValueError("model_queue_thresholds_must_be_positive")
        if self.critical_model_queue_depth < self.warning_model_queue_depth:
            raise ValueError("critical_model_queue_depth_must_cover_warning")
        if self.kill_switch_review_critical_alerts <= 0:
            raise ValueError("kill_switch_review_critical_alerts_must_be_positive")
        for field_name, value in (
            ("warning_miner_online_rate", self.warning_miner_online_rate),
            ("critical_miner_online_rate", self.critical_miner_online_rate),
            ("warning_proof_rejection_rate", self.warning_proof_rejection_rate),
            ("critical_proof_rejection_rate", self.critical_proof_rejection_rate),
            ("warning_duplicate_rate", self.warning_duplicate_rate),
            ("critical_duplicate_rate", self.critical_duplicate_rate),
            ("warning_stale_rate", self.warning_stale_rate),
            ("critical_stale_rate", self.critical_stale_rate),
            (
                "warning_pool_evidence_missing_rate",
                self.warning_pool_evidence_missing_rate,
            ),
            (
                "critical_pool_evidence_missing_rate",
                self.critical_pool_evidence_missing_rate,
            ),
            ("warning_model_error_rate", self.warning_model_error_rate),
            ("critical_model_error_rate", self.critical_model_error_rate),
            ("warning_model_timeout_rate", self.warning_model_timeout_rate),
            ("critical_model_timeout_rate", self.critical_model_timeout_rate),
        ):
            _validate_ratio(field_name, value)
        _validate_warning_critical(
            "miner_online_rate",
            warning=self.warning_miner_online_rate,
            critical=self.critical_miner_online_rate,
            lower_is_worse=True,
        )
        for label, warning, critical in (
            (
                "proof_rejection_rate",
                self.warning_proof_rejection_rate,
                self.critical_proof_rejection_rate,
            ),
            ("duplicate_rate", self.warning_duplicate_rate, self.critical_duplicate_rate),
            ("stale_rate", self.warning_stale_rate, self.critical_stale_rate),
            (
                "pool_evidence_missing_rate",
                self.warning_pool_evidence_missing_rate,
                self.critical_pool_evidence_missing_rate,
            ),
            ("model_error_rate", self.warning_model_error_rate, self.critical_model_error_rate),
            (
                "model_timeout_rate",
                self.warning_model_timeout_rate,
                self.critical_model_timeout_rate,
            ),
        ):
            _validate_warning_critical(
                label,
                warning=warning,
                critical=critical,
                lower_is_worse=False,
            )


@dataclass(frozen=True, slots=True)
class MinerHeartbeatSample:
    miner_id: str
    observed_at: datetime
    last_heartbeat_at: datetime | None
    expected_online: bool = True

    def __post_init__(self) -> None:
        _validate_public_text("miner_id", self.miner_id)
        validate_aware_timestamp("observed_at", self.observed_at)
        if self.last_heartbeat_at is not None:
            validate_aware_timestamp("last_heartbeat_at", self.last_heartbeat_at)
            if self.last_heartbeat_at > self.observed_at:
                raise ValueError("last_heartbeat_at_must_not_be_after_observed_at")

    def heartbeat_age_seconds(self) -> int | None:
        if self.last_heartbeat_at is None:
            return None
        return int((self.observed_at - self.last_heartbeat_at).total_seconds())


@dataclass(frozen=True, slots=True)
class MinerHeartbeatSummary:
    observed_at: datetime
    expected_count: int
    online_count: int
    stale_count: int
    offline_count: int
    stale_miner_ids: tuple[str, ...] = ()
    offline_miner_ids: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        validate_aware_timestamp("observed_at", self.observed_at)
        for field_name, value in (
            ("expected_count", self.expected_count),
            ("online_count", self.online_count),
            ("stale_count", self.stale_count),
            ("offline_count", self.offline_count),
        ):
            _validate_non_negative_int(field_name, value)
        if self.online_count + self.stale_count + self.offline_count != self.expected_count:
            raise ValueError("miner_heartbeat_summary_counts_must_match_expected")
        for field_name, values in (
            ("stale_miner_ids", self.stale_miner_ids),
            ("offline_miner_ids", self.offline_miner_ids),
        ):
            for value in values:
                _validate_public_text(field_name, value)

    @property
    def online_rate(self) -> Decimal:
        if self.expected_count == 0:
            return ONE_DECIMAL
        return _rate(self.online_count, self.expected_count)


@dataclass(frozen=True, slots=True)
class ProofIngestCounters:
    accepted_count: int = 0
    rejected_count: int = 0
    under_review_count: int = 0
    duplicate_count: int = 0
    tamper_count: int = 0
    stale_count: int = 0
    payout_mismatch_count: int = 0
    pool_evidence_missing_count: int = 0
    window_seconds: int = 300

    def __post_init__(self) -> None:
        for field_name, value in (
            ("accepted_count", self.accepted_count),
            ("rejected_count", self.rejected_count),
            ("under_review_count", self.under_review_count),
            ("duplicate_count", self.duplicate_count),
            ("tamper_count", self.tamper_count),
            ("stale_count", self.stale_count),
            ("payout_mismatch_count", self.payout_mismatch_count),
            ("pool_evidence_missing_count", self.pool_evidence_missing_count),
        ):
            _validate_non_negative_int(field_name, value)
        if self.window_seconds <= 0:
            raise ValueError("window_seconds_must_be_positive")
        total = self.total_ingested
        for field_name, value in (
            ("duplicate_count", self.duplicate_count),
            ("tamper_count", self.tamper_count),
            ("stale_count", self.stale_count),
            ("payout_mismatch_count", self.payout_mismatch_count),
            ("pool_evidence_missing_count", self.pool_evidence_missing_count),
        ):
            if total > 0 and value > total:
                raise ValueError(f"{field_name}_must_not_exceed_total_ingested")

    @property
    def total_ingested(self) -> int:
        return self.accepted_count + self.rejected_count + self.under_review_count

    @property
    def rejection_rate(self) -> Decimal:
        return _rate_or_zero(self.rejected_count, self.total_ingested)

    @property
    def duplicate_rate(self) -> Decimal:
        return _rate_or_zero(self.duplicate_count, self.total_ingested)

    @property
    def stale_rate(self) -> Decimal:
        return _rate_or_zero(self.stale_count, self.total_ingested)

    @property
    def pool_evidence_missing_rate(self) -> Decimal:
        return _rate_or_zero(self.pool_evidence_missing_count, self.total_ingested)


@dataclass(frozen=True, slots=True)
class ModelInferenceHealthCounters:
    total_requests: int = 0
    success_count: int = 0
    error_count: int = 0
    timeout_count: int = 0
    queued_count: int = 0
    oldest_queued_seconds: int = 0
    p95_latency_ms: int | None = None

    def __post_init__(self) -> None:
        for field_name, value in (
            ("total_requests", self.total_requests),
            ("success_count", self.success_count),
            ("error_count", self.error_count),
            ("timeout_count", self.timeout_count),
            ("queued_count", self.queued_count),
            ("oldest_queued_seconds", self.oldest_queued_seconds),
        ):
            _validate_non_negative_int(field_name, value)
        if self.p95_latency_ms is not None:
            _validate_non_negative_int("p95_latency_ms", self.p95_latency_ms)
        completed = self.success_count + self.error_count + self.timeout_count
        if completed > self.total_requests:
            raise ValueError("model_inference_completed_counts_must_not_exceed_total")

    @property
    def error_rate(self) -> Decimal:
        return _rate_or_zero(self.error_count, self.total_requests)

    @property
    def timeout_rate(self) -> Decimal:
        return _rate_or_zero(self.timeout_count, self.total_requests)


@dataclass(frozen=True, slots=True)
class OpsMonitorSnapshot:
    observed_at: datetime
    miner_heartbeats: tuple[MinerHeartbeatSample, ...]
    proof_ingest: ProofIngestCounters
    model_inference: ModelInferenceHealthCounters | None = None
    kill_switch_review_requested: bool = False
    contract_version: str = OPS_MONITOR_CONTRACT_VERSION

    def __post_init__(self) -> None:
        validate_aware_timestamp("observed_at", self.observed_at)
        _validate_public_text("contract_version", self.contract_version)
        seen_miners: set[str] = set()
        for sample in self.miner_heartbeats:
            if sample.observed_at != self.observed_at:
                raise ValueError("miner_heartbeat_observed_at_must_match_snapshot")
            if sample.miner_id in seen_miners:
                raise ValueError("miner_heartbeat_samples_must_not_duplicate_miner")
            seen_miners.add(sample.miner_id)


@dataclass(frozen=True, slots=True)
class OpsMonitorAlertPayload:
    alert_id: str
    severity: AlertSeverity
    reason_code: str
    message: str
    observed_at: datetime
    recommended_action: RecommendedAction
    metric_value: Decimal | int | str | None = None
    threshold_value: Decimal | int | str | None = None
    contract_version: str = OPS_MONITOR_CONTRACT_VERSION

    def __post_init__(self) -> None:
        _validate_public_text("alert_id", self.alert_id)
        if self.severity not in (SEVERITY_INFO, SEVERITY_WARNING, SEVERITY_CRITICAL):
            raise ValueError("ops_monitor_alert_severity_is_unsupported")
        _validate_public_text("reason_code", self.reason_code)
        _validate_public_text("message", self.message)
        if "\n" in self.message:
            raise ValueError("ops_monitor_alert_message_must_be_single_line")
        validate_aware_timestamp("observed_at", self.observed_at)
        if self.recommended_action not in (
            ACTION_ALLOW,
            ACTION_CAP,
            ACTION_UNDER_REVIEW,
            ACTION_REJECT,
            ACTION_KILL_SWITCH_REVIEW,
        ):
            raise ValueError("ops_monitor_recommended_action_is_unsupported")
        _validate_public_text("contract_version", self.contract_version)

    def as_jsonable(self) -> dict[str, object]:
        return {
            "alert_id": self.alert_id,
            "severity": self.severity,
            "reason_code": self.reason_code,
            "message": self.message,
            "observed_at": self.observed_at.isoformat(),
            "recommended_action": self.recommended_action,
            "metric_value": _json_scalar(self.metric_value),
            "threshold_value": _json_scalar(self.threshold_value),
            "contract_version": self.contract_version,
        }


@dataclass(frozen=True, slots=True)
class OpsMonitorEvaluation:
    observed_at: datetime
    miner_heartbeat: MinerHeartbeatSummary
    proof_ingest: ProofIngestCounters
    model_inference: ModelInferenceHealthCounters | None
    alerts: tuple[OpsMonitorAlertPayload, ...]
    recommended_action: RecommendedAction
    kill_switch_review_recommended: bool
    kill_switch_auto_triggered: bool = False
    production_mutation_allowed: bool = False
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False
    chain_transfer_enabled: bool = False
    contract_version: str = OPS_MONITOR_CONTRACT_VERSION

    def __post_init__(self) -> None:
        validate_aware_timestamp("observed_at", self.observed_at)
        if self.recommended_action not in (
            ACTION_ALLOW,
            ACTION_CAP,
            ACTION_UNDER_REVIEW,
            ACTION_REJECT,
            ACTION_KILL_SWITCH_REVIEW,
        ):
            raise ValueError("ops_monitor_recommended_action_is_unsupported")
        if self.kill_switch_auto_triggered:
            raise ValueError("ops_monitor_must_not_auto_trigger_kill_switch")
        if self.production_mutation_allowed:
            raise ValueError("ops_monitor_must_not_allow_production_mutation")
        if self.live_reward_enabled:
            raise ValueError("ops_monitor_must_not_enable_live_reward")
        if self.payout_executor_enabled:
            raise ValueError("ops_monitor_must_not_enable_payout_executor")
        if self.chain_transfer_enabled:
            raise ValueError("ops_monitor_must_not_enable_chain_transfer")
        _validate_public_text("contract_version", self.contract_version)

    def as_jsonable(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "observed_at": self.observed_at.isoformat(),
            "recommended_action": self.recommended_action,
            "kill_switch_review_recommended": self.kill_switch_review_recommended,
            "kill_switch_auto_triggered": self.kill_switch_auto_triggered,
            "production_mutation_allowed": self.production_mutation_allowed,
            "live_reward_enabled": self.live_reward_enabled,
            "payout_executor_enabled": self.payout_executor_enabled,
            "chain_transfer_enabled": self.chain_transfer_enabled,
            "miner_heartbeat": {
                "expected_count": self.miner_heartbeat.expected_count,
                "online_count": self.miner_heartbeat.online_count,
                "stale_count": self.miner_heartbeat.stale_count,
                "offline_count": self.miner_heartbeat.offline_count,
                "online_rate": str(self.miner_heartbeat.online_rate),
                "stale_miner_ids": list(self.miner_heartbeat.stale_miner_ids),
                "offline_miner_ids": list(self.miner_heartbeat.offline_miner_ids),
            },
            "proof_ingest": {
                "accepted_count": self.proof_ingest.accepted_count,
                "rejected_count": self.proof_ingest.rejected_count,
                "under_review_count": self.proof_ingest.under_review_count,
                "duplicate_count": self.proof_ingest.duplicate_count,
                "tamper_count": self.proof_ingest.tamper_count,
                "stale_count": self.proof_ingest.stale_count,
                "payout_mismatch_count": self.proof_ingest.payout_mismatch_count,
                "pool_evidence_missing_count": self.proof_ingest.pool_evidence_missing_count,
                "rejection_rate": str(self.proof_ingest.rejection_rate),
                "duplicate_rate": str(self.proof_ingest.duplicate_rate),
                "stale_rate": str(self.proof_ingest.stale_rate),
                "pool_evidence_missing_rate": str(
                    self.proof_ingest.pool_evidence_missing_rate
                ),
                "window_seconds": self.proof_ingest.window_seconds,
            },
            "model_inference": (
                None
                if self.model_inference is None
                else {
                    "total_requests": self.model_inference.total_requests,
                    "success_count": self.model_inference.success_count,
                    "error_count": self.model_inference.error_count,
                    "timeout_count": self.model_inference.timeout_count,
                    "queued_count": self.model_inference.queued_count,
                    "oldest_queued_seconds": self.model_inference.oldest_queued_seconds,
                    "p95_latency_ms": self.model_inference.p95_latency_ms,
                    "error_rate": str(self.model_inference.error_rate),
                    "timeout_rate": str(self.model_inference.timeout_rate),
                }
            ),
            "alerts": [alert.as_jsonable() for alert in self.alerts],
        }


@dataclass(frozen=True, slots=True)
class OpsMonitorAuditRecord:
    sequence: int
    created_at: datetime
    record_type: str
    payload: dict[str, object]
    contract_version: str = OPS_MONITOR_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.sequence <= 0:
            raise ValueError("ops_monitor_audit_sequence_must_be_positive")
        validate_aware_timestamp("created_at", self.created_at)
        _validate_public_text("record_type", self.record_type)
        _validate_public_text("contract_version", self.contract_version)

    def as_jsonable(self) -> dict[str, object]:
        return {
            "sequence": self.sequence,
            "created_at": self.created_at.isoformat(),
            "record_type": self.record_type,
            "contract_version": self.contract_version,
            "payload": self.payload,
        }


def _rate_or_zero(count: int, total: int) -> Decimal:
    if total == 0:
        return ZERO_DECIMAL
    return _rate(count, total)


def _rate(count: int, total: int) -> Decimal:
    return (Decimal(count) / Decimal(total)).quantize(Decimal("0.000001"))


def _validate_ratio(field_name: str, value: Decimal) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(f"{field_name}_must_be_decimal")
    if value < ZERO_DECIMAL or value > ONE_DECIMAL:
        raise ValueError(f"{field_name}_must_be_between_zero_and_one")


def _validate_warning_critical(
    label: str,
    *,
    warning: Decimal,
    critical: Decimal,
    lower_is_worse: bool,
) -> None:
    if lower_is_worse:
        if critical > warning:
            raise ValueError(f"critical_{label}_must_be_at_or_below_warning")
        return
    if critical < warning:
        raise ValueError(f"critical_{label}_must_cover_warning")


def _validate_non_negative_int(field_name: str, value: int) -> None:
    if value < 0:
        raise ValueError(f"{field_name}_must_be_non_negative")


def _validate_public_text(field_name: str, value: str) -> None:
    if not value or not value.strip():
        raise ValueError(f"{field_name}_must_be_non_empty")
    ensure_no_raw_secret(value, field_name=field_name)
    ensure_no_production_alice_reference(value, field_name=field_name)


def _json_scalar(value: Decimal | int | str | None) -> object:
    if isinstance(value, Decimal):
        return str(value)
    return value

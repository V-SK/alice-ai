from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

from alice_acp.policy_engine.fraud_classes import (
    EvaluationPhase,
    RiskSignalContext,
    RuntimeIntegrityStatus,
)

SignalType = Literal[
    "verifier_verdict",
    "seeded_mttd",
    "p1_sanitizer",
    "payment_finality",
    "requester_miner_correlation",
    "runtime_integrity",
    "cluster_lifecycle",
    "speculative_route",
    "quality_drift",
]
SignalSeverity = Literal["info", "low", "medium", "high", "critical"]

_SUPPORTED_CLUSTER_STATUS = {
    "none",
    "candidate_cluster_high",
    "confirmed_cluster_no_fraud",
    "confirmed_fraud_cluster",
}


@dataclass(frozen=True, slots=True)
class EvidenceRef:
    evidence_ref: str
    evidence_type: str
    digest: str
    producer: str
    captured_at: datetime
    retention_class: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        required = (self.evidence_ref, self.evidence_type, self.digest, self.producer)
        if any(not value for value in required):
            raise ValueError("evidence reference fields must be non-empty")
        if self.captured_at.tzinfo is None:
            raise ValueError("captured_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class RiskSignal:
    signal_id: str
    signal_type: SignalType
    producer: str
    policy_version: str
    observed_at: datetime
    admission_id: str
    attempt_id: str
    route_contract_id: str
    confidence: Decimal
    severity: SignalSeverity
    reason_code: str
    evidence_refs: tuple[EvidenceRef, ...] = ()
    payload: dict[str, Any] = field(default_factory=dict)
    reservation_id: str | None = None
    liability_id: str | None = None
    passport_id: str | None = None
    wallet_id: str | None = None
    host_id: str | None = None
    cluster_id: str | None = None

    def __post_init__(self) -> None:
        required = (
            self.signal_id,
            self.producer,
            self.policy_version,
            self.admission_id,
            self.attempt_id,
            self.route_contract_id,
            self.reason_code,
        )
        if any(not value for value in required):
            raise ValueError("risk signal string fields must be non-empty")
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        if self.confidence < Decimal("0") or self.confidence > Decimal("1"):
            raise ValueError("confidence must be between 0 and 1")


def risk_signals_to_context(
    *,
    route_source_class: str,
    signals: tuple[RiskSignal, ...],
    evaluation_phase: EvaluationPhase = "settlement",
    mode: str = "",
    model_tier: str = "",
    privacy_class: str = "",
    p1: bool = False,
) -> RiskSignalContext:
    ordered = tuple(sorted(signals, key=lambda signal: (signal.observed_at, signal.signal_id)))
    correlation_score = _latest_decimal_payload(
        ordered,
        "requester_miner_correlation_score",
        fallback_key="correlation_score",
    )
    cluster_status = _latest_string_payload(ordered, "cluster_status") or "none"
    if cluster_status not in _SUPPORTED_CLUSTER_STATUS:
        raise ValueError(f"unsupported cluster_status {cluster_status}")

    return RiskSignalContext(
        route_source_class=route_source_class,
        evaluation_phase=evaluation_phase,
        mode=mode,
        model_tier=model_tier,
        privacy_class=privacy_class,
        p1=p1 or bool(_latest_payload_value(ordered, "p1", default=False)),
        cluster_status=cluster_status,
        requester_miner_correlation_score=correlation_score,
        runtime_integrity_status=_runtime_integrity_status(ordered),
        speculative_route_signal=_any_truthy_payload(ordered, "speculative_route_signal"),
        quality_drift_signal=_any_truthy_payload(ordered, "quality_drift_signal"),
    )


def _latest_payload_value(
    signals: tuple[RiskSignal, ...],
    key: str,
    *,
    default: Any = None,
) -> Any:
    for signal in reversed(signals):
        if key in signal.payload:
            return signal.payload[key]
    return default


def _latest_string_payload(signals: tuple[RiskSignal, ...], key: str) -> str | None:
    value = _latest_payload_value(signals, key)
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError(f"{key} must be a non-empty string")
    return value


def _latest_decimal_payload(
    signals: tuple[RiskSignal, ...],
    key: str,
    *,
    fallback_key: str | None = None,
) -> Decimal | None:
    value = _latest_payload_value(signals, key)
    if value is None and fallback_key is not None:
        value = _latest_payload_value(signals, fallback_key)
    if value is None:
        return None
    decimal_value = value if isinstance(value, Decimal) else Decimal(str(value))
    if decimal_value < Decimal("0") or decimal_value > Decimal("1"):
        raise ValueError(f"{key} must be between 0 and 1")
    return decimal_value


def _any_truthy_payload(signals: tuple[RiskSignal, ...], key: str) -> bool:
    return any(bool(signal.payload.get(key)) for signal in signals)


def _runtime_integrity_status(signals: tuple[RiskSignal, ...]) -> RuntimeIntegrityStatus:
    statuses = [
        signal.payload["runtime_integrity_status"]
        for signal in signals
        if "runtime_integrity_status" in signal.payload
    ]
    for status in statuses:
        if status not in {"unknown", "pass", "fail"}:
            raise ValueError(f"unsupported runtime_integrity_status {status}")
    if "fail" in statuses:
        return "fail"
    if "pass" in statuses:
        return "pass"
    return "unknown"

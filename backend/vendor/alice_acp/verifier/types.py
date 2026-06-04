from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Literal

from alice_acp.policy_engine import EvidenceRef, RiskSignal
from alice_acp.settlement.types import VerifierVerdict

MTTDSampleStatus = Literal["sufficient_samples", "insufficient_samples"]
VerifierIngestionStatus = Literal["terminal_zero_ready", "under_review", "verification_ready"]


@dataclass(frozen=True, slots=True)
class VerifierSignal:
    verifier_signal_id: str
    reservation_id: str
    admission_id: str
    attempt_id: str
    route_contract_id: str
    verifier_verdict: VerifierVerdict
    verified_acu: Decimal
    verification_completed_at: datetime
    verifier_policy_version: str
    evidence_refs: tuple[EvidenceRef, ...]
    risk_signals: tuple[RiskSignal, ...] = ()

    def __post_init__(self) -> None:
        required = (
            self.verifier_signal_id,
            self.reservation_id,
            self.admission_id,
            self.attempt_id,
            self.route_contract_id,
            self.verifier_policy_version,
        )
        if any(not value for value in required):
            raise ValueError("verifier signal string fields must be non-empty")
        if self.verified_acu < 0:
            raise ValueError("verified_acu must be non-negative")
        if self.verification_completed_at.tzinfo is None:
            raise ValueError("verification_completed_at must be timezone-aware")
        if not self.evidence_refs:
            raise ValueError("verifier signal requires at least one evidence ref")

    @property
    def payable_input(self) -> bool:
        return self.verifier_verdict == "pass"


@dataclass(frozen=True, slots=True)
class VerifierQueueSample:
    reservation_id: str
    enqueued_at: datetime
    observed_at: datetime
    fraud_class: str
    verdict: Literal["pass", "fail", "disputed", "delayed", "pending"] = "pending"
    completed_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.reservation_id or not self.fraud_class:
            raise ValueError("queue sample ids must be non-empty")
        if self.enqueued_at.tzinfo is None or self.observed_at.tzinfo is None:
            raise ValueError("queue sample timestamps must be timezone-aware")
        if self.completed_at is not None and self.completed_at.tzinfo is None:
            raise ValueError("completed_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class VerifierBacklogMetrics:
    queue_depth: int
    oldest_pending_age_seconds: int
    p50_mttd_seconds: int
    p95_mttd_seconds: int
    verdict_counts_by_fraud_class: dict[str, dict[str, int]] = field(default_factory=dict)
    delayed_or_disputed_count: int = 0
    completed_sample_count: int = 0
    min_completed_samples: int = 2
    p95_sample_status: MTTDSampleStatus = "insufficient_samples"


@dataclass(frozen=True, slots=True)
class VerifierIngestionResult:
    status: VerifierIngestionStatus
    verifier_signal_id: str
    verification_result: object | None = None
    risk_signals: tuple[RiskSignal, ...] = ()
    reason_code: str | None = None

    @property
    def payable_input(self) -> bool:
        return self.status == "verification_ready"

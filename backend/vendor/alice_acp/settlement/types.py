from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Literal

ACU_QUANT = Decimal("0.000000000001")

VerifierVerdict = Literal["pass", "fail", "disputed", "delayed"]
SettlementConsumeStatus = Literal[
    "liability_created",
    "idempotent_replay",
    "rejected",
    "under_review",
]
SettlementTrancheSplitStatus = Literal["split_applied", "rejected", "under_review"]
CorrectionType = Literal[
    "over_credit_before_paid",
    "over_credit_after_paid",
    "under_credit_before_paid",
    "under_credit_after_paid",
]
SettlementCorrectionStatus = Literal["correction_applied", "idempotent_replay", "rejected"]

BAD_RESERVATION_STATE = "BAD_RESERVATION_STATE"
BAD_LIABILITY_STATE = "BAD_LIABILITY_STATE"
CORRECTION_AMOUNT_EXCEEDS_UNPAID_CREDIT = "CORRECTION_AMOUNT_EXCEEDS_UNPAID_CREDIT"
LIABILITY_ID_COLLISION = "LIABILITY_ID_COLLISION"
RELEASE_FAILED = "RELEASE_FAILED"
RISK_TRANCHE_SPLIT_APPLIED = "RISK_TRANCHE_SPLIT_APPLIED"
TRANCHE_POLICY_VERSION_MISMATCH = "TRANCHE_POLICY_VERSION_MISMATCH"
VERIFICATION_PAYLOAD_MISMATCH = "VERIFICATION_PAYLOAD_MISMATCH"
VERIFIED_ACU_EXCEEDS_RESERVED = "VERIFIED_ACU_EXCEEDS_RESERVED"
VERIFIER_VERDICT_FAILED_WITH_VERIFIED_ACU = "VERIFIER_VERDICT_FAILED_WITH_VERIFIED_ACU"
VERIFIER_VERDICT_NOT_FINAL = "VERIFIER_VERDICT_NOT_FINAL"

LIABILITY_CREATED_PENDING_TRANCHE = "liability_created_pending_tranche"
PENDING_A5_RISK_TRANCHE_SPLIT = "PENDING_A5_RISK_TRANCHE_SPLIT"
RISK_RESERVE_ACTIVE = "risk_reserve_active"
TERMINAL_ZERO = "terminal_zero"
TERMINAL_ZERO_VERIFIER_FAILED = "TERMINAL_ZERO_VERIFIER_FAILED"
UNDER_REVIEW = "under_review"

CORRECTION_INCIDENT_LOGGED = "incident_logged"
CORRECTION_MAKE_UP_PENDING = "make_up_pending"
CORRECTION_REJECTED = "rejected"
CORRECTION_UNDER_REVIEW_MOVED = "under_review_moved"

OVER_CREDIT_BEFORE_PAID = "over_credit_before_paid"
OVER_CREDIT_AFTER_PAID = "over_credit_after_paid"
UNDER_CREDIT_BEFORE_PAID = "under_credit_before_paid"
UNDER_CREDIT_AFTER_PAID = "under_credit_after_paid"


@dataclass(frozen=True, slots=True)
class VerificationResult:
    reservation_id: str
    admission_id: str
    attempt_id: str
    verifier_verdict: VerifierVerdict
    verified_acu: Decimal
    verification_completed_at: datetime
    applicable_signal_snapshot: dict[str, Any] = field(default_factory=dict)
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.reservation_id or not self.admission_id or not self.attempt_id:
            raise ValueError("verification result ids must be non-empty")
        if self.verified_acu < 0:
            raise ValueError("verified_acu must be non-negative")
        if self.verification_completed_at.tzinfo is None:
            raise ValueError("verification_completed_at must be timezone-aware")
        if any(not ref for ref in self.evidence_refs):
            raise ValueError("evidence refs must be non-empty strings")


@dataclass(frozen=True, slots=True)
class SettlementConsumeResult:
    status: SettlementConsumeStatus
    liability_id: str | None = None
    reason_code: str | None = None
    idempotent_replay: bool = False

    @property
    def accepted(self) -> bool:
        return self.status in {"liability_created", "idempotent_replay"}


@dataclass(frozen=True, slots=True)
class SettlementTrancheSplitResult:
    status: SettlementTrancheSplitStatus
    liability_id: str | None = None
    reason_code: str | None = None

    @property
    def accepted(self) -> bool:
        return self.status == "split_applied"


@dataclass(frozen=True, slots=True)
class SettlementCorrectionRequest:
    liability_id: str
    correction_type: CorrectionType
    amount_acu: Decimal
    reason_code: str
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.liability_id:
            raise ValueError("liability_id must be non-empty")
        if self.amount_acu <= 0:
            raise ValueError("amount_acu must be positive")
        if self.amount_acu != self.amount_acu.quantize(ACU_QUANT):
            raise ValueError("amount_acu must not exceed ACU scale")
        if not self.reason_code:
            raise ValueError("reason_code must be non-empty")
        if any(not ref for ref in self.evidence_refs):
            raise ValueError("evidence refs must be non-empty strings")


@dataclass(frozen=True, slots=True)
class SettlementCorrectionResult:
    status: SettlementCorrectionStatus
    correction_id: str | None = None
    idempotency_key: str | None = None
    liability_id: str | None = None
    correction_state: str | None = None
    reason_code: str | None = None
    idempotent_replay: bool = False

    @property
    def accepted(self) -> bool:
        return self.status in {"correction_applied", "idempotent_replay"}


class SettlementReleasePreflightError(Exception):
    """Raised by tests or preflight checks before any release writes occur."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from alice_acp.policy_engine import EvidenceRef

PaymentFinalityLevel = Literal[
    "level_1_authorized",
    "level_2_captured",
    "level_3_settled",
    "level_4_chargeback_window_passed",
]
CreditType = Literal["paid", "promotional", "internal_test"]


@dataclass(frozen=True, slots=True)
class CreditProvenance:
    provenance_id: str
    admission_id: str
    attempt_id: str
    route_contract_id: str
    finality_level: PaymentFinalityLevel
    credit_type: CreditType
    policy_version: str
    observed_at: datetime
    evidence_refs: tuple[EvidenceRef, ...] = ()
    policy_approved_promotional_r3: bool = False
    chargeback_observed: bool = False

    def __post_init__(self) -> None:
        required = (
            self.provenance_id,
            self.admission_id,
            self.attempt_id,
            self.route_contract_id,
            self.policy_version,
        )
        if any(not value for value in required):
            raise ValueError("credit provenance fields must be non-empty")
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class R3EligibilityResult:
    eligible: bool
    finality_level: PaymentFinalityLevel
    reason_code: str
    cap_treatment: str
    revokes_future_r3: bool = False

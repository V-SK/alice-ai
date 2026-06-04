from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

ClusterStatus = Literal[
    "none",
    "candidate_cluster_high",
    "confirmed_cluster_no_fraud",
    "confirmed_fraud_cluster",
]
PassportVelocityWindow = Literal["hour", "day", "epoch"]
ReservationResultStatus = Literal["active_reserved", "rejected"]

DIMENSION_CAP_EXCEEDED = "DIMENSION_CAP_EXCEEDED"
IDEMPOTENCY_PAYLOAD_MISMATCH = "IDEMPOTENCY_PAYLOAD_MISMATCH"
INVALID_CLUSTER_DIMENSION = "INVALID_CLUSTER_DIMENSION"
MISSING_DIMENSION_ACCOUNT = "MISSING_DIMENSION_ACCOUNT"
SERIALIZATION_RETRY_EXHAUSTED = "SERIALIZATION_RETRY_EXHAUSTED"


@dataclass(frozen=True, slots=True)
class ReservationRequest:
    admission_id: str
    attempt_id: str
    route_contract_id: str
    epoch_id: str
    source_budget_id: str
    mode_budget_id: str
    passport_id: str
    wallet_id: str
    host_id: str
    accelerator_id: str
    max_rewardable_acu: Decimal
    formula_version: str
    tranche_policy_version: str
    reservation_expires_at: datetime
    cluster_status: ClusterStatus = "none"
    cluster_id: str | None = None
    passport_velocity_window: PassportVelocityWindow = "epoch"
    reward_bearing: bool = True

    def __post_init__(self) -> None:
        required_strings = (
            self.admission_id,
            self.attempt_id,
            self.route_contract_id,
            self.epoch_id,
            self.source_budget_id,
            self.mode_budget_id,
            self.passport_id,
            self.wallet_id,
            self.host_id,
            self.accelerator_id,
            self.formula_version,
            self.tranche_policy_version,
        )
        if any(not value for value in required_strings):
            raise ValueError("reservation request string fields must be non-empty")
        if self.max_rewardable_acu <= 0:
            raise ValueError("max_rewardable_acu must be positive")
        if self.reservation_expires_at.tzinfo is None:
            raise ValueError("reservation_expires_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class ReservationDimension:
    dimension_type: str
    dimension_key: str


@dataclass(frozen=True, slots=True)
class ReservationResult:
    status: ReservationResultStatus
    reservation_id: str | None = None
    reason_code: str | None = None
    idempotent_replay: bool = False
    failed_dimension: ReservationDimension | None = None
    retryable: bool = False
    retry_attempts: int = 0

    @property
    def accepted(self) -> bool:
        return self.status == "active_reserved"


class ABRSReject(Exception):
    def __init__(
        self,
        reason_code: str,
        *,
        failed_dimension: ReservationDimension | None = None,
    ) -> None:
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.failed_dimension = failed_dimension

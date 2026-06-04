from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from alice_acp.abrs.types import ClusterStatus, PassportVelocityWindow

ROUTE_REASSIGNMENT_REQUIRES_NEW_ATTEMPT = "ROUTE_REASSIGNMENT_REQUIRES_NEW_ATTEMPT"

WACContractStatus = Literal["accepted", "rejected"]


@dataclass(frozen=True, slots=True)
class WACAdmissionAttempt:
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
            raise ValueError("WAC admission attempt string fields must be non-empty")
        if self.max_rewardable_acu <= 0:
            raise ValueError("max_rewardable_acu must be positive")
        if self.reservation_expires_at.tzinfo is None:
            raise ValueError("reservation_expires_at must be timezone-aware")


@dataclass(frozen=True, slots=True)
class WACContractResult:
    status: WACContractStatus
    admission_id: str
    attempt_id: str
    route_contract_id: str
    reason_code: str | None = None

    @property
    def accepted(self) -> bool:
        return self.status == "accepted"

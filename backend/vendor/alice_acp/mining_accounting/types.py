from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Literal

from alice_acp.mining_pool.types import AcceptedShareProof
from alice_acp.mining_session.types import RVN_KAWPOW, MiningAlgorithm

ZERO_ACU = Decimal("0")
ACU_QUANT = Decimal("0.000000000001")
RVN_KAWPOW_ALGORITHM_COEFFICIENT = Decimal("0.000001")
DEFAULT_MODE_COEFFICIENT = Decimal("1")
DEFAULT_FALLBACK_DISCOUNT = Decimal("1")
RVN_KAWPOW_ACU_FORMULA_VERSION = "mining-rvn-kawpow-share-difficulty-v1"

PoolValidity = Literal[
    "pool_accepted",
    "pool_rejected",
    "invalid_session",
    "invalid_pool",
]
AcuEstimateStatus = Literal["rewardable", "nonrewardable", "rejected"]
ShadowEntryStatus = Literal["shadow_counted", "shadow_not_counted", "shadow_rejected"]
ABRSBindingStatus = Literal[
    "supported_without_migration",
    "shadow_only_no_rewardable_acu",
]


@dataclass(frozen=True, slots=True)
class MiningAcuFormula:
    formula_version: str = RVN_KAWPOW_ACU_FORMULA_VERSION
    algorithm: MiningAlgorithm = RVN_KAWPOW
    algorithm_coefficient: Decimal = RVN_KAWPOW_ALGORITHM_COEFFICIENT
    mode_coefficient: Decimal = DEFAULT_MODE_COEFFICIENT
    fallback_discount: Decimal = DEFAULT_FALLBACK_DISCOUNT

    def __post_init__(self) -> None:
        for field_name, value in (
            ("algorithm_coefficient", self.algorithm_coefficient),
            ("mode_coefficient", self.mode_coefficient),
            ("fallback_discount", self.fallback_discount),
        ):
            if not isinstance(value, Decimal):
                raise TypeError(f"{field_name} must be Decimal")
            if value <= ZERO_ACU:
                raise ValueError(f"{field_name} must be positive")
        if self.fallback_discount > DEFAULT_FALLBACK_DISCOUNT:
            raise ValueError("fallback_discount must not exceed 1")
        if not self.formula_version:
            raise ValueError("formula_version must be non-empty")


@dataclass(frozen=True, slots=True)
class MiningAcuInput:
    proof: AcceptedShareProof | None
    pool_validity: PoolValidity
    formula: MiningAcuFormula = field(default_factory=MiningAcuFormula)


@dataclass(frozen=True, slots=True)
class MiningAcuEstimate:
    status: AcuEstimateStatus
    rewardable: bool
    reason_code: str
    mining_acu: Decimal
    formula_version: str
    proof_identity: tuple[str, str, str, str, str] | None = None
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MiningShadowLedgerEntry:
    status: ShadowEntryStatus
    reason_code: str
    session_id: str
    worker_id: str
    mining_acu: Decimal
    formula_version: str
    proof_identity: tuple[str, str, str, str, str] | None = None
    evidence_refs: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class MiningShadowLedgerReport:
    run_id: str
    session_id: str
    entries: tuple[MiningShadowLedgerEntry, ...]
    total_mining_acu: Decimal
    paid_acu: Decimal = ZERO_ACU
    reservation_id: str | None = None
    liability_id: str | None = None
    abrs_binding_status: ABRSBindingStatus = "shadow_only_no_rewardable_acu"
    service_functions: tuple[str, ...] = ()

    @property
    def accepted_share_count(self) -> int:
        return sum(1 for entry in self.entries if entry.status == "shadow_counted")

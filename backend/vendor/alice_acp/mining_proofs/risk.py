from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from alice_acp.mining_proofs.collector import (
    PROOF_COLLECTION_ADDRESS_MISMATCH,
    PROOF_DIFFICULTY_ANOMALY,
    PROOF_DUPLICATE,
    PROOF_FUTURE_TIMESTAMP,
    PROOF_STALE_TIMESTAMP,
)
from alice_acp.mining_proofs.cross_check import (
    CROSS_CHECK_ACCEPTED_ABSENT,
    PoolCrossCheckResult,
)
from alice_acp.mining_proofs.types import ProofCollectionResult

RiskAction = Literal["allow", "cap", "under_review", "reject"]

ZERO = Decimal("0")
ONE = Decimal("1")


@dataclass(frozen=True, slots=True)
class MiningRiskSignal:
    duplicate_rate: Decimal
    stale_rate: Decimal
    rejected_rate: Decimal
    payout_tamper_seen: bool
    difficulty_anomaly_seen: bool
    pool_cross_check_missing_rate: Decimal
    recommended_action: RiskAction

    def __post_init__(self) -> None:
        for field_name, value in (
            ("duplicate_rate", self.duplicate_rate),
            ("stale_rate", self.stale_rate),
            ("rejected_rate", self.rejected_rate),
            ("pool_cross_check_missing_rate", self.pool_cross_check_missing_rate),
        ):
            if not isinstance(value, Decimal):
                raise TypeError(f"{field_name} must be Decimal")
            if value < ZERO or value > ONE:
                raise ValueError(f"{field_name} must be between 0 and 1")


def summarize_mining_risk(
    collection_results: tuple[ProofCollectionResult, ...],
    *,
    cross_check_results: tuple[PoolCrossCheckResult, ...] = (),
) -> MiningRiskSignal:
    total = Decimal(len(collection_results)) if collection_results else Decimal("1")
    duplicate_rate = _rate(
        sum(1 for result in collection_results if result.reason_code == PROOF_DUPLICATE),
        total,
    )
    stale_rate = _rate(
        sum(
            1
            for result in collection_results
            if result.reason_code in {PROOF_STALE_TIMESTAMP, PROOF_FUTURE_TIMESTAMP}
        ),
        total,
    )
    rejected_rate = _rate(
        sum(1 for result in collection_results if result.status in {"rejected", "under_review"}),
        total,
    )
    payout_tamper_seen = any(
        result.reason_code == PROOF_COLLECTION_ADDRESS_MISMATCH
        for result in collection_results
    )
    difficulty_anomaly_seen = any(
        result.reason_code == PROOF_DIFFICULTY_ANOMALY for result in collection_results
    )

    cross_total = Decimal(len(cross_check_results)) if cross_check_results else Decimal("1")
    missing_rate = _rate(
        sum(
            1
            for result in cross_check_results
            if result.reason_code == CROSS_CHECK_ACCEPTED_ABSENT
        ),
        cross_total,
    )

    return MiningRiskSignal(
        duplicate_rate=duplicate_rate,
        stale_rate=stale_rate,
        rejected_rate=rejected_rate,
        payout_tamper_seen=payout_tamper_seen,
        difficulty_anomaly_seen=difficulty_anomaly_seen,
        pool_cross_check_missing_rate=missing_rate,
        recommended_action=_recommended_action(
            duplicate_rate=duplicate_rate,
            stale_rate=stale_rate,
            rejected_rate=rejected_rate,
            payout_tamper_seen=payout_tamper_seen,
            difficulty_anomaly_seen=difficulty_anomaly_seen,
            pool_cross_check_missing_rate=missing_rate,
        ),
    )


def _recommended_action(
    *,
    duplicate_rate: Decimal,
    stale_rate: Decimal,
    rejected_rate: Decimal,
    payout_tamper_seen: bool,
    difficulty_anomaly_seen: bool,
    pool_cross_check_missing_rate: Decimal,
) -> RiskAction:
    if payout_tamper_seen:
        return "reject"
    if difficulty_anomaly_seen or pool_cross_check_missing_rate > ZERO:
        return "under_review"
    if duplicate_rate > ZERO or stale_rate > ZERO or rejected_rate >= Decimal("0.5"):
        return "cap"
    return "allow"


def _rate(count: int, total: Decimal) -> Decimal:
    return (Decimal(count) / total).quantize(Decimal("0.000001"))

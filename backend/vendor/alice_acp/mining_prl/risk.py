from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from alice_acp.mining_prl.collector import (
    PRL_AUTHORITY_ACCEPTED_ABSENT,
    PRL_PROOF_COLLECTION_ADDRESS_MISMATCH,
    PRL_PROOF_DUPLICATE,
    PRL_PROOF_FUTURE_TIMESTAMP,
    PRL_PROOF_STALE_TIMESTAMP,
)
from alice_acp.mining_prl.types import PRLProofCollectionResult, PRLRiskAction

ZERO = Decimal("0")
ONE = Decimal("1")


@dataclass(frozen=True, slots=True)
class PRLRiskSignal:
    duplicate_rate: Decimal
    stale_rate: Decimal
    rejected_rate: Decimal
    pool_evidence_missing_rate: Decimal
    collection_address_tamper_seen: bool
    recommended_action: PRLRiskAction

    def __post_init__(self) -> None:
        for field_name, value in (
            ("duplicate_rate", self.duplicate_rate),
            ("stale_rate", self.stale_rate),
            ("rejected_rate", self.rejected_rate),
            ("pool_evidence_missing_rate", self.pool_evidence_missing_rate),
        ):
            if not isinstance(value, Decimal):
                raise TypeError(f"{field_name} must be Decimal")
            if value < ZERO or value > ONE:
                raise ValueError(f"{field_name} must be between 0 and 1")


def summarize_prl_risk(
    collection_results: tuple[PRLProofCollectionResult, ...],
) -> PRLRiskSignal:
    total = Decimal(len(collection_results)) if collection_results else ONE
    duplicate_rate = _rate(
        sum(1 for result in collection_results if result.reason_code == PRL_PROOF_DUPLICATE),
        total,
    )
    stale_rate = _rate(
        sum(
            1
            for result in collection_results
            if result.reason_code in {PRL_PROOF_STALE_TIMESTAMP, PRL_PROOF_FUTURE_TIMESTAMP}
        ),
        total,
    )
    rejected_rate = _rate(
        sum(1 for result in collection_results if result.status in {"rejected", "under_review"}),
        total,
    )
    missing_rate = _rate(
        sum(
            1
            for result in collection_results
            if result.reason_code == PRL_AUTHORITY_ACCEPTED_ABSENT
        ),
        total,
    )
    address_tamper_seen = any(
        result.reason_code == PRL_PROOF_COLLECTION_ADDRESS_MISMATCH
        for result in collection_results
    )
    return PRLRiskSignal(
        duplicate_rate=duplicate_rate,
        stale_rate=stale_rate,
        rejected_rate=rejected_rate,
        pool_evidence_missing_rate=missing_rate,
        collection_address_tamper_seen=address_tamper_seen,
        recommended_action=_recommended_action(
            duplicate_rate=duplicate_rate,
            stale_rate=stale_rate,
            rejected_rate=rejected_rate,
            pool_evidence_missing_rate=missing_rate,
            address_tamper_seen=address_tamper_seen,
        ),
    )


def _recommended_action(
    *,
    duplicate_rate: Decimal,
    stale_rate: Decimal,
    rejected_rate: Decimal,
    pool_evidence_missing_rate: Decimal,
    address_tamper_seen: bool,
) -> PRLRiskAction:
    if address_tamper_seen:
        return "reject"
    if pool_evidence_missing_rate > ZERO or stale_rate > ZERO:
        return "under_review"
    if duplicate_rate > ZERO or rejected_rate >= Decimal("0.5"):
        return "cap"
    return "allow"


def _rate(count: int, total: Decimal) -> Decimal:
    return (Decimal(count) / total).quantize(Decimal("0.000001"))

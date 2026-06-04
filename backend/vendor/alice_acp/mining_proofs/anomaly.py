from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal

from alice_acp.mining_proofs.collector import (
    PROOF_COLLECTION_ADDRESS_MISMATCH,
    PROOF_DIFFICULTY_ANOMALY,
    PROOF_DIFFICULTY_BELOW_MINIMUM,
    PROOF_DUPLICATE,
    PROOF_FUTURE_TIMESTAMP,
    PROOF_STALE_TIMESTAMP,
    PROOF_TARGET_DIFFICULTY_MISMATCH,
)
from alice_acp.mining_proofs.pool_evidence import (
    POOL_AUTHORITY_ACCEPTED_ABSENT,
    POOL_AUTHORITY_DUPLICATE_SHARE,
    POOL_AUTHORITY_INVALID_SHARE,
    POOL_AUTHORITY_REJECTED_SHARE,
    POOL_AUTHORITY_STALE_SHARE,
    PoolEvidenceAuthorityResult,
)
from alice_acp.mining_proofs.risk import RiskAction
from alice_acp.mining_proofs.types import MiningShareProof, ProofCollectionResult

AUTHORITY_CLIENT_LOG_ONLY = "AUTHORITY_CLIENT_LOG_ONLY"

_POOL_REJECT_REASONS = frozenset(
    {
        POOL_AUTHORITY_DUPLICATE_SHARE,
        POOL_AUTHORITY_INVALID_SHARE,
        POOL_AUTHORITY_REJECTED_SHARE,
        POOL_AUTHORITY_STALE_SHARE,
    }
)
_CAP_REASONS = frozenset(
    {
        PROOF_DIFFICULTY_BELOW_MINIMUM,
        PROOF_DUPLICATE,
        PROOF_FUTURE_TIMESTAMP,
        PROOF_STALE_TIMESTAMP,
    }
)
_UNDER_REVIEW_REASONS = frozenset(
    {
        AUTHORITY_CLIENT_LOG_ONLY,
        POOL_AUTHORITY_ACCEPTED_ABSENT,
        PROOF_DIFFICULTY_ANOMALY,
        PROOF_TARGET_DIFFICULTY_MISMATCH,
    }
)


@dataclass(frozen=True, slots=True)
class AuthorityRiskActionSummary:
    recommended_action: RiskAction
    reason_codes: tuple[str, ...]
    allow_count: int
    cap_count: int
    under_review_count: int
    reject_count: int


def evaluate_difficulty_anomaly(
    proof: MiningShareProof,
    *,
    previous_share_difficulty: Decimal | None,
    difficulty_jump_factor: Decimal,
) -> ProofCollectionResult | None:
    if difficulty_jump_factor <= Decimal("0"):
        raise ValueError("difficulty_jump_factor must be positive")
    if previous_share_difficulty is None:
        return None
    if previous_share_difficulty <= Decimal("0"):
        raise ValueError("previous_share_difficulty must be positive")
    if proof.share_difficulty > previous_share_difficulty * difficulty_jump_factor:
        return ProofCollectionResult(
            status="under_review",
            counted=False,
            reason_code=PROOF_DIFFICULTY_ANOMALY,
        )
    return None


def summarize_authority_risk_actions(
    *,
    collection_results: tuple[ProofCollectionResult, ...] = (),
    pool_results: tuple[PoolEvidenceAuthorityResult, ...] = (),
    client_log_only_count: int = 0,
) -> AuthorityRiskActionSummary:
    if client_log_only_count < 0:
        raise ValueError("client_log_only_count must be non-negative")

    reason_codes = tuple(
        result.reason_code
        for result in collection_results
        if result.reason_code
    ) + tuple(result.reason_code for result in pool_results if result.reason_code)
    if client_log_only_count:
        reason_codes += (AUTHORITY_CLIENT_LOG_ONLY,)

    reject_count = sum(1 for code in reason_codes if _rejects(code))
    under_review_count = sum(1 for code in reason_codes if code in _UNDER_REVIEW_REASONS)
    cap_count = sum(1 for code in reason_codes if code in _CAP_REASONS)
    allow_count = max(
        0,
        len(reason_codes) - reject_count - under_review_count - cap_count,
    )

    if reject_count:
        recommended_action: RiskAction = "reject"
    elif under_review_count:
        recommended_action = "under_review"
    elif cap_count:
        recommended_action = "cap"
    else:
        recommended_action = "allow"

    return AuthorityRiskActionSummary(
        recommended_action=recommended_action,
        reason_codes=reason_codes,
        allow_count=allow_count,
        cap_count=cap_count,
        under_review_count=under_review_count,
        reject_count=reject_count,
    )


def _rejects(reason_code: str) -> bool:
    return reason_code == PROOF_COLLECTION_ADDRESS_MISMATCH or reason_code in _POOL_REJECT_REASONS

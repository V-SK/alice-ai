from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from alice_acp.mining_prl.collector import PRLProofAuthorityDecision
from alice_acp.mining_prl.pool_export import PRLPoolExportValidationResult
from alice_acp.mining_prl.types import PRLProofSession, PRLShareProof
from alice_acp.reward_backend.types import (
    ANTI_CHEAT_PASSED,
    ANTI_CHEAT_REJECTED,
    ANTI_CHEAT_UNDER_REVIEW,
    AUTHORITY_REJECTED,
    AUTHORITY_REWARDABLE_CANDIDATE,
    AUTHORITY_UNDER_REVIEW,
    LANE_PRL_GPU,
    ZERO_DECIMAL,
    RewardAccountingSourceRecord,
    validate_public_identifier,
)


def build_prl_reward_accounting_source(
    *,
    window_id: str,
    session: PRLProofSession,
    proof: PRLShareProof,
    authority: PRLProofAuthorityDecision,
    export_custody: PRLPoolExportValidationResult,
    observed_at: datetime,
    source_id: str | None = None,
    proof_id: str | None = None,
) -> RewardAccountingSourceRecord:
    """Bridge PRL proof authority into reward staging without live payout.

    PRL is only rewardable when both local proof authority and production export
    custody are ready. Client logs or accepted proofs without export custody
    become rejected/under-review zero-score sources.
    """

    validate_public_identifier("window_id", window_id)
    canonical_hash = authority.canonical_hash
    resolved_source_id = source_id or f"prl-authority:{canonical_hash}"
    resolved_proof_id = proof_id or f"prl-proof:{canonical_hash}"

    if authority.rewardable_candidate and export_custody.production_readiness_ready:
        authority_status = AUTHORITY_REWARDABLE_CANDIDATE
        anti_cheat_status = ANTI_CHEAT_PASSED
        rewardable_score = proof.work_score
        reason_code = authority.reason_code
    elif authority.status == "rejected" or export_custody.status == "rejected":
        authority_status = AUTHORITY_REJECTED
        anti_cheat_status = ANTI_CHEAT_REJECTED
        rewardable_score = ZERO_DECIMAL
        reason_code = _first_reason(authority, export_custody)
    else:
        authority_status = AUTHORITY_UNDER_REVIEW
        anti_cheat_status = ANTI_CHEAT_UNDER_REVIEW
        rewardable_score = ZERO_DECIMAL
        reason_code = _first_reason(authority, export_custody)

    return RewardAccountingSourceRecord(
        source_id=resolved_source_id,
        window_id=window_id,
        session_id=session.session_id,
        proof_id=resolved_proof_id,
        passport_id=session.passport_id,
        device_id=session.device_id,
        lane=LANE_PRL_GPU,
        observed_at=observed_at,
        authority_status=authority_status,
        anti_cheat_status=anti_cheat_status,
        rewardable_score=_decimal_score(rewardable_score),
        reason_code=reason_code,
        canonical_proof_hash=canonical_hash,
    )


def _first_reason(
    authority: PRLProofAuthorityDecision,
    export_custody: PRLPoolExportValidationResult,
) -> str:
    if export_custody.reason_codes:
        return export_custody.reason_codes[0]
    return authority.reason_code


def _decimal_score(value: Decimal) -> Decimal:
    if not isinstance(value, Decimal):
        raise TypeError("PRL rewardable score must be Decimal")
    return value

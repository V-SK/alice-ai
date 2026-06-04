from __future__ import annotations

from alice_acp.mining_pool.types import AcceptedShareProof, PoolAdapterResult, PoolShareEvent
from alice_acp.mining_session import (
    SESSION_COLLECTION_ADDRESS_MISMATCH,
    validate_signed_mining_session,
)
from alice_acp.mining_session.types import SignedMiningSession

SHARE_ACCEPTED = "SHARE_ACCEPTED"
SHARE_REJECTED_NOT_REWARDABLE = "SHARE_REJECTED_NOT_REWARDABLE"
SHARE_SESSION_MISMATCH = "SHARE_SESSION_MISMATCH"
SHARE_WORKER_MISMATCH = "SHARE_WORKER_MISMATCH"
SHARE_POOL_MISMATCH = "SHARE_POOL_MISMATCH"
SHARE_ROUTE_MISMATCH = "SHARE_ROUTE_MISMATCH"


def build_share_proof(
    session: SignedMiningSession,
    event: PoolShareEvent,
) -> PoolAdapterResult:
    if event.session_id != session.session_id:
        return _rejected(SHARE_SESSION_MISMATCH)
    if event.worker_id != session.worker_id:
        return _rejected(SHARE_WORKER_MISMATCH)
    if event.pool_id != session.pool_id:
        return _rejected(SHARE_POOL_MISMATCH)
    if event.algorithm != session.algorithm:
        return _rejected(SHARE_ROUTE_MISMATCH)

    validation = validate_signed_mining_session(
        session,
        observed_at=event.submitted_at,
        expected_collection_address=event.observed_collection_address,
        expected_worker_id=event.worker_id,
        expected_pool_id=event.pool_id,
    )
    if validation.reason_code == SESSION_COLLECTION_ADDRESS_MISMATCH:
        return PoolAdapterResult(
            status="not_rewardable",
            rewardable=False,
            reason_code=SESSION_COLLECTION_ADDRESS_MISMATCH,
        )
    if not validation.accepted:
        return PoolAdapterResult(
            status="rejected",
            rewardable=False,
            reason_code=validation.reason_code or SHARE_SESSION_MISMATCH,
        )
    if event.pool_result != "accepted":
        return PoolAdapterResult(
            status="not_rewardable",
            rewardable=False,
            reason_code=SHARE_REJECTED_NOT_REWARDABLE,
        )

    proof = AcceptedShareProof(
        pool_id=event.pool_id,
        session_id=event.session_id,
        worker_id=event.worker_id,
        share_id=event.share_id,
        nonce=event.nonce,
        submitted_at=event.submitted_at,
        share_difficulty=event.share_difficulty,
        raw_ref_hash=event.raw_ref_hash,
        evidence_ref=event.evidence_ref,
        collection_address=event.observed_collection_address,
        algorithm=event.algorithm,
    )
    return PoolAdapterResult(
        status="proof_created",
        rewardable=True,
        reason_code=SHARE_ACCEPTED,
        proof=proof,
    )


def _rejected(reason_code: str) -> PoolAdapterResult:
    return PoolAdapterResult(status="rejected", rewardable=False, reason_code=reason_code)

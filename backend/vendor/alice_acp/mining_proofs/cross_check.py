from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from alice_acp.evidence.types import validate_aware_timestamp, validate_sha256
from alice_acp.mining_proofs.canonical import canonical_share_hash
from alice_acp.mining_proofs.types import MiningShareProof

EvidenceSourceType = Literal["fixture", "manual", "export"]
PoolCrossCheckStatus = Literal["matched", "under_review", "rejected"]

CROSS_CHECK_MATCHED = "CROSS_CHECK_MATCHED"
CROSS_CHECK_POOL_MISMATCH = "CROSS_CHECK_POOL_MISMATCH"
CROSS_CHECK_WORKER_MISMATCH = "CROSS_CHECK_WORKER_MISMATCH"
CROSS_CHECK_SESSION_MISMATCH = "CROSS_CHECK_SESSION_MISMATCH"
CROSS_CHECK_ACCEPTED_ABSENT = "CROSS_CHECK_ACCEPTED_ABSENT"
CROSS_CHECK_POOL_REJECTED = "CROSS_CHECK_POOL_REJECTED"
CROSS_CHECK_PROOF_NOT_ACCEPTED = "CROSS_CHECK_PROOF_NOT_ACCEPTED"


@dataclass(frozen=True, slots=True)
class PoolEvidenceSnapshot:
    pool_id: str
    worker_name: str
    session_id: str
    accepted_share_count: int
    rejected_share_count: int
    accepted_share_hashes: tuple[str, ...]
    evidence_generated_at: datetime
    evidence_source_type: EvidenceSourceType
    rejected_share_hashes: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        required = (
            self.pool_id,
            self.worker_name,
            self.session_id,
            self.evidence_source_type,
        )
        if any(not value for value in required):
            raise ValueError("pool evidence snapshot fields must be non-empty")
        if self.accepted_share_count < 0 or self.rejected_share_count < 0:
            raise ValueError("share counts must be non-negative")
        if self.accepted_share_count != len(set(self.accepted_share_hashes)):
            raise ValueError("accepted_share_count must match unique accepted hashes")
        if self.rejected_share_count != len(set(self.rejected_share_hashes)):
            raise ValueError("rejected_share_count must match unique rejected hashes")
        validate_aware_timestamp("evidence_generated_at", self.evidence_generated_at)
        for share_hash in (*self.accepted_share_hashes, *self.rejected_share_hashes):
            validate_sha256(share_hash, field_name="share_hash")


@dataclass(frozen=True, slots=True)
class PoolCrossCheckResult:
    status: PoolCrossCheckStatus
    rewardable: bool
    reason_code: str
    canonical_share_hash: str

    @property
    def matched(self) -> bool:
        return self.status == "matched"


def cross_check_pool_evidence(
    proof: MiningShareProof,
    snapshot: PoolEvidenceSnapshot,
) -> PoolCrossCheckResult:
    share_hash = canonical_share_hash(proof)
    if proof.pool_id != snapshot.pool_id:
        return _rejected(CROSS_CHECK_POOL_MISMATCH, share_hash)
    if proof.pool_worker_name != snapshot.worker_name:
        return _rejected(CROSS_CHECK_WORKER_MISMATCH, share_hash)
    if proof.session_id != snapshot.session_id:
        return _rejected(CROSS_CHECK_SESSION_MISMATCH, share_hash)
    if proof.pool_result != "accepted":
        return _under_review(CROSS_CHECK_PROOF_NOT_ACCEPTED, share_hash)
    if share_hash in snapshot.rejected_share_hashes:
        return _under_review(CROSS_CHECK_POOL_REJECTED, share_hash)
    if share_hash not in snapshot.accepted_share_hashes:
        return _under_review(CROSS_CHECK_ACCEPTED_ABSENT, share_hash)
    return PoolCrossCheckResult(
        status="matched",
        rewardable=True,
        reason_code=CROSS_CHECK_MATCHED,
        canonical_share_hash=share_hash,
    )


def _rejected(reason_code: str, share_hash: str) -> PoolCrossCheckResult:
    return PoolCrossCheckResult(
        status="rejected",
        rewardable=False,
        reason_code=reason_code,
        canonical_share_hash=share_hash,
    )


def _under_review(reason_code: str, share_hash: str) -> PoolCrossCheckResult:
    return PoolCrossCheckResult(
        status="under_review",
        rewardable=False,
        reason_code=reason_code,
        canonical_share_hash=share_hash,
    )

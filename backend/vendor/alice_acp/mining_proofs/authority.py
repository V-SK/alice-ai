from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

from alice_acp.mining_proofs.anomaly import (
    AUTHORITY_CLIENT_LOG_ONLY,
    evaluate_difficulty_anomaly,
)
from alice_acp.mining_proofs.canonical import canonical_share_hash
from alice_acp.mining_proofs.collector import (
    DEFAULT_DIFFICULTY_JUMP_FACTOR,
    DEFAULT_MAX_FUTURE_SKEW,
    DEFAULT_MINIMUM_SHARE_DIFFICULTY,
    validate_mining_share_proof,
)
from alice_acp.mining_proofs.pool_evidence import (
    POOL_AUTHORITY_CONFIRMED,
    PRL_EPOCH_AUTHORITY_CONFIRMED,
    SELF_VALIDATED_AUTHORITY_CONFIRMED,
    PoolEvidenceAuthority,
    PoolEvidenceAuthorityResult,
    PrlEpochEvidenceAuthority,
    SelfValidatedShareAuthority,
    cross_check_pool_authority,
    cross_check_prl_epoch_authority,
    cross_check_self_validated_share,
    prl_epoch_rewardable_score,
)
from alice_acp.mining_proofs.risk import RiskAction
from alice_acp.mining_proofs.types import MiningShareProof, ProofCollectionResult
from alice_acp.mining_session.types import SignedMiningSession

ProofAuthorityStatus = Literal[
    "rewardable_candidate",
    "under_review",
    "rejected",
    "nonrewardable",
]

PROOF_AUTHORITY_REWARDABLE_CANDIDATE = "PROOF_AUTHORITY_REWARDABLE_CANDIDATE"
PROOF_AUTHORITY_EVIDENCE_REQUIRED = "PROOF_AUTHORITY_EVIDENCE_REQUIRED"


@dataclass(frozen=True, slots=True)
class ProofAuthorityDecision:
    status: ProofAuthorityStatus
    rewardable_candidate: bool
    reason_code: str
    canonical_share_hash: str
    risk_action: RiskAction
    collection_result: ProofCollectionResult
    pool_result: PoolEvidenceAuthorityResult | None = None
    #: PRL-only: the epoch-credit magnitude (``epoch.share * SHARE_SCALE`` with a
    #: flat-unit fallback) carried out of the PRL epoch branch so the bridge derives
    #: the ledger ``rewardable_score`` from the validated EPOCH work instead of the
    #: reconstructed proof's flat ``share_difficulty``. ``None`` on every share-hash
    #: lane (XMR/RVN/LTC), which keeps deriving the score from ``share_difficulty``
    #: exactly as before — so this leaves those lanes' magnitude untouched.
    rewardable_score_override: Decimal | None = None

    @property
    def accepted(self) -> bool:
        return self.status == "rewardable_candidate"


def evaluate_public_beta_proof_authority(
    session: SignedMiningSession,
    proof: MiningShareProof,
    *,
    observed_at: datetime,
    pool_evidence: PoolEvidenceAuthority
    | PrlEpochEvidenceAuthority
    | SelfValidatedShareAuthority
    | None,
    previous_share_difficulty: Decimal | None = None,
    minimum_share_difficulty: Decimal = DEFAULT_MINIMUM_SHARE_DIFFICULTY,
    difficulty_jump_factor: Decimal = DEFAULT_DIFFICULTY_JUMP_FACTOR,
    max_future_skew: timedelta = DEFAULT_MAX_FUTURE_SKEW,
) -> ProofAuthorityDecision:
    share_hash = canonical_share_hash(proof)
    collection = validate_mining_share_proof(
        session,
        proof,
        observed_at=observed_at,
        minimum_share_difficulty=minimum_share_difficulty,
        max_future_skew=max_future_skew,
    )
    if collection.status == "rejected":
        return _decision(
            status="rejected",
            reason_code=collection.reason_code,
            share_hash=share_hash,
            risk_action="reject",
            collection=collection,
        )
    if collection.status == "nonrewardable":
        return _decision(
            status="nonrewardable",
            reason_code=collection.reason_code,
            share_hash=share_hash,
            risk_action="cap",
            collection=collection,
        )
    if collection.status == "under_review":
        return _decision(
            status="under_review",
            reason_code=collection.reason_code,
            share_hash=share_hash,
            risk_action="under_review",
            collection=collection,
        )

    anomaly = evaluate_difficulty_anomaly(
        proof,
        previous_share_difficulty=previous_share_difficulty,
        difficulty_jump_factor=difficulty_jump_factor,
    )
    if anomaly is not None:
        return _decision(
            status="under_review",
            reason_code=anomaly.reason_code,
            share_hash=share_hash,
            risk_action="under_review",
            collection=anomaly,
        )

    if pool_evidence is None:
        return _decision(
            status="under_review",
            reason_code=PROOF_AUTHORITY_EVIDENCE_REQUIRED,
            share_hash=share_hash,
            risk_action="under_review",
            collection=ProofCollectionResult(
                status="under_review",
                counted=False,
                reason_code=AUTHORITY_CLIENT_LOG_ONLY,
            ),
        )

    # THREE-BRANCH DISPATCH (doc §2.2/§2.4 + §3 Q1): the evidence type selects the
    # gate. (1) A SELF-VALIDATED share-hash proxy proof arrives with a
    # SelfValidatedShareAuthority (Alice's OWN re-hash is the authority — the
    # proxy-pool model). Route it to the self-validated gate and thread the validated
    # share_difficulty through the SAME rewardable_score_override seam PRL uses, so
    # the ledger score reflects the REAL difficulty. (2) A PRL-lane proof arrives with
    # a PrlEpochEvidenceAuthority (the parallel, NO-share-hash epoch evidence). (3)
    # Every OTHER (legacy upstream-attested) lane carries a PoolEvidenceAuthority and
    # stays on the unchanged share-hash gate. All three gates return the SAME
    # PoolEvidenceAuthorityResult shape, so everything below is identical. The PRL and
    # legacy gate bodies are unchanged from before this branch was added.
    rewardable_score_override: Decimal | None = None
    if isinstance(pool_evidence, SelfValidatedShareAuthority):
        pool_result = cross_check_self_validated_share(proof, pool_evidence)
        confirmed_reason_code = SELF_VALIDATED_AUTHORITY_CONFIRMED
        if pool_result.reason_code == confirmed_reason_code:
            rewardable_score_override = pool_evidence.share_difficulty
    elif isinstance(pool_evidence, PrlEpochEvidenceAuthority):
        pool_result = cross_check_prl_epoch_authority(proof, pool_evidence)
        confirmed_reason_code = PRL_EPOCH_AUTHORITY_CONFIRMED
        if pool_result.reason_code == confirmed_reason_code:
            rewardable_score_override = prl_epoch_rewardable_score(pool_evidence)
    else:
        pool_result = cross_check_pool_authority(proof, pool_evidence)
        confirmed_reason_code = POOL_AUTHORITY_CONFIRMED
    if pool_result.reason_code == confirmed_reason_code:
        return ProofAuthorityDecision(
            status="rewardable_candidate",
            rewardable_candidate=True,
            reason_code=PROOF_AUTHORITY_REWARDABLE_CANDIDATE,
            canonical_share_hash=share_hash,
            risk_action="allow",
            collection_result=collection,
            pool_result=pool_result,
            rewardable_score_override=rewardable_score_override,
        )
    if pool_result.status == "under_review":
        return _decision(
            status="under_review",
            reason_code=pool_result.reason_code,
            share_hash=share_hash,
            risk_action="under_review",
            collection=collection,
            pool_result=pool_result,
        )
    return _decision(
        status="rejected",
        reason_code=pool_result.reason_code,
        share_hash=share_hash,
        risk_action="reject",
        collection=collection,
        pool_result=pool_result,
    )


def _decision(
    *,
    status: ProofAuthorityStatus,
    reason_code: str,
    share_hash: str,
    risk_action: RiskAction,
    collection: ProofCollectionResult,
    pool_result: PoolEvidenceAuthorityResult | None = None,
) -> ProofAuthorityDecision:
    return ProofAuthorityDecision(
        status=status,
        rewardable_candidate=False,
        reason_code=reason_code,
        canonical_share_hash=share_hash,
        risk_action=risk_action,
        collection_result=collection,
        pool_result=pool_result,
    )

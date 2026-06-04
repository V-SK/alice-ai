from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from alice_acp.evidence.types import (
    ensure_no_raw_secret,
    validate_aware_timestamp,
    validate_sha256,
)
from alice_acp.mining_prl.canonical import canonical_prl_hash, full_prl_payload_digest
from alice_acp.mining_prl.types import (
    PRLEvidenceSourceType,
    PRLPoolResult,
    PRLProofCollectionResult,
    PRLProofCollectorSummary,
    PRLProofSession,
    PRLRiskAction,
    PRLShareProof,
)

PRL_PROOF_ACCEPTED = "PRL_PROOF_ACCEPTED"
PRL_PROOF_DUPLICATE = "PRL_PROOF_DUPLICATE"
PRL_PROOF_REPLAY_PAYLOAD_MISMATCH = "PRL_PROOF_REPLAY_PAYLOAD_MISMATCH"
PRL_PROOF_CANONICAL_HASH_MISMATCH = "PRL_PROOF_CANONICAL_HASH_MISMATCH"
PRL_PROOF_NONREWARDABLE_POOL_RESULT = "PRL_PROOF_NONREWARDABLE_POOL_RESULT"
PRL_PROOF_SESSION_MISMATCH = "PRL_PROOF_SESSION_MISMATCH"
PRL_PROOF_PASSPORT_MISMATCH = "PRL_PROOF_PASSPORT_MISMATCH"
PRL_PROOF_POOL_MISMATCH = "PRL_PROOF_POOL_MISMATCH"
PRL_PROOF_WORKER_MISMATCH = "PRL_PROOF_WORKER_MISMATCH"
PRL_PROOF_DEVICE_MISMATCH = "PRL_PROOF_DEVICE_MISMATCH"
PRL_PROOF_COLLECTION_ADDRESS_MISMATCH = "PRL_PROOF_COLLECTION_ADDRESS_MISMATCH"
PRL_PROOF_STALE_TIMESTAMP = "PRL_PROOF_STALE_TIMESTAMP"
PRL_PROOF_FUTURE_TIMESTAMP = "PRL_PROOF_FUTURE_TIMESTAMP"
PRL_PROOF_WORK_SCORE_BELOW_MINIMUM = "PRL_PROOF_WORK_SCORE_BELOW_MINIMUM"
PRL_AUTHORITY_EVIDENCE_REQUIRED = "PRL_AUTHORITY_EVIDENCE_REQUIRED"
PRL_AUTHORITY_ACCEPTED_ABSENT = "PRL_AUTHORITY_ACCEPTED_ABSENT"
PRL_AUTHORITY_CONFIRMED = "PRL_AUTHORITY_CONFIRMED"
PRL_AUTHORITY_POOL_MISMATCH = "PRL_AUTHORITY_POOL_MISMATCH"
PRL_AUTHORITY_SESSION_MISMATCH = "PRL_AUTHORITY_SESSION_MISMATCH"
PRL_AUTHORITY_WORKER_MISMATCH = "PRL_AUTHORITY_WORKER_MISMATCH"
PRL_AUTHORITY_COLLECTION_ADDRESS_MISMATCH = "PRL_AUTHORITY_COLLECTION_ADDRESS_MISMATCH"
PRL_AUTHORITY_REJECTED_PROOF = "PRL_AUTHORITY_REJECTED_PROOF"
PRL_AUTHORITY_STALE_PROOF = "PRL_AUTHORITY_STALE_PROOF"
PRL_AUTHORITY_DUPLICATE_PROOF = "PRL_AUTHORITY_DUPLICATE_PROOF"
PRL_AUTHORITY_INVALID_PROOF = "PRL_AUTHORITY_INVALID_PROOF"

DEFAULT_MINIMUM_WORK_SCORE = Decimal("1")
DEFAULT_MAX_FUTURE_SKEW = timedelta(seconds=60)

_SUPPORTED_SOURCE_TYPES = frozenset({"fixture", "manual", "export"})
_REJECTED_REASON_BY_RESULT: dict[str, str] = {
    "rejected": PRL_AUTHORITY_REJECTED_PROOF,
    "stale": PRL_AUTHORITY_STALE_PROOF,
    "duplicate": PRL_AUTHORITY_DUPLICATE_PROOF,
    "invalid": PRL_AUTHORITY_INVALID_PROOF,
}


@dataclass(frozen=True, slots=True)
class PRLRejectedPoolEvidence:
    canonical_hash: str
    pool_result: PRLPoolResult

    def __post_init__(self) -> None:
        validate_sha256(self.canonical_hash, field_name="canonical_hash")
        if self.pool_result == "accepted":
            raise ValueError("rejected PRL pool evidence must not use accepted result")


@dataclass(frozen=True, slots=True)
class PRLPoolEvidenceSnapshot:
    pool_id: str
    session_id: str
    worker_name: str
    collection_address: str
    accepted_proof_hashes: tuple[str, ...]
    rejected_proof_hashes: tuple[str, ...]
    generated_at: datetime
    source_type: PRLEvidenceSourceType
    rejected_proof_results: tuple[PRLRejectedPoolEvidence, ...] = ()

    def __post_init__(self) -> None:
        required = (
            self.pool_id,
            self.session_id,
            self.worker_name,
            self.collection_address,
            self.source_type,
        )
        if any(not value for value in required):
            raise ValueError("PRL pool evidence snapshot fields must be non-empty")
        if self.source_type not in _SUPPORTED_SOURCE_TYPES:
            raise ValueError("source_type must be fixture, manual, or export")
        validate_aware_timestamp("generated_at", self.generated_at)
        for field_name, value in (
            ("pool_id", self.pool_id),
            ("session_id", self.session_id),
            ("worker_name", self.worker_name),
            ("collection_address", self.collection_address),
        ):
            ensure_no_raw_secret(value, field_name=field_name)

        accepted = _validated_unique_hashes(
            self.accepted_proof_hashes,
            field_name="accepted_proof_hashes",
        )
        rejected = _validated_unique_hashes(
            self.rejected_proof_hashes,
            field_name="rejected_proof_hashes",
        )
        if accepted & rejected:
            raise ValueError("accepted and rejected PRL proof hashes must not overlap")

        rejected_detail_hashes = set()
        for detail in self.rejected_proof_results:
            if detail.canonical_hash not in rejected:
                raise ValueError("rejected_proof_results must reference rejected hashes")
            if detail.canonical_hash in rejected_detail_hashes:
                raise ValueError("rejected_proof_results must be unique per hash")
            rejected_detail_hashes.add(detail.canonical_hash)

    def rejected_reason_for(self, proof_hash: str) -> str:
        for detail in self.rejected_proof_results:
            if detail.canonical_hash == proof_hash:
                return _REJECTED_REASON_BY_RESULT[detail.pool_result]
        return PRL_AUTHORITY_REJECTED_PROOF


@dataclass(frozen=True, slots=True)
class PRLProofAuthorityDecision:
    status: str
    rewardable_candidate: bool
    reason_code: str
    canonical_hash: str
    risk_action: PRLRiskAction
    collection_result: PRLProofCollectionResult

    @property
    def accepted(self) -> bool:
        return self.status == "rewardable_candidate"


@dataclass(slots=True)
class PRLProofCollector:
    session: PRLProofSession
    minimum_work_score: Decimal = DEFAULT_MINIMUM_WORK_SCORE
    max_future_skew: timedelta = DEFAULT_MAX_FUTURE_SKEW
    payload_by_canonical_hash: dict[str, str] = field(default_factory=dict)
    canonical_hash_by_identity: dict[tuple[str, str, str, str, str, str], str] = (
        field(default_factory=dict)
    )
    accepted_proof_count: int = 0
    total_work_score: Decimal = Decimal("0")

    def collect(
        self,
        proof: PRLShareProof,
        *,
        observed_at: datetime,
        pool_evidence: PRLPoolEvidenceSnapshot | None,
    ) -> PRLProofAuthorityDecision:
        decision = evaluate_prl_proof_authority(
            self.session,
            proof,
            observed_at=observed_at,
            pool_evidence=pool_evidence,
            minimum_work_score=self.minimum_work_score,
            max_future_skew=self.max_future_skew,
        )
        if not decision.accepted:
            return decision

        proof_hash = decision.canonical_hash
        payload_digest = full_prl_payload_digest(proof)
        previous_payload = self.payload_by_canonical_hash.get(proof_hash)
        if previous_payload == payload_digest:
            return _decision(
                status="duplicate",
                reason_code=PRL_PROOF_DUPLICATE,
                proof_hash=proof_hash,
                risk_action="cap",
                collection=_duplicate(PRL_PROOF_DUPLICATE),
            )
        if previous_payload is not None:
            return _decision(
                status="rejected",
                reason_code=PRL_PROOF_REPLAY_PAYLOAD_MISMATCH,
                proof_hash=proof_hash,
                risk_action="reject",
                collection=_rejected(PRL_PROOF_REPLAY_PAYLOAD_MISMATCH),
            )

        previous_hash = self.canonical_hash_by_identity.get(proof.proof_identity)
        if previous_hash is not None and previous_hash != proof_hash:
            return _decision(
                status="rejected",
                reason_code=PRL_PROOF_REPLAY_PAYLOAD_MISMATCH,
                proof_hash=proof_hash,
                risk_action="reject",
                collection=_rejected(PRL_PROOF_REPLAY_PAYLOAD_MISMATCH),
            )

        self.payload_by_canonical_hash[proof_hash] = payload_digest
        self.canonical_hash_by_identity[proof.proof_identity] = proof_hash
        self.accepted_proof_count += 1
        self.total_work_score += proof.work_score
        return decision

    def summary(self) -> PRLProofCollectorSummary:
        return PRLProofCollectorSummary(
            accepted_proof_count=self.accepted_proof_count,
            total_work_score=self.total_work_score,
        )


def evaluate_prl_proof_authority(
    session: PRLProofSession,
    proof: PRLShareProof,
    *,
    observed_at: datetime,
    pool_evidence: PRLPoolEvidenceSnapshot | None,
    minimum_work_score: Decimal = DEFAULT_MINIMUM_WORK_SCORE,
    max_future_skew: timedelta = DEFAULT_MAX_FUTURE_SKEW,
) -> PRLProofAuthorityDecision:
    proof_hash = canonical_prl_hash(proof)
    collection = validate_prl_share_proof(
        session,
        proof,
        observed_at=observed_at,
        minimum_work_score=minimum_work_score,
        max_future_skew=max_future_skew,
    )
    if collection.status == "rejected":
        return _decision(
            status="rejected",
            reason_code=collection.reason_code,
            proof_hash=proof_hash,
            risk_action="reject",
            collection=collection,
        )
    if collection.status == "nonrewardable":
        return _decision(
            status="nonrewardable",
            reason_code=collection.reason_code,
            proof_hash=proof_hash,
            risk_action="cap",
            collection=collection,
        )

    if pool_evidence is None:
        return _decision(
            status="under_review",
            reason_code=PRL_AUTHORITY_EVIDENCE_REQUIRED,
            proof_hash=proof_hash,
            risk_action="under_review",
            collection=_under_review(PRL_AUTHORITY_EVIDENCE_REQUIRED),
        )

    authority = cross_check_prl_pool_evidence(proof, pool_evidence)
    if authority.status != "accepted":
        return _decision(
            status=authority.status,
            reason_code=authority.reason_code,
            proof_hash=proof_hash,
            risk_action="under_review" if authority.status == "under_review" else "reject",
            collection=authority,
        )

    return PRLProofAuthorityDecision(
        status="rewardable_candidate",
        rewardable_candidate=True,
        reason_code=PRL_AUTHORITY_CONFIRMED,
        canonical_hash=proof_hash,
        risk_action="allow",
        collection_result=collection,
    )


def validate_prl_share_proof(
    session: PRLProofSession,
    proof: PRLShareProof,
    *,
    observed_at: datetime,
    minimum_work_score: Decimal = DEFAULT_MINIMUM_WORK_SCORE,
    max_future_skew: timedelta = DEFAULT_MAX_FUTURE_SKEW,
) -> PRLProofCollectionResult:
    validate_aware_timestamp("observed_at", observed_at)
    if not isinstance(minimum_work_score, Decimal):
        raise TypeError("minimum_work_score must be Decimal")
    if proof.session_id != session.session_id:
        return _rejected(PRL_PROOF_SESSION_MISMATCH)
    if proof.passport_id != session.passport_id:
        return _rejected(PRL_PROOF_PASSPORT_MISMATCH)
    if proof.pool_id != session.pool_id:
        return _rejected(PRL_PROOF_POOL_MISMATCH)
    if proof.worker_id != session.worker_id or proof.worker_name != session.worker_name:
        return _rejected(PRL_PROOF_WORKER_MISMATCH)
    if proof.device_id != session.device_id:
        return _rejected(PRL_PROOF_DEVICE_MISMATCH)
    if proof.collection_address != session.collection_address:
        return _rejected(PRL_PROOF_COLLECTION_ADDRESS_MISMATCH)
    if proof.submitted_at > observed_at + max_future_skew:
        return _rejected(PRL_PROOF_FUTURE_TIMESTAMP)
    if proof.accepted_at > observed_at + max_future_skew:
        return _rejected(PRL_PROOF_FUTURE_TIMESTAMP)
    if proof.submitted_at < session.issued_at or proof.accepted_at < proof.submitted_at:
        return _rejected(PRL_PROOF_STALE_TIMESTAMP)
    if proof.accepted_at > session.expires_at:
        return _rejected(PRL_PROOF_STALE_TIMESTAMP)
    expected_hash = canonical_prl_hash(proof)
    if proof.canonical_hash is not None and proof.canonical_hash != expected_hash:
        return _rejected(PRL_PROOF_CANONICAL_HASH_MISMATCH)
    if proof.pool_result != "accepted":
        return _nonrewardable(PRL_PROOF_NONREWARDABLE_POOL_RESULT)
    if proof.work_score < minimum_work_score:
        return _nonrewardable(PRL_PROOF_WORK_SCORE_BELOW_MINIMUM)
    return PRLProofCollectionResult(
        status="accepted",
        counted=True,
        reason_code=PRL_PROOF_ACCEPTED,
    )


def cross_check_prl_pool_evidence(
    proof: PRLShareProof,
    evidence: PRLPoolEvidenceSnapshot,
) -> PRLProofCollectionResult:
    proof_hash = canonical_prl_hash(proof)
    if proof.pool_id != evidence.pool_id:
        return _rejected(PRL_AUTHORITY_POOL_MISMATCH)
    if proof.session_id != evidence.session_id:
        return _rejected(PRL_AUTHORITY_SESSION_MISMATCH)
    if proof.worker_name != evidence.worker_name:
        return _rejected(PRL_AUTHORITY_WORKER_MISMATCH)
    if proof.collection_address != evidence.collection_address:
        return _rejected(PRL_AUTHORITY_COLLECTION_ADDRESS_MISMATCH)
    if proof_hash in evidence.rejected_proof_hashes:
        return _rejected(evidence.rejected_reason_for(proof_hash))
    if proof_hash not in evidence.accepted_proof_hashes:
        return _under_review(PRL_AUTHORITY_ACCEPTED_ABSENT)
    return PRLProofCollectionResult(
        status="accepted",
        counted=True,
        reason_code=PRL_AUTHORITY_CONFIRMED,
    )


def _validated_unique_hashes(values: tuple[str, ...], *, field_name: str) -> set[str]:
    unique = set(values)
    if len(unique) != len(values):
        raise ValueError(f"{field_name} must be unique")
    for value in values:
        validate_sha256(value, field_name=field_name)
    return unique


def _decision(
    *,
    status: str,
    reason_code: str,
    proof_hash: str,
    risk_action: PRLRiskAction,
    collection: PRLProofCollectionResult,
) -> PRLProofAuthorityDecision:
    return PRLProofAuthorityDecision(
        status=status,
        rewardable_candidate=False,
        reason_code=reason_code,
        canonical_hash=proof_hash,
        risk_action=risk_action,
        collection_result=collection,
    )


def _duplicate(reason_code: str) -> PRLProofCollectionResult:
    return PRLProofCollectionResult(status="duplicate", counted=False, reason_code=reason_code)


def _rejected(reason_code: str) -> PRLProofCollectionResult:
    return PRLProofCollectionResult(status="rejected", counted=False, reason_code=reason_code)


def _nonrewardable(reason_code: str) -> PRLProofCollectionResult:
    return PRLProofCollectionResult(status="nonrewardable", counted=False, reason_code=reason_code)


def _under_review(reason_code: str) -> PRLProofCollectionResult:
    return PRLProofCollectionResult(status="under_review", counted=False, reason_code=reason_code)

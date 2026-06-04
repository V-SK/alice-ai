from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from alice_acp.evidence.types import validate_aware_timestamp
from alice_acp.mining_pool.types import AcceptedShareProof
from alice_acp.mining_proofs.canonical import (
    canonical_share_hash,
    full_share_payload_digest,
)
from alice_acp.mining_proofs.difficulty_baseline import DifficultyBaselineStore
from alice_acp.mining_proofs.types import (
    MiningShareProof,
    ProofCollectionResult,
    ProofCollectorSummary,
)
from alice_acp.mining_session import (
    SESSION_COLLECTION_ADDRESS_MISMATCH,
    SESSION_EXPIRED,
    SESSION_PASSPORT_MISMATCH,
    SESSION_POOL_MUTATION,
    SESSION_WORKER_MISMATCH,
    validate_signed_mining_session,
)
from alice_acp.mining_session.types import SignedMiningSession

PROOF_ACCEPTED = "PROOF_ACCEPTED"
PROOF_DUPLICATE = "PROOF_DUPLICATE"
PROOF_REPLAY_PAYLOAD_MISMATCH = "PROOF_REPLAY_PAYLOAD_MISMATCH"
PROOF_TIMESTAMP_OUTSIDE_SESSION = "PROOF_TIMESTAMP_OUTSIDE_SESSION"
PROOF_SESSION_MISMATCH = "PROOF_SESSION_MISMATCH"
PROOF_SESSION_INVALID = "PROOF_SESSION_INVALID"
PROOF_COLLECTION_ADDRESS_MISMATCH = "PROOF_COLLECTION_ADDRESS_MISMATCH"
PROOF_CANONICAL_HASH_MISMATCH = "PROOF_CANONICAL_HASH_MISMATCH"
PROOF_SHARE_NONCE_REPLAY = "PROOF_SHARE_NONCE_REPLAY"
PROOF_STALE_TIMESTAMP = "PROOF_STALE_TIMESTAMP"
PROOF_FUTURE_TIMESTAMP = "PROOF_FUTURE_TIMESTAMP"
PROOF_NONREWARDABLE_POOL_RESULT = "PROOF_NONREWARDABLE_POOL_RESULT"
PROOF_DIFFICULTY_BELOW_MINIMUM = "PROOF_DIFFICULTY_BELOW_MINIMUM"
PROOF_TARGET_DIFFICULTY_MISMATCH = "PROOF_TARGET_DIFFICULTY_MISMATCH"
PROOF_DIFFICULTY_ANOMALY = "PROOF_DIFFICULTY_ANOMALY"

DEFAULT_MINIMUM_SHARE_DIFFICULTY = Decimal("1")
DEFAULT_DIFFICULTY_JUMP_FACTOR = Decimal("16")
DEFAULT_MAX_FUTURE_SKEW = timedelta(seconds=60)


@dataclass(slots=True)
class MiningProofCollector:
    session: SignedMiningSession
    proofs_by_identity: dict[tuple[str, str, str, str, str], str] = field(default_factory=dict)
    accepted_share_count: int = 0
    total_share_difficulty: Decimal = Decimal("0")

    def collect(self, proof: AcceptedShareProof) -> ProofCollectionResult:
        if proof.session_id != self.session.session_id:
            return _rejected(PROOF_SESSION_MISMATCH)
        if (
            proof.submitted_at < self.session.issued_at
            or proof.submitted_at > self.session.expires_at
        ):
            return _rejected(PROOF_TIMESTAMP_OUTSIDE_SESSION)
        validation = validate_signed_mining_session(
            self.session,
            observed_at=proof.submitted_at,
            expected_collection_address=proof.collection_address,
            expected_worker_id=proof.worker_id,
            expected_pool_id=proof.pool_id,
        )
        if validation.reason_code == SESSION_COLLECTION_ADDRESS_MISMATCH:
            return _rejected(PROOF_COLLECTION_ADDRESS_MISMATCH)
        if not validation.accepted:
            return _rejected(validation.reason_code or PROOF_SESSION_INVALID)

        digest = proof_payload_digest(proof)
        previous_digest = self.proofs_by_identity.get(proof.identity)
        if previous_digest == digest:
            return ProofCollectionResult(
                status="duplicate",
                counted=False,
                reason_code=PROOF_DUPLICATE,
            )
        if previous_digest is not None:
            return _rejected(PROOF_REPLAY_PAYLOAD_MISMATCH)

        self.proofs_by_identity[proof.identity] = digest
        self.accepted_share_count += 1
        self.total_share_difficulty += proof.share_difficulty
        return ProofCollectionResult(status="accepted", counted=True, reason_code=PROOF_ACCEPTED)

    def summary(self) -> ProofCollectorSummary:
        return ProofCollectorSummary(
            accepted_share_count=self.accepted_share_count,
            total_share_difficulty=self.total_share_difficulty,
        )


@dataclass(slots=True)
class StrongMiningProofCollector:
    session: SignedMiningSession
    minimum_share_difficulty: Decimal = DEFAULT_MINIMUM_SHARE_DIFFICULTY
    difficulty_jump_factor: Decimal = DEFAULT_DIFFICULTY_JUMP_FACTOR
    max_future_skew: timedelta = DEFAULT_MAX_FUTURE_SKEW
    payload_by_canonical_hash: dict[str, str] = field(default_factory=dict)
    canonical_hash_by_share_identity: dict[tuple[str, str, str, str], str] = (
        field(default_factory=dict)
    )
    accepted_share_count: int = 0
    total_share_difficulty: Decimal = Decimal("0")
    last_share_difficulty: Decimal | None = None
    # Phase F (H4): cross-session per-worker difficulty baseline seam. When a
    # store + key are supplied, the difficulty-anomaly baseline is seeded from and
    # written back to the store, so a new session for the same worker CANNOT reset
    # the baseline to None and bypass the jump guard on its first share.
    baseline_store: DifficultyBaselineStore | None = None
    baseline_key: str | None = None

    def __post_init__(self) -> None:
        if self.baseline_store is not None and self.baseline_key is not None:
            persisted = self._read_baseline()
            if persisted is not None and (
                self.last_share_difficulty is None
                or persisted > self.last_share_difficulty
            ):
                self.last_share_difficulty = persisted

    def collect(
        self,
        proof: MiningShareProof,
        *,
        observed_at: datetime,
    ) -> ProofCollectionResult:
        result = validate_mining_share_proof(
            self.session,
            proof,
            observed_at=observed_at,
            minimum_share_difficulty=self.minimum_share_difficulty,
            max_future_skew=self.max_future_skew,
        )
        if not result.accepted:
            return result

        expected_hash = canonical_share_hash(proof)
        payload_digest = full_share_payload_digest(proof)
        previous_payload = self.payload_by_canonical_hash.get(expected_hash)
        if previous_payload == payload_digest:
            return ProofCollectionResult(
                status="duplicate",
                counted=False,
                reason_code=PROOF_DUPLICATE,
            )
        if previous_payload is not None:
            return _rejected(PROOF_REPLAY_PAYLOAD_MISMATCH)

        previous_hash = self.canonical_hash_by_share_identity.get(proof.share_identity)
        if previous_hash is not None and previous_hash != expected_hash:
            return _rejected(PROOF_SHARE_NONCE_REPLAY)

        # H4: the baseline may be seeded from a prior session via the store, so a
        # session reset no longer clears the anomaly comparison.
        baseline = self._effective_baseline()
        if baseline is not None:
            anomaly_threshold = baseline * self.difficulty_jump_factor
            if proof.share_difficulty > anomaly_threshold:
                return _under_review(PROOF_DIFFICULTY_ANOMALY)

        self.payload_by_canonical_hash[expected_hash] = payload_digest
        self.canonical_hash_by_share_identity[proof.share_identity] = expected_hash
        self.accepted_share_count += 1
        self.total_share_difficulty += proof.share_difficulty
        self.last_share_difficulty = proof.share_difficulty
        self._persist_baseline(proof.share_difficulty)
        return ProofCollectionResult(status="accepted", counted=True, reason_code=PROOF_ACCEPTED)

    def _effective_baseline(self) -> Decimal | None:
        if self.baseline_store is not None and self.baseline_key is not None:
            persisted = self._read_baseline()
            if persisted is not None and (
                self.last_share_difficulty is None
                or persisted > self.last_share_difficulty
            ):
                return persisted
        return self.last_share_difficulty

    def _read_baseline(self) -> Decimal | None:
        if self.baseline_store is None or self.baseline_key is None:
            return None
        # Fail-closed: a store error never invents a permissive high baseline; it
        # falls back to the in-instance baseline (first-share-establishes).
        try:
            return self.baseline_store.get_baseline(self.baseline_key)
        except (OSError, ValueError):
            return None

    def _persist_baseline(self, difficulty: Decimal) -> None:
        if self.baseline_store is None or self.baseline_key is None:
            return
        try:
            self.baseline_store.record_baseline(self.baseline_key, difficulty)
        except (OSError, ValueError):
            return

    def summary(self) -> ProofCollectorSummary:
        return ProofCollectorSummary(
            accepted_share_count=self.accepted_share_count,
            total_share_difficulty=self.total_share_difficulty,
        )


def validate_mining_share_proof(
    session: SignedMiningSession,
    proof: MiningShareProof,
    *,
    observed_at: datetime,
    minimum_share_difficulty: Decimal = DEFAULT_MINIMUM_SHARE_DIFFICULTY,
    max_future_skew: timedelta = DEFAULT_MAX_FUTURE_SKEW,
) -> ProofCollectionResult:
    validate_aware_timestamp("observed_at", observed_at)
    if proof.session_id != session.session_id:
        return _rejected(PROOF_SESSION_MISMATCH)
    if proof.pool_id != session.pool_id:
        return _rejected(SESSION_POOL_MUTATION)
    if proof.worker_id != session.worker_id or proof.pool_worker_name != session.worker_id:
        return _rejected(SESSION_WORKER_MISMATCH)
    if proof.passport_id != session.passport_id:
        return _rejected(SESSION_PASSPORT_MISMATCH)
    if proof.alice_collection_address != session.alice_collection_address:
        return _rejected(PROOF_COLLECTION_ADDRESS_MISMATCH)
    if proof.submitted_at > observed_at + max_future_skew:
        return _rejected(PROOF_FUTURE_TIMESTAMP)
    if proof.accepted_at > observed_at + max_future_skew:
        return _rejected(PROOF_FUTURE_TIMESTAMP)
    if proof.submitted_at < session.issued_at or proof.accepted_at < proof.submitted_at:
        return _rejected(PROOF_STALE_TIMESTAMP)

    validation = validate_signed_mining_session(
        session,
        observed_at=proof.accepted_at,
        expected_collection_address=proof.alice_collection_address,
        expected_worker_id=proof.worker_id,
        expected_passport_id=proof.passport_id,
        expected_pool_id=proof.pool_id,
    )
    if validation.reason_code == SESSION_COLLECTION_ADDRESS_MISMATCH:
        return _rejected(PROOF_COLLECTION_ADDRESS_MISMATCH)
    if validation.reason_code == SESSION_EXPIRED:
        return _rejected(SESSION_EXPIRED)
    if not validation.accepted:
        return _rejected(validation.reason_code or PROOF_SESSION_INVALID)

    expected_hash = canonical_share_hash(proof)
    if proof.canonical_share_hash is not None and proof.canonical_share_hash != expected_hash:
        return _rejected(PROOF_CANONICAL_HASH_MISMATCH)
    if proof.pool_result != "accepted":
        return _nonrewardable(PROOF_NONREWARDABLE_POOL_RESULT)
    if proof.share_difficulty < minimum_share_difficulty:
        return _nonrewardable(PROOF_DIFFICULTY_BELOW_MINIMUM)
    if proof.share_difficulty < proof.target_difficulty:
        return _under_review(PROOF_TARGET_DIFFICULTY_MISMATCH)
    return ProofCollectionResult(status="accepted", counted=True, reason_code=PROOF_ACCEPTED)


def proof_payload_digest(proof: AcceptedShareProof) -> str:
    validate_aware_timestamp("submitted_at", proof.submitted_at)
    payload = {
        "algorithm": proof.algorithm,
        "collection_address": proof.collection_address,
        "evidence_ref": proof.evidence_ref,
        "nonce": proof.nonce,
        "pool_id": proof.pool_id,
        "raw_ref_hash": proof.raw_ref_hash,
        "session_id": proof.session_id,
        "share_difficulty": str(proof.share_difficulty),
        "share_id": proof.share_id,
        "submitted_at": proof.submitted_at.isoformat(),
        "worker_id": proof.worker_id,
    }
    canonical = json.dumps(payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _rejected(reason_code: str) -> ProofCollectionResult:
    return ProofCollectionResult(status="rejected", counted=False, reason_code=reason_code)


def _nonrewardable(reason_code: str) -> ProofCollectionResult:
    return ProofCollectionResult(status="nonrewardable", counted=False, reason_code=reason_code)


def _under_review(reason_code: str) -> ProofCollectionResult:
    return ProofCollectionResult(status="under_review", counted=False, reason_code=reason_code)

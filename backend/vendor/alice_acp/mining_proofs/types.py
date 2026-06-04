from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from alice_acp.evidence.types import (
    ensure_no_raw_secret,
    parse_evidence_ref,
    validate_aware_timestamp,
    validate_sha256,
)
from alice_acp.mining_session.types import (
    SUPPORTED_MINING_ALGORITHMS,
    MiningAlgorithm,
)

PoolShareResult = Literal["accepted", "rejected", "stale", "duplicate", "invalid"]
ProofCollectionStatus = Literal[
    "accepted",
    "duplicate",
    "rejected",
    "nonrewardable",
    "under_review",
]


@dataclass(frozen=True, slots=True)
class ProofCollectionResult:
    status: ProofCollectionStatus
    counted: bool
    reason_code: str

    @property
    def accepted(self) -> bool:
        return self.status == "accepted"


@dataclass(frozen=True, slots=True)
class ProofCollectorSummary:
    accepted_share_count: int
    total_share_difficulty: Decimal


@dataclass(frozen=True, slots=True)
class MiningShareProof:
    pool_id: str
    algorithm: MiningAlgorithm
    pool_job_id: str
    pool_worker_name: str
    session_id: str
    passport_id: str
    worker_id: str
    alice_collection_address: str
    share_nonce: str
    share_hash: str
    header_hash: str
    share_difficulty: Decimal
    target_difficulty: Decimal
    submitted_at: datetime
    accepted_at: datetime
    pool_result: PoolShareResult
    pool_evidence_ref: str
    canonical_share_hash: str | None = None

    def __post_init__(self) -> None:
        required = (
            self.pool_id,
            self.algorithm,
            self.pool_job_id,
            self.pool_worker_name,
            self.session_id,
            self.passport_id,
            self.worker_id,
            self.alice_collection_address,
            self.share_nonce,
            self.share_hash,
            self.header_hash,
            self.pool_result,
            self.pool_evidence_ref,
        )
        if any(not value for value in required):
            raise ValueError("mining share proof fields must be non-empty")
        if self.algorithm not in SUPPORTED_MINING_ALGORITHMS:
            raise ValueError("unsupported mining algorithm")
        for field_name, value in (
            ("share_difficulty", self.share_difficulty),
            ("target_difficulty", self.target_difficulty),
        ):
            if not isinstance(value, Decimal):
                raise TypeError(f"{field_name} must be Decimal")
            if value <= Decimal("0"):
                raise ValueError(f"{field_name} must be positive")
        validate_aware_timestamp("submitted_at", self.submitted_at)
        validate_aware_timestamp("accepted_at", self.accepted_at)
        validate_sha256(self.share_hash, field_name="share_hash")
        validate_sha256(self.header_hash, field_name="header_hash")
        if self.canonical_share_hash is not None:
            validate_sha256(self.canonical_share_hash, field_name="canonical_share_hash")
        parse_evidence_ref(self.pool_evidence_ref)
        for field_name, value in (
            ("pool_id", self.pool_id),
            ("pool_worker_name", self.pool_worker_name),
            ("session_id", self.session_id),
            ("passport_id", self.passport_id),
            ("worker_id", self.worker_id),
            ("alice_collection_address", self.alice_collection_address),
            ("share_nonce", self.share_nonce),
        ):
            ensure_no_raw_secret(value, field_name=field_name)

    @property
    def share_identity(self) -> tuple[str, str, str, str]:
        return (
            self.pool_id,
            self.session_id,
            self.pool_job_id,
            self.share_nonce,
        )

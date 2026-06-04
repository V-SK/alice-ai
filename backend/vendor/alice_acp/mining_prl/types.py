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

PRL_ALGORITHM = "PRL"
RVN_FALLBACK_ALGORITHM = "RVN_KAWPOW"
ALICE_REWARDED_MINING_MODE = "ALICE_REWARDED_MINING"

PRLPoolResult = Literal["accepted", "rejected", "stale", "duplicate", "invalid"]
PRLCollectionStatus = Literal[
    "accepted",
    "duplicate",
    "rejected",
    "nonrewardable",
    "under_review",
]
PRLEvidenceSourceType = Literal["fixture", "manual", "export"]
PRLRiskAction = Literal["allow", "cap", "under_review", "reject"]


@dataclass(frozen=True, slots=True)
class PRLProofSession:
    session_id: str
    passport_id: str
    pool_id: str
    worker_id: str
    device_id: str
    worker_name: str
    collection_address: str
    issued_at: datetime
    expires_at: datetime
    algorithm: str = PRL_ALGORITHM
    fallback_algorithm: str = RVN_FALLBACK_ALGORITHM
    mode: str = ALICE_REWARDED_MINING_MODE

    def __post_init__(self) -> None:
        required = (
            self.session_id,
            self.passport_id,
            self.pool_id,
            self.worker_id,
            self.device_id,
            self.worker_name,
            self.collection_address,
            self.algorithm,
            self.fallback_algorithm,
            self.mode,
        )
        if any(not value for value in required):
            raise ValueError("PRL proof session fields must be non-empty")
        if self.algorithm != PRL_ALGORITHM:
            raise ValueError("PRL proof sessions must use PRL as primary algorithm")
        if self.fallback_algorithm != RVN_FALLBACK_ALGORITHM:
            raise ValueError("PRL proof sessions must keep RVN_KAWPOW as fallback")
        if self.mode != ALICE_REWARDED_MINING_MODE:
            raise ValueError("Direct Pool Mode is not supported")
        validate_aware_timestamp("issued_at", self.issued_at)
        validate_aware_timestamp("expires_at", self.expires_at)
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must be after issued_at")
        for field_name, value in (
            ("session_id", self.session_id),
            ("passport_id", self.passport_id),
            ("pool_id", self.pool_id),
            ("worker_id", self.worker_id),
            ("device_id", self.device_id),
            ("worker_name", self.worker_name),
            ("collection_address", self.collection_address),
        ):
            ensure_no_raw_secret(value, field_name=field_name)


@dataclass(frozen=True, slots=True)
class PRLProofCollectionResult:
    status: PRLCollectionStatus
    counted: bool
    reason_code: str

    @property
    def accepted(self) -> bool:
        return self.status == "accepted"


@dataclass(frozen=True, slots=True)
class PRLProofCollectorSummary:
    accepted_proof_count: int
    total_work_score: Decimal


@dataclass(frozen=True, slots=True)
class PRLShareProof:
    pool_id: str
    session_id: str
    passport_id: str
    worker_id: str
    device_id: str
    worker_name: str
    collection_address: str
    job_id: str
    nonce: str
    raw_ref: str
    share_difficulty: Decimal
    work_score: Decimal
    submitted_at: datetime
    accepted_at: datetime
    pool_result: PRLPoolResult
    pool_evidence_ref: str
    canonical_hash: str | None = None

    def __post_init__(self) -> None:
        required = (
            self.pool_id,
            self.session_id,
            self.passport_id,
            self.worker_id,
            self.device_id,
            self.worker_name,
            self.collection_address,
            self.job_id,
            self.nonce,
            self.raw_ref,
            self.pool_result,
            self.pool_evidence_ref,
        )
        if any(not value for value in required):
            raise ValueError("PRL share proof fields must be non-empty")
        for field_name, value in (
            ("share_difficulty", self.share_difficulty),
            ("work_score", self.work_score),
        ):
            if not isinstance(value, Decimal):
                raise TypeError(f"{field_name} must be Decimal")
            if value <= Decimal("0"):
                raise ValueError(f"{field_name} must be positive")
        validate_aware_timestamp("submitted_at", self.submitted_at)
        validate_aware_timestamp("accepted_at", self.accepted_at)
        if self.canonical_hash is not None:
            validate_sha256(self.canonical_hash, field_name="canonical_hash")
        parse_evidence_ref(self.pool_evidence_ref)
        for field_name, value in (
            ("pool_id", self.pool_id),
            ("session_id", self.session_id),
            ("passport_id", self.passport_id),
            ("worker_id", self.worker_id),
            ("device_id", self.device_id),
            ("worker_name", self.worker_name),
            ("collection_address", self.collection_address),
            ("job_id", self.job_id),
            ("nonce", self.nonce),
            ("raw_ref", self.raw_ref),
        ):
            ensure_no_raw_secret(value, field_name=field_name)

    @property
    def proof_identity(self) -> tuple[str, str, str, str, str, str]:
        return (
            self.pool_id,
            self.session_id,
            self.worker_id,
            self.device_id,
            self.job_id,
            self.nonce,
        )

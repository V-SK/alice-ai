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
from alice_acp.mining_session.types import RVN_KAWPOW, MiningAlgorithm

PoolShareResult = Literal["accepted", "rejected", "stale", "duplicate", "invalid"]
PoolAdapterStatus = Literal["proof_created", "not_rewardable", "rejected"]


@dataclass(frozen=True, slots=True)
class PoolShareEvent:
    pool_id: str
    session_id: str
    worker_id: str
    share_id: str
    nonce: str
    submitted_at: datetime
    share_difficulty: Decimal
    pool_result: PoolShareResult
    raw_ref_hash: str
    evidence_ref: str
    observed_collection_address: str
    algorithm: MiningAlgorithm = RVN_KAWPOW

    def __post_init__(self) -> None:
        required = (
            self.pool_id,
            self.session_id,
            self.worker_id,
            self.share_id,
            self.nonce,
            self.pool_result,
            self.raw_ref_hash,
            self.evidence_ref,
            self.observed_collection_address,
            self.algorithm,
        )
        if any(not value for value in required):
            raise ValueError("pool share event fields must be non-empty")
        if self.share_difficulty <= 0:
            raise ValueError("share_difficulty must be positive")
        validate_aware_timestamp("submitted_at", self.submitted_at)
        validate_sha256(self.raw_ref_hash, field_name="raw_ref_hash")
        parse_evidence_ref(self.evidence_ref)
        for field_name, value in (
            ("pool_id", self.pool_id),
            ("worker_id", self.worker_id),
            ("observed_collection_address", self.observed_collection_address),
        ):
            ensure_no_raw_secret(value, field_name=field_name)


@dataclass(frozen=True, slots=True)
class AcceptedShareProof:
    pool_id: str
    session_id: str
    worker_id: str
    share_id: str
    nonce: str
    submitted_at: datetime
    share_difficulty: Decimal
    raw_ref_hash: str
    evidence_ref: str
    collection_address: str
    algorithm: MiningAlgorithm = RVN_KAWPOW

    @property
    def identity(self) -> tuple[str, str, str, str, str]:
        return (
            self.pool_id,
            self.session_id,
            self.worker_id,
            self.share_id,
            self.nonce,
        )


@dataclass(frozen=True, slots=True)
class PoolAdapterResult:
    status: PoolAdapterStatus
    rewardable: bool
    reason_code: str
    proof: AcceptedShareProof | None = None

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from alice_acp.evidence.types import (
    ensure_no_raw_secret,
    validate_aware_timestamp,
    validate_sha256,
)

ALICE_REWARDED_MINING_MODE = "ALICE_REWARDED_MINING"
RVN_KAWPOW = "RVN_KAWPOW"
# Milestone 0 (D1 — the engineering unlock): the algorithm lock is widened from
# RVN-only to the three share-hash legs the multi-algo credit proxy pool feeds
# (RVN/KawPoW GPU, XMR/RandomX CPU, LTC/Scrypt ASIC). The ledger, scheduler,
# lanes, and pool budgets already accommodate all three lanes
# (``MiningProofIngestRequest.algorithm`` is a bare ``str``; ``Lane`` has
# xmr_pool / scrypt_pool). PRL is UNAFFECTED — it never builds a
# ``SignedMiningSession`` with these algorithms (it rides the epoch-credit lane).
XMR_RANDOMX = "XMR_RANDOMX"
LTC_SCRYPT = "LTC_SCRYPT"
SUPPORTED_MINING_ALGORITHMS = (RVN_KAWPOW, XMR_RANDOMX, LTC_SCRYPT)

MiningMode = Literal["ALICE_REWARDED_MINING"]
MiningAlgorithm = Literal["RVN_KAWPOW", "XMR_RANDOMX", "LTC_SCRYPT"]
SessionValidationStatus = Literal["valid", "invalid", "no_reward"]


@dataclass(frozen=True, slots=True)
class SignatureEnvelope:
    signer_ref: str
    payload_digest: str
    signed_at: datetime
    signature_ref: str
    signature_scheme: str = "alice_local_envelope_v1"
    key_id: str | None = None
    signature_b64: str | None = None

    def __post_init__(self) -> None:
        required = (
            self.signer_ref,
            self.payload_digest,
            self.signature_ref,
            self.signature_scheme,
        )
        if any(not value for value in required):
            raise ValueError("signature envelope fields must be non-empty")
        ensure_no_raw_secret(self.signer_ref, field_name="signer_ref")
        ensure_no_raw_secret(self.signature_ref, field_name="signature_ref")
        validate_sha256(self.payload_digest, field_name="payload_digest")
        validate_aware_timestamp("signed_at", self.signed_at)
        if self.key_id is not None:
            if not self.key_id.strip():
                raise ValueError("key_id must be non-empty when provided")
            ensure_no_raw_secret(self.key_id, field_name="key_id")
        if self.signature_b64 is not None:
            if not self.signature_b64.strip():
                raise ValueError("signature_b64 must be non-empty when provided")
            ensure_no_raw_secret(self.signature_b64, field_name="signature_b64")


@dataclass(frozen=True, slots=True)
class SignedMiningSession:
    session_id: str
    passport_id: str
    attempt_id: str
    pool_id: str
    alice_collection_address: str
    worker_id: str
    issued_at: datetime
    expires_at: datetime
    route_policy_version: str
    session_policy_version: str
    signature: SignatureEnvelope
    algorithm: MiningAlgorithm = RVN_KAWPOW
    mode: MiningMode = ALICE_REWARDED_MINING_MODE

    def __post_init__(self) -> None:
        required = (
            self.session_id,
            self.passport_id,
            self.attempt_id,
            self.pool_id,
            self.algorithm,
            self.alice_collection_address,
            self.worker_id,
            self.route_policy_version,
            self.session_policy_version,
            self.mode,
        )
        if any(not value for value in required):
            raise ValueError("signed mining session fields must be non-empty")
        for field_name, value in (
            ("session_id", self.session_id),
            ("passport_id", self.passport_id),
            ("pool_id", self.pool_id),
            ("alice_collection_address", self.alice_collection_address),
            ("worker_id", self.worker_id),
        ):
            ensure_no_raw_secret(value, field_name=field_name)
        validate_aware_timestamp("issued_at", self.issued_at)
        validate_aware_timestamp("expires_at", self.expires_at)
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must be after issued_at")
        if self.algorithm not in SUPPORTED_MINING_ALGORITHMS:
            raise ValueError("unsupported mining algorithm")
        if self.mode != ALICE_REWARDED_MINING_MODE:
            raise ValueError("Direct Pool Mode is not supported")


@dataclass(frozen=True, slots=True)
class SessionValidationResult:
    status: SessionValidationStatus
    session_id: str
    reason_code: str | None = None

    @property
    def accepted(self) -> bool:
        return self.status == "valid"

    @property
    def rewardable(self) -> bool:
        return self.status == "valid"

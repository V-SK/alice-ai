from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal

from alice_acp.evidence.types import ensure_no_raw_secret, validate_aware_timestamp, validate_sha256
from alice_acp.mining_device.types import BackendCapabilityResult
from alice_acp.mining_identity.public_key_registry import (
    MinerSessionRequestPayload,
    MinerSessionRequestSignature,
    MinerSessionRequestVerificationResult,
    PassportPublicKeyRegistryProtocol,
    SessionNonceReplayStoreProtocol,
    SessionRequestVerificationPolicy,
)
from alice_acp.mining_identity.types import MinerIdentitySignaturePayload
from alice_acp.mining_internal_test.types import (
    CollectionWalletPolicy,
    InternalMiningTestGate,
    KillSwitchPolicy,
    TestPoolProfile,
)
from alice_acp.mining_proofs.types import MiningShareProof
from alice_acp.mining_session.types import (
    ALICE_REWARDED_MINING_MODE,
    RVN_KAWPOW,
    MiningAlgorithm,
    MiningMode,
    SignedMiningSession,
)

SERVER_SESSION_ISSUED = "SERVER_SESSION_ISSUED"
SERVER_PROOF_ACCEPTED = "SERVER_PROOF_ACCEPTED"
SERVER_PROOF_REJECTED = "SERVER_PROOF_REJECTED"
SERVER_PROOF_UNDER_REVIEW = "SERVER_PROOF_UNDER_REVIEW"
SERVER_SHADOW_RECORDED = "SERVER_SHADOW_RECORDED"
SERVER_SESSION_UNKNOWN = "SERVER_SESSION_UNKNOWN"
SERVER_WALLET_SIGNATURE_REQUIRED = "SERVER_WALLET_SIGNATURE_REQUIRED"
SERVER_WALLET_SIGNATURE_PASSPORT_MISMATCH = (
    "SERVER_WALLET_SIGNATURE_PASSPORT_MISMATCH"
)
SERVER_BACKEND_UNSUPPORTED = "SERVER_BACKEND_UNSUPPORTED"
SERVER_COLLECTION_ADDRESS_REQUIRED = "SERVER_COLLECTION_ADDRESS_REQUIRED"
SERVER_ALGORITHM_UNSUPPORTED = "SERVER_ALGORITHM_UNSUPPORTED"
SERVER_LIVE_REWARD_FORBIDDEN = "SERVER_LIVE_REWARD_FORBIDDEN"
SERVER_PAYOUT_EXECUTOR_FORBIDDEN = "SERVER_PAYOUT_EXECUTOR_FORBIDDEN"
SERVER_DIRECT_POOL_MODE_REJECTED = "SERVER_DIRECT_POOL_MODE_REJECTED"
SERVER_MINER_PAYOUT_ADDRESS_REJECTED = "SERVER_MINER_PAYOUT_ADDRESS_REJECTED"
SERVER_PASSPORT_NOT_ALLOWED = "SERVER_PASSPORT_NOT_ALLOWED"
SERVER_DEVICE_NOT_ALLOWED = "SERVER_DEVICE_NOT_ALLOWED"
SERVER_POOL_NOT_ALLOWED = "SERVER_POOL_NOT_ALLOWED"
SERVER_POOL_PROFILE_MISMATCH = "SERVER_POOL_PROFILE_MISMATCH"
SERVER_KILL_SWITCH_BLOCKED = "SERVER_KILL_SWITCH_BLOCKED"
SERVER_POOL_CROSS_CHECK_REJECTED = "SERVER_POOL_CROSS_CHECK_REJECTED"
SERVER_POOL_CROSS_CHECK_UNDER_REVIEW = "SERVER_POOL_CROSS_CHECK_UNDER_REVIEW"
SERVER_POOL_EVIDENCE_SNAPSHOT_REQUIRED = "SERVER_POOL_EVIDENCE_SNAPSHOT_REQUIRED"
SERVER_SESSION_REGISTRY_VERIFICATION_REQUIRED = (
    "SERVER_SESSION_REGISTRY_VERIFICATION_REQUIRED"
)
SERVER_SESSION_REGISTRY_PAYLOAD_HASH_REQUIRED = (
    "SERVER_SESSION_REGISTRY_PAYLOAD_HASH_REQUIRED"
)
SERVER_SESSION_REGISTRY_SIGNATURE_REQUIRED = "SERVER_SESSION_REGISTRY_SIGNATURE_REQUIRED"
SERVER_SESSION_REGISTRY_ADAPTER_REQUIRED = "SERVER_SESSION_REGISTRY_ADAPTER_REQUIRED"
SERVER_SESSION_REGISTRY_PAYLOAD_MISMATCH = "SERVER_SESSION_REGISTRY_PAYLOAD_MISMATCH"

EndpointDecisionStatus = Literal["accepted", "rejected", "under_review"]


@dataclass(frozen=True, slots=True)
class InternalAllowlistAdapter:
    allowed_passports: tuple[str, ...] = ()
    allowed_devices: tuple[str, ...] = ()
    allowed_pools: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        for field_name, values in (
            ("allowed_passports", self.allowed_passports),
            ("allowed_devices", self.allowed_devices),
            ("allowed_pools", self.allowed_pools),
        ):
            if not values:
                raise ValueError(f"{field_name} must be non-empty")
            for value in values:
                _validate_public_id(field_name, value)


@dataclass(frozen=True, slots=True)
class AllowlistDecision:
    allowed: bool
    reason_code: str


@dataclass(frozen=True, slots=True)
class MiningServerConfig:
    gate: InternalMiningTestGate
    wallet_policy: CollectionWalletPolicy
    pool_profile: TestPoolProfile
    allowlist: InternalAllowlistAdapter
    kill_switch: KillSwitchPolicy = field(default_factory=KillSwitchPolicy)
    require_session_registry_verification: bool = False
    session_request_registry: PassportPublicKeyRegistryProtocol | None = None
    session_request_replay_store: SessionNonceReplayStoreProtocol | None = None
    session_request_verification_policy: SessionRequestVerificationPolicy = field(
        default_factory=SessionRequestVerificationPolicy
    )
    session_ttl: timedelta = timedelta(minutes=30)
    route_policy_version: str = "server-mining-route-policy-v1"
    session_policy_version: str = "server-mining-session-policy-v1"
    signer_ref: str = "approval://alice-mining/server-session-signer"
    signature_ref_prefix: str = "approval://alice-mining/server-session-signature"

    def __post_init__(self) -> None:
        if self.session_ttl.total_seconds() <= 0:
            raise ValueError("session_ttl must be positive")
        for field_name, value in (
            ("route_policy_version", self.route_policy_version),
            ("session_policy_version", self.session_policy_version),
            ("signer_ref", self.signer_ref),
            ("signature_ref_prefix", self.signature_ref_prefix),
        ):
            _validate_public_id(field_name, value)


@dataclass(frozen=True, slots=True)
class MiningSessionIssueRequest:
    miner_passport_id: str
    device_id: str
    wallet_signature_payload: MinerIdentitySignaturePayload | None
    backend_capability: BackendCapabilityResult
    requested_algorithm: MiningAlgorithm = RVN_KAWPOW
    requested_pool_id: str = ""
    requested_mode: MiningMode = ALICE_REWARDED_MINING_MODE
    requested_at: datetime | None = None
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False
    miner_provided_payout_address: str | None = None
    session_request_payload: MinerSessionRequestPayload | Mapping[str, object] | None = None
    session_request_payload_hash: str | None = None
    session_request_signature: MinerSessionRequestSignature | Mapping[str, object] | None = None
    session_request_verification_result: MinerSessionRequestVerificationResult | None = None

    def __post_init__(self) -> None:
        for field_name, value in (
            ("miner_passport_id", self.miner_passport_id),
            ("device_id", self.device_id),
            ("requested_pool_id", self.requested_pool_id),
        ):
            _validate_public_id(field_name, value)
        if self.requested_at is not None:
            validate_aware_timestamp("requested_at", self.requested_at)
        if self.miner_provided_payout_address is not None:
            ensure_no_raw_secret(
                self.miner_provided_payout_address,
                field_name="miner_provided_payout_address",
            )
        if self.session_request_payload_hash is not None:
            validate_sha256(
                self.session_request_payload_hash,
                field_name="session_request_payload_hash",
            )


@dataclass(frozen=True, slots=True)
class MiningSessionIssueResult:
    status: EndpointDecisionStatus
    reason_code: str
    session: SignedMiningSession | None = None
    worker_id: str | None = None
    alice_collection_address: str | None = None
    expires_at: datetime | None = None
    policy_version: str | None = None
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False
    identity_payload_digest: str | None = None
    session_request_payload_hash: str | None = None
    session_request_key_id: str | None = None
    session_request_registry_revision: str | None = None

    @property
    def accepted(self) -> bool:
        return self.status == "accepted"


@dataclass(frozen=True, slots=True)
class IssuedMiningSessionContext:
    session: SignedMiningSession
    device_id: str
    backend: str
    identity_payload_digest: str
    session_request_payload_hash: str | None = None
    session_request_key_id: str | None = None
    session_request_registry_revision: str | None = None


@dataclass(frozen=True, slots=True)
class ProofIngestRequest:
    session: SignedMiningSession
    proof: MiningShareProof
    observed_at: datetime
    pool_evidence_snapshot: object | None = None

    def __post_init__(self) -> None:
        validate_aware_timestamp("observed_at", self.observed_at)


@dataclass(frozen=True, slots=True)
class ProofIngestResult:
    status: EndpointDecisionStatus
    canonical_share_hash: str
    rewardable_acu_shadow: Decimal
    reason_code: str
    shadow_record: ShadowMiningRecord | None = None
    paid_acu: Decimal = Decimal("0")

    @property
    def accepted(self) -> bool:
        return self.status == "accepted"


@dataclass(frozen=True, slots=True)
class ShadowMiningRecord:
    session_id: str
    passport_id: str
    device_id: str
    canonical_share_hash: str
    mining_acu: Decimal
    reason_code: str
    recorded_at: datetime
    live_reward_enabled: bool = False
    paid_acu: Decimal = Decimal("0")

    def __post_init__(self) -> None:
        validate_aware_timestamp("recorded_at", self.recorded_at)
        for field_name, value in (
            ("session_id", self.session_id),
            ("passport_id", self.passport_id),
            ("device_id", self.device_id),
            ("canonical_share_hash", self.canonical_share_hash),
            ("reason_code", self.reason_code),
        ):
            _validate_public_id(field_name, value)
        if self.mining_acu < Decimal("0"):
            raise ValueError("mining_acu must be non-negative")
        if self.live_reward_enabled:
            raise ValueError(SERVER_LIVE_REWARD_FORBIDDEN)
        if self.paid_acu != Decimal("0"):
            raise ValueError("paid_acu must remain zero")


def _validate_public_id(field_name: str, value: str) -> None:
    if not value:
        raise ValueError(f"{field_name} must be non-empty")
    ensure_no_raw_secret(value, field_name=field_name)

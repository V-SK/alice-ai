from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from alice_acp.mining_accounting.acu import estimate_mining_acu
from alice_acp.mining_accounting.types import ZERO_ACU, MiningAcuInput
from alice_acp.mining_identity.public_key_registry import (
    MinerSessionRequestPayload,
    MinerSessionRequestVerificationResult,
    verify_authoritative_miner_session_request,
)
from alice_acp.mining_identity.signatures import LocalNonceVerifier
from alice_acp.mining_internal_test.gates import evaluate_internal_test_gate
from alice_acp.mining_internal_test.kill_switch import evaluate_kill_switch
from alice_acp.mining_internal_test.pool_profile import evaluate_test_pool_profile
from alice_acp.mining_internal_test.types import (
    KILL_SWITCH_ALLOW,
    KillSwitchPolicy,
)
from alice_acp.mining_internal_test.wallet_policy import evaluate_collection_wallet_policy
from alice_acp.mining_proofs.canonical import canonical_share_hash
from alice_acp.mining_proofs.collector import StrongMiningProofCollector
from alice_acp.mining_proofs.cross_check import (
    PoolEvidenceSnapshot,
    cross_check_pool_evidence,
)
from alice_acp.mining_proofs.difficulty_baseline import (
    DifficultyBaselineStore,
    InMemoryDifficultyBaselineStore,
    worker_baseline_key,
)
from alice_acp.mining_server.contracts import (
    accepted_share_from_mining_proof,
    evaluate_internal_allowlist,
    issue_signed_mining_session,
)
from alice_acp.mining_server.shadow_store import InMemoryShadowMiningStore
from alice_acp.mining_server.types import (
    SERVER_ALGORITHM_UNSUPPORTED,
    SERVER_BACKEND_UNSUPPORTED,
    SERVER_COLLECTION_ADDRESS_REQUIRED,
    SERVER_DIRECT_POOL_MODE_REJECTED,
    SERVER_KILL_SWITCH_BLOCKED,
    SERVER_LIVE_REWARD_FORBIDDEN,
    SERVER_MINER_PAYOUT_ADDRESS_REJECTED,
    SERVER_PAYOUT_EXECUTOR_FORBIDDEN,
    SERVER_POOL_CROSS_CHECK_REJECTED,
    SERVER_POOL_CROSS_CHECK_UNDER_REVIEW,
    SERVER_POOL_EVIDENCE_SNAPSHOT_REQUIRED,
    SERVER_POOL_PROFILE_MISMATCH,
    SERVER_PROOF_ACCEPTED,
    SERVER_PROOF_REJECTED,
    SERVER_PROOF_UNDER_REVIEW,
    SERVER_SESSION_ISSUED,
    SERVER_SESSION_REGISTRY_ADAPTER_REQUIRED,
    SERVER_SESSION_REGISTRY_PAYLOAD_HASH_REQUIRED,
    SERVER_SESSION_REGISTRY_PAYLOAD_MISMATCH,
    SERVER_SESSION_REGISTRY_SIGNATURE_REQUIRED,
    SERVER_SESSION_REGISTRY_VERIFICATION_REQUIRED,
    SERVER_SESSION_UNKNOWN,
    SERVER_WALLET_SIGNATURE_PASSPORT_MISMATCH,
    SERVER_WALLET_SIGNATURE_REQUIRED,
    IssuedMiningSessionContext,
    MiningServerConfig,
    MiningSessionIssueRequest,
    MiningSessionIssueResult,
    ProofIngestRequest,
    ProofIngestResult,
    ShadowMiningRecord,
)
from alice_acp.mining_session.types import ALICE_REWARDED_MINING_MODE, RVN_KAWPOW


@dataclass(slots=True)
class MiningServerEndpointHarness:
    config: MiningServerConfig
    nonce_verifier: LocalNonceVerifier = field(default_factory=LocalNonceVerifier)
    shadow_store: InMemoryShadowMiningStore = field(default_factory=InMemoryShadowMiningStore)
    issued_sessions: dict[str, IssuedMiningSessionContext] = field(default_factory=dict)
    collectors: dict[str, StrongMiningProofCollector] = field(default_factory=dict)
    # Phase F (H4): one baseline store shared across every session this endpoint
    # issues, keyed per (pool, worker). A new session seeds its collector from the
    # persisted baseline, so rotating sessions can no longer reset the
    # difficulty-anomaly baseline. Default is process-lifetime in-memory; a durable
    # JsonlDifficultyBaselineStore can be injected for restart durability.
    difficulty_baseline_store: DifficultyBaselineStore = field(
        default_factory=InMemoryDifficultyBaselineStore
    )

    def replace_kill_switch(self, policy: KillSwitchPolicy) -> None:
        self.config = MiningServerConfig(
            gate=self.config.gate,
            wallet_policy=self.config.wallet_policy,
            pool_profile=self.config.pool_profile,
            allowlist=self.config.allowlist,
            kill_switch=policy,
            require_session_registry_verification=(
                self.config.require_session_registry_verification
            ),
            session_request_registry=self.config.session_request_registry,
            session_request_replay_store=self.config.session_request_replay_store,
            session_request_verification_policy=(
                self.config.session_request_verification_policy
            ),
            session_ttl=self.config.session_ttl,
            route_policy_version=self.config.route_policy_version,
            session_policy_version=self.config.session_policy_version,
            signer_ref=self.config.signer_ref,
            signature_ref_prefix=self.config.signature_ref_prefix,
        )

    def issue_session(self, request: MiningSessionIssueRequest) -> MiningSessionIssueResult:
        observed_at = request.requested_at or _now_utc()
        if request.live_reward_enabled:
            return _session_rejected(SERVER_LIVE_REWARD_FORBIDDEN)
        if request.payout_executor_enabled:
            return _session_rejected(SERVER_PAYOUT_EXECUTOR_FORBIDDEN)
        if request.miner_provided_payout_address is not None:
            return _session_rejected(SERVER_MINER_PAYOUT_ADDRESS_REJECTED)
        if request.requested_mode != ALICE_REWARDED_MINING_MODE:
            return _session_rejected(SERVER_DIRECT_POOL_MODE_REJECTED)
        if request.requested_algorithm != RVN_KAWPOW:
            return _session_rejected(SERVER_ALGORITHM_UNSUPPORTED)
        if not request.backend_capability.mining_supported:
            return _session_rejected(SERVER_BACKEND_UNSUPPORTED)
        if request.wallet_signature_payload is None:
            return _session_rejected(SERVER_WALLET_SIGNATURE_REQUIRED)
        if request.wallet_signature_payload.miner_passport_id != request.miner_passport_id:
            return _session_rejected(SERVER_WALLET_SIGNATURE_PASSPORT_MISMATCH)

        gate_decision = evaluate_internal_test_gate(
            self.config.gate,
            passport_id=request.miner_passport_id,
            device_id=request.device_id,
            pool_id=request.requested_pool_id,
            require_real_pool=False,
        )
        if not gate_decision.allowed:
            return _session_rejected(gate_decision.reason_code)

        pool_decision = evaluate_test_pool_profile(
            self.config.pool_profile,
            gate=self.config.gate,
        )
        if not pool_decision.ready:
            return _session_rejected(pool_decision.reason_code)
        if request.requested_pool_id != self.config.pool_profile.pool_id:
            return _session_rejected(SERVER_POOL_PROFILE_MISMATCH)

        nonce_decision = self.nonce_verifier.verify_once(
            request.wallet_signature_payload,
            observed_at=observed_at,
        )
        if not nonce_decision.accepted:
            return _session_rejected(nonce_decision.reason_code or SERVER_WALLET_SIGNATURE_REQUIRED)

        registry_rejection, registry_verification = _verify_session_request_registry(
            self.config,
            request,
            observed_at=observed_at,
        )
        if registry_rejection is not None:
            return registry_rejection

        wallet_decision = evaluate_collection_wallet_policy(
            self.config.wallet_policy,
            require_real_address=True,
        )
        if not wallet_decision.ready or self.config.wallet_policy.collection_address is None:
            reason_code = wallet_decision.reason_code or SERVER_COLLECTION_ADDRESS_REQUIRED
            return _session_rejected(reason_code)

        allowlist = evaluate_internal_allowlist(
            self.config.allowlist,
            passport_id=request.miner_passport_id,
            device_id=request.device_id,
            pool_id=request.requested_pool_id,
        )
        if not allowlist.allowed:
            return _session_rejected(allowlist.reason_code)

        kill = evaluate_kill_switch(
            self.config.kill_switch,
            pool_id=request.requested_pool_id,
            passport_id=request.miner_passport_id,
            device_id=request.device_id,
        )
        if kill.blocked:
            return _session_rejected(f"{SERVER_KILL_SWITCH_BLOCKED}:{kill.reason_code}")

        expires_at = observed_at + self.config.session_ttl
        session, identity_digest = issue_signed_mining_session(
            passport_id=request.miner_passport_id,
            device_id=request.device_id,
            pool_id=request.requested_pool_id,
            alice_collection_address=self.config.wallet_policy.collection_address,
            identity_payload=request.wallet_signature_payload,
            issued_at=observed_at,
            expires_at=expires_at,
            route_policy_version=self.config.route_policy_version,
            session_policy_version=self.config.session_policy_version,
            signer_ref=self.config.signer_ref,
            signature_ref_prefix=self.config.signature_ref_prefix,
        )
        self.issued_sessions[session.session_id] = IssuedMiningSessionContext(
            session=session,
            device_id=request.device_id,
            backend=request.backend_capability.backend,
            identity_payload_digest=identity_digest,
            session_request_payload_hash=(
                registry_verification.payload_hash if registry_verification else None
            ),
            session_request_key_id=(
                registry_verification.key_id if registry_verification else None
            ),
            session_request_registry_revision=(
                registry_verification.registry_revision if registry_verification else None
            ),
        )
        self.collectors[session.session_id] = StrongMiningProofCollector(
            session,
            baseline_store=self.difficulty_baseline_store,
            baseline_key=worker_baseline_key(
                pool_id=session.pool_id,
                worker_id=session.worker_id,
            ),
        )
        return MiningSessionIssueResult(
            status="accepted",
            reason_code=SERVER_SESSION_ISSUED,
            session=session,
            worker_id=session.worker_id,
            alice_collection_address=session.alice_collection_address,
            expires_at=session.expires_at,
            policy_version=session.session_policy_version,
            live_reward_enabled=False,
            payout_executor_enabled=False,
            identity_payload_digest=identity_digest,
            session_request_payload_hash=(
                registry_verification.payload_hash if registry_verification else None
            ),
            session_request_key_id=(
                registry_verification.key_id if registry_verification else None
            ),
            session_request_registry_revision=(
                registry_verification.registry_revision if registry_verification else None
            ),
        )

    def ingest_proof(self, request: ProofIngestRequest) -> ProofIngestResult:
        share_hash = canonical_share_hash(request.proof)
        context = self.issued_sessions.get(request.session.session_id)
        if context is None:
            return _proof_rejected(share_hash, SERVER_SESSION_UNKNOWN)

        kill = evaluate_kill_switch(
            self.config.kill_switch,
            pool_id=request.session.pool_id,
            passport_id=request.session.passport_id,
            device_id=context.device_id,
        )
        if kill.blocked:
            return _proof_rejected(share_hash, f"{SERVER_KILL_SWITCH_BLOCKED}:{kill.reason_code}")

        if not isinstance(request.pool_evidence_snapshot, PoolEvidenceSnapshot):
            return _proof_under_review(
                share_hash,
                f"{SERVER_POOL_CROSS_CHECK_UNDER_REVIEW}:"
                f"{SERVER_POOL_EVIDENCE_SNAPSHOT_REQUIRED}",
            )

        cross_check_result = cross_check_pool_evidence(
            request.proof,
            request.pool_evidence_snapshot,
        )
        if cross_check_result.status == "rejected":
            return _proof_rejected(
                share_hash,
                f"{SERVER_POOL_CROSS_CHECK_REJECTED}:{cross_check_result.reason_code}",
            )
        if cross_check_result.status == "under_review":
            return _proof_under_review(
                share_hash,
                f"{SERVER_POOL_CROSS_CHECK_UNDER_REVIEW}:{cross_check_result.reason_code}",
            )

        collector = self.collectors[request.session.session_id]
        collection = collector.collect(request.proof, observed_at=request.observed_at)
        if not collection.accepted:
            if collection.status == "under_review":
                return _proof_under_review(share_hash, collection.reason_code)
            return _proof_rejected(share_hash, collection.reason_code)

        estimate = estimate_mining_acu(
            MiningAcuInput(
                proof=accepted_share_from_mining_proof(request.proof),
                pool_validity="pool_accepted",
            )
        )
        if not estimate.rewardable:
            return _proof_rejected(share_hash, estimate.reason_code)

        record = ShadowMiningRecord(
            session_id=request.session.session_id,
            passport_id=request.session.passport_id,
            device_id=context.device_id,
            canonical_share_hash=share_hash,
            mining_acu=estimate.mining_acu,
            reason_code=SERVER_PROOF_ACCEPTED,
            recorded_at=request.observed_at,
            live_reward_enabled=False,
            paid_acu=ZERO_ACU,
        )
        stored = self.shadow_store.add_record_once(record)
        return ProofIngestResult(
            status="accepted",
            canonical_share_hash=share_hash,
            rewardable_acu_shadow=stored.mining_acu,
            reason_code=SERVER_PROOF_ACCEPTED,
            shadow_record=stored,
            paid_acu=ZERO_ACU,
        )


def build_kill_switch_policy(
    *,
    global_stop: bool = False,
    disabled_pool_id: str | None = None,
    disabled_passport_id: str | None = None,
    disabled_device_id: str | None = None,
    reason_code: str,
) -> KillSwitchPolicy:
    return KillSwitchPolicy(
        global_stop=global_stop,
        disabled_pool_ids=(disabled_pool_id,) if disabled_pool_id else (),
        disabled_passport_ids=(disabled_passport_id,) if disabled_passport_id else (),
        disabled_device_ids=(disabled_device_id,) if disabled_device_id else (),
        reason_code=reason_code,
        audit_payload={"source": "mining_server_contract"},
    )


def allow_kill_switch_policy() -> KillSwitchPolicy:
    return KillSwitchPolicy(reason_code=KILL_SWITCH_ALLOW)


def _session_rejected(reason_code: str) -> MiningSessionIssueResult:
    return MiningSessionIssueResult(status="rejected", reason_code=reason_code)


def _verify_session_request_registry(
    config: MiningServerConfig,
    request: MiningSessionIssueRequest,
    *,
    observed_at: datetime,
) -> tuple[MiningSessionIssueResult | None, MinerSessionRequestVerificationResult | None]:
    if not config.require_session_registry_verification:
        return None, None

    preverified = request.session_request_verification_result
    if preverified is not None:
        if not preverified.accepted:
            return (
                _session_rejected(
                    preverified.reason_code or SERVER_SESSION_REGISTRY_VERIFICATION_REQUIRED
                ),
                None,
            )
        if (
            preverified.passport_id != request.miner_passport_id
            or preverified.device_id != request.device_id
            or preverified.payload_hash is None
            or preverified.key_id is None
        ):
            return _session_rejected(SERVER_SESSION_REGISTRY_PAYLOAD_MISMATCH), None
        return None, preverified

    if request.session_request_payload is None:
        return _session_rejected(SERVER_SESSION_REGISTRY_VERIFICATION_REQUIRED), None
    if request.session_request_payload_hash is None:
        return _session_rejected(SERVER_SESSION_REGISTRY_PAYLOAD_HASH_REQUIRED), None
    if request.session_request_signature is None:
        return _session_rejected(SERVER_SESSION_REGISTRY_SIGNATURE_REQUIRED), None
    if config.session_request_registry is None:
        return _session_rejected(SERVER_SESSION_REGISTRY_ADAPTER_REQUIRED), None

    try:
        payload = (
            request.session_request_payload
            if isinstance(request.session_request_payload, MinerSessionRequestPayload)
            else MinerSessionRequestPayload.from_mapping(request.session_request_payload)
        )
    except ValueError as exc:
        return _session_rejected(str(exc)), None

    if (
        payload.passport_id != request.miner_passport_id
        or payload.device_id != request.device_id
        or payload.requested_algorithm != request.requested_algorithm
        or payload.requested_pool_id != request.requested_pool_id
    ):
        return _session_rejected(SERVER_SESSION_REGISTRY_PAYLOAD_MISMATCH), None

    verification = verify_authoritative_miner_session_request(
        payload,
        payload_hash=request.session_request_payload_hash,
        signature=request.session_request_signature,
        registry=config.session_request_registry,
        replay_store=config.session_request_replay_store,
        observed_at=observed_at,
        policy=config.session_request_verification_policy,
    )
    if not verification.accepted:
        return (
            _session_rejected(
                verification.reason_code or SERVER_SESSION_REGISTRY_VERIFICATION_REQUIRED
            ),
            None,
        )
    if (
        verification.passport_id != request.miner_passport_id
        or verification.device_id != request.device_id
        or verification.payload_hash != request.session_request_payload_hash
    ):
        return _session_rejected(SERVER_SESSION_REGISTRY_PAYLOAD_MISMATCH), None

    return None, verification


def _proof_rejected(share_hash: str, reason_code: str) -> ProofIngestResult:
    return ProofIngestResult(
        status="rejected",
        canonical_share_hash=share_hash,
        rewardable_acu_shadow=ZERO_ACU,
        reason_code=reason_code or SERVER_PROOF_REJECTED,
        paid_acu=ZERO_ACU,
    )


def _proof_under_review(share_hash: str, reason_code: str) -> ProofIngestResult:
    return ProofIngestResult(
        status="under_review",
        canonical_share_hash=share_hash,
        rewardable_acu_shadow=ZERO_ACU,
        reason_code=reason_code or SERVER_PROOF_UNDER_REVIEW,
        paid_acu=ZERO_ACU,
    )


def _now_utc() -> datetime:
    return datetime.now(UTC)

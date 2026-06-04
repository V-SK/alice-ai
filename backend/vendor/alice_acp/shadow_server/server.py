from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from alice_acp.mining_proofs.collector import (
    DEFAULT_DIFFICULTY_JUMP_FACTOR,
    DEFAULT_MAX_FUTURE_SKEW,
    DEFAULT_MINIMUM_SHARE_DIFFICULTY,
)
from alice_acp.mining_proofs.types import MiningShareProof
from alice_acp.mining_session.contracts import signature_envelope_for_session_fields
from alice_acp.mining_session.types import (
    LTC_SCRYPT,
    RVN_KAWPOW,
    XMR_RANDOMX,
    MiningAlgorithm,
    SignedMiningSession,
)
from alice_acp.shadow_server.ledger import ShadowRewardLedger
from alice_acp.shadow_server.mining_authority_bridge import (
    NO_POOL_EVIDENCE_PROVIDER,
    PoolEvidenceProvider,
    authority_result_for_proof,
)
from alice_acp.shadow_server.types import (
    MAIN_POOL_GPU_PRL,
    MAIN_POOL_GPU_QUAI,
    MAIN_POOL_GPU_RVN,
    REASON_HEARTBEAT_RECORDED,
    REASON_KILL_SWITCH,
    SCRYPT_POOL,
    XMR_POOL,
    ZERO_DECIMAL,
    InferenceCompletionRequest,
    Lane,
    MiningProofIngestRequest,
    ProofIngestResult,
    SessionIssueResult,
    SettlementResult,
    SettlementWindow,
    ShadowBalance,
    ShadowHeartbeatRequest,
    ShadowSession,
    ShadowSessionIssueRequest,
)

#: Cap on how many accepted-share units one ``credit_attested_shares`` call will
#: spend in a single poll for a single worker. The provider's cursor store already
#: bounds credit to the server-read pool delta and returns ``None`` once exhausted
#: (the loop's real terminator); this is a defensive belt against an unexpectedly
#: huge delta wedging one tick. Generous enough never to clip a legitimate cadence.
MAX_SHARES_PER_POLL = 4096

#: Deterministic placeholder difficulty for a reconstructed accepted share. The
#: SERVER does not learn each share's true difficulty from a pool's per-worker
#: accepted-COUNT endpoint (it exposes counts, not per-share difficulty), so the
#: reconstructed proof carries a fixed, positive, target-meeting difficulty that
#: satisfies the authority floor. This sets the ledger ``rewardable_score`` per
#: accepted share to a uniform 1 unit — i.e. each pool-attested accepted share is
#: weighted equally. Milestone 0 (doc §2.4): ``credit_attested_shares`` /
#: ``_reconstruct_share_proof`` now accept a ``share_difficulty`` argument so a
#: caller backed by Alice's OWN validator (the ``ProxyPoolEvidenceProvider`` /
#: ``SelfValidatedShareAuthority`` path) can carry the REAL validated
#: ``share_difficulty`` through instead of this flat unit; the default stays the
#: flat unit for backward compatibility with the count-only upstream-poll lanes.
RECONSTRUCTED_SHARE_DIFFICULTY = Decimal("1")
RECONSTRUCTED_TARGET_DIFFICULTY = Decimal("1")


#: Milestone 0 (D1): map a credit lane to its mining algorithm. ``_reconstruct_session``
#: / ``_reconstruct_share_proof`` derive the algorithm from the lane/target instead of
#: hardcoding ``RVN_KAWPOW`` so the XMR (RandomX) and LTC (Scrypt) share-hash legs
#: reconstruct with the right algorithm once the algorithm Literal is widened.
_LANE_ALGORITHM: dict[str, MiningAlgorithm] = {
    MAIN_POOL_GPU_RVN: RVN_KAWPOW,
    # The Quai lane is KawPoW — same RVN_KAWPOW algorithm + verifier as RVN (the
    # crypto is identical; reuse is keyed by algorithm, not a new constant).
    MAIN_POOL_GPU_QUAI: RVN_KAWPOW,
    # The PRL (pearlhash/PoUW) GPU lane reconstructs with RVN_KAWPOW too: the algorithm
    # is INERT on the PRL epoch-credit path (the PRL provider matches by pool_id and the
    # epoch authority gate checks no algorithm), and PRL credit previously rode the
    # ``main_pool_gpu_rvn`` lane which already resolved here to RVN_KAWPOW — so the
    # reconstructed proof is byte-for-byte what it was before PRL got its own lane.
    MAIN_POOL_GPU_PRL: RVN_KAWPOW,
    XMR_POOL: XMR_RANDOMX,
    SCRYPT_POOL: LTC_SCRYPT,
}


def _algorithm_for_lane(lane: Lane) -> MiningAlgorithm:
    """Derive the share-hash lane's mining algorithm (doc §2.4 / D1).

    XMR (``xmr_pool``) → ``XMR_RANDOMX``; LTC (``scrypt_pool``) → ``LTC_SCRYPT``;
    every GPU lane (``main_pool_gpu_rvn`` / ``main_pool_gpu_quai`` / the PRL
    ``main_pool_gpu_prl`` epoch lane) → ``RVN_KAWPOW``. The default also returns
    ``RVN_KAWPOW`` for any unmapped GPU lane, so the PRL epoch path and the existing
    RVN lane are byte-for-byte unchanged.
    """

    return _LANE_ALGORITHM.get(lane, RVN_KAWPOW)


@dataclass(frozen=True, slots=True)
class CreditAttestedSharesSummary:
    """Count summary for one :meth:`ShadowServerHarness.credit_attested_shares`.

    CREDIT-ONLY: ``paid_acu`` is fixed ``"0"`` (asserted by the harness against
    every produced ledger result). ``shares_credited`` is how many accepted-share
    units the server read from the pool delta and recorded as
    :class:`ShadowWorkRecord`s on this poll; ``shares_seen`` is how many
    reconstruct/ingest attempts ran (== credited + the final non-crediting attempt
    that detected the delta was exhausted).
    """

    worker_name: str
    shares_credited: int = 0
    shares_seen: int = 0
    paid_acu: str = "0"

    def __post_init__(self) -> None:
        assert self.paid_acu == "0"


@dataclass(slots=True)
class ShadowServerHarness:
    ledger: ShadowRewardLedger = field(default_factory=ShadowRewardLedger)
    # Phase H_b: the SERVER-side pool-evidence source used to evaluate mining
    # proof authority. Defaults to the fail-closed NoPoolEvidenceProvider (no
    # evidence => every proof stays under_review), so an unconfigured deployment
    # is fail-closed. The deployed edge wires a real LaneRoutingPoolEvidenceProvider
    # (alice_acp.shadow_server.pool_evidence_providers) that polls each lane's pool
    # API server-side via an injectable HTTP client. CREDIT-ONLY throughout.
    evidence_provider: PoolEvidenceProvider = NO_POOL_EVIDENCE_PROVIDER

    def health(self) -> dict[str, object]:
        return {
            "ok": True,
            "service": "queue13s-shadow-server-harness",
            "staging_internal_only": True,
            "live_reward_enabled": False,
            "payout_executor_enabled": False,
        }

    def session_issue(self, request: ShadowSessionIssueRequest) -> SessionIssueResult:
        return self.ledger.issue_session(request)

    def proof_ingest(self, request: MiningProofIngestRequest) -> ProofIngestResult:
        return self.ledger.ingest_mining_proof(request)

    def proof_ingest_with_authority(
        self,
        *,
        proof_id: str,
        lane: Lane,
        session_signature: str,
        session: SignedMiningSession,
        proof: MiningShareProof,
        observed_at: datetime,
        evidence_provider: PoolEvidenceProvider | None = None,
        accepted_count: int = 1,
        rejected_count: int = 0,
        previous_share_difficulty: Decimal | None = None,
        minimum_share_difficulty: Decimal = DEFAULT_MINIMUM_SHARE_DIFFICULTY,
        difficulty_jump_factor: Decimal = DEFAULT_DIFFICULTY_JUMP_FACTOR,
        max_future_skew: timedelta = DEFAULT_MAX_FUTURE_SKEW,
    ) -> ProofIngestResult:
        """Close the server-side mining credit loop for a reconstructed proof.

        This is the Phase B mining-proof path: the server evaluates proof
        authority itself (via the bridge, which delegates to the authoritative
        evaluator), populates ``authority_result`` + ``canonical_share_hash`` +
        ``pool_evidence_ref`` onto a :class:`MiningProofIngestRequest`, and then
        hands it to :meth:`ShadowRewardLedger.ingest_mining_proof`, which
        re-applies every ledger guard before recording credit.

        CREDIT-ONLY: no reward/payout/chain flag is set anywhere in this path and
        ``paid_acu`` stays ``"0"``. When the evidence provider yields no
        authoritative evidence (the default :class:`NoPoolEvidenceProvider`), the
        authority result is non-accepted and the ledger keeps the proof
        ``under_review`` -- it is never auto-passed.
        """

        return self._evaluate_and_ingest(
            proof_id=proof_id,
            lane=lane,
            session_signature=session_signature,
            session=session,
            proof=proof,
            observed_at=observed_at,
            evidence_provider=evidence_provider,
            accepted_count=accepted_count,
            rejected_count=rejected_count,
            previous_share_difficulty=previous_share_difficulty,
            minimum_share_difficulty=minimum_share_difficulty,
            difficulty_jump_factor=difficulty_jump_factor,
            max_future_skew=max_future_skew,
        )

    def _evaluate_and_ingest(
        self,
        *,
        proof_id: str,
        lane: Lane,
        session_signature: str,
        session: SignedMiningSession,
        proof: MiningShareProof,
        observed_at: datetime,
        evidence_provider: PoolEvidenceProvider | None,
        accepted_count: int = 1,
        rejected_count: int = 0,
        previous_share_difficulty: Decimal | None = None,
        minimum_share_difficulty: Decimal = DEFAULT_MINIMUM_SHARE_DIFFICULTY,
        difficulty_jump_factor: Decimal = DEFAULT_DIFFICULTY_JUMP_FACTOR,
        max_future_skew: timedelta = DEFAULT_MAX_FUTURE_SKEW,
    ) -> ProofIngestResult:
        """Shared reconstruct-evidence-evaluate-ingest tail.

        Both :meth:`proof_ingest_with_authority` and :meth:`credit_attested_shares`
        funnel through this single body so the authority evaluation + every ledger
        guard runs in EXACTLY ONE place: evaluate authority via the bridge
        (delegating to the authoritative evaluator + the pool-evidence provider),
        then hand the populated :class:`MiningProofIngestRequest` to
        :meth:`ShadowRewardLedger.ingest_mining_proof`, which re-applies every guard
        (proof-id dedup, session validation, observed-window clamp, lane match,
        authority gate, ``rewardable_score > 0``, canonical-hash + pool-evidence-ref
        dedup) before recording credit. CREDIT-ONLY: no reward/payout/chain symbol
        is set; ``paid_acu`` stays ``"0"``.
        """

        # Default to the harness-configured provider (fail-closed
        # NoPoolEvidenceProvider unless an operator wired a real one); an explicit
        # ``evidence_provider`` argument (e.g. the offline test shim) still wins.
        provider = evidence_provider if evidence_provider is not None else self.evidence_provider
        authority_result = authority_result_for_proof(
            session,
            proof,
            observed_at=observed_at,
            evidence_provider=provider,
            previous_share_difficulty=previous_share_difficulty,
            minimum_share_difficulty=minimum_share_difficulty,
            difficulty_jump_factor=difficulty_jump_factor,
            max_future_skew=max_future_skew,
        )
        request = MiningProofIngestRequest(
            proof_id=proof_id,
            session_id=session.session_id,
            session_signature=session_signature,
            lane=lane,
            pool_result=proof.pool_result,
            share_difficulty=proof.share_difficulty,
            accepted_count=accepted_count,
            rejected_count=rejected_count,
            observed_at=observed_at,
            algorithm=proof.algorithm,
            worker_id=proof.worker_id,
            canonical_share_hash=authority_result.canonical_share_hash,
            pool_evidence_ref=proof.pool_evidence_ref,
            authority_result=authority_result,
        )
        return self.ledger.ingest_mining_proof(request)

    def credit_attested_shares(
        self,
        *,
        lane: Lane,
        pool_id: str,
        alice_collection_address: str,
        worker_name: str,
        session: ShadowSession,
        observed_at: datetime,
        evidence_provider: PoolEvidenceProvider | None = None,
        share_difficulty: Decimal = RECONSTRUCTED_SHARE_DIFFICULTY,
        minimum_share_difficulty: Decimal = DEFAULT_MINIMUM_SHARE_DIFFICULTY,
        difficulty_jump_factor: Decimal = DEFAULT_DIFFICULTY_JUMP_FACTOR,
        max_future_skew: timedelta = DEFAULT_MAX_FUTURE_SKEW,
    ) -> CreditAttestedSharesSummary:
        """Approach B: credit a worker's un-spent pool accepted-share delta.

        Reconstructs a SERVER-TRUSTED :class:`SignedMiningSession` (algorithm
        derived from the LANE — ``xmr_pool``→``XMR_RANDOMX``,
        ``scrypt_pool``→``LTC_SCRYPT``, else ``RVN_KAWPOW``; doc §2.4 / D1;
        ``pool_id`` + collection address from the *arguments* — server-owned,
        NEVER a client body; ``worker_id`` ==
        ``session.worker_name``, the H_a/H_b correlation key; passport/session_id +
        the issued/expires window from ``session``). It then loops, reconstructing
        one :class:`MiningShareProof` per newly-attested accepted share with a
        DETERMINISTIC ``proof_id`` / ``share_nonce`` / ``share_hash`` derived from
        ``(pool_id, worker_name, cursor_index, observed_at)`` (the per-poll snapshot
        time ``observed_at`` is folded in so a fresh poll of a GROWN counter mints
        fresh identifiers and credits the new delta without colliding with the prior
        poll's records), and runs the SAME
        ``authority_result_for_proof -> ingest_mining_proof`` chain as
        :meth:`proof_ingest_with_authority` (via :meth:`_evaluate_and_ingest`).

        The provider's DURABLE ``(pool, worker, cursor)`` cursor store — NOT the
        per-poll proof_id — is what makes credit idempotent and stable across
        restarts: it SPENDS one cursor unit per ``evidence_for`` call and returns
        ``None`` once the server-read delta is exhausted, at which point the
        authority evaluator yields ``under_review`` and the ledger returns a
        NON-accepted result — the loop's terminator. So this credits EXACTLY the
        increment the server read from the pool (dedup by ``(pool, worker, cursor)``
        durably, plus proof_id + canonical-share within a poll), never a client
        claim, and is a no-op when there is no new delta. :data:`MAX_SHARES_PER_POLL`
        is a defensive upper bound only.

        CREDIT-ONLY: no reward/payout/chain symbol is set anywhere; every produced
        :class:`ProofIngestResult` is asserted ``paid_acu == ZERO_DECIMAL``.
        """

        rebuilt_session = self._reconstruct_session(
            lane=lane,
            pool_id=pool_id,
            alice_collection_address=alice_collection_address,
            worker_name=worker_name,
            session=session,
        )
        # CROSS-PROCESS CREDIT PLANE (doc §2.8): when this session was minted in a
        # DIFFERENT process (the transport service), it is not yet in THIS credit
        # ledger's ``sessions`` — so ``ingest_mining_proof`` would reject it
        # ``REASON_SESSION_UNKNOWN``. Admit it ONLY after re-verifying its HMAC
        # signature against this ledger's OWN signing secret (the shared auth-secret):
        # a forged/tampered/wrong-secret session fails verification, is NOT registered,
        # and every reconstructed proof below then rejects ``session_unknown`` (no
        # credit, fail-closed). A session natively issued here is already present, so
        # this is a no-op for the in-process path (byte-for-byte unchanged). It is
        # CREDIT-ONLY: admission never re-issues and sets no reward/payout/chain flag.
        if session.session_id not in self.ledger.sessions:
            self.ledger.admit_cross_process_session(session)
        # CROSS-PROCESS HASH ALIGNMENT (the credit-correctness seam): the active provider
        # is asked, per loop, whether the next un-spent unit has a FIXED reconstruction
        # ``observed_at`` to align to. The self-validated lane returns its peeked share's
        # ``validated_at`` (the exact instant the validator folded into the carried
        # ``canonical_share_hash`` at cursor 0), so reconstructing the proof at THAT
        # instant makes ``cross_check_self_validated_share``'s recompute-and-compare match
        # even though THIS process's tick clock differs from the transport's. The
        # upstream-poll / PRL lanes return ``None`` → the EXISTING cursor/poll path
        # (byte-for-byte unchanged). The provider may be the explicit arg or the
        # harness default; both are probed via getattr so providers without the hint
        # safely fall through to the unchanged path.
        provider_for_hint = (
            evidence_provider if evidence_provider is not None else self.evidence_provider
        )
        reconstruction_hint = getattr(
            provider_for_hint, "next_reconstruction_observed_at", lambda **_k: None
        )
        shares_credited = 0
        shares_seen = 0
        for cursor_index in range(MAX_SHARES_PER_POLL):
            hint = reconstruction_hint(pool_id=pool_id, worker_name=worker_name)
            if isinstance(hint, datetime):
                # Self-validated path: reconstruct at the validator's cursor 0 + its
                # exact observed_at so the canonical hash matches cross-process.
                proof_cursor_index = 0
                proof_observed_at = hint
            else:
                # Poll lanes: the unchanged per-cursor + poll-time reconstruction.
                proof_cursor_index = cursor_index
                proof_observed_at = observed_at
            proof = self._reconstruct_share_proof(
                session=rebuilt_session,
                lane=lane,
                pool_id=pool_id,
                alice_collection_address=alice_collection_address,
                worker_name=worker_name,
                cursor_index=proof_cursor_index,
                observed_at=proof_observed_at,
                share_difficulty=share_difficulty,
            )
            shares_seen += 1
            result = self._evaluate_and_ingest(
                proof_id=_deterministic_proof_id(
                    pool_id, worker_name, proof_cursor_index, proof_observed_at
                ),
                lane=lane,
                session_signature=session.signature,
                session=rebuilt_session,
                proof=proof,
                observed_at=observed_at,
                evidence_provider=evidence_provider,
                minimum_share_difficulty=minimum_share_difficulty,
                difficulty_jump_factor=difficulty_jump_factor,
                max_future_skew=max_future_skew,
            )
            # CREDIT-ONLY: the ledger result must always carry paid_acu == 0.
            if result.paid_acu != ZERO_DECIMAL:
                raise AssertionError("credit_attested_shares_paid_acu_must_be_zero")
            if not result.accepted:
                # Delta exhausted (provider returned no evidence => under_review),
                # or a within-poll guard re-rejected (e.g. proof-id dedup on a
                # re-poll at the same observed_at): stop. The non-accepted attempt
                # is the terminator, never credited.
                break
            shares_credited += 1
        return CreditAttestedSharesSummary(
            worker_name=worker_name,
            shares_credited=shares_credited,
            shares_seen=shares_seen,
        )

    @staticmethod
    def _reconstruct_session(
        *,
        lane: Lane,
        pool_id: str,
        alice_collection_address: str,
        worker_name: str,
        session: ShadowSession,
    ) -> SignedMiningSession:
        """Build a server-trusted ``SignedMiningSession`` from server-owned facts.

        ``pool_id`` + ``alice_collection_address`` come from the caller (the
        provider's configured per-lane values), NOT from any client body;
        ``worker_id`` is the SERVER-ASSIGNED ``session.worker_name``
        (``validate_mining_share_proof`` requires
        ``proof.worker_id == proof.pool_worker_name == session.worker_id``). The
        issued/expires window is taken from the issued :class:`ShadowSession`. The
        ``algorithm`` is derived from the LANE (doc §2.4 / D1), not hardcoded to
        ``RVN_KAWPOW``, so the XMR/RandomX and LTC/Scrypt legs reconstruct with the
        right algorithm.
        """

        if not session.worker_name:
            raise ValueError("credit_attested_shares_requires_server_assigned_worker_name")
        if worker_name != session.worker_name:
            raise ValueError("credit_attested_shares_worker_name_mismatch")
        fields: dict[str, object] = {
            "session_id": session.session_id,
            "passport_id": session.passport_id,
            "attempt_id": f"poll-{session.session_id}",
            "pool_id": pool_id,
            "algorithm": _algorithm_for_lane(lane),
            "alice_collection_address": alice_collection_address,
            "worker_id": worker_name,
            "issued_at": session.issued_at,
            "expires_at": session.expires_at,
            "route_policy_version": "alice-acp-proof-authority-poll-v1",
            "session_policy_version": "alice-acp-proof-authority-poll-v1",
            "mode": "ALICE_REWARDED_MINING",
        }
        return SignedMiningSession(
            signature=signature_envelope_for_session_fields(
                fields, signed_at=session.issued_at
            ),
            **fields,
        )

    @staticmethod
    def _reconstruct_share_proof(
        *,
        session: SignedMiningSession,
        lane: Lane,
        pool_id: str,
        alice_collection_address: str,
        worker_name: str,
        cursor_index: int,
        observed_at: datetime,
        share_difficulty: Decimal = RECONSTRUCTED_SHARE_DIFFICULTY,
    ) -> MiningShareProof:
        """Reconstruct one accepted-share proof for cursor unit ``cursor_index``.

        ``proof_id`` / ``share_nonce`` / ``share_hash`` / ``pool_evidence_ref`` are
        DETERMINISTIC functions of ``(pool_id, worker_name, cursor_index,
        observed_at)`` — i.e. the per-poll snapshot time ``observed_at`` is folded
        in so a fresh poll mints fresh identifiers (the durable ``(pool, worker,
        cursor)`` store, NOT proof-id dedup, is what makes credit idempotent /
        stable across restarts; see ``credit_attested_shares``). The proof's
        worker/pool/collection/session fields all mirror the reconstructed session
        (so ``validate_mining_share_proof`` passes), and the timestamps sit inside
        the session window and at/just-before ``observed_at`` (so the future/stale
        checks pass).

        Milestone 0 (doc §2.4 / D1): the ``algorithm`` is derived from the LANE
        (not hardcoded ``RVN_KAWPOW``), and ``share_difficulty`` is a parameter that
        defaults to the flat :data:`RECONSTRUCTED_SHARE_DIFFICULTY` for the
        count-only upstream-poll lanes but carries the REAL validated difficulty
        when a caller backed by Alice's own validator supplies it (so
        ``derive_rewardable_score``'s ``score = proof.share_difficulty`` branch
        weights credit by the real difficulty instead of the flat unit).
        """

        share_nonce = _deterministic_nonce(pool_id, worker_name, cursor_index, observed_at)
        share_hash = _deterministic_hash("share", pool_id, worker_name, cursor_index, observed_at)
        header_hash = _deterministic_hash(
            "header", pool_id, worker_name, cursor_index, observed_at
        )
        # Clamp the proof timestamps into [session.issued_at, observed_at] so both
        # the future-skew and the stale (>= issued_at) checks pass deterministically.
        accepted_at = observed_at if observed_at >= session.issued_at else session.issued_at
        return MiningShareProof(
            pool_id=pool_id,
            algorithm=_algorithm_for_lane(lane),
            pool_job_id=_deterministic_proof_id(pool_id, worker_name, cursor_index, observed_at),
            pool_worker_name=worker_name,
            session_id=session.session_id,
            passport_id=session.passport_id,
            worker_id=worker_name,
            alice_collection_address=alice_collection_address,
            share_nonce=share_nonce,
            share_hash=share_hash,
            header_hash=header_hash,
            share_difficulty=share_difficulty,
            target_difficulty=RECONSTRUCTED_TARGET_DIFFICULTY,
            submitted_at=accepted_at,
            accepted_at=accepted_at,
            pool_result="accepted",
            pool_evidence_ref=_deterministic_evidence_ref(
                pool_id, worker_name, cursor_index, observed_at
            ),
        )

    def inference_admit(self, request: ShadowSessionIssueRequest) -> SessionIssueResult:
        return self.ledger.issue_session(request, admit_inference_demand=True)

    def inference_complete(self, request: InferenceCompletionRequest) -> ProofIngestResult:
        return self.ledger.complete_inference(request)

    def heartbeat(self, request: ShadowHeartbeatRequest) -> dict[str, object]:
        if self.ledger.kill_switch_enabled:
            return {
                "accepted": False,
                "status": "rejected",
                "reason_code": REASON_KILL_SWITCH,
                "live_reward_enabled": False,
                "payout_executor_enabled": False,
            }
        return {
            "accepted": True,
            "status": "accepted",
            "reason_code": REASON_HEARTBEAT_RECORDED,
            "passport_id": request.passport_id,
            "device_id": request.device_id,
            "device_label": request.device_label,
            "supported_lanes": list(request.supported_lanes),
            "miner_status": request.status,
            "observed_at": request.observed_at.isoformat(),
            "live_reward_enabled": False,
            "payout_executor_enabled": False,
        }

    def shadow_window(self, window: SettlementWindow) -> SettlementResult:
        return self.ledger.settle_window(window)

    def shadow_balance(
        self,
        *,
        passport_id: str,
        device_id: str,
        window: SettlementWindow,
    ) -> ShadowBalance:
        return self.ledger.balance_for(passport_id, device_id, window)

    def admin_kill_switch(self, *, enabled: bool) -> dict[str, object]:
        self.ledger.set_kill_switch(enabled)
        return {"kill_switch_enabled": self.ledger.kill_switch_enabled}


# --- deterministic reconstruction helpers (Approach B) -----------------------
#
# Every reconstructed-share identifier is a pure function of
# ``(pool_id, worker_name, cursor_index, observed_at)`` — the per-poll snapshot
# time ``observed_at`` is folded in so a fresh poll mints fresh, collision-free
# identifiers (multiple polls of a GROWN counter each credit the new delta without
# tripping proof-id / canonical-share dedup on the prior poll's records). What
# makes credit idempotent and stable across restarts is the DURABLE
# ``(pool, worker, cursor)`` cursor store inside the provider, NOT these
# identifiers: it spends each pool accepted-share increment exactly once. The
# domain-separated tag prevents share_hash / header_hash collisions.


def _digest(*parts: str) -> str:
    payload = "\x1f".join(parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _poll_anchor(observed_at: datetime) -> str:
    return observed_at.isoformat()


def _deterministic_proof_id(
    pool_id: str, worker_name: str, cursor_index: int, observed_at: datetime
) -> str:
    return "alc-poll-" + _digest(
        "proof_id", pool_id, worker_name, str(cursor_index), _poll_anchor(observed_at)
    )[:32]


def _deterministic_nonce(
    pool_id: str, worker_name: str, cursor_index: int, observed_at: datetime
) -> str:
    return "alc-nonce-" + _digest(
        "share_nonce", pool_id, worker_name, str(cursor_index), _poll_anchor(observed_at)
    )[:32]


def _deterministic_hash(
    tag: str, pool_id: str, worker_name: str, cursor_index: int, observed_at: datetime
) -> str:
    # A full 64-hex sha256 (the MiningShareProof contract requires share_hash and
    # header_hash to be lowercase sha256 hex digests).
    return _digest(tag, pool_id, worker_name, str(cursor_index), _poll_anchor(observed_at))


def _deterministic_evidence_ref(
    pool_id: str, worker_name: str, cursor_index: int, observed_at: datetime
) -> str:
    ref_id = _digest(
        "evidence_ref", pool_id, worker_name, str(cursor_index), _poll_anchor(observed_at)
    )[:32]
    return f"evidence://alice-acp-proof-authority-poll/{ref_id}"

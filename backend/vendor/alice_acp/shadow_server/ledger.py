from __future__ import annotations

import hashlib
import hmac
import json
import secrets
import string
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from alice_acp.shadow_server.dedup_store import (
    InMemoryProofDedupStore,
    ProofDedupClaim,
    ProofDedupDecision,
    ProofDedupStore,
)
from alice_acp.shadow_server.demand_admission import (
    DemandAdmissionStore,
    DemandAdmissionStoreUnavailable,
    RejectingDemandAdmissionStore,
)
from alice_acp.shadow_server.device_pop import (
    REASON_DEVICE_POP_INVALID,
    REASON_DEVICE_POP_REQUIRED,
    REASON_ISSUANCE_RATE_LIMITED,
    DeviceRegistry,
    RejectingDeviceRegistry,
    verify_device_proof_of_possession,
)
from alice_acp.shadow_server.inference_acu import (
    InferenceAcuValidationError,
    bounded_simulated_api_payment,
    calculate_verified_inference_acu,
)
from alice_acp.shadow_server.issuance_nonce_store import (
    IssuanceNonceStore,
    IssuanceNonceStoreUnavailable,
)
from alice_acp.shadow_server.pool_budget import pool_budgets_for_window
from alice_acp.shadow_server.scoring import settle_reward_scores
from alice_acp.shadow_server.types import (
    DEFAULT_MAX_OBSERVED_BACKDATE,
    DEFAULT_MAX_OBSERVED_SKEW,
    DEFAULT_TOTAL_WINDOW_EMISSION,
    MAIN_POOL_AI,
    REASON_AI_DEMAND_NOT_ADMITTED,
    REASON_AUTHORITY_REJECTED,
    REASON_AUTHORITY_REQUIRED,
    REASON_AUTHORITY_SCORE_REQUIRED,
    REASON_AUTHORITY_UNDER_REVIEW,
    REASON_CANONICAL_SHARE_HASH_INVALID,
    REASON_CANONICAL_SHARE_HASH_MISMATCH,
    REASON_CANONICAL_SHARE_HASH_REQUIRED,
    REASON_INFERENCE_MODEL_MISMATCH,
    REASON_INFERENCE_USAGE_INVALID,
    REASON_KILL_SWITCH,
    REASON_NONREWARDABLE_RESULT,
    REASON_OBSERVED_AT_BACKDATED,
    REASON_OBSERVED_AT_FUTURE,
    REASON_POOL_EVIDENCE_REF_REQUIRED,
    REASON_PROOF_DEDUP_STORE_UNAVAILABLE,
    REASON_RECORDED,
    REASON_SESSION_EXPIRED,
    REASON_SESSION_ISSUED,
    REASON_SESSION_LANE_MISMATCH,
    REASON_SESSION_NONCE_NOT_SERVER_ISSUED,
    REASON_SESSION_NONCE_REQUIRED,
    REASON_SESSION_NONCE_REUSED,
    REASON_SESSION_NONCE_STORE_UNAVAILABLE,
    REASON_SESSION_SECRET_UNAVAILABLE,
    REASON_SESSION_TAMPERED,
    REASON_SESSION_UNKNOWN,
    SESSION_KIND_INFERENCE,
    STATUS_ACCEPTED,
    STATUS_REJECTED,
    STATUS_UNDER_REVIEW,
    ZERO_DECIMAL,
    FoundationRevenueRecord,
    InferenceCompletionRequest,
    MiningProofIngestRequest,
    ProofIngestResult,
    SessionIssueResult,
    SettlementResult,
    SettlementWindow,
    ShadowBalance,
    ShadowSession,
    ShadowSessionIssueRequest,
    ShadowWorkRecord,
    pool_key_for_lane,
    utc_now,
)


@dataclass(slots=True)
class ShadowRewardLedger:
    sessions: dict[str, ShadowSession] = field(default_factory=dict)
    work_records: dict[str, ShadowWorkRecord] = field(default_factory=dict)
    revenue_records: dict[str, FoundationRevenueRecord] = field(default_factory=dict)
    kill_switch_enabled: bool = False
    proof_dedup_store: ProofDedupStore = field(default_factory=InMemoryProofDedupStore)
    # Phase E (C3): keyed-HMAC signing secret. Production callers MUST supply
    # one and set ``require_server_secret`` so a missing secret refuses to
    # issue. In-process/test callers may omit it: a per-instance random
    # ephemeral secret is generated so the signature is STILL a keyed HMAC that
    # cannot be recomputed from source (the old keyless SHA-256 hole).
    server_secret: bytes | None = None
    require_server_secret: bool = False
    # Phase E (C2): device proof-of-possession + registry seam. ``require_device_pop``
    # is opt-in so trusted in-process callers can issue without a PoP; the
    # untrusted HTTP edge sets it True with a real/fake registry (fail-closed:
    # unregistered identity or bad PoP => REJECT).
    device_registry: DeviceRegistry = field(default_factory=RejectingDeviceRegistry)
    require_device_pop: bool = False
    # Phase H_a: server-issued issuance-nonce gate. When ``require_server_issued_nonce``
    # is set (the untrusted HTTP edge does this), the issuance_nonce the device signs
    # in its PoP must be a nonce the SERVER minted (POST /session/nonce) and not yet
    # consumed — a client cannot present a self-chosen nonce. It is opt-in (default
    # OFF) so trusted in-process/test callers, which legitimately pick their own
    # nonce, keep working. Fail-closed: a missing store while required, an unknown/
    # expired nonce, or a store OSError all REJECT. ``IssuanceNonceStore`` is
    # injected, never hardcoded.
    issuance_nonce_store: IssuanceNonceStore | None = None
    require_server_issued_nonce: bool = False
    # Phase H_a: resolves the SERVER-ASSIGNED worker_name for (passport, device)
    # from the miner roster, stamped onto the issued session envelope (H_b pool
    # correlation). Injected seam (default None => no worker_name, e.g. trusted
    # in-process callers with no roster). Never the client's self-named worker.
    worker_name_resolver: Callable[[str, str], str | None] | None = None
    # M5: optional Route-1 peg resolver wired with a PER-DEVICE M_rate reader (the
    # device's real PRL credit/hour). Default None => the inference-ACU calc builds
    # the plain per-class resolver (the M1 behaviour), so an un-wired ledger is
    # byte-for-byte unchanged. When set, the per-device M_rate flows into the credit.
    # Typed object to avoid a ledger->route1_peg import edge; the ACU calc accepts
    # the structural resolver. Credit-only (M_rate is a credit rate).
    route1_resolver: object | None = None
    # Phase E (C2): per passport/device issuance rate limit (sybil throttle).
    max_issuances_per_identity: int = 0  # 0 => disabled
    # Phase E (C1): verified-demand admission seam. Default fail-closed: an
    # unknown demand id is NOT admitted (no AI credit downstream).
    demand_store: DemandAdmissionStore = field(default_factory=RejectingDemandAdmissionStore)
    # Phase F (M2): absolute server-clock window for client-supplied observed_at.
    # ``enforce_observed_window`` is opt-in (same trusted-in-process vs untrusted-
    # edge split as C2/C3): trusted/test callers keep replaying fixed historical
    # timestamps, while the HTTP edge turns it ON so a client cannot backdate work
    # into an old settlement window or post a far-future timestamp. ``server_clock``
    # is injectable for deterministic tests; the server-validated time (the clamp
    # reference) is what gets stored on the work record.
    enforce_observed_window: bool = False
    max_observed_backdate: timedelta = DEFAULT_MAX_OBSERVED_BACKDATE
    max_observed_skew: timedelta = DEFAULT_MAX_OBSERVED_SKEW
    server_clock: Callable[[], datetime] = utc_now
    _session_counter: int = 0
    _ephemeral_secret: bytes | None = None
    _used_session_nonces: set[str] = field(default_factory=set)
    _issuances_by_identity: dict[tuple[str, str], int] = field(default_factory=dict)

    def set_kill_switch(self, enabled: bool) -> None:
        self.kill_switch_enabled = enabled

    def _signing_secret(self) -> bytes | None:
        if self.server_secret is not None:
            return self.server_secret
        if self.require_server_secret:
            return None
        if self._ephemeral_secret is None:
            self._ephemeral_secret = secrets.token_bytes(32)
        return self._ephemeral_secret

    def issue_session(
        self,
        request: ShadowSessionIssueRequest,
        *,
        admit_inference_demand: bool = False,
    ) -> SessionIssueResult:
        if self.kill_switch_enabled:
            return SessionIssueResult(STATUS_REJECTED, REASON_KILL_SWITCH)
        if request.live_reward_enabled:
            return SessionIssueResult(STATUS_REJECTED, "live_reward_forbidden")
        if request.payout_executor_enabled:
            return SessionIssueResult(STATUS_REJECTED, "payout_executor_forbidden")
        if request.miner_provided_payout_address:
            return SessionIssueResult(STATUS_REJECTED, "miner_payout_address_rejected")

        # C3: fail-closed on a missing production signing secret.
        signing_secret = self._signing_secret()
        if signing_secret is None:
            return SessionIssueResult(STATUS_REJECTED, REASON_SESSION_SECRET_UNAVAILABLE)

        # C2: device proof-of-possession + registry gate (fail-closed when
        # required). Binds the issuance nonce, so a captured PoP cannot be
        # replayed for another identity/lane or reused.
        identity_key = (request.passport_id, request.device_id)
        rate_rejection = self._issuance_rate_rejection(identity_key)
        if rate_rejection is not None:
            return rate_rejection
        device_public_key_b64: str | None = None
        if self.require_device_pop:
            pop_rejection, device_public_key_b64 = self._verify_pop(request)
            if pop_rejection is not None:
                return pop_rejection

        # C3: random per-session nonce feeds BOTH the session_id and the HMAC;
        # reuse is rejected (collision / replay guard).
        session_nonce = secrets.token_hex(16)
        if session_nonce in self._used_session_nonces:
            return SessionIssueResult(STATUS_REJECTED, REASON_SESSION_NONCE_REUSED)

        self._session_counter += 1
        expires_at = request.requested_at + request.ttl
        session_id = _stable_hash(
            {
                "counter": self._session_counter,
                "device_id": request.device_id,
                "issued_at": request.requested_at.isoformat(),
                "lane": request.lane,
                "passport_id": request.passport_id,
                "session_kind": request.session_kind,
                "session_nonce": session_nonce,
            }
        )[:24]
        signature = _session_signature(
            secret=signing_secret,
            session_id=session_id,
            passport_id=request.passport_id,
            device_id=request.device_id,
            lane=request.lane,
            expires_at=expires_at,
            session_nonce=session_nonce,
        )

        # C1: AI-demand admission is gated on a verified-demand store lookup.
        ai_demand_admitted = False
        demand_session_id = None
        if (
            admit_inference_demand
            and request.session_kind == SESSION_KIND_INFERENCE
            and request.lane == MAIN_POOL_AI
            and request.demand_session_id is not None
        ):
            admission = self._resolve_demand(request)
            if admission.admitted:
                ai_demand_admitted = True
                demand_session_id = admission.demand_session_id or request.demand_session_id

        # All gates passed — commit nonce + rate-limit state.
        self._used_session_nonces.add(session_nonce)
        if self.max_issuances_per_identity > 0:
            self._issuances_by_identity[identity_key] = (
                self._issuances_by_identity.get(identity_key, 0) + 1
            )

        # H_a: stamp the server-assigned worker_name (from the roster) onto the
        # session envelope for downstream H_b pool-evidence correlation.
        worker_name = (
            self.worker_name_resolver(request.passport_id, request.device_id)
            if self.worker_name_resolver is not None
            else None
        )

        session = ShadowSession(
            session_id=session_id,
            passport_id=request.passport_id,
            device_id=request.device_id,
            lane=request.lane,
            session_kind=request.session_kind,
            issued_at=request.requested_at,
            expires_at=expires_at,
            signature=signature,
            worker_id=request.worker_id,
            model_id=request.model_id,
            ai_demand_admitted=ai_demand_admitted,
            demand_session_id=demand_session_id,
            device_public_key_b64=device_public_key_b64,
            worker_name=worker_name,
            # Carry the PUBLIC anti-replay nonce on the envelope so a SEPARATE
            # credit-server process can re-verify this signature against the shared
            # auth-secret (cross-process credit plane; doc §2.8). Not secret material.
            session_nonce=session_nonce,
        )
        self.sessions[session.session_id] = session
        return SessionIssueResult(STATUS_ACCEPTED, REASON_SESSION_ISSUED, session)

    def _issuance_rate_rejection(
        self,
        identity_key: tuple[str, str],
    ) -> SessionIssueResult | None:
        if self.max_issuances_per_identity <= 0:
            return None
        if self._issuances_by_identity.get(identity_key, 0) >= self.max_issuances_per_identity:
            return SessionIssueResult(STATUS_REJECTED, REASON_ISSUANCE_RATE_LIMITED)
        return None

    def _verify_pop(
        self,
        request: ShadowSessionIssueRequest,
    ) -> tuple[SessionIssueResult | None, str | None]:
        if request.issuance_nonce is None or not request.issuance_nonce:
            return SessionIssueResult(STATUS_REJECTED, REASON_SESSION_NONCE_REQUIRED), None
        if request.device_pop is None:
            return SessionIssueResult(STATUS_REJECTED, REASON_DEVICE_POP_REQUIRED), None
        # Single-use anti-replay: the PoP nonce may not be reused.
        if request.issuance_nonce in self._used_session_nonces:
            return SessionIssueResult(STATUS_REJECTED, REASON_SESSION_NONCE_REUSED), None
        result = verify_device_proof_of_possession(
            request.device_pop,
            registry=self.device_registry,
            passport_id=request.passport_id,
            device_id=request.device_id,
            lane=request.lane,
            session_kind=request.session_kind,
            issuance_nonce=request.issuance_nonce,
            observed_at=request.requested_at,
        )
        if not result.accepted:
            return SessionIssueResult(
                STATUS_REJECTED, result.reason_code or REASON_DEVICE_POP_INVALID
            ), None
        # H_a: the PoP signature is valid; now enforce that the nonce it signed
        # was SERVER-issued + single-use (durable). Only consume after a valid
        # PoP so a bad-PoP attempt cannot burn a victim's nonce.
        nonce_rejection = self._consume_server_issued_nonce(request)
        if nonce_rejection is not None:
            return nonce_rejection, None
        self._used_session_nonces.add(request.issuance_nonce)
        registration = result.registration
        return None, registration.device_public_key_b64 if registration else None

    def _consume_server_issued_nonce(
        self,
        request: ShadowSessionIssueRequest,
    ) -> SessionIssueResult | None:
        if not self.require_server_issued_nonce:
            return None
        assert request.issuance_nonce is not None
        if self.issuance_nonce_store is None:
            # Required but unconfigured => fail-closed.
            return SessionIssueResult(
                STATUS_REJECTED, REASON_SESSION_NONCE_STORE_UNAVAILABLE
            )
        try:
            decision = self.issuance_nonce_store.consume_nonce(
                nonce=request.issuance_nonce,
                observed_at=request.requested_at,
            )
        except IssuanceNonceStoreUnavailable:
            return SessionIssueResult(STATUS_REJECTED, REASON_SESSION_NONCE_STORE_UNAVAILABLE)
        if not decision.accepted:
            return SessionIssueResult(
                STATUS_REJECTED,
                decision.reason_code or REASON_SESSION_NONCE_NOT_SERVER_ISSUED,
            )
        return None

    def _resolve_demand(self, request: ShadowSessionIssueRequest):
        from alice_acp.shadow_server.demand_admission import DemandAdmissionResult

        assert request.demand_session_id is not None
        try:
            return self.demand_store.is_admitted_demand(
                demand_session_id=request.demand_session_id,
                passport_id=request.passport_id,
                device_id=request.device_id,
                observed_at=request.requested_at,
            )
        except DemandAdmissionStoreUnavailable:
            return DemandAdmissionResult(False)

    def ingest_mining_proof(self, request: MiningProofIngestRequest) -> ProofIngestResult:
        if self.kill_switch_enabled:
            return ProofIngestResult(STATUS_REJECTED, REASON_KILL_SWITCH)
        proof_id_claim = _mining_dedup_claim(request)
        proof_id_decision = self._claim_proof_id(proof_id_claim)
        if not proof_id_decision.accepted:
            return ProofIngestResult(STATUS_REJECTED, proof_id_decision.reason_code)
        session = self.sessions.get(request.session_id)
        if session is None:
            return ProofIngestResult(STATUS_REJECTED, REASON_SESSION_UNKNOWN)
        rejection = self._validate_session(session, request.session_signature, request.observed_at)
        if rejection is not None:
            return ProofIngestResult(STATUS_REJECTED, rejection)
        window_rejection, server_validated_at = self._validate_observed_window(request.observed_at)
        if window_rejection is not None:
            return ProofIngestResult(STATUS_REJECTED, window_rejection)
        if request.lane != session.lane:
            return ProofIngestResult(STATUS_REJECTED, REASON_SESSION_LANE_MISMATCH)
        # Phase C: the SCRYPT_POOL (LTC/DOGE) lane is a REAL creditable 15% lane.
        # The former unconditional `REASON_SCRYPT_POOL_INACTIVE` reject used to sit
        # HERE (before the pool_result / authority / pool_evidence / canonical-hash /
        # dedup gating below) and short-circuited scrypt entirely. It is deliberately
        # removed so SCRYPT_POOL now falls through into the EXACT SAME authority-gated
        # credit path as RVN/XMR -- it gains NO free pass: without
        # authority_result.accepted + rewardable_score > 0 + pool_evidence_ref +
        # canonical_share_hash a scrypt proof still ends UNDER_REVIEW / REJECTED here,
        # identically to every other lane. reward/payout/chain stay OFF.
        if request.pool_result != STATUS_ACCEPTED:
            return ProofIngestResult(STATUS_REJECTED, REASON_NONREWARDABLE_RESULT)
        authority_result = request.authority_result
        if authority_result is None:
            return ProofIngestResult(STATUS_UNDER_REVIEW, REASON_AUTHORITY_REQUIRED)
        if not authority_result.accepted:
            if authority_result.status == STATUS_UNDER_REVIEW:
                reason = authority_result.reason_code or REASON_AUTHORITY_UNDER_REVIEW
                return ProofIngestResult(STATUS_UNDER_REVIEW, reason)
            reason = authority_result.reason_code or REASON_AUTHORITY_REJECTED
            return ProofIngestResult(STATUS_REJECTED, reason)
        score = authority_result.rewardable_score
        if score <= ZERO_DECIMAL:
            return ProofIngestResult(STATUS_UNDER_REVIEW, REASON_AUTHORITY_SCORE_REQUIRED)
        canonical_share_hash, rejection_reason = _authority_canonical_share_hash(request)
        if rejection_reason is not None:
            return ProofIngestResult(STATUS_REJECTED, rejection_reason)
        if request.pool_evidence_ref is None:
            return ProofIngestResult(STATUS_REJECTED, REASON_POOL_EVIDENCE_REF_REQUIRED)
        canonical_decision = self._claim_canonical_share(
            _mining_dedup_claim(
                request,
                canonical_share_hash=canonical_share_hash,
                pool_evidence_ref=request.pool_evidence_ref,
            )
        )
        if not canonical_decision.accepted:
            return ProofIngestResult(STATUS_REJECTED, canonical_decision.reason_code)
        record = ShadowWorkRecord(
            proof_id=request.proof_id,
            session_id=session.session_id,
            passport_id=session.passport_id,
            device_id=session.device_id,
            lane=request.lane,
            pool_key=pool_key_for_lane(request.lane),
            verified_score=score,
            score_kind="accepted_share_score",
            recorded_at=server_validated_at,
            reason_code=authority_result.reason_code,
            canonical_share_hash=canonical_share_hash,
            pool_evidence_ref=request.pool_evidence_ref,
        )
        self.work_records[request.proof_id] = record
        return ProofIngestResult(
            STATUS_ACCEPTED,
            REASON_RECORDED,
            record,
            rewardable_score=score,
        )

    def complete_inference(self, request: InferenceCompletionRequest) -> ProofIngestResult:
        if self.kill_switch_enabled:
            return ProofIngestResult(STATUS_REJECTED, REASON_KILL_SWITCH)
        proof_id_decision = self._claim_proof_id(_inference_dedup_claim(request))
        if not proof_id_decision.accepted:
            return ProofIngestResult(STATUS_REJECTED, proof_id_decision.reason_code)
        session = self.sessions.get(request.session_id)
        if session is None:
            return ProofIngestResult(STATUS_REJECTED, REASON_SESSION_UNKNOWN)
        rejection = self._validate_session(session, request.session_signature, request.observed_at)
        if rejection is not None:
            return ProofIngestResult(STATUS_REJECTED, rejection)
        window_rejection, server_validated_at = self._validate_observed_window(request.observed_at)
        if window_rejection is not None:
            return ProofIngestResult(STATUS_REJECTED, window_rejection)
        if session.session_kind != SESSION_KIND_INFERENCE or session.lane != MAIN_POOL_AI:
            return ProofIngestResult(STATUS_REJECTED, REASON_SESSION_LANE_MISMATCH)
        if not session.ai_demand_admitted or session.demand_session_id is None:
            return ProofIngestResult(STATUS_REJECTED, REASON_AI_DEMAND_NOT_ADMITTED)
        if session.model_id is not None and request.model_id != session.model_id:
            return ProofIngestResult(STATUS_REJECTED, REASON_INFERENCE_MODEL_MISMATCH)
        try:
            verified_inference_acu = calculate_verified_inference_acu(
                request, route1_resolver=self.route1_resolver  # type: ignore[arg-type]
            )
        except InferenceAcuValidationError:
            return ProofIngestResult(STATUS_REJECTED, REASON_INFERENCE_USAGE_INVALID)
        record = ShadowWorkRecord(
            proof_id=request.proof_id,
            session_id=session.session_id,
            passport_id=session.passport_id,
            device_id=session.device_id,
            lane=session.lane,
            pool_key=pool_key_for_lane(session.lane),
            verified_score=verified_inference_acu,
            score_kind="verified_inference_acu",
            recorded_at=server_validated_at,
            reason_code=REASON_RECORDED,
            demand_session_id=session.demand_session_id,
        )
        self.work_records[request.proof_id] = record
        # M1: never record the client face-value unbounded. Clamp the simulated
        # API payment to (input+output tokens) * a max per-token rate so a single
        # completion cannot inject an absurd headline revenue number.
        bounded_payment = bounded_simulated_api_payment(
            request.simulated_api_payment,
            input_tokens=request.input_tokens,
            output_tokens=request.output_tokens,
        )
        if bounded_payment > ZERO_DECIMAL:
            self.record_foundation_revenue(
                payment_id=f"payment-{request.proof_id}",
                session_id=session.session_id,
                amount=bounded_payment,
                recorded_at=record.recorded_at,
            )
        return ProofIngestResult(
            STATUS_ACCEPTED,
            REASON_RECORDED,
            record,
            rewardable_score=verified_inference_acu,
        )

    def record_foundation_revenue(
        self,
        *,
        payment_id: str,
        session_id: str,
        amount: Decimal,
        recorded_at: datetime,
    ) -> FoundationRevenueRecord:
        record = FoundationRevenueRecord(
            payment_id=payment_id,
            session_id=session_id,
            amount=amount,
            recorded_at=recorded_at,
        )
        self.revenue_records[payment_id] = record
        return record

    def settle_window(self, window: SettlementWindow) -> SettlementResult:
        budgets = pool_budgets_for_window(window)
        scoring = settle_reward_scores(
            records=self.work_records.values(),
            window=window,
            pool_budgets=budgets,
        )
        return SettlementResult(
            window=window,
            pool_budgets=budgets,
            device_credits=scoring.device_credits,
            reserve_roll_forward=scoring.reserve_roll_forward,
            reward_statements=scoring.reward_statements,
        )

    def balance_for(
        self,
        passport_id: str,
        device_id: str,
        window: SettlementWindow,
    ) -> ShadowBalance:
        settlement = self.settle_window(window)
        credit = settlement.device_credits.get((passport_id, device_id), ZERO_DECIMAL)
        return ShadowBalance(passport_id, device_id, credit)

    def _validate_session(
        self,
        session: ShadowSession,
        observed_signature: str,
        observed_at: datetime,
    ) -> str | None:
        if not hmac.compare_digest(session.signature, observed_signature):
            return REASON_SESSION_TAMPERED
        if observed_at >= session.expires_at:
            return REASON_SESSION_EXPIRED
        return None

    def admit_cross_process_session(self, session: ShadowSession) -> bool:
        """Register a session MINTED IN ANOTHER PROCESS, fail-closed on signature.

        The cross-process credit plane (doc §2.8): the TRANSPORT service mints a
        :class:`ShadowSession` in ITS OWN ledger when a miner logs in; the SEPARATE
        credit-server process never saw that issuance, so its ``self.sessions`` does
        not contain the session the validated share is bound to. This method lets the
        credit server admit such a session for crediting ONLY after re-verifying its
        HMAC signature against THIS ledger's OWN signing secret (the shared
        auth-secret both processes load): a forged or tampered session whose signature
        does not recompute is REFUSED (returns ``False``) and is never registered —
        ``ingest_mining_proof`` then keeps it ``REASON_SESSION_UNKNOWN`` (no credit).

        The trust anchor is the shared secret, NOT trust in the carrier: the public
        ``session.session_nonce`` (a per-session anti-replay value the signature mixes
        in — never the secret) is carried on the envelope so the keyed MAC can be
        recomputed here. An idempotent re-admission of an already-registered session
        is a no-op (it must still verify). require_device_pop is NOT re-applied here —
        the session was roster-gated at mint on the transport's ledger; this side does
        not re-issue it, it only admits the signature-verified envelope so the
        validated share bound to it can credit. CREDIT-ONLY: nothing here sets a
        reward/payout/chain symbol.
        """

        if not self.verify_session_signature(session):
            return False
        if session.session_id not in self.sessions:
            self.sessions[session.session_id] = session
        # Idempotent when already present: the stored copy carries the same signature
        # (verified above for the carried copy), so a later ingest HMAC self-compare
        # passes regardless of which copy is kept.
        return True

    def verify_session_signature(self, session: ShadowSession) -> bool:
        """Recompute the session HMAC over its public fields + nonce, fail-closed.

        Returns ``True`` IFF this ledger's OWN signing secret recomputes EXACTLY the
        signature carried on ``session`` (constant-time compare). Used to verify a
        session minted in another process before crediting against it. Fail-closed:
        a missing signing secret (``require_server_secret`` with none configured) OR a
        session with no carried ``session_nonce`` (legacy / not cross-process-issued)
        returns ``False`` — the credit server cannot vouch for a session it cannot
        verify.
        """

        if session.session_nonce is None:
            return False
        signing_secret = self._signing_secret()
        if signing_secret is None:
            return False
        expected = _session_signature(
            secret=signing_secret,
            session_id=session.session_id,
            passport_id=session.passport_id,
            device_id=session.device_id,
            lane=session.lane,
            expires_at=session.expires_at,
            session_nonce=session.session_nonce,
        )
        return hmac.compare_digest(expected, session.signature)

    def _validate_observed_window(self, observed_at: datetime) -> tuple[str | None, datetime]:
        """M2: clamp/validate ``observed_at`` to an ABSOLUTE server-clock window.

        Returns ``(reason_code_or_None, server_validated_at)``. When enforcement
        is off (trusted in-process/test callers) the client timestamp is accepted
        as-is. When ON (the untrusted HTTP edge), a timestamp older than
        ``server_now - max_observed_backdate`` or newer than
        ``server_now + max_observed_skew`` is rejected fail-closed; this stops a
        client backdating work into a stale settlement window or stamping a
        far-future time. The stored ``recorded_at`` becomes the server-validated
        time (the clamped client value), never an unbounded client face value.
        """

        if not self.enforce_observed_window:
            return None, observed_at
        server_now = self.server_clock()
        if observed_at < server_now - self.max_observed_backdate:
            return REASON_OBSERVED_AT_BACKDATED, observed_at
        if observed_at > server_now + self.max_observed_skew:
            return REASON_OBSERVED_AT_FUTURE, observed_at
        # In-window: store the client-declared time (already proven inside the
        # server window) as the server-validated reference.
        return None, observed_at

    def _claim_proof_id(self, claim: ProofDedupClaim) -> ProofDedupDecision:
        try:
            return self.proof_dedup_store.claim_proof_id(claim)
        except OSError:
            return _dedup_unavailable()

    def _claim_canonical_share(self, claim: ProofDedupClaim) -> ProofDedupDecision:
        try:
            return self.proof_dedup_store.claim_canonical_share(claim)
        except OSError:
            return _dedup_unavailable()


def default_settlement_window(starts_at: datetime) -> SettlementWindow:
    from alice_acp.shadow_server.types import DEFAULT_WINDOW_DURATION

    return SettlementWindow(
        window_id=f"window-{starts_at.isoformat()}",
        starts_at=starts_at,
        ends_at=starts_at + DEFAULT_WINDOW_DURATION,
        total_window_emission=DEFAULT_TOTAL_WINDOW_EMISSION,
    )


def _session_signature(
    *,
    secret: bytes,
    session_id: str,
    passport_id: str,
    device_id: str,
    lane: str,
    expires_at: datetime,
    session_nonce: str,
) -> str:
    """Phase E (C3): keyed HMAC-SHA256 over the public session fields + nonce.

    The former implementation was a *keyless* SHA-256 of the public fields with
    the signer baked into source, so anyone could recompute a valid signature.
    This keys the MAC on the server secret and mixes in the per-session nonce,
    so a signature cannot be forged without the secret and is unique per issue.
    """

    payload = json.dumps(
        {
            "device_id": device_id,
            "expires_at": expires_at.isoformat(),
            "lane": lane,
            "passport_id": passport_id,
            "session_id": session_id,
            "session_nonce": session_nonce,
            "signer": "queue13s-shadow-session-signer",
        },
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def _stable_hash(data: dict[str, object]) -> str:
    payload = json.dumps(data, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _mining_dedup_claim(
    request: MiningProofIngestRequest,
    *,
    canonical_share_hash: str | None = None,
    pool_evidence_ref: str | None = None,
) -> ProofDedupClaim:
    return ProofDedupClaim(
        proof_id=request.proof_id,
        session_id=request.session_id,
        worker_id=request.worker_id,
        lane=request.lane,
        observed_at=request.observed_at,
        canonical_share_hash=canonical_share_hash,
        pool_evidence_ref=pool_evidence_ref,
    )


def _inference_dedup_claim(request: InferenceCompletionRequest) -> ProofDedupClaim:
    return ProofDedupClaim(
        proof_id=request.proof_id,
        session_id=request.session_id,
        worker_id="",
        lane=MAIN_POOL_AI,
        observed_at=request.observed_at,
    )


def _authority_canonical_share_hash(
    request: MiningProofIngestRequest,
) -> tuple[str, str | None]:
    authority_result = request.authority_result
    authority_hash = authority_result.canonical_share_hash if authority_result else None
    request_hash = request.canonical_share_hash
    if authority_hash is not None and request_hash is not None and authority_hash != request_hash:
        return "", REASON_CANONICAL_SHARE_HASH_MISMATCH
    canonical_share_hash = authority_hash or request_hash
    if canonical_share_hash is None:
        return "", REASON_CANONICAL_SHARE_HASH_REQUIRED
    if not _looks_like_sha256(canonical_share_hash):
        return "", REASON_CANONICAL_SHARE_HASH_INVALID
    return canonical_share_hash, None


def _looks_like_sha256(value: str) -> bool:
    return len(value) == 64 and all(char in string.hexdigits for char in value)


def _dedup_unavailable() -> ProofDedupDecision:
    return ProofDedupDecision(False, REASON_PROOF_DEDUP_STORE_UNAVAILABLE)

from __future__ import annotations

import argparse
import json
import os
from collections.abc import Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from alice_acp.shadow_server.admission_store import (
    AdmissionStore,
    AdmissionStoreUnavailable,
    InMemoryAdmissionStore,
)
from alice_acp.shadow_server.dedup_store import JsonlProofDedupStore
from alice_acp.shadow_server.demand_admission import (
    DemandAdmissionStore,
    RejectingDemandAdmissionStore,
)
from alice_acp.shadow_server.device_pop import (
    DEVICE_ENROLLMENT_SIGNATURE_SCHEME,
    DeviceProofOfPossession,
    DeviceRegistry,
    RejectingDeviceRegistry,
)
from alice_acp.shadow_server.hardening import (
    RATE_LIMIT_IDENTITY_MISSING_REASON,
    ShadowAccessDecision,
    ShadowAccessPolicy,
    ShadowRateLimitPolicy,
    build_shadow_health_payload,
    build_shadow_kill_switch_payload,
    evaluate_shadow_access,
    is_public_miner_mining_request,
    resolve_shadow_client_host,
    resolve_shadow_rate_limit_identity,
)
from alice_acp.shadow_server.issuance_nonce_store import (
    IssuanceNonceStore,
    IssuanceNonceStoreUnavailable,
    JsonlIssuanceNonceStore,
)
from alice_acp.shadow_server.ledger import ShadowRewardLedger
from alice_acp.shadow_server.miner_roster import (
    JsonlMinerRoster,
    MinerRosterUnavailable,
)
from alice_acp.shadow_server.mining_authority_bridge import (
    NO_POOL_EVIDENCE_PROVIDER,
    PoolEvidenceProvider,
)
from alice_acp.shadow_server.server import ShadowServerHarness
from alice_acp.shadow_server.storage import JsonlAuditStore
from alice_acp.shadow_server.types import (
    DEFAULT_TOTAL_WINDOW_EMISSION,
    DEFAULT_WINDOW_DURATION,
    MAIN_POOL_GPU_PRL,
    REASON_SESSION_NONCE_NOT_SERVER_ISSUED,
    REASON_SESSION_NONCE_STORE_UNAVAILABLE,
    InferenceCompletionRequest,
    MiningProofIngestRequest,
    SettlementWindow,
    ShadowHeartbeatRequest,
    ShadowSessionIssueRequest,
)
from alice_acp.transport_front.account_poll_enrollment import (
    ACCOUNT_POLL_DISABLED,
    ACCOUNT_POLL_STORE_UNAVAILABLE,
    AccountPollEnrollmentRequest,
    InMemoryPrlSessionRegistrationStore,
    JsonlPrlSessionRegistrationStore,
    PrlSessionRegistrationStore,
    account_poll_enrollment_enabled,
    enroll_account_poll_worker,
)
from alice_acp.transport_front.open_enrollment import (
    OpenEnrollmentLimiter,
    OpenEnrollmentLimits,
)

# H_a: no durable roster/registry provisioned => self-enrollment cannot persist,
# so /device/register fails closed with this reason rather than silently passing.
REASON_DEVICE_REGISTRY_NOT_CONFIGURED = "device_registry_not_configured"


@dataclass(frozen=True, slots=True)
class ShadowHttpConfig:
    bind_host: str = "127.0.0.1"
    port: int = 18130
    data_dir: Path = Path("/var/lib/alice-acp-shadow")
    auth_secret: str | None = None
    localhost_only: bool = True
    service_name: str = "queue13s-shadow-server-harness"
    deployment_role: str = "shadow_beta"
    public_primary_candidate: bool = False
    reward_mode: str = "pending_verified_work_no_payout"
    staging_internal_only: bool = True
    access_policy: ShadowAccessPolicy = field(default_factory=ShadowAccessPolicy)
    rate_limit_policy: ShadowRateLimitPolicy = field(default_factory=ShadowRateLimitPolicy)
    # Phase E (C2): max session issuances per (passport, device) (0 => disabled).
    max_issuances_per_identity: int = 256
    # Phase H_a: when set, the device-PoP must sign a SERVER-issued, single-use
    # issuance nonce (POST /session/nonce) — a client cannot present a self-chosen
    # nonce. The flag defaults OFF here so legacy in-process/edge callers that pass
    # their own nonce keep working; the deployed edge entrypoint (``main``) turns
    # it ON. The new H_a edge tests set it ON explicitly to prove enforcement.
    require_server_issued_nonce: bool = False
    # Phase H_a: credit-only external-miner self-enrollment (POST /device/register)
    # is OPEN by default. TIGHTEN BEFORE REWARD (see miner_roster.enroll): gate
    # before any paid_acu>0 lane. Set False to run fail-closed (reject all
    # enrollment).
    enrollment_open: bool = True
    # Phase F (M2): server clock keying the absolute observed_at window enforced on
    # the untrusted HTTP edge. Defaults to the real UTC clock (production); tests
    # pin it so they can replay fixed historical timestamps deterministically.
    server_clock: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    # Phase H_b: the SERVER-side pool-evidence provider the harness uses to
    # evaluate mining-proof authority. Defaults to the fail-closed
    # NoPoolEvidenceProvider, so an unconfigured edge credits NOTHING (every proof
    # under_review). The deployed edge wires a real LaneRoutingPoolEvidenceProvider
    # (alice_acp.shadow_server.pool_evidence_providers) built from the per-lane Alice
    # pool addresses + an injectable HTTP client; the F2Pool token is read from the
    # ALICE_F2POOL_API_SECRET env var and never committed. CREDIT-ONLY.
    pool_evidence_provider: PoolEvidenceProvider = NO_POOL_EVIDENCE_PROVIDER


@dataclass(frozen=True, slots=True)
class ShadowHttpResponse:
    status: int
    payload: dict[str, Any]


INFERENCE_COMPLETE_AUDIT_KEYS = frozenset(
    {
        "proof_id",
        "session_id",
        "session_signature",
        "model_id",
        "input_tokens",
        "output_tokens",
        "context_length",
        "latency_ms",
        "model_class",
        "observed_at",
        "simulated_api_payment",
    }
)
RAW_PROMPT_KEYS = frozenset({"prompt", "raw_prompt", "messages", "input", "conversation"})


class ShadowHttpApp:
    def __init__(
        self,
        config: ShadowHttpConfig,
        *,
        harness: ShadowServerHarness | None = None,
        store: JsonlAuditStore | None = None,
        admission_store: AdmissionStore | None = None,
        rate_limiter: AdmissionStore | None = None,
        device_registry: DeviceRegistry | None = None,
        demand_store: DemandAdmissionStore | None = None,
        miner_roster: JsonlMinerRoster | None = None,
        issuance_nonce_store: IssuanceNonceStore | None = None,
        prl_session_store: PrlSessionRegistrationStore | None = None,
        prl_enrollment_limiter: OpenEnrollmentLimiter | None = None,
    ) -> None:
        self.config = config
        # Phase H_a: the durable miner roster unifies enrollment + identity +
        # liveness + the server-assigned worker_name. When supplied it is BOTH
        # the C2 device-registry source of truth (registry = roster.device_registry)
        # AND the worker_name resolver stamped onto the issued session envelope.
        # Default fail-closed: no roster + RejectingDeviceRegistry => every
        # issuance is rejected until an operator provisions a roster/registry.
        self.miner_roster = miner_roster
        if miner_roster is not None:
            resolved_registry: DeviceRegistry = miner_roster.device_registry
            worker_name_resolver: Callable[[str, str], str | None] | None = (
                lambda passport_id, device_id: miner_roster.worker_name_for(
                    passport_id=passport_id, device_id=device_id
                )
            )
        else:
            resolved_registry = device_registry or RejectingDeviceRegistry()
            worker_name_resolver = None
        # Phase H_a: server-issued, single-use issuance-nonce store (POST
        # /session/nonce). Durable JSONL by default at the edge; the device must
        # sign one of these nonces in its issuance PoP.
        self.issuance_nonce_store = issuance_nonce_store or JsonlIssuanceNonceStore(
            config.data_dir
        )
        # Phase E: the HTTP surface is the UNTRUSTED edge, so its ledger is
        # fully fail-closed:
        #   C3 -- the loaded auth_secret keys the session-signature HMAC and a
        #         missing secret refuses to issue (require_server_secret=True).
        #   C2 -- device proof-of-possession is REQUIRED and validated against a
        #         DeviceRegistry that defaults to RejectingDeviceRegistry
        #         (unregistered identity => REJECT); plus a per-identity issuance
        #         rate limit to throttle sybil minting.
        #   C1 -- AI-demand admission is gated on a DemandAdmissionStore that
        #         defaults to RejectingDemandAdmissionStore (unknown demand =>
        #         NOT admitted).
        #   M2 -- a client-supplied observed_at is clamped to an ABSOLUTE server
        #         window (enforce_observed_window=True), so work cannot be
        #         backdated into a stale settlement window or post-dated.
        #   H_a -- the issuance_nonce must be a SERVER-issued, unconsumed nonce
        #         (require_server_issued_nonce); the server stamps a
        #         server-assigned worker_name onto the session envelope.
        # All of reward/payout/chain stay OFF.
        self.harness = harness or ShadowServerHarness(
            # H_b: fail-closed NoPoolEvidenceProvider by default; the deployed edge
            # supplies a real LaneRoutingPoolEvidenceProvider via config.
            evidence_provider=config.pool_evidence_provider,
            ledger=ShadowRewardLedger(
                proof_dedup_store=JsonlProofDedupStore(config.data_dir),
                server_secret=(
                    config.auth_secret.encode("utf-8")
                    if config.auth_secret is not None
                    else None
                ),
                require_server_secret=True,
                device_registry=resolved_registry,
                require_device_pop=True,
                issuance_nonce_store=self.issuance_nonce_store,
                require_server_issued_nonce=config.require_server_issued_nonce,
                worker_name_resolver=worker_name_resolver,
                max_issuances_per_identity=config.max_issuances_per_identity,
                demand_store=demand_store or RejectingDemandAdmissionStore(),
                enforce_observed_window=True,
                server_clock=config.server_clock,
            )
        )
        self.store = store or JsonlAuditStore(config.data_dir)
        self.admission_store = admission_store or rate_limiter or InMemoryAdmissionStore()
        # ACCOUNT-POLL (PRL/pearlhash) self-serve enrollment (POST /prl/enroll).
        # PRL has NO stratum front (pearl-miner mines pearlhash DIRECTLY), so a PRL
        # miner never logs in through the transport, so no Alice session is ever
        # minted, so the proof-authority scheduler never gets a target for the PRL
        # provider → credit never auto-flows. This endpoint closes that loop: a miner
        # registers ONCE with their Alice SS58-300 address (+ optional label) and the
        # server mints the HMAC-signed session the scheduler discovers. ALL of it is
        # gated by ALICE_PRL_ACCOUNT_POLL_ENROLLMENT (default OFF), checked per-request
        # in :meth:`_prl_enroll`, so until an operator flips it on this surface refuses
        # every registration (fail-closed) and the credit loop is byte-for-byte today's.
        #
        # The DEDICATED, REGISTRATION-LESS enrollment ledger (NOT ``self.harness``):
        # the self-serve path has NO roster row + NO device PoP, but the session MUST
        # still be keyed-HMAC signed with the SAME ``auth_secret`` the credit server
        # loads (the cross-process trust anchor it re-verifies before any epoch drains).
        # So this ledger shares ``config.auth_secret`` (require_server_secret=True — a
        # missing secret refuses to issue, fail-closed) but sets require_device_pop=False
        # and wires NO registry/roster/nonce/demand store (none of the issuance gates the
        # mining-proof edge needs apply to a registration-less credit-identity mint). It
        # mirrors the transport's open-enrollment ledger exactly. CREDIT-ONLY: it inherits
        # every reward/payout/chain guard OFF; nothing here sets a payout symbol.
        self.prl_enrollment_ledger = ShadowRewardLedger(
            server_secret=(
                config.auth_secret.encode("utf-8") if config.auth_secret is not None else None
            ),
            require_server_secret=True,
            require_device_pop=False,
        )
        # The scheduler-discoverable registration store + the anti-spam budget. Both
        # default to the in-process implementations; the deployed edge (``main``) injects
        # the durable JSONL store (shared with the credit server via the env path) and an
        # env-tuned limiter. The store is the SAME seam the proof-authority target source
        # unions via ``pending_sessions()`` (see ``_build_proof_authority_targets``).
        self.prl_session_store: PrlSessionRegistrationStore = (
            prl_session_store or InMemoryPrlSessionRegistrationStore()
        )
        self.prl_enrollment_limiter = prl_enrollment_limiter or OpenEnrollmentLimiter(
            limits=OpenEnrollmentLimits.from_env()
        )

    def handle(
        self,
        *,
        method: str,
        path: str,
        headers: dict[str, str],
        body: bytes = b"",
        client_host: str = "127.0.0.1",
    ) -> ShadowHttpResponse:
        parsed = urlparse(path)
        # public-XFF: ``client_host`` is the raw connection/socket peer the HTTP
        # layer saw. Behind the launch nginx every miner shows up as the nginx
        # IP, so derive the REAL client host from X-Forwarded-For ONLY when that
        # socket peer is a configured trusted proxy (rightmost-untrusted hop);
        # otherwise the forwarded header is ignored and the socket peer is used.
        # Default empty allowlist => fail-closed (socket peer wins). The resolved
        # host is what classification / rate-limit-identity / audit anchor on.
        resolved = resolve_shadow_client_host(
            socket_peer=client_host,
            headers=headers,
            policy=self.config.access_policy,
        )
        client_host = resolved.client_host
        # B1: when the public-miner gate is ON and the request arrived via the
        # trusted public proxy, resolve whether this is a MINING-flow request so the
        # chokepoint can admit an external miner to the ``public_miner`` scope for
        # the mining flow ONLY. Gate OFF (default) => this stays None and the access
        # decision is byte-for-byte the pre-B1 path (no body peek, no behavior
        # change). ``payload`` is reused downstream so the body is parsed at most
        # once. A malformed body here is left to surface as the normal 400 in the
        # per-route parse below (we swallow the peek error rather than admit).
        payload: dict[str, Any] | None = None
        is_mining_route = False
        via_trusted_proxy = resolved.forwarded
        if self.config.access_policy.public_miner_gate_enabled and via_trusted_proxy:
            session_kind: str | None = None
            if method == "POST" and parsed.path in {"/session/issue", "/inference/admit"}:
                try:
                    payload = _json_body(body)
                except ValueError:
                    payload = None
                if isinstance(payload, dict):
                    raw_kind = payload.get("session_kind")
                    session_kind = raw_kind if isinstance(raw_kind, str) else None
                    # /inference/admit forces the inference kind downstream, so its
                    # effective session_kind is inference regardless of the body.
                    if parsed.path == "/inference/admit":
                        session_kind = "inference"
            is_mining_route = is_public_miner_mining_request(
                method, parsed.path, session_kind=session_kind
            )
        access = self._authorize(
            headers,
            client_host,
            via_trusted_proxy=via_trusted_proxy,
            is_mining_route=is_mining_route,
        )
        if not access.allowed:
            return self._response(
                HTTPStatus(access.http_status),
                {
                    "ok": False,
                    "reason_code": access.reason_code,
                    "client_scope": access.client_scope,
                    "auth_required": access.auth_required,
                },
            )
        try:
            if method == "GET" and parsed.path == "/health":
                return self._health()
            if method == "GET" and parsed.path == "/status":
                return self._status()
            if method == "GET" and parsed.path == "/readiness":
                return self._readiness()
            # ``payload`` may already hold the body peeked for the B1 public-miner
            # mining-route decision above; reuse it so the body is parsed at most
            # once (None unless the gate is ON and the request came via the proxy).
            if self._rate_limited_route(method, parsed.path):
                if payload is None:
                    payload = _json_body(body)
                rate_limit_response = self._enforce_rate_limit(
                    route=parsed.path,
                    headers=headers,
                    payload=payload,
                    client_host=client_host,
                )
                if rate_limit_response is not None:
                    return rate_limit_response
            if method == "POST" and parsed.path == "/session/nonce":
                return self._session_nonce()
            if method == "POST" and parsed.path == "/device/register":
                return self._device_register(payload if payload is not None else _json_body(body))
            if method == "POST" and parsed.path == "/prl/enroll":
                return self._prl_enroll(
                    payload if payload is not None else _json_body(body),
                    client_host=client_host,
                )
            if method == "POST" and parsed.path == "/session/issue":
                return self._session_issue(payload if payload is not None else _json_body(body))
            if method == "POST" and parsed.path == "/proof/ingest":
                return self._proof_ingest(payload if payload is not None else _json_body(body))
            if method == "POST" and parsed.path == "/inference/admit":
                payload = payload if payload is not None else _json_body(body)
                payload["session_kind"] = "inference"
                return self._session_issue(payload, event_type="inference_session_issued")
            if method == "POST" and parsed.path == "/inference/complete":
                payload = payload if payload is not None else _json_body(body)
                return self._inference_complete(payload)
            if method == "POST" and parsed.path == "/heartbeat":
                return self._heartbeat(payload if payload is not None else _json_body(body))
            if method == "GET" and parsed.path == "/shadow/window":
                return self._shadow_window(parse_qs(parsed.query), persist=True)
            if method == "GET" and parsed.path == "/shadow/window/preview":
                return self._shadow_window(parse_qs(parsed.query), persist=False)
            if method == "GET" and parsed.path == "/shadow/balance":
                return self._shadow_balance(parse_qs(parsed.query), persist=True)
            if method == "GET" and parsed.path == "/shadow/balance/preview":
                return self._shadow_balance(parse_qs(parsed.query), persist=False)
            if method == "POST" and parsed.path == "/admin/kill-switch":
                return self._kill_switch(_json_body(body))
        except ValueError as exc:
            return self._response(HTTPStatus.BAD_REQUEST, {"ok": False, "reason_code": str(exc)})
        return self._response(HTTPStatus.NOT_FOUND, {"ok": False, "reason_code": "not_found"})

    def _health(self) -> ShadowHttpResponse:
        payload = self._health_payload(readiness=False)
        self.store.append("heartbeats", "health", payload)
        return self._response(HTTPStatus.OK, payload)

    def _status(self) -> ShadowHttpResponse:
        payload = self._health_payload(readiness=True) | {
            "status_contract": "alice_acp_primary_status_v1",
            "status_endpoint": "/status",
            "read_only": True,
        }
        return self._response(HTTPStatus.OK, payload)

    def _readiness(self) -> ShadowHttpResponse:
        payload = self._health_payload(readiness=True)
        self.store.append("heartbeats", "readiness", payload)
        return self._response(HTTPStatus.OK, payload)

    def _session_issue(
        self,
        payload: dict[str, Any],
        *,
        event_type: str = "session_issued",
    ) -> ShadowHttpResponse:
        request = ShadowSessionIssueRequest(
            passport_id=_required_text(payload, "passport_id"),
            device_id=_required_text(payload, "device_id"),
            lane=_required_text(payload, "lane"),  # type: ignore[arg-type]
            session_kind=_required_text(payload, "session_kind"),  # type: ignore[arg-type]
            worker_id=_optional_text(payload, "worker_id"),
            model_id=_optional_text(payload, "model_id"),
            requested_at=_datetime(payload.get("requested_at")),
            ttl=timedelta(seconds=int(payload.get("ttl_seconds", 1800))),
            live_reward_enabled=bool(payload.get("live_reward_enabled", False)),
            payout_executor_enabled=bool(payload.get("payout_executor_enabled", False)),
            miner_provided_payout_address=_optional_text(payload, "miner_provided_payout_address"),
            # Phase E (C2/C1): device PoP + nonce + the demand id to verify.
            issuance_nonce=_optional_text(payload, "issuance_nonce"),
            device_pop=_device_pop(payload.get("device_pop")),
            demand_session_id=_optional_text(payload, "demand_session_id"),
        )
        if event_type == "inference_session_issued":
            result = self.harness.inference_admit(request)
        else:
            result = self.harness.session_issue(request)
        response = _session_result_json(result)
        if result.accepted:
            self.store.append("sessions", event_type, response)
        else:
            self.store.append("rejections", "session_rejected", response)
        return self._response(
            HTTPStatus.OK if result.accepted else HTTPStatus.BAD_REQUEST,
            response,
        )

    def _session_nonce(self) -> ShadowHttpResponse:
        # H_a: mint a short-TTL, single-use SERVER nonce the device must sign in
        # its issuance PoP. Fail-closed: a nonce-store OSError surfaces as 503.
        try:
            issued = self.issuance_nonce_store.issue_nonce(
                observed_at=self.config.server_clock()
            )
        except IssuanceNonceStoreUnavailable:
            audit_payload = _credit_only_envelope(
                {"ok": False, "reason_code": REASON_SESSION_NONCE_STORE_UNAVAILABLE}
            )
            self.store.append("events", "issuance_nonce_store_unavailable", audit_payload)
            return self._response(HTTPStatus.SERVICE_UNAVAILABLE, audit_payload)
        payload = _credit_only_envelope(
            {
                "ok": True,
                "issuance_nonce": issued.nonce,
                "issued_at": issued.issued_at.isoformat(),
                "expires_at": issued.expires_at.isoformat(),
                "nonce_contract": "alice_shadow_issuance_nonce_v1",
                "single_use": True,
            }
        )
        self.store.append("events", "issuance_nonce_issued", {"expires_at": payload["expires_at"]})
        return self._response(HTTPStatus.OK, payload)

    def _device_register(self, payload: dict[str, Any]) -> ShadowHttpResponse:
        # H_a: external-miner self-enrollment. Fail-closed when no roster is
        # provisioned (no durable backend to write into) or enrollment is closed.
        if self.miner_roster is None:
            audit_payload = _credit_only_envelope(
                {"ok": False, "reason_code": REASON_DEVICE_REGISTRY_NOT_CONFIGURED}
            )
            self.store.append("rejections", "device_register_rejected", audit_payload)
            return self._response(HTTPStatus.SERVICE_UNAVAILABLE, audit_payload)
        passport_id = _required_text(payload, "passport_id")
        device_id = _required_text(payload, "device_id")
        device_public_key_b64 = _required_text(payload, "device_public_key_b64")
        enrollment_nonce = _required_text(payload, "enrollment_nonce")
        pop = _enrollment_pop(payload.get("enrollment_pop"))
        # The enrollment nonce must itself be a server-issued, single-use nonce
        # (same durable store as issuance). Consume it BEFORE the roster write so
        # one nonce mints at most one enrollment attempt.
        try:
            nonce_decision = self.issuance_nonce_store.consume_nonce(
                nonce=enrollment_nonce,
                observed_at=self.config.server_clock(),
            )
        except IssuanceNonceStoreUnavailable:
            audit_payload = _credit_only_envelope(
                {"ok": False, "reason_code": REASON_SESSION_NONCE_STORE_UNAVAILABLE}
            )
            self.store.append("events", "issuance_nonce_store_unavailable", audit_payload)
            return self._response(HTTPStatus.SERVICE_UNAVAILABLE, audit_payload)
        if not nonce_decision.accepted:
            audit_payload = _credit_only_envelope(
                {
                    "ok": False,
                    "reason_code": nonce_decision.reason_code
                    or REASON_SESSION_NONCE_NOT_SERVER_ISSUED,
                }
            )
            self.store.append("rejections", "device_register_rejected", audit_payload)
            return self._response(HTTPStatus.BAD_REQUEST, audit_payload)
        try:
            result = self.miner_roster.enroll(
                passport_id=passport_id,
                device_id=device_id,
                device_public_key_b64=device_public_key_b64,
                pop=pop,
                enrollment_nonce=enrollment_nonce,
                observed_at=self.config.server_clock(),
            )
        except MinerRosterUnavailable:
            audit_payload = _credit_only_envelope(
                {"ok": False, "reason_code": "miner_roster_store_unavailable"}
            )
            self.store.append("events", "miner_roster_store_unavailable", audit_payload)
            return self._response(HTTPStatus.SERVICE_UNAVAILABLE, audit_payload)
        if not result.accepted or result.entry is None:
            audit_payload = _credit_only_envelope(
                {
                    "ok": False,
                    "reason_code": result.reason_code or "device_enrollment_rejected",
                    "passport_id": passport_id,
                    "device_id": device_id,
                }
            )
            self.store.append("rejections", "device_register_rejected", audit_payload)
            return self._response(HTTPStatus.BAD_REQUEST, audit_payload)
        payload_out = _credit_only_envelope(
            {
                "ok": True,
                "enrolled": True,
                "passport_id": result.entry.passport_id,
                "device_id": result.entry.device_id,
                # H_b: server-assigned worker_name for pool-evidence correlation.
                "worker_name": result.entry.worker_name,
                "enrollment_contract": "alice_shadow_device_enrollment_v1",
                # TIGHTEN BEFORE REWARD: open enrollment is sybil-able (Phase J).
                "enrollment_open": self.config.enrollment_open,
            }
        )
        self.store.append("events", "device_registered", payload_out)
        return self._response(HTTPStatus.OK, payload_out)

    def _prl_enroll(
        self, payload: dict[str, Any], *, client_host: str
    ) -> ShadowHttpResponse:
        """ACCOUNT-POLL (PRL/pearlhash) self-serve enrollment, fail-closed throughout.

        The ONE credit-flow piece PRL needs that the stratum lanes get for free at
        login: a miner POSTs {alice_address, label}; the server validates the Alice
        SS58-300 address, applies the per-address/IP anti-spam budget, derives the
        SERVER-OWNED ``worker_name = open_worker_name(address, label)``, mints the
        HMAC-signed session (so the proof-authority scheduler discovers it via the
        registration store's ``pending_sessions()``), and returns the derived
        ``worker_name`` — which the miner passes as ``pearl-miner --worker <name>``
        mining to the single Alice pearlhash wallet. Credit then flows when the PRL
        provider polls that wallet and splits each matured epoch by hashrate proportion.

        ARMOR (mirrors the transport-front open-enrollment edge byte-for-byte):
        * GATE: ALICE_PRL_ACCOUNT_POLL_ENROLLMENT (default OFF). Disabled => 404-shaped
          reject so the endpoint is INVISIBLE until an operator turns it on (it cannot
          be flipped by any stratum open-enrollment flag — independent env var).
        * ADDRESS: the FULL SS58 format-300 + blake2b checksum gate (inside
          ``enroll_account_poll_worker``), NOT a regex — a typo'd/wrong-network/junk
          address is rejected, never persisted under an un-ownable credit key.
        * ANTI-SPAM: the per-address + per-IP + global-new-address budget
          (``self.prl_enrollment_limiter``), the SAME ``OpenEnrollmentLimiter`` the
          stratum open lane uses; over-budget => reject (records nothing).
        * NO PII IN LOGS: the audit envelope carries only the stable reason_code and the
          OPAQUE derived ``worker_name`` (``alc-w-<hex>``) — NEVER the raw Alice address,
          NEVER the client IP. Errors are stable secret-free reason codes.

        CREDIT-ONLY: the minted session inherits every reward/payout/chain guard OFF;
        the response is stamped + asserted credit-only (``paid_acu`` "0").
        """

        # GATE (default OFF) — checked FIRST so the surface is invisible/inert until an
        # operator opts in. A disabled endpoint never parses the body / touches a store.
        if not account_poll_enrollment_enabled():
            audit_payload = _credit_only_envelope(
                {"ok": False, "reason_code": ACCOUNT_POLL_DISABLED}
            )
            self.store.append("rejections", "prl_enroll_rejected", audit_payload)
            # NOT_FOUND-shaped (like an unrouted path): the disabled endpoint reveals
            # nothing about itself beyond the stable reason — no enable/secret hint.
            return self._response(HTTPStatus.NOT_FOUND, audit_payload)

        # Parse the (only) client inputs: the Alice address + optional label. A missing
        # address surfaces as the normal secret-free 400 (``missing_alice_address``); the
        # label is optional (sanitized + defaulted server-side inside the enrollment).
        alice_address = _required_text(payload, "alice_address")
        worker_label = _optional_text(payload, "label")

        # The whole validate -> anti-spam -> derive -> mint -> record chain (fail-closed
        # at every step). ``client_host`` is the SERVER-OBSERVED source IP (the same
        # XFF-resolved host the access edge anchors on, NEVER a client body field) — it
        # feeds ONLY the per-IP anti-spam cap, never the credit identity.
        result = enroll_account_poll_worker(
            AccountPollEnrollmentRequest(
                alice_address=alice_address,
                lane=MAIN_POOL_GPU_PRL,
                worker_label=worker_label,
                peer_ip=client_host,
            ),
            ledger=self.prl_enrollment_ledger,
            store=self.prl_session_store,
            now=self.config.server_clock(),
            limiter=self.prl_enrollment_limiter,
        )
        if not result.accepted:
            # Stable, secret-free reason (bad address / anti-spam / disabled / store
            # unavailable). NO PII: never echo the submitted address back.
            audit_payload = _credit_only_envelope(
                {"ok": False, "reason_code": result.reason_code}
            )
            self.store.append("rejections", "prl_enroll_rejected", audit_payload)
            # A durable-store write failure is a transient 503 (the miner retries);
            # every other reject (bad address / anti-spam / disabled) is a 400.
            status = (
                HTTPStatus.SERVICE_UNAVAILABLE
                if result.reason_code == ACCOUNT_POLL_STORE_UNAVAILABLE
                else HTTPStatus.BAD_REQUEST
            )
            return self._response(status, audit_payload)

        # ACCEPT: return the SERVER-derived worker_name the miner must mine under. The
        # audit + response carry ONLY the opaque worker_name + lane (no raw address/IP).
        payload_out = _credit_only_envelope(
            {
                "ok": True,
                "enrolled": True,
                "reason_code": result.reason_code,
                # The opaque (``alc-w-<hex>``) name the miner passes to
                # ``pearl-miner --worker <worker_name>`` (NOT the raw label).
                "worker_name": result.worker_name,
                "lane": MAIN_POOL_GPU_PRL,
                "enrollment_contract": "alice_prl_account_poll_enrollment_v1",
            }
        )
        # AUDIT carries no PII: the opaque worker_name only (never the Alice address/IP).
        self.store.append(
            "events",
            "prl_enrolled",
            _credit_only_envelope(
                {"ok": True, "worker_name": result.worker_name, "lane": MAIN_POOL_GPU_PRL}
            ),
        )
        return self._response(HTTPStatus.OK, payload_out)

    def _proof_ingest(self, payload: dict[str, Any]) -> ShadowHttpResponse:
        request = MiningProofIngestRequest(
            proof_id=_required_text(payload, "proof_id"),
            session_id=_required_text(payload, "session_id"),
            session_signature=_required_text(payload, "session_signature"),
            lane=_required_text(payload, "lane"),  # type: ignore[arg-type]
            pool_result=_required_text(payload, "pool_result"),
            share_difficulty=_decimal(payload.get("share_difficulty", "0")),
            accepted_count=int(payload.get("accepted_count", 0)),
            rejected_count=int(payload.get("rejected_count", 0)),
            observed_at=_datetime(payload.get("observed_at")),
            algorithm=_required_text(payload, "algorithm"),
            worker_id=_required_text(payload, "worker_id"),
            hashrate=_optional_decimal(payload.get("hashrate")),
            temperature_c=_optional_int(payload.get("temperature_c")),
            power_w=_optional_decimal(payload.get("power_w")),
            efficiency=_optional_decimal(payload.get("efficiency")),
            verified_score=_optional_decimal(payload.get("verified_score")),
            canonical_share_hash=_optional_text(payload, "canonical_share_hash"),
            pool_evidence_ref=_optional_text(payload, "pool_evidence_ref"),
        )
        result = self.harness.proof_ingest(request)
        response = _proof_result_json(result)
        stream = "proofs" if result.accepted else "rejections"
        self.store.append(
            stream,
            "proof_ingest" if result.accepted else "proof_rejected",
            {"request": payload, "result": response},
        )
        return self._response(
            HTTPStatus.OK if result.accepted else HTTPStatus.BAD_REQUEST,
            response,
        )

    def _inference_complete(self, payload: dict[str, Any]) -> ShadowHttpResponse:
        request = InferenceCompletionRequest(
            proof_id=_required_text(payload, "proof_id"),
            session_id=_required_text(payload, "session_id"),
            session_signature=_required_text(payload, "session_signature"),
            model_id=_required_text(payload, "model_id"),
            input_tokens=_required_int(payload, "input_tokens"),
            output_tokens=_required_int(payload, "output_tokens"),
            context_length=_required_int(payload, "context_length"),
            latency_ms=_required_decimal(payload, "latency_ms"),
            model_class=_required_text(payload, "model_class"),
            observed_at=_datetime(payload.get("observed_at")),
            simulated_api_payment=_decimal(payload.get("simulated_api_payment", "0")),
        )
        before_revenue = len(self.harness.ledger.revenue_records)
        result = self.harness.inference_complete(request)
        response = _proof_result_json(result)
        stream = "proofs" if result.accepted else "rejections"
        self.store.append(
            stream,
            "inference_complete" if result.accepted else "inference_rejected",
            {"request": _inference_complete_audit_payload(payload), "result": response},
        )
        if result.accepted and len(self.harness.ledger.revenue_records) > before_revenue:
            self.store.append(
                "foundation_revenue_mock",
                "foundation_revenue_mock_recorded",
                {
                    "proof_id": request.proof_id,
                    "session_id": request.session_id,
                    "amount": request.simulated_api_payment,
                    "source": "simulated_api_payment",
                },
            )
        return self._response(
            HTTPStatus.OK if result.accepted else HTTPStatus.BAD_REQUEST,
            response,
        )

    def _heartbeat(self, payload: dict[str, Any]) -> ShadowHttpResponse:
        request = ShadowHeartbeatRequest(
            passport_id=_required_text(payload, "passport_id"),
            device_id=_required_text(payload, "device_id"),
            device_label=_required_text(payload, "device_label"),
            supported_lanes=_lane_tuple(payload.get("supported_lanes")),
            status=_required_text(payload, "status"),
            observed_at=_datetime(payload.get("observed_at")),
        )
        result = self.harness.heartbeat(request)
        # H_a: bump roster last_seen liveness for an ENROLLED identity (no-op for
        # an unenrolled one — heartbeat never implicitly enrolls). Echo the
        # server-assigned worker_name when present. A roster-store OSError must
        # not 500 the heartbeat: liveness is best-effort telemetry, the heartbeat
        # itself already succeeded.
        if result["accepted"] and self.miner_roster is not None:
            try:
                entry = self.miner_roster.record_liveness(
                    passport_id=request.passport_id,
                    device_id=request.device_id,
                    observed_at=request.observed_at,
                )
            except MinerRosterUnavailable:
                entry = None
            if entry is not None:
                result = dict(result)
                result["worker_name"] = entry.worker_name
                result["roster_liveness_recorded"] = True
        stream = "heartbeats" if result["accepted"] else "rejections"
        event_type = "heartbeat" if result["accepted"] else "heartbeat_rejected"
        self.store.append(stream, event_type, result)
        return self._response(
            HTTPStatus.OK if result["accepted"] else HTTPStatus.BAD_REQUEST,
            result,
        )

    def _shadow_window(
        self,
        query: dict[str, list[str]],
        *,
        persist: bool,
    ) -> ShadowHttpResponse:
        window = _window_from_query(query)
        result = self.harness.shadow_window(window)
        payload = _settlement_result_json(result)
        payload["read_only_preview"] = not persist
        if persist:
            self.store.append("settlement_windows", "settlement_window", payload)
            for balance in payload["device_credits"].values():
                self.store.append("balances", "balance", balance)
        return self._response(HTTPStatus.OK, payload)

    def _shadow_balance(
        self,
        query: dict[str, list[str]],
        *,
        persist: bool,
    ) -> ShadowHttpResponse:
        passport_id = _query_text(query, "passport_id")
        device_id = _query_text(query, "device_id")
        window = _window_from_query(query)
        balance = self.harness.shadow_balance(
            passport_id=passport_id,
            device_id=device_id,
            window=window,
        )
        payload = _dataclass_json(balance)
        payload["read_only_preview"] = not persist
        if persist:
            self.store.append("balances", "balance_lookup", payload)
        return self._response(HTTPStatus.OK, payload)

    def _kill_switch(self, payload: dict[str, Any]) -> ShadowHttpResponse:
        enabled = bool(payload.get("enabled", True))
        reason_code = _required_text(payload, "reason_code")
        actor = _optional_text(payload, "actor") or "shadow_admin"
        self.harness.admin_kill_switch(enabled=enabled)
        result = build_shadow_kill_switch_payload(
            enabled=enabled,
            reason_code=reason_code,
            actor=actor,
        )
        self.store.append("events", "admin_kill_switch", result)
        return self._response(HTTPStatus.OK, result)

    def _health_payload(self, *, readiness: bool) -> dict[str, object]:
        return build_shadow_health_payload(
            self.harness.health()
            | {
                "ok": True,
                "service": self.config.service_name,
                "deployment_role": self.config.deployment_role,
                "public_primary_candidate": self.config.public_primary_candidate,
                "production_primary_candidate": self.config.public_primary_candidate,
                "staging_internal_only": self.config.staging_internal_only,
                "readiness": "ready_for_shadow_beta" if readiness else "health_only",
                "bind_host": self.config.bind_host,
                "port": self.config.port,
                "storage": "jsonl_append_only",
                "audit_storage": "append_only_jsonl",
                "reward_mode": self.config.reward_mode,
                "chain_writes_enabled": False,
                # credit-only: advertise paid_acu="0" on the liveness/contract
                # surface alongside the other disabled reward gates (mirrors the
                # per-response _credit_only_envelope invariant).
                "paid_acu": "0",
                "live_reward_state": "disabled_pending_runtime_budget_anticheat_audit",
                "payout_executor_state": "disabled",
                "endpoint_contract": {
                    "status": "/status",
                    "readiness": "/readiness",
                    "session_nonce": "/session/nonce",
                    "device_register": "/device/register",
                    "session_issue": "/session/issue",
                    "proof_ingest": "/proof/ingest",
                    "heartbeat": "/heartbeat",
                    "inference_admit": "/inference/admit",
                    "inference_complete": "/inference/complete",
                    "settlement_preview": "/shadow/window/preview",
                    "balance_preview": "/shadow/balance/preview",
                    "admin_kill_switch": "/admin/kill-switch",
                },
                "miner_enrollment": {
                    "contract": "alice_shadow_device_enrollment_v1",
                    "require_server_issued_nonce": self.config.require_server_issued_nonce,
                    "enrollment_open": self.config.enrollment_open,
                    "roster_configured": self.miner_roster is not None,
                    # TIGHTEN BEFORE REWARD: open enrollment is sybil-able (Phase J).
                    "reward_gate": "tighten_enrollment_before_paid_acu",
                },
                "access_policy": self.config.access_policy.as_payload(),
                "rate_limit": self.config.rate_limit_policy.as_payload(),
                "admission_store": {
                    "contract": "alice_shadow_admission_store_v1",
                    "identity_material": "hashed_only",
                    "audit_hash": self.admission_store.audit_hash(),
                },
                "proof_dedup": {
                    "contract": "alice_shadow_proof_dedup_store_v1",
                    "storage": "append_only_jsonl",
                    "local_only": True,
                },
            }
        )

    def _authorize(
        self,
        headers: dict[str, str],
        client_host: str,
        *,
        via_trusted_proxy: bool = False,
        is_mining_route: bool = False,
    ) -> ShadowAccessDecision:
        if self.config.localhost_only and client_host in self.config.access_policy.local_hosts:
            return ShadowAccessDecision(True, "authorized_localhost", "local", False, 200)
        # B1: ``via_trusted_proxy`` / ``is_mining_route`` are non-default ONLY when
        # the public-miner gate is ON and the request arrived via the trusted proxy
        # (resolved upstream in ``handle``); otherwise both are False and this is the
        # pre-B1 access decision unchanged.
        return evaluate_shadow_access(
            policy=self.config.access_policy,
            client_host=client_host,
            authorization_header=headers.get("authorization", ""),
            auth_secret=self.config.auth_secret,
            via_trusted_proxy=via_trusted_proxy,
            is_mining_route=is_mining_route,
        )

    def _enforce_rate_limit(
        self,
        *,
        route: str,
        headers: dict[str, str],
        payload: dict[str, Any],
        client_host: str,
    ) -> ShadowHttpResponse | None:
        if self.harness.ledger.kill_switch_enabled:
            return None
        identity = resolve_shadow_rate_limit_identity(
            headers=headers,
            payload=payload,
            client_host=client_host,
        )
        if identity is None:
            audit_payload = {
                "ok": False,
                "route": route,
                "reason_code": RATE_LIMIT_IDENTITY_MISSING_REASON,
                "identity_material": "hashed_only",
            }
            self.store.append("events", "rate_limit_identity_missing", audit_payload)
            return self._response(HTTPStatus.UNAUTHORIZED, audit_payload)
        try:
            key_status = (
                self.admission_store.key_status(api_key_hash=identity.api_key_hash)
                if identity.api_key_hash
                else None
            )
            if key_status is not None and not key_status.active:
                audit_payload = {
                    "ok": False,
                    "route": route,
                    "reason_code": "api_key_revoked",
                    "rate_limit": {
                        "identity": identity.as_audit_payload(),
                        "key_status": key_status.state,
                        "key_reason_code": key_status.reason_code,
                    },
                    "identity_material": "hashed_only",
                }
                self.store.append("events", "admission_api_key_revoked", audit_payload)
                return self._response(HTTPStatus.FORBIDDEN, audit_payload)
            decision = self.admission_store.evaluate_and_record(
                policy=self.config.rate_limit_policy,
                route=route,
                identity=identity,
                observed_at=datetime.now(UTC),
            )
        except AdmissionStoreUnavailable:
            audit_payload = {
                "ok": False,
                "route": route,
                "reason_code": "admission_store_unavailable",
                "identity_material": "hashed_only",
            }
            self.store.append("events", "admission_store_unavailable", audit_payload)
            return self._response(HTTPStatus.SERVICE_UNAVAILABLE, audit_payload)
        if decision.allowed:
            return None
        audit_payload = {
            "ok": False,
            "route": route,
            "reason_code": decision.reason_code,
            "retry_after": decision.retry_after_seconds,
            "rate_limit": {
                "identity": identity.as_audit_payload(),
                "limit": decision.limit,
                "observed_count": decision.observed_count,
                "window_seconds": decision.window_seconds,
                "window_kind": decision.window_kind,
                "remaining_hourly": decision.remaining_hourly,
                "remaining_burst": decision.remaining_burst,
                "retry_after": decision.retry_after_seconds,
            },
            "identity_material": "hashed_only",
        }
        self.store.append("events", "rate_limit_rejected", audit_payload)
        return self._response(HTTPStatus.TOO_MANY_REQUESTS, audit_payload)

    @staticmethod
    def _rate_limited_route(method: str, path: str) -> bool:
        if method != "POST":
            return False
        # /device/register carries (passport, device) + client-IP identity, so it
        # is throttled like the other write routes. /session/nonce is pre-identity
        # (no body) and bounded by short-TTL single-use, so it is not in this set.
        return path in {
            "/session/issue",
            "/proof/ingest",
            "/heartbeat",
            "/device/register",
            # ACCOUNT-POLL (PRL) self-serve enrollment: a write route carrying the
            # Alice-address + client-IP identity, so it rides the SAME per-identity HTTP
            # rate limiter the other write routes do (the transport-front
            # OpenEnrollmentLimiter is the SECOND, per-Alice-address/IP anti-spam budget
            # enforced inside ``_prl_enroll``). Two independent throttles, both fail-closed.
            "/prl/enroll",
        } or path.startswith("/inference/")

    @staticmethod
    def _response(status: HTTPStatus, payload: dict[str, Any]) -> ShadowHttpResponse:
        return ShadowHttpResponse(int(status), payload)


def create_http_server(app: ShadowHttpApp) -> ThreadingHTTPServer:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self._dispatch()

        def do_POST(self) -> None:
            self._dispatch()

        def log_message(self, format: str, *args: object) -> None:
            return

        def _dispatch(self) -> None:
            length = int(self.headers.get("content-length", "0") or "0")
            body = self.rfile.read(length) if length else b""
            response = app.handle(
                method=self.command,
                path=self.path,
                headers={key.lower(): value for key, value in self.headers.items()},
                body=body,
                client_host=self.client_address[0],
            )
            body_bytes = json.dumps(response.payload, sort_keys=True).encode("utf-8")
            self.send_response(response.status)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(body_bytes)))
            self.end_headers()
            self.wfile.write(body_bytes)

    return ThreadingHTTPServer((app.config.bind_host, app.config.port), Handler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Alice ACP shadow reward server")
    parser.add_argument("--host", default=os.environ.get("ALICE_ACP_SHADOW_HOST", "127.0.0.1"))
    parser.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("ALICE_ACP_SHADOW_PORT", "18130")),
    )
    parser.add_argument(
        "--data-dir",
        default=os.environ.get("ALICE_ACP_SHADOW_DATA_DIR", "/var/lib/alice-acp-shadow"),
    )
    parser.add_argument("--auth-secret-file", default=os.environ.get("ALICE_ACP_SHADOW_AUTH_FILE"))
    parser.add_argument("--require-auth", action="store_true")
    parser.add_argument("--service-name", default=os.environ.get("ALICE_ACP_SERVICE_NAME"))
    parser.add_argument("--deployment-role", default=os.environ.get("ALICE_ACP_DEPLOYMENT_ROLE"))
    parser.add_argument(
        "--public-primary-candidate",
        action="store_true",
        default=_env_truthy("ALICE_ACP_PUBLIC_PRIMARY_CANDIDATE"),
    )
    # public-XFF wiring: the trusted reverse-proxy allowlist (comma-separated IPs,
    # whitespace-tolerant). The launch topology is PUBLIC (external miners -> nginx
    # -> internal ACP), so the edge recovers the real client IP from
    # X-Forwarded-For ONLY when the socket peer is one of these proxy IPs. Empty /
    # unset => () => fail-closed (never trust a client-supplied forwarded header;
    # the socket peer is authoritative) — the one-line rollback. Invalid IPs are
    # rejected at config-construction time (ShadowAccessPolicy.__post_init__).
    parser.add_argument(
        "--trusted-proxy-ips",
        default=os.environ.get("ALICE_ACP_SHADOW_TRUSTED_PROXY_IPS", ""),
    )
    # H_a: external-miner self-enrollment. Open by default for credit-only (TIGHTEN
    # BEFORE REWARD, Phase J). --no-enrollment flips the roster fully fail-closed.
    parser.add_argument(
        "--no-enrollment",
        dest="enrollment_open",
        action="store_false",
        default=not _env_truthy("ALICE_ACP_SHADOW_ENROLLMENT_CLOSED"),
    )
    args = parser.parse_args()

    auth_secret = None
    if args.auth_secret_file:
        auth_secret = Path(args.auth_secret_file).read_text(encoding="utf-8").strip()
    localhost_only = args.host in {"127.0.0.1", "localhost", "::1"} and not args.require_auth
    service_name = args.service_name or (
        "alice-acp-primary-candidate"
        if args.public_primary_candidate
        else "queue13s-shadow-server-harness"
    )
    deployment_role = args.deployment_role or (
        "production_primary_candidate" if args.public_primary_candidate else "shadow_beta"
    )
    # public-XFF wiring: build the access policy with the deploy-configured trusted
    # reverse-proxy allowlist. Comma-separated, whitespace-tolerant; empty/unset =>
    # () => fail-closed. ShadowAccessPolicy.__post_init__ raises ValueError on any
    # invalid IP, so a misconfigured allowlist fails at startup, not at request time.
    trusted_proxy_ips = tuple(
        ip.strip() for ip in args.trusted_proxy_ips.split(",") if ip.strip()
    )
    # B1 ("public-miner gate"): DEFAULT OFF, named + wired like the existing
    # scheduler flags (ALICE_ACP_SETTLEMENT_SCHEDULER_ENABLED /
    # ALICE_ACP_PROOF_AUTHORITY_SCHEDULER_ENABLED). OFF => external clients are
    # rejected exactly as today (the access edge never enters the public-miner
    # branch). ON => an external client behind the trusted proxy may reach the
    # mining flow ONLY (session-nonce / device-register / mining session-issue /
    # proof-ingest / heartbeat), proving identity via the unchanged downstream
    # PoP/nonce/roster ledger gates; everything else stays rejected. CREDIT-ONLY:
    # this opens transport for the mining flow; it never touches a reward/payout/
    # chain guard. The one-line rollback is unsetting the env var (=> default OFF).
    public_miner_gate_enabled = _env_truthy("ALICE_ACP_PUBLIC_MINER_GATE_ENABLED")
    access_policy = ShadowAccessPolicy(
        trusted_proxy_ips=trusted_proxy_ips,
        public_miner_gate_enabled=public_miner_gate_enabled,
    )
    data_dir = Path(args.data_dir)
    server_clock: Callable[[], datetime] = lambda: datetime.now(UTC)
    # H_b: wire the REAL per-lane pool-evidence providers from env (owner-known
    # pool addresses). Any lane without its address/pool_id env vars stays
    # fail-closed; with NO lane vars set this is an all-fail-closed router (the
    # production default, identical to NoPoolEvidenceProvider). The F2Pool API
    # secret is read at poll time from ALICE_F2POOL_API_SECRET, never here.
    #
    # M0/M1 PROXY-POOL CREDIT PLANE (doc §2.4/§2.8): when
    # ALICE_PROXY_VALIDATED_SHARE_STORE names a path, the share-hash lanes
    # (XMR/RVN/LTC) are wired with the SELF-VALIDATED ProxyPoolEvidenceProvider
    # DRAINING that shared durable JSONL — the same file the SEPARATE
    # transport-service process APPENDS validated shares to. Active-passive credit
    # plane: the transport service is the SOLE APPENDER; this credit server is the
    # SOLE DRAINER (drain_one writes a "spent" marker). build_pool_evidence_provider_
    # from_env reads the env path itself; unset => the historical upstream-poll
    # providers (purely additive). CREDIT-ONLY: this is a credit gate on Alice's own
    # re-hash; it never touches a reward/payout/chain symbol.
    from alice_acp.shadow_server.pool_evidence_providers import (
        JsonlPoolShareCursorStore,
        JsonlPrlEpochCursorStore,
        JsonlPrlPendingShareSnapshotStore,
        UrllibPoolHttpClient,
        build_pool_evidence_provider_from_env,
    )

    pool_evidence_provider = build_pool_evidence_provider_from_env(
        http_client=UrllibPoolHttpClient(),
        clock=server_clock,
        cursor_store=JsonlPoolShareCursorStore(data_dir / "pool_share_cursors.jsonl"),
        # PRL lane durable stores (doc §2.3/§2.4): the spent-SET cursor (dedup) and
        # the pending-share snapshot (the work-weight carried across the days-long
        # pending->matured gap so credit is WORK-WEIGHTED, not flat). Both are
        # CREDIT-ONLY (routing triple + opaque share fraction; no payout/chain) and
        # live next to the share cursor under data_dir.
        prl_epoch_cursor=JsonlPrlEpochCursorStore(data_dir / "prl_epoch_cursors.jsonl"),
        prl_pending_share_store=JsonlPrlPendingShareSnapshotStore(
            data_dir / "prl_pending_shares.jsonl"
        ),
        # The shared validated-share store path (ALICE_PROXY_VALIDATED_SHARE_STORE) is
        # read inside the builder; passing env=None makes os.environ authoritative,
        # matching how the other lane env vars are read here.
        env=None,
    )
    config = ShadowHttpConfig(
        bind_host=args.host,
        port=args.port,
        data_dir=data_dir,
        auth_secret=auth_secret,
        localhost_only=localhost_only,
        service_name=service_name,
        deployment_role=deployment_role,
        public_primary_candidate=args.public_primary_candidate,
        # public-XFF: deploy-configured trusted reverse-proxy allowlist (fail-closed
        # by default; the one-line rollback is unsetting ALICE_ACP_SHADOW_TRUSTED_PROXY_IPS).
        access_policy=access_policy,
        staging_internal_only=not args.public_primary_candidate,
        enrollment_open=args.enrollment_open,
        # H_a: the deployed edge enforces server-issued single-use issuance nonces.
        require_server_issued_nonce=True,
        server_clock=server_clock,
        # H_b: real pool-evidence providers (fail-closed for any unconfigured lane).
        pool_evidence_provider=pool_evidence_provider,
    )
    # H_a: provision the durable miner roster (its device registry is the C2
    # source of truth) + server-issued nonce store, both rooted at data_dir.
    roster = JsonlMinerRoster(data_dir, enrollment_open=args.enrollment_open)
    # ACCOUNT-POLL (PRL) registration store: build it ONCE here and share the SAME
    # instance between (a) the /prl/enroll endpoint (which APPENDS the minted session)
    # and (b) the proof-authority scheduler's target source (which DISCOVERS it via
    # ``pending_sessions()``). In this SINGLE credit-server process the two halves of
    # the loop MUST point at one store/file or an enrollment would never become a poll
    # target. ``_build_prl_account_poll_store`` returns the durable JSONL ONLY when
    # account-poll is enabled AND ALICE_PRL_ACCOUNT_POLL_SESSION_STORE names a path
    # (the deploy default); else ``None`` => the app's in-memory default + the scheduler
    # discovers no account-poll sessions (byte-for-byte today's behaviour — default OFF).
    prl_session_store = _build_prl_account_poll_store()
    app = ShadowHttpApp(
        config,
        miner_roster=roster,
        issuance_nonce_store=JsonlIssuanceNonceStore(data_dir),
        prl_session_store=prl_session_store,
    )
    server = create_http_server(app)
    # H_d / Approach B: construct the two server-side cadence schedulers (settlement
    # + proof-authority poll). BOTH are env-gated and OFF by default, so with no
    # flags set NO thread starts and every request path (incl. /proof/ingest) is
    # byte-for-byte unchanged. reward/payout/chain stay OFF; the credit is settled /
    # produced exclusively through the unchanged ledger guards (paid_acu == "0").
    schedulers = _build_schedulers(
        app=app,
        server_clock=server_clock,
        pool_evidence_provider=pool_evidence_provider,
        roster=roster,
        # Share the EXACT store instance (or ``None`` when account-poll is
        # disabled/unconfigured) with the scheduler's target source, so an enrollment
        # the endpoint records here is the one the scheduler discovers.
        prl_session_store=prl_session_store,
    )
    for scheduler in schedulers:
        _start_scheduler_thread(scheduler)
    try:
        server.serve_forever()
    finally:
        # Fail-safe shutdown: stop every started scheduler so its daemon thread
        # wakes from the interruptible wait and exits promptly.
        for scheduler in schedulers:
            scheduler.stop()


def _build_schedulers(
    *,
    app: ShadowHttpApp,
    server_clock: Callable[[], datetime],
    pool_evidence_provider: object,
    roster: JsonlMinerRoster,
    prl_session_store: object | None = None,
) -> list[object]:
    """Build the ENABLED server-side cadence schedulers (settlement + proof-poll).

    Returns only the schedulers whose env flag is truthy. Both default OFF
    (explicit-enable), so an operator opts each in at deploy. Each is given the
    real ``server_clock`` and an audit hook that appends the tick's
    ``to_public_dict()`` to ``app.store`` (``events`` stream), mirroring the rest
    of the credit-only audit surface. CREDIT-ONLY throughout.
    """

    from alice_acp.shadow_server.proof_authority_scheduler import (
        ProofAuthorityScheduler,
        ProofAuthoritySchedulerConfig,
        ProofAuthorityTickResult,
    )
    from alice_acp.shadow_server.settlement_scheduler import (
        SettlementScheduler,
        SettlementSchedulerConfig,
        SettlementTickResult,
    )

    started: list[object] = []

    if _env_truthy("ALICE_ACP_SETTLEMENT_SCHEDULER_ENABLED"):
        interval = _env_interval(
            "ALICE_ACP_SETTLEMENT_INTERVAL_SECONDS", DEFAULT_WINDOW_DURATION
        )

        def _settlement_audit(result: SettlementTickResult) -> None:
            app.store.append("events", "settlement_tick", result.to_public_dict())

        started.append(
            SettlementScheduler(
                harness=app.harness,
                config=SettlementSchedulerConfig(enabled=True, interval=interval),
                clock=server_clock,
                audit_hook=_settlement_audit,
            )
        )

    if _env_truthy("ALICE_ACP_PROOF_AUTHORITY_SCHEDULER_ENABLED"):
        poll_interval = _env_interval(
            "ALICE_ACP_PROOF_AUTHORITY_POLL_INTERVAL_SECONDS", timedelta(seconds=60)
        )

        def _proof_authority_audit(result: ProofAuthorityTickResult) -> None:
            app.store.append("events", "proof_authority_tick", result.to_public_dict())

        started.append(
            ProofAuthorityScheduler(
                harness=app.harness,
                config=ProofAuthoritySchedulerConfig(
                    enabled=True, poll_interval=poll_interval
                ),
                worker_targets=_build_proof_authority_targets(
                    app=app,
                    roster=roster,
                    pool_evidence_provider=pool_evidence_provider,
                    # The SAME registration store the /prl/enroll endpoint appends to
                    # (built once in ``main`` and injected into both halves), so a
                    # self-serve PRL enrollment in THIS process becomes a poll target.
                    # ``None`` when account-poll is disabled / unconfigured (default).
                    prl_session_store=prl_session_store,
                ),
                clock=server_clock,
                # No explicit provider: the harness uses its configured provider
                # (the same LaneRoutingPoolEvidenceProvider wired into config).
                audit_hook=_proof_authority_audit,
            )
        )

    return started


#: The durable JSONL the ACCOUNT-POLL (PRL) enrollment endpoint appends signed
#: sessions to and the proof-authority scheduler discovers via ``pending_sessions()``.
#: When this two-process split applies, the enrollment edge and the credit server
#: point at the SAME path. Unset (or account-poll disabled) => no account-poll
#: sessions are discovered (the historical behaviour). NOT a secret — it is a
#: filesystem path to public session envelopes (the file is created restrictively).
PRL_ACCOUNT_POLL_SESSION_STORE_ENV = "ALICE_PRL_ACCOUNT_POLL_SESSION_STORE"


def _build_prl_account_poll_store() -> object | None:
    """Build the durable PRL account-poll registration store from env, else ``None``.

    Returns a :class:`JsonlPrlSessionRegistrationStore` ONLY when account-poll
    enrollment is enabled AND :data:`PRL_ACCOUNT_POLL_SESSION_STORE_ENV` names a path
    (the deploy default). With either unset this returns ``None`` so the scheduler
    discovers no account-poll sessions — byte-for-byte the historical behaviour, so
    this whole path is additive + reversible (default OFF).
    """

    if not account_poll_enrollment_enabled():
        return None
    path = os.environ.get(PRL_ACCOUNT_POLL_SESSION_STORE_ENV, "").strip()
    if not path:
        return None
    return JsonlPrlSessionRegistrationStore(path=Path(path))


def _build_proof_authority_targets(
    *,
    app: ShadowHttpApp,
    roster: JsonlMinerRoster,
    pool_evidence_provider: object,
    env: dict[str, str] | None = None,
    prl_session_store: object | None = None,
) -> Callable[[], list[Any]]:
    """Build the live target source: roster ∩ provider lanes ∩ ledger.sessions.

    ``env`` is the source the OPEN-enrollment gate is read from (the per-lane
    ``ALICE_LTC_OPEN_ENROLLMENT`` flag); ``None`` => ``os.environ`` (the deploy
    default — the same source the rest of this module's flags use). It is injectable
    so a two-process test can pin the credit server's open-enrollment posture.

    ``prl_session_store`` is the ACCOUNT-POLL (PRL/pearlhash) registration store
    (a ``PrlSessionRegistrationStore``); when supplied, its
    :meth:`pending_sessions` are unioned into the candidates exactly like the proxy
    store's, so a self-serve PRL worker (which has NO stratum session) becomes a poll
    target and its epoch-share credits. ``None`` => no account-poll sessions (the
    historical behaviour). The credit-side re-verifies each such session's signature
    before crediting, so discovering it here is safe.

    Re-read on every tick (a zero-arg callable), so workers enrolled / sessions
    issued after start are picked up next cycle. For each session whose SERVER-OWNED
    ``worker_name`` resolves (an ENROLLED, ACTIVE roster worker — OR, on the open
    self-serve lane, an open-enrollment identity re-derived from its HMAC-signed
    address+label; see :func:`_credit_worker_name`), emit one
    :class:`ProofAuthorityTarget` per CONFIGURED provider lane, carrying the
    session's lane, that provider's server-owned ``pool_id`` + bound collection
    address (``expected_collection_address`` or ``alice_address`` — NEVER a client
    body field), the server-owned ``worker_name`` and the session.

    Pairing a session with every configured provider is SAFE and self-gating for the
    SHARE-HASH lanes: a provider's :meth:`evidence_for` returns ``None`` (no credit)
    unless the reconstructed proof's ``pool_id`` AND bound collection address match the
    provider AND the pool's per-worker snapshot lists that worker with an un-spent
    accepted-share delta. So a share-hash session paired with a non-matching lane's
    provider simply credits nothing — there is no lane→pool_id field on the session to
    pre-filter on (lane→pool_id is many-to-one), and the provider cache means a burst
    of sessions on one lane still costs one upstream request per cadence.

    PRL TARGET-BINDING (the audit's attribution fix): the PRL epoch provider
    (:class:`PearlhashPoolEvidenceProvider`) is bound to the PRL ACCOUNT-POLL lane
    (``main_pool_gpu_prl``) ONLY. A non-PRL session (an LTC/XMR/RVN stratum session) is
    NEVER paired with the PRL provider, and a PRL account-poll session is never paired
    with a non-PRL provider. This is belt-and-braces with the provider's own
    participation gate (the PRL provider emits no evidence + spends no epoch for a
    worker absent from pearlhash's ``connected_workers[]``, so a non-PRL worker collects
    ZERO PRL credit even if mis-paired): the lane pre-filter additionally avoids
    needless PRL polls for non-PRL workers and stops a non-PRL worker_name from ever
    reaching the PRL epoch path at all.
    """

    from alice_acp.shadow_server.pool_evidence_providers import (
        LaneRoutingPoolEvidenceProvider,
        PearlhashPoolEvidenceProvider,
        ProxyPoolEvidenceProvider,
    )
    from alice_acp.shadow_server.proof_authority_scheduler import ProofAuthorityTarget

    #: The lane the ACCOUNT-POLL (non-stratum) enrollment may credit. PRL is the
    #: GPU-PRIMARY lane and has its OWN credit lane ``main_pool_gpu_prl`` (a sibling GPU
    #: lane sharing the GPU sub-budget with RVN + Quai, NOT the stratum RVN lane id);
    #: pearlhash is its pool. Keying on a PRL-specific lane keeps the account-poll credit
    #: gate from colliding with the stratum RVN open lane. Gated additionally by the
    #: account-poll flag below.
    from alice_acp.shadow_server.types import MAIN_POOL_GPU_PRL as _ACCOUNT_POLL_LANE
    from alice_acp.transport_front.account_poll_enrollment import (
        account_poll_enrollment_enabled,
    )
    from alice_acp.transport_front.identity import _OPEN_ENROLLMENT_LANES
    from alice_acp.transport_front.open_enrollment import (
        open_enrollment_enabled,
        open_worker_name,
    )

    def _candidate_sessions(
        provider_router: LaneRoutingPoolEvidenceProvider,
    ) -> list[Any]:
        """Sessions to credit this tick: ledger.sessions UNION carried sessions.

        CROSS-PROCESS CREDIT PLANE (doc §2.8): on the credit server, sessions minted
        by the SEPARATE transport service are NOT in ``ledger.sessions`` (the
        transport minted them in its own ledger). They instead ride the shared
        validated-share store on each un-spent share. So the target source unions
        ``ledger.sessions`` (the in-process / HTTP-issued sessions) with the carried
        sessions the proxy providers' stores expose via ``pending_sessions()``
        (NON-consuming), deduped by ``session_id``. ``credit_attested_shares`` then
        re-verifies a carried session's signature against the shared auth-secret
        before admitting it (fail-closed), so discovering it here is safe — a
        forged/tampered carried session simply credits nothing.

        ACCOUNT-POLL CREDIT PLANE (PRL/pearlhash): PRL has NO stratum front (pearl-
        miner mines pearlhash directly), so a self-serve PRL worker has no stratum
        session in EITHER ledger or the proxy store. Its HMAC-signed session lives in
        the account-poll registration store (``prl_session_store``); union its
        ``pending_sessions()`` here so the worker becomes a poll target. SAME safety:
        the credit call re-verifies the signature before any epoch drains.
        """

        by_id: dict[str, Any] = {
            session.session_id: session for session in app.harness.ledger.sessions.values()
        }
        for provider in provider_router.providers_by_pool_id.values():
            if not isinstance(provider, ProxyPoolEvidenceProvider):
                continue
            for session in provider.validated_share_store.pending_sessions():
                by_id.setdefault(session.session_id, session)
        # Account-poll (PRL) registrations — the non-stratum credit plane.
        if prl_session_store is not None:
            for session in prl_session_store.pending_sessions():
                by_id.setdefault(session.session_id, session)
        return list(by_id.values())

    def _credit_worker_name(session: Any) -> str | None:
        """The SERVER-OWNED ``worker_name`` to credit this session under, or ``None``.

        Two trust models, mutually exclusive and both anchored on SERVER-OWNED state
        (never the carried, unsigned ``session.worker_name`` field):

        * ENROLLED (the roster HIT, byte-for-byte unchanged): the SHARED durable roster
          resolves ``(passport_id, device_id)`` to an opaque ``alc-w-<hex>`` name AND
          that name equals the carried ``worker_name``. This is the only path that ever
          existed; a revoked / unknown enrolled identity yields no (matching) name and
          is filtered out (fail-closed).

        * OPEN self-serve (the roster MISS): the identity was admitted on a VALIDATED
          ALICE address (the SS58 format-300 Alice-token destination — V cross-lane
          directive; no roster row exists for it, so the recheck above can NEVER
          match). We re-derive the same deterministic ``open_worker_name`` from the
          HMAC-**SIGNED** ``passport_id`` (the Alice address) + ``device_id`` (the
          sanitized worker label) — fields covered by the session signature this
          scheduler's credit call re-verifies (``_session_signature`` mixes
          passport_id/device_id/lane/expiry/nonce; ``worker_name`` is NOT in the MAC).
          TWO independently-gated sources mint such a session, each on the SAME signed
          derivation:

          - the STRATUM open lanes (``_OPEN_ENROLLMENT_LANES``) when the per-lane
            ``ALICE_*_OPEN_ENROLLMENT`` flag is on (``open_enrollment_enabled``);
          - the ACCOUNT-POLL (non-stratum) lane (PRL/pearlhash —
            ``main_pool_gpu_prl``) when ``ALICE_PRL_ACCOUNT_POLL_ENROLLMENT`` is on
            (``account_poll_enrollment_enabled``). PRL has no stratum front, so this is
            the ONLY way a PRL worker gets a credit name. The two flags are
            INDEPENDENT — neither can enable the other's path; and because PRL now keys
            on its OWN lane (NOT ``main_pool_gpu_rvn``), the account-poll gate and the
            stratum RVN open gate no longer collide on one lane.

          A session on neither path returns ``None`` → filtered out. The carried
          ``worker_name`` is used ONLY as a defensive equality assert, NEVER as the
          trust anchor: an attacker who forges the unsigned field cannot move credit
          because the address+label that key the credit are the SIGNED ones and the
          signature is re-verified before any drain.
        """

        passport_id = session.passport_id
        device_id = session.device_id
        carried = session.worker_name

        # ENROLLED path (unchanged): roster HIT that matches the carried name.
        roster_name = roster.worker_name_for(passport_id=passport_id, device_id=device_id)
        if roster_name is not None and roster_name == carried:
            return roster_name
        # A roster row that exists but DISAGREES with the carried name is a tampered /
        # stale envelope on an enrolled identity → fail-closed (never fall through to
        # the open derivation for an on-roster identity).
        if roster_name is not None:
            return None

        # OPEN / ACCOUNT-POLL path (roster MISS): re-derive from SIGNED fields, gated to
        # an enrolled lane + the matching per-lane flag. The session signature is
        # re-verified by the credit call (admit_cross_process_session) before any share
        # drains, so trusting the signed passport_id/device_id here is safe; a session
        # that fails verification credits nothing regardless of the name we derive.
        stratum_open = (
            session.lane in _OPEN_ENROLLMENT_LANES and open_enrollment_enabled(env)
        )
        account_poll = (
            session.lane == _ACCOUNT_POLL_LANE and account_poll_enrollment_enabled(env)
        )
        if not (stratum_open or account_poll):
            return None
        derived = open_worker_name(address=passport_id, worker_label=device_id)
        # Defensive ONLY: the transport stamps this exact name onto the carried session,
        # so a present-but-different carried name is a construction skew → fail-closed.
        # The trust is the re-derivation from signed fields, not this compare.
        if carried is not None and carried != derived:
            return None
        return derived

    def _lane_provider_compatible(session: Any, provider: object) -> bool:
        """Gate the PRL epoch provider to the PRL account-poll lane (and vice-versa).

        The PRL provider must ONLY poll-and-credit PRL-enrolled sessions
        (``main_pool_gpu_prl``); a non-PRL (LTC/XMR/RVN stratum) session must never be
        paired with it, and a PRL account-poll session must never be paired with a
        non-PRL (share-hash) provider. Every other (non-PRL session, non-PRL provider)
        pairing stays UNCHANGED — those lanes self-gate on pool_id + collection +
        snapshot membership exactly as before (lane→pool_id is many-to-one there, so
        there is nothing safe to pre-filter on).
        """

        is_prl_provider = isinstance(provider, PearlhashPoolEvidenceProvider)
        is_prl_session = session.lane == _ACCOUNT_POLL_LANE
        # XOR: a PRL provider takes ONLY PRL-lane sessions; a PRL-lane session goes
        # ONLY to the PRL provider.
        return is_prl_provider == is_prl_session

    def _targets() -> list[Any]:
        if not isinstance(pool_evidence_provider, LaneRoutingPoolEvidenceProvider):
            return []
        providers = pool_evidence_provider.providers_by_pool_id
        targets: list[ProofAuthorityTarget] = []
        for session in _candidate_sessions(pool_evidence_provider):
            worker_name = _credit_worker_name(session)
            if not worker_name:
                continue
            for pool_id, provider in providers.items():
                if not _lane_provider_compatible(session, provider):
                    continue
                bound_address = provider.expected_collection_address or provider.alice_address
                targets.append(
                    ProofAuthorityTarget(
                        lane=session.lane,
                        pool_id=pool_id,
                        alice_collection_address=bound_address,
                        worker_name=worker_name,
                        session=session,
                    )
                )
        return targets

    return _targets


def _start_scheduler_thread(scheduler: object) -> None:
    import threading

    thread = threading.Thread(
        target=scheduler.run_forever,  # type: ignore[attr-defined]
        name=f"{type(scheduler).__name__}-loop",
        daemon=True,
    )
    thread.start()


def _env_interval(name: str, default: timedelta) -> timedelta:
    raw = os.environ.get(name, "").strip()
    if not raw:
        return default
    try:
        seconds = float(raw)
    except ValueError:
        return default
    if seconds <= 0:
        return default
    return timedelta(seconds=seconds)


def _json_body(body: bytes) -> dict[str, Any]:
    if not body:
        return {}
    parsed = json.loads(body.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ValueError("json_body_must_be_object")
    return parsed


def _env_truthy(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {"1", "true", "yes", "on"}


def _required_text(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value:
        raise ValueError(f"missing_{key}")
    return value


def _optional_text(payload: dict[str, Any], key: str) -> str | None:
    value = payload.get(key)
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError(f"invalid_{key}")
    return value or None


def _device_pop(value: object) -> DeviceProofOfPossession | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("invalid_device_pop")
    public_key = value.get("device_public_key_b64")
    signature = value.get("signature_b64")
    if not isinstance(public_key, str) or not public_key:
        raise ValueError("missing_device_public_key_b64")
    if not isinstance(signature, str) or not signature:
        raise ValueError("missing_device_pop_signature")
    scheme = value.get("scheme")
    kwargs: dict[str, str] = {
        "device_public_key_b64": public_key,
        "signature_b64": signature,
    }
    if scheme is not None:
        if not isinstance(scheme, str) or not scheme:
            raise ValueError("invalid_device_pop_scheme")
        kwargs["scheme"] = scheme
    return DeviceProofOfPossession(**kwargs)


def _enrollment_pop(value: object) -> DeviceProofOfPossession:
    """Parse the enrollment PoP, defaulting the scheme to the enrollment scheme.

    Unlike the issuance ``_device_pop`` (which defaults to the issuance scheme),
    an enrollment PoP that omits ``scheme`` is treated as the enrollment scheme —
    the two schemes are domain-separated so the signatures are never
    interchangeable.
    """

    if not isinstance(value, dict):
        raise ValueError("missing_enrollment_pop")
    public_key = value.get("device_public_key_b64")
    signature = value.get("signature_b64")
    if not isinstance(public_key, str) or not public_key:
        raise ValueError("missing_device_public_key_b64")
    if not isinstance(signature, str) or not signature:
        raise ValueError("missing_enrollment_pop_signature")
    scheme = value.get("scheme")
    if scheme is None:
        scheme = DEVICE_ENROLLMENT_SIGNATURE_SCHEME
    elif not isinstance(scheme, str) or not scheme:
        raise ValueError("invalid_enrollment_pop_scheme")
    return DeviceProofOfPossession(
        device_public_key_b64=public_key,
        signature_b64=signature,
        scheme=scheme,
    )


def _credit_only_envelope(payload: dict[str, Any]) -> dict[str, Any]:
    """Stamp + assert the credit-only invariant on a new H_a route response.

    Reward/payout/chain stay OFF: every new DTO carries (and is asserted to
    carry) the disabled flags + ``paid_acu`` "0", mirroring the existing
    session/proof/settlement responses.
    """

    enriched = dict(payload)
    enriched.setdefault("live_reward_enabled", False)
    enriched.setdefault("payout_executor_enabled", False)
    enriched.setdefault("chain_writes_enabled", False)
    enriched.setdefault("paid_acu", "0")
    assert enriched["live_reward_enabled"] is False
    assert enriched["payout_executor_enabled"] is False
    assert enriched["chain_writes_enabled"] is False
    assert enriched["paid_acu"] == "0"
    return enriched


def _query_text(query: dict[str, list[str]], key: str) -> str:
    values = query.get(key, [])
    if not values or not values[0]:
        raise ValueError(f"missing_{key}")
    return values[0]


def _datetime(value: object) -> datetime:
    if value is None:
        return datetime.now(UTC)
    if not isinstance(value, str):
        raise ValueError("invalid_datetime")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed


def _decimal(value: object) -> Decimal:
    return Decimal(str(value))


def _required_int(payload: dict[str, Any], key: str) -> int:
    if key not in payload:
        raise ValueError(f"missing_{key}")
    return int(payload[key])


def _required_decimal(payload: dict[str, Any], key: str) -> Decimal:
    if key not in payload:
        raise ValueError(f"missing_{key}")
    return _decimal(payload[key])


def _optional_decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    return _decimal(value)


def _optional_int(value: object) -> int | None:
    if value is None:
        return None
    return int(value)


def _lane_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, list | tuple) or not value:
        raise ValueError("missing_supported_lanes")
    lanes: list[str] = []
    for item in value:
        if not isinstance(item, str) or not item:
            raise ValueError("invalid_supported_lanes")
        lanes.append(item)
    return tuple(lanes)


def _window_from_query(query: dict[str, list[str]]) -> SettlementWindow:
    starts_at = _datetime(query.get("starts_at", [None])[0])
    hours = Decimal(query.get("hours", ["4"])[0])
    total = _decimal(query.get("total_window_emission", [str(DEFAULT_TOTAL_WINDOW_EMISSION)])[0])
    duration = DEFAULT_WINDOW_DURATION if hours == Decimal("4") else timedelta(hours=float(hours))
    return SettlementWindow(
        window_id=query.get("window_id", [f"window-{starts_at.isoformat()}"])[0],
        starts_at=starts_at,
        ends_at=starts_at + duration,
        total_window_emission=total,
    )


def _session_result_json(result: Any) -> dict[str, Any]:
    payload = {
        "status": result.status,
        "reason_code": result.reason_code,
        "accepted": result.accepted,
    }
    if result.session is not None:
        payload["session"] = _dataclass_json(result.session)
    return payload


def _proof_result_json(result: Any) -> dict[str, Any]:
    payload = {
        "status": result.status,
        "reason_code": result.reason_code,
        "accepted": result.accepted,
        "rewardable_score": str(result.rewardable_score),
        "paid_acu": str(result.paid_acu),
    }
    if result.record is not None:
        payload["record"] = _dataclass_json(result.record)
    return payload


def _settlement_result_json(result: Any) -> dict[str, Any]:
    device_credits = {}
    for (passport_id, device_id), credit in result.device_credits.items():
        device_credits[f"{passport_id}|{device_id}"] = {
            "passport_id": passport_id,
            "device_id": device_id,
            "simulated_alice_credit": str(credit),
            "paid_acu": "0",
        }
    return {
        "window": _dataclass_json(result.window),
        "pool_budgets": {key: str(value) for key, value in result.pool_budgets.items()},
        "device_credits": device_credits,
        "reward_statements": [
            _dataclass_json(statement) for statement in result.reward_statements
        ],
        "reserve_roll_forward": str(result.reserve_roll_forward),
        "paid_acu": str(result.paid_acu),
    }


def _dataclass_json(value: Any) -> dict[str, Any]:
    payload = asdict(value)
    return _jsonable_dict(payload)


def _inference_complete_audit_payload(payload: dict[str, Any]) -> dict[str, Any]:
    audit_payload = {
        key: value
        for key, value in payload.items()
        if key in INFERENCE_COMPLETE_AUDIT_KEYS and key not in RAW_PROMPT_KEYS
    }
    if "verified_inference_acu" in payload:
        audit_payload["client_verified_inference_acu_ignored"] = True
    if RAW_PROMPT_KEYS.intersection(payload):
        audit_payload["raw_prompt_dropped"] = True
    return audit_payload


def _jsonable_dict(payload: dict[str, Any]) -> dict[str, Any]:
    jsonable: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, Decimal):
            jsonable[key] = str(value)
        elif isinstance(value, datetime):
            jsonable[key] = value.isoformat()
        elif isinstance(value, dict):
            jsonable[key] = _jsonable_dict(value)
        else:
            jsonable[key] = value
    return jsonable


if __name__ == "__main__":
    main()

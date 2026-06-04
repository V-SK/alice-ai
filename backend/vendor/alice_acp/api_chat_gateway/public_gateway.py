from __future__ import annotations

import hashlib
import hmac
import ipaddress
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal
from urllib.parse import urlsplit

from alice_acp.api_chat.abuse import InMemoryApiChatAbuseGuard
from alice_acp.api_chat.contracts import prompt_hash, request_id_for_hash, stable_hash
from alice_acp.api_chat.model_catalog import is_roleplay_model_class
from alice_acp.api_chat.rate_limit import InMemoryApiChatRateLimiter, hashed_bucket_value
from alice_acp.api_chat.scheduler import model_lane_for_request
from alice_acp.api_chat.types import (
    DEFAULT_CHAT_MODEL_ID,
    ApiChatGatewayConfig,
    ApiChatGatewayMode,
    ApiChatGatewayRequest,
    ApiChatModelClass,
    ApiChatRateLimitBucket,
    utc_now,
    validate_public_identifier,
)
from alice_acp.api_chat.validators import (
    ensure_no_raw_secret,
    validate_aware_timestamp,
    validate_sha256,
)
from alice_acp.api_chat_gateway.inference_side_channel import (
    InferenceJobSideChannel,
    SideChannelTransport,
)
from alice_acp.api_chat_gateway.local_harness import (
    ApiChatWorkerTransportHarness,
    WorkerTransportHarnessConfig,
)
from alice_acp.api_chat_gateway.model_routing import ModelRouteRateLimitResult
from alice_acp.api_chat_gateway.public_contract import (
    PublicAbuseGuardDTO,
    public_route_policy_contract,
)
from alice_acp.api_chat_gateway.types import (
    ChatCompletionRequest,
    ChatMessage,
    GatewayMetadata,
    GatewayMode,
    GatewayResponse,
    canonical_gateway_mode,
)
from alice_acp.api_chat_gateway.worker_bridge import (
    InferenceJobRequestDTO,
    WorkerBridgeRouteResult,
)
from alice_acp.api_chat_gateway.worker_queue import WorkerQueueOperationDTO
from alice_acp.api_chat_gateway.worker_transport import WorkerAuthHandleDTO

PUBLIC_CHAT_GATEWAY_CONTRACT_VERSION = "q40-public-chat-api-worker-bridge-contract-v1"

REASON_PUBLIC_GATEWAY_DISABLED = "api_chat_public_gateway_default_off"
REASON_PUBLIC_GATEWAY_PUBLIC_SERVICE_FORBIDDEN = (
    "api_chat_public_gateway_public_service_forbidden"
)
REASON_PUBLIC_GATEWAY_ADMITTED = "api_chat_public_gateway_worker_queue_admitted"
REASON_PUBLIC_GATEWAY_INVALID_REQUEST = "api_chat_public_gateway_invalid_request"
REASON_PUBLIC_FREE_RATE_LIMIT = "api_chat_public_free_hourly_limit_exceeded"
REASON_PUBLIC_API_KEY_RATE_LIMIT = "api_chat_public_api_key_hourly_limit_exceeded"
REASON_PUBLIC_RAW_SECRET_PATTERN = "api_chat_public_raw_secret_pattern"
REASON_PUBLIC_RAW_API_KEY_FORBIDDEN = "api_chat_public_raw_api_key_forbidden"
REASON_PUBLIC_CREDENTIAL_BODY_FORBIDDEN = "api_chat_public_credential_body_forbidden"

# Phase F (H1): advisory sentinel used as the network anchor when no connection
# peer is known AND no trusted-proxy-forwarded address is available. It is NOT a
# routable or loopback address, so it can never be mistaken for a local/trusted
# client; it simply yields a stable anonymous bucket for peerless contract calls.
UNKNOWN_PEER_SENTINEL = "unknown-peer"

PublicIdentityKind = Literal["anonymous", "api_key"]

_FORBIDDEN_BODY_CREDENTIAL_FIELDS = frozenset(
    {
        "access_token",
        "api_key",
        "authorization",
        "bearer",
        "bearer_token",
        "password",
        "private_key",
        "raw_secret",
        "raw_token",
        "secret",
        "token",
    }
)


@dataclass(frozen=True, slots=True)
class PublicChatGatewayConfig:
    staging_contract_enabled: bool = False
    public_service_enabled: bool = False
    local_contract_only: bool = True
    free_requests_per_hour: int = 10
    api_key_requests_per_hour: int = 120
    max_prompt_chars: int = 8_000
    max_input_tokens: int = 2_048
    default_max_output_tokens: int = 512
    max_output_tokens: int = 1_024
    timeout_ms: int = 30_000
    blocked_phrases: tuple[str, ...] = ("blocked-test-fixture",)
    # Phase F (H1): trusted-proxy allowlist. ``x-forwarded-for`` is honored ONLY
    # when the connection peer is one of these IPs (a known reverse proxy). Default
    # empty => never trust a client-supplied forwarded chain; fall back to the
    # connection/declared peer. ``x-alice-client-ip`` is ignored unconditionally.
    trusted_proxy_ips: tuple[str, ...] = ()
    # Phase F (H2): server secret keying the optional signed user-id token. When
    # set, a ``x-alice-user-token`` that is a valid HMAC over the supplied
    # ``x-alice-user-id`` is accepted as identity; otherwise the user-id is
    # advisory-only and never used for bucketing/audit identity. Default None =>
    # signed user tokens are unavailable (fail-closed: bare user-id is ignored).
    auth_secret: str | None = None

    def __post_init__(self) -> None:
        if not self.local_contract_only or self.public_service_enabled:
            raise ValueError(REASON_PUBLIC_GATEWAY_PUBLIC_SERVICE_FORBIDDEN)
        for field_name, value in (
            ("free_requests_per_hour", self.free_requests_per_hour),
            ("api_key_requests_per_hour", self.api_key_requests_per_hour),
            ("max_prompt_chars", self.max_prompt_chars),
            ("max_input_tokens", self.max_input_tokens),
            ("default_max_output_tokens", self.default_max_output_tokens),
            ("max_output_tokens", self.max_output_tokens),
            ("timeout_ms", self.timeout_ms),
        ):
            if value <= 0:
                raise ValueError(f"{field_name} must be positive")
        for phrase in self.blocked_phrases:
            if not phrase.strip():
                raise ValueError("blocked phrase must be non-empty")
            ensure_no_raw_secret(phrase, field_name="blocked_phrase")
        for proxy_ip in self.trusted_proxy_ips:
            try:
                ipaddress.ip_address(proxy_ip.strip())
            except ValueError as exc:
                raise ValueError("trusted_proxy_ips must be valid IP addresses") from exc
        if self.auth_secret is not None and not self.auth_secret.strip():
            raise ValueError("auth_secret must be non-empty when provided")

    @property
    def normalized_trusted_proxy_ips(self) -> frozenset[str]:
        return frozenset(
            ipaddress.ip_address(proxy_ip.strip()).compressed.lower()
            for proxy_ip in self.trusted_proxy_ips
        )

    def abuse_config(self) -> ApiChatGatewayConfig:
        return ApiChatGatewayConfig(
            max_prompt_chars=self.max_prompt_chars,
            blocked_phrases=self.blocked_phrases,
        )


@dataclass(frozen=True, slots=True)
class PublicChatGatewayRequest:
    method: str
    path: str
    headers: Mapping[str, str] = field(default_factory=dict)
    body: Mapping[str, object] | None = None
    observed_at: datetime = field(default_factory=utc_now)
    # Phase F (H1): the connection/socket peer the HTTP layer actually saw. This
    # is the authoritative network anchor; ``x-forwarded-for`` is only consulted
    # when this peer is a configured trusted proxy. ``None`` for direct contract
    # callers with no socket => the anonymous bucket anchors on the advisory
    # UNKNOWN_PEER_SENTINEL rather than a spoofable header or a loopback fallback.
    declared_peer_ip: str | None = None

    def __post_init__(self) -> None:
        if not self.method:
            raise ValueError("method must be non-empty")
        if not self.path:
            raise ValueError("path must be non-empty")
        validate_aware_timestamp("observed_at", self.observed_at)


@dataclass(frozen=True, slots=True)
class PublicGatewayClientContext:
    normalized_ip: str
    fingerprint_hash: str
    user_agent_hash: str
    api_key_subject: str | None = None
    user_id: str | None = None
    # Phase F (H1): True when ``normalized_ip`` came from the real connection peer
    # (or a trusted-proxy-forwarded address), so the anonymous bucket is anchored
    # to the network and not solely the attacker-controlled user-agent/fingerprint
    # headers. When False (peerless contract call) the fingerprint is advisory.
    network_anchored: bool = False
    # Phase F (H2): the source that authenticated ``user_id``. ``None`` means no
    # trusted user identity was presented; the bare ``x-alice-user-id`` header is
    # never promoted to identity on its own.
    user_id_source: Literal["api_key_subject", "signed_token"] | None = None

    @property
    def identity_kind(self) -> PublicIdentityKind:
        return "api_key" if self.api_key_subject is not None else "anonymous"

    @property
    def rate_limit_key_hash(self) -> str:
        if self.api_key_subject is not None:
            return hashed_bucket_value("api_key_subject", self.api_key_subject)
        return hashed_bucket_value(
            "anonymous_client",
            f"{self.normalized_ip}:{self.fingerprint_hash}",
        )


@dataclass(slots=True)
class PublicChatApiGateway:
    config: PublicChatGatewayConfig = field(default_factory=PublicChatGatewayConfig)
    worker_harness: ApiChatWorkerTransportHarness = field(
        default_factory=lambda: ApiChatWorkerTransportHarness(
            WorkerTransportHarnessConfig(staging_internal_only=True)
        )
    )
    rate_limiter: InMemoryApiChatRateLimiter = field(
        default_factory=InMemoryApiChatRateLimiter
    )
    abuse_guard: InMemoryApiChatAbuseGuard = field(default_factory=InMemoryApiChatAbuseGuard)
    # STEP 0: transient job_id-keyed raw-prompt carrier (gateway -> co-located
    # worker). IN-PROCESS for now; a SideChannelTransport so STEP 5 can swap in a
    # network form without touching this call site. The durable queue still
    # carries ONLY the prompt_hash; the raw prompt rides this side-channel and is
    # purged the instant a worker takes it (or on a failed enqueue, below).
    side_channel: SideChannelTransport = field(default_factory=InferenceJobSideChannel)

    def handle(self, request: PublicChatGatewayRequest) -> GatewayResponse:
        method = request.method.upper()
        path = _canonical_path(request.path)
        if path == "/health":
            if method != "GET":
                return _error_response(405, "method_not_allowed", "method not allowed")
            return self.health(now=request.observed_at)
        if path == "/limits":
            if method != "GET":
                return _error_response(405, "method_not_allowed", "method not allowed")
            return self.limits()
        if path not in {"/chat", "/v1/chat/completions"}:
            return _error_response(404, "route_not_found", "route not found")
        if method != "POST":
            return _error_response(405, "method_not_allowed", "method not allowed")
        if not self.config.staging_contract_enabled:
            return _error_response(
                503,
                REASON_PUBLIC_GATEWAY_DISABLED,
                "public chat gateway staging contract is disabled",
                metadata=self._base_metadata(fail_closed=True),
            )

        try:
            chat_request = _chat_request_from_public_payload(path, request.body)
            context = _client_context_from_headers(
                request.headers,
                config=self.config,
                declared_peer_ip=request.declared_peer_ip,
            )
        except PublicGatewayRequestError as exc:
            return _error_response(
                exc.status_code,
                exc.reason_code,
                exc.message,
                metadata=self._base_metadata(fail_closed=True),
            )
        except ValueError as exc:
            return _error_response(
                400,
                REASON_PUBLIC_GATEWAY_INVALID_REQUEST,
                str(exc),
                metadata=self._base_metadata(fail_closed=True),
            )

        rendered_prompt = _render_chat_prompt(chat_request.messages)
        prompt_digest = prompt_hash(rendered_prompt)
        secret_guard = _raw_secret_guard(rendered_prompt)
        if secret_guard is not None:
            return _error_response(
                400,
                REASON_PUBLIC_RAW_SECRET_PATTERN,
                "prompt contains raw secret-like material",
                metadata={
                    **self._base_metadata(fail_closed=True),
                    "prompt_hash": prompt_digest,
                    "abuse_guard": PublicAbuseGuardDTO(
                        admitted=False,
                        reason_code=REASON_PUBLIC_RAW_SECRET_PATTERN,
                        prompt_hash=prompt_digest,
                        denied_reasons=(REASON_PUBLIC_RAW_SECRET_PATTERN,),
                        fail_closed=True,
                    ).to_public_dict(),
                    "raw_prompt_persisted": False,
                },
            )

        abuse = self.abuse_guard.assess(
            ApiChatGatewayRequest(
                request_kind="api" if context.identity_kind == "api_key" else "chat",
                prompt=rendered_prompt,
                client_ip=context.normalized_ip,
                mode=_api_mode(chat_request.metadata.mode),
                requested_model_class=_model_class_for_model_id(chat_request.model),
                requested_at=request.observed_at,
                user_id=context.user_id or chat_request.user,
                api_key_id=context.api_key_subject,
            ),
            config=self.config.abuse_config(),
        )
        if not abuse.admitted:
            return _error_response(
                400,
                abuse.reason_code,
                "request rejected by public gateway abuse guard",
                metadata={
                    **self._base_metadata(fail_closed=True),
                    "prompt_hash": abuse.prompt_hash,
                    "abuse_guard": PublicAbuseGuardDTO.from_decision(
                        abuse,
                        fail_closed=True,
                    ).to_public_dict(),
                    "raw_prompt_persisted": False,
                },
            )

        rate_limit_result = self._rate_limit(context, request.observed_at)
        if not rate_limit_result.admitted:
            return _error_response(
                429,
                rate_limit_result.reason_code,
                "rate limit exceeded",
                headers=_retry_headers(rate_limit_result),
                retry_after=rate_limit_result.retry_after_seconds,
                metadata={
                    **self._base_metadata(fail_closed=True),
                    "prompt_hash": prompt_digest,
                    "abuse_guard": PublicAbuseGuardDTO.from_decision(
                        abuse,
                        fail_closed=False,
                    ).to_public_dict(),
                    "raw_prompt_persisted": False,
                    "rate_limit_result": rate_limit_result.to_public_dict(),
                },
            )

        job = _job_from_chat_request(
            chat_request,
            prompt_digest=prompt_digest,
            context=context,
            observed_at=request.observed_at,
            config=self.config,
        )
        route = self.worker_harness.dispatcher.route(
            job,
            rate_limit_result=rate_limit_result,
        )
        if route.status != "routed":
            return self._route_error(route)

        # STEP 0: hand the raw prompt to the worker via the transient side-channel
        # (keyed by job_id) BEFORE enqueue. The durable queue records only the
        # prompt_hash; the raw prompt never enters it. If the enqueue is rejected
        # we discard the prompt immediately so no orphaned raw text lingers.
        self.side_channel.publish_prompt(job_id=job.job_id, prompt=rendered_prompt)
        operation = self.worker_harness.enqueue(
            route,
            _dispatch_auth_handle(route, observed_at=request.observed_at),
            now=request.observed_at,
        )
        if operation.status != "accepted":
            self.side_channel.discard(job.job_id)
            return self._queue_error(route, operation)

        return GatewayResponse(
            status_code=202,
            headers={"X-Alice-Local-Contract-Only": "true"},
            body={
                "id": job.job_id,
                "object": "chat.completion.queued",
                "status": "queued",
                "reason_code": REASON_PUBLIC_GATEWAY_ADMITTED,
                "model": chat_request.model,
                "choices": [],
                "metadata": {
                    **self._base_metadata(fail_closed=False),
                    "request_id": job.job_id,
                    "prompt_hash": job.prompt_hash,
                    "abuse_guard": PublicAbuseGuardDTO.from_decision(
                        abuse,
                        fail_closed=False,
                    ).to_public_dict(),
                    "model_tier": job.model_tier,
                    "lane": job.lane,
                    "rate_limit_result": rate_limit_result.to_public_dict(),
                    "model_route": route.route_decision.to_public_dict()
                    if route.route_decision is not None
                    else None,
                    "worker_queue": operation.to_public_dict(),
                    "raw_prompt_persisted": False,
                    "raw_api_key_persisted": False,
                    "raw_response_persisted": False,
                },
            },
        )

    def health(self, *, now: datetime | None = None) -> GatewayResponse:
        harness_health = self.worker_harness.health(now=now)
        return GatewayResponse(
            status_code=200,
            headers={"X-Alice-Local-Contract-Only": "true"},
            body={
                "ok": self.config.staging_contract_enabled
                and harness_health.get("ok") is True,
                "contract_version": PUBLIC_CHAT_GATEWAY_CONTRACT_VERSION,
                "staging_contract_enabled": self.config.staging_contract_enabled,
                "public_service_enabled": False,
                "local_contract_only": True,
                "long_running_service_started": False,
                "endpoints": (
                    "GET /health",
                    "GET /limits",
                    "POST /chat",
                    "POST /v1/chat/completions",
                ),
                "worker_transport": harness_health,
                "raw_prompt_persisted": False,
                "raw_api_key_persisted": False,
                "live_reward_enabled": False,
                "payout_executor_enabled": False,
                "paid_acu": "0",
                "public_route_policy": public_route_policy_contract(),
            },
        )

    def limits(self) -> GatewayResponse:
        return GatewayResponse(
            status_code=200,
            headers={"X-Alice-Local-Contract-Only": "true"},
            body={
                "contract_version": PUBLIC_CHAT_GATEWAY_CONTRACT_VERSION,
                "anonymous_free_requests_per_hour": self.config.free_requests_per_hour,
                "api_key_requests_per_hour": self.config.api_key_requests_per_hour,
                "max_prompt_chars": self.config.max_prompt_chars,
                "max_input_tokens": self.config.max_input_tokens,
                "max_output_tokens": self.config.max_output_tokens,
                "rate_limit_identity_keys": (
                    "normalized_ip_plus_client_fingerprint",
                    "opaque_api_key_handle",
                ),
                "raw_api_key_persisted": False,
                "raw_prompt_persisted": False,
                "public_service_enabled": False,
                "public_route_policy": public_route_policy_contract(),
            },
        )

    def _rate_limit(
        self,
        context: PublicGatewayClientContext,
        observed_at: datetime,
    ) -> ModelRouteRateLimitResult:
        if context.identity_kind == "api_key":
            requests_per_hour = self.config.api_key_requests_per_hour
            reason_code = REASON_PUBLIC_API_KEY_RATE_LIMIT
            bucket_name = "api_key"
        else:
            requests_per_hour = self.config.free_requests_per_hour
            reason_code = REASON_PUBLIC_FREE_RATE_LIMIT
            bucket_name = "free_chat"
        decision = self.rate_limiter.consume(
            buckets=(
                ApiChatRateLimitBucket(
                    name=bucket_name,
                    key_hash=context.rate_limit_key_hash,
                    requests_per_hour=requests_per_hour,
                    reason_code=reason_code,
                ),
            ),
            observed_at=observed_at,
        )
        return ModelRouteRateLimitResult(
            admitted=decision.admitted,
            reason_code=decision.reason_code,
            identity_kind=context.identity_kind,
            limit_requests_per_hour=requests_per_hour,
            remaining_requests=decision.remaining_requests,
            remaining_burst=decision.remaining_requests,
            retry_after_seconds=decision.retry_after_seconds,
        )

    def _route_error(self, route: WorkerBridgeRouteResult) -> GatewayResponse:
        return _error_response(
            503,
            route.reason_code,
            "request rejected by worker model route",
            metadata={
                **self._base_metadata(fail_closed=True),
                "worker_route": route.to_public_dict(),
            },
        )

    def _queue_error(
        self,
        route: WorkerBridgeRouteResult,
        operation: WorkerQueueOperationDTO,
    ) -> GatewayResponse:
        return _error_response(
            503,
            operation.reason_code,
            "request rejected by worker queue",
            metadata={
                **self._base_metadata(fail_closed=True),
                "worker_route": route.to_public_dict(),
                "worker_queue": operation.to_public_dict(),
            },
        )

    def _base_metadata(self, *, fail_closed: bool) -> dict[str, object]:
        return {
            "contract_version": PUBLIC_CHAT_GATEWAY_CONTRACT_VERSION,
            "staging_contract_enabled": self.config.staging_contract_enabled,
            "public_service_enabled": False,
            "local_contract_only": True,
            "fail_closed": fail_closed,
            "raw_prompt_persisted": False,
            "raw_api_key_persisted": False,
            "raw_response_persisted": False,
            "model_dispatch_enabled": False,
            "remote_model_call_performed": False,
            "live_reward_enabled": False,
            "payout_executor_enabled": False,
            "paid_acu": "0",
            "public_route_policy": public_route_policy_contract(),
        }


class PublicGatewayRequestError(ValueError):
    def __init__(self, status_code: int, reason_code: str, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.reason_code = reason_code
        self.message = message


def _chat_request_from_public_payload(
    path: str,
    payload: Mapping[str, object] | None,
) -> ChatCompletionRequest:
    if payload is None:
        raise PublicGatewayRequestError(
            400,
            REASON_PUBLIC_GATEWAY_INVALID_REQUEST,
            "request body must be an object",
        )
    _reject_credential_body_fields(payload)
    if path == "/chat":
        message = payload.get("message", payload.get("prompt"))
        if not isinstance(message, str) or not message.strip():
            raise PublicGatewayRequestError(
                400,
                REASON_PUBLIC_GATEWAY_INVALID_REQUEST,
                "message must be a non-empty string",
            )
        mode = canonical_gateway_mode(payload.get("mode", "Auto"))
        model = payload.get("model", DEFAULT_CHAT_MODEL_ID)
        max_tokens = _optional_positive_int(payload.get("max_tokens"))
        user = payload.get("user")
        if not isinstance(model, str) or not model.strip():
            raise PublicGatewayRequestError(
                400,
                REASON_PUBLIC_GATEWAY_INVALID_REQUEST,
                "model must be a non-empty string",
            )
        if user is not None and not isinstance(user, str):
            raise PublicGatewayRequestError(
                400,
                REASON_PUBLIC_GATEWAY_INVALID_REQUEST,
                "user must be a string",
            )
        return ChatCompletionRequest(
            model=model,
            messages=(ChatMessage(role="user", content=message),),
            metadata=GatewayMetadata(mode=mode),
            max_tokens=max_tokens,
            user=user,
        )
    return ChatCompletionRequest.from_payload(payload)


def _reject_credential_body_fields(payload: Mapping[str, object]) -> None:
    forbidden = _FORBIDDEN_BODY_CREDENTIAL_FIELDS.intersection(
        str(field).lower() for field in payload
    )
    if forbidden:
        raise PublicGatewayRequestError(
            400,
            REASON_PUBLIC_CREDENTIAL_BODY_FORBIDDEN,
            "credentials must not be supplied in the JSON body",
        )


def _client_context_from_headers(
    headers: Mapping[str, str],
    *,
    config: PublicChatGatewayConfig,
    declared_peer_ip: str | None,
) -> PublicGatewayClientContext:
    normalized = {key.lower(): value for key, value in headers.items()}
    if _optional_header(normalized, "authorization") is not None:
        raise PublicGatewayRequestError(
            401,
            REASON_PUBLIC_RAW_API_KEY_FORBIDDEN,
            "raw Authorization credentials are forbidden in this local contract",
        )
    # H1: resolve the network anchor from the trusted connection peer (and a
    # forwarded chain ONLY behind a configured trusted proxy). The spoofable
    # ``x-alice-client-ip`` header is ignored entirely; there is no loopback
    # fallback for a non-local request.
    client_ip, network_anchored = _resolve_network_anchor(
        normalized,
        config=config,
        declared_peer_ip=declared_peer_ip,
    )
    # H1: the fingerprint is anchored to the network peer so it is not derived
    # SOLELY from the attacker-controlled user-agent / client-fingerprint headers.
    advisory_fingerprint = (
        _optional_header(normalized, "x-alice-client-fingerprint")
        or _optional_header(normalized, "user-agent")
        or "unknown-client"
    )
    ensure_no_raw_secret(advisory_fingerprint, field_name="client_fingerprint")
    fingerprint_hash = stable_hash(
        {
            "network_anchor": client_ip,
            "network_anchored": network_anchored,
            "advisory_fingerprint": _normalize_fingerprint(advisory_fingerprint),
        }
    )
    user_agent_hash = stable_hash(
        {"user_agent": _normalize_fingerprint(_optional_header(normalized, "user-agent") or "")}
    )
    api_key_subject = _api_key_subject_from_headers(normalized)
    user_id, user_id_source = _resolve_trusted_user_id(
        normalized,
        config=config,
        api_key_subject=api_key_subject,
    )
    return PublicGatewayClientContext(
        normalized_ip=client_ip,
        fingerprint_hash=fingerprint_hash,
        user_agent_hash=user_agent_hash,
        api_key_subject=api_key_subject,
        user_id=user_id,
        network_anchored=network_anchored,
        user_id_source=user_id_source,
    )


def _resolve_network_anchor(
    headers: Mapping[str, str],
    *,
    config: PublicChatGatewayConfig,
    declared_peer_ip: str | None,
) -> tuple[str, bool]:
    """H1: derive the network bucket anchor fail-closed.

    Priority:
      1. If the connection peer is a configured trusted proxy, trust the first
         ``x-forwarded-for`` hop (the real client behind the proxy).
      2. Otherwise use the connection peer itself (ignoring all forwarding
         headers and the spoofable ``x-alice-client-ip``).
      3. With no peer at all (peerless contract call), anchor on the advisory
         ``UNKNOWN_PEER_SENTINEL`` -- never a loopback/127.0.0.1 fallback.
    Returns ``(anchor, network_anchored)`` where ``network_anchored`` is True only
    for cases (1)/(2).
    """

    peer = declared_peer_ip.strip() if declared_peer_ip else None
    trusted = config.normalized_trusted_proxy_ips
    if peer:
        normalized_peer = _normalize_client_ip(peer)
        if normalized_peer in trusted:
            forwarded = _forwarded_for_ip(_optional_header(headers, "x-forwarded-for"))
            if forwarded is not None:
                return _normalize_client_ip(forwarded), True
        return normalized_peer, True
    return UNKNOWN_PEER_SENTINEL, False


def _resolve_trusted_user_id(
    headers: Mapping[str, str],
    *,
    config: PublicChatGatewayConfig,
    api_key_subject: str | None,
) -> tuple[str | None, Literal["api_key_subject", "signed_token"] | None]:
    """H2: only accept a user-id that is bound to a trusted credential.

    A bare ``x-alice-user-id`` header is NEVER promoted to identity. It is honored
    only when it is bound to the authenticated api-key subject, or carried by a
    valid ``x-alice-user-token`` HMAC (keyed on the server ``auth_secret``) over
    that user-id. Otherwise the request is anonymous (returns ``(None, None)``).
    """

    user_id = _optional_header(headers, "x-alice-user-id")
    if user_id is None:
        return None, None
    validate_public_identifier("x-alice-user-id", user_id)
    # Path A: api-key authenticated subject vouches for the user id.
    if api_key_subject is not None:
        return user_id, "api_key_subject"
    # Path B: a signed token (HMAC over the user id with the server secret).
    token = _optional_header(headers, "x-alice-user-token")
    if token is not None and config.auth_secret is not None:
        expected = _signed_user_id_token(config.auth_secret, user_id)
        if hmac.compare_digest(token, expected):
            return user_id, "signed_token"
    # No trusted binding => advisory only; treat as anonymous for identity.
    return None, None


def _signed_user_id_token(auth_secret: str, user_id: str) -> str:
    return hmac.new(
        auth_secret.encode("utf-8"),
        f"alice-public-user-id:{user_id}".encode(),
        hashlib.sha256,
    ).hexdigest()


def _api_key_subject_from_headers(headers: Mapping[str, str]) -> str | None:
    key_hash = _optional_header(headers, "x-alice-api-key-hash")
    if key_hash is not None:
        validate_sha256(key_hash, field_name="x-alice-api-key-hash")
        # M4: use the full sha256 digest, not a 24-hex prefix (collision /
        # quota-sharing across distinct keys that share a 24-hex prefix).
        return f"api-key-sha256-{key_hash}"
    handle = _optional_header(headers, "x-alice-api-key-handle") or _optional_header(
        headers,
        "x-alice-api-key-id",
    )
    if handle is None:
        return None
    ensure_no_raw_secret(handle, field_name="api_key_handle")
    # M4: full sha256 digest of the handle (64 hex), not a 24-hex prefix.
    digest = stable_hash({"api_key_handle": handle})
    return f"api-key-handle-{digest}"


def _job_from_chat_request(
    request: ChatCompletionRequest,
    *,
    prompt_digest: str,
    context: PublicGatewayClientContext,
    observed_at: datetime,
    config: PublicChatGatewayConfig,
) -> InferenceJobRequestDTO:
    requested_model_class = _model_class_for_model_id(request.model)
    model_tier = model_lane_for_request(
        mode=_api_mode(request.metadata.mode),
        requested_model_class=requested_model_class,
    )
    lane = "roleplay" if is_roleplay_model_class(model_tier) else "general"
    max_output_tokens = min(
        request.max_tokens or config.default_max_output_tokens,
        config.max_output_tokens,
    )
    request_hash = stable_hash(
        {
            "contract": PUBLIC_CHAT_GATEWAY_CONTRACT_VERSION,
            "prompt_hash": prompt_digest,
            "identity_kind": context.identity_kind,
            "rate_limit_key_hash": context.rate_limit_key_hash,
            "model": request.model,
            "mode": request.metadata.mode,
            "observed_at": observed_at,
        }
    )
    return InferenceJobRequestDTO(
        job_id=request_id_for_hash(request_hash),
        model_tier=model_tier,
        lane=lane,
        prompt_hash=prompt_digest,
        max_input_tokens=config.max_input_tokens,
        max_output_tokens=max_output_tokens,
        timeout_ms=config.timeout_ms,
        requested_at=observed_at,
    )


def _dispatch_auth_handle(
    route: WorkerBridgeRouteResult,
    *,
    observed_at: datetime,
) -> WorkerAuthHandleDTO:
    if route.selected_worker is None:
        raise ValueError("selected worker is required for dispatch auth")
    selected_passport = "public-gateway-dispatch"
    if route.route_decision is not None and route.route_decision.selected_miner_id is not None:
        selected_passport = route.route_decision.selected_miner_id
    return WorkerAuthHandleDTO(
        worker_id=route.selected_worker,
        passport_id=selected_passport,
        key_id="public-gateway-dispatch",
        key_hash=stable_hash(
            {
                "worker_id": route.selected_worker,
                "passport_id": selected_passport,
                "purpose": "public-gateway-dispatch",
            }
        ),
        scopes=("dispatch",),
        authenticated_at=observed_at,
    )


def _model_class_for_model_id(model_id: str) -> ApiChatModelClass:
    mapping: dict[str, ApiChatModelClass] = {
        DEFAULT_CHAT_MODEL_ID: "auto",
        "alice-lite-4b@contract": "alice_lite_4b",
        "alice-standard-9b@contract": "alice_standard_9b",
        "alice-pro-27b@contract": "alice_pro_27b",
        "alice-pro-35b-moe@contract": "alice_pro_35b_moe",
        "alice-rp-lite-9b@contract": "rp_lite_9b",
        "alice-rp-pro-27b@contract": "rp_pro_27b",
    }
    try:
        return mapping[model_id]
    except KeyError as exc:
        raise PublicGatewayRequestError(
            400,
            REASON_PUBLIC_GATEWAY_INVALID_REQUEST,
            "requested model is not in the public gateway local contract",
        ) from exc


def _api_mode(mode: GatewayMode) -> ApiChatGatewayMode:
    return {
        "Auto": "auto",
        "Fast": "fast",
        "Standard": "standard",
        "Roleplay": "roleplay",
        "Best": "best",
        "RP Lite": "rp_lite",
        "RP Pro": "rp_pro",
    }[mode]


def _render_chat_prompt(messages: tuple[ChatMessage, ...]) -> str:
    return "\n".join(f"{message.role}: {message.content}" for message in messages)


def _raw_secret_guard(value: str) -> str | None:
    try:
        ensure_no_raw_secret(value, field_name="prompt")
    except ValueError as exc:
        return str(exc)
    return None


def _normalize_client_ip(value: str) -> str:
    candidate = value.strip()
    ensure_no_raw_secret(candidate, field_name="client_ip")
    try:
        return ipaddress.ip_address(candidate).compressed.lower()
    except ValueError:
        return " ".join(candidate.lower().split())


def _normalize_fingerprint(value: str) -> str:
    normalized = " ".join(value.strip().lower().split())
    return normalized[:256] or "unknown-client"


def _forwarded_for_ip(value: str | None) -> str | None:
    if value is None:
        return None
    first = value.split(",", 1)[0].strip()
    return first or None


def _optional_header(headers: Mapping[str, str], name: str) -> str | None:
    value = headers.get(name)
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def _optional_positive_int(value: object) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool) or value <= 0:
        raise PublicGatewayRequestError(
            400,
            REASON_PUBLIC_GATEWAY_INVALID_REQUEST,
            "max_tokens must be a positive integer",
        )
    return value


def _canonical_path(raw_path: str) -> str:
    return urlsplit(raw_path).path or "/"


def _retry_headers(rate_limit: ModelRouteRateLimitResult) -> dict[str, str]:
    if rate_limit.retry_after_seconds is None:
        return {}
    return {"Retry-After": str(rate_limit.retry_after_seconds)}


def _error_response(
    status_code: int,
    code: str,
    message: str,
    *,
    headers: Mapping[str, str] | None = None,
    retry_after: int | None = None,
    metadata: Mapping[str, object] | None = None,
) -> GatewayResponse:
    response_headers = dict(headers or {})
    if retry_after is not None:
        response_headers["Retry-After"] = str(retry_after)
    error_type = "invalid_request_error"
    if status_code == 401:
        error_type = "authentication_error"
    elif status_code == 429:
        error_type = "rate_limit_error"
    elif status_code >= 500:
        error_type = "service_unavailable"
    return GatewayResponse(
        status_code=status_code,
        headers=response_headers,
        body={
            "error": {
                "type": error_type,
                "code": code,
                "message": message,
            },
            "metadata": dict(metadata or {}),
        },
    )

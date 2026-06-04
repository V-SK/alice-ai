from __future__ import annotations

import hashlib
import json
import math
import sqlite3
import threading
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from ipaddress import ip_address
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from alice_acp.api_chat.contracts import prompt_hash
from alice_acp.api_chat.rate_limit import (
    REASON_RATE_LIMIT_ADMITTED,
    InMemoryApiChatRateLimiter,
)
from alice_acp.api_chat.types import (
    REASON_LIVE_REWARD_FORBIDDEN,
    REASON_PAYOUT_EXECUTOR_FORBIDDEN,
    REASON_PUBLIC_SERVICE_FORBIDDEN,
    ApiChatRateLimitBucket,
    ApiChatRateLimitDecision,
    utc_now,
)
from alice_acp.api_chat.validators import validate_aware_timestamp
from alice_acp.api_chat_gateway.http_harness import (
    StagingHttpRequest,
    StagingHttpResponse,
)
from alice_acp.api_chat_gateway.local_harness import (
    ApiChatWorkerTransportHarness,
    WorkerTransportHarnessConfig,
)
from alice_acp.api_chat_gateway.public_gateway import (
    PUBLIC_CHAT_GATEWAY_CONTRACT_VERSION,
    PublicChatApiGateway,
    PublicChatGatewayConfig,
    PublicChatGatewayRequest,
)
from alice_acp.api_chat_gateway.route_policy import (
    StagingRoutePolicy,
    StagingRoutePolicyConfig,
    validate_internal_bind_host,
)
from alice_acp.api_chat_gateway.worker_bridge import ApiChatWorkerBridgeDispatcher

PUBLIC_CHAT_STAGING_HTTP_WRAPPER_CONTRACT_VERSION = (
    "q44-public-chat-staging-http-wrapper-contract-v1"
)
REASON_PUBLIC_HTTP_WRAPPER_DISABLED = "api_chat_public_http_wrapper_default_off"
REASON_PUBLIC_HTTP_INVALID_JSON = "api_chat_public_http_invalid_json"
REASON_PUBLIC_HTTP_RATE_STORE_UNAVAILABLE = "api_chat_public_http_rate_store_unavailable"
REASON_PUBLIC_HTTP_AUDIT_STORE_UNAVAILABLE = "api_chat_public_http_audit_store_unavailable"
REASON_PUBLIC_HTTP_WORKER_TRANSPORT_REQUIRES_CONTRACT = (
    "api_chat_public_http_worker_transport_requires_staging_contract"
)

_CHAT_ROUTES = frozenset({"/chat", "/v1/chat/completions"})


@dataclass(frozen=True, slots=True)
class PublicChatStagingHttpWrapperConfig:
    enabled: bool = False
    staging_contract_enabled: bool = False
    bind_host: str = "127.0.0.1"
    allow_private_internal_bind: bool = False
    port: int = 0
    path_prefix: str = ""
    rate_limit_store_path: Path | None = None
    audit_log_path: Path | None = None
    worker_transport_enabled: bool = False
    worker_queue_capacity: int = 128
    public_service_enabled: bool = False
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False
    api_key_requests_per_hour: int = 120
    max_prompt_chars: int = 8_000
    max_input_tokens: int = 2_048
    default_max_output_tokens: int = 512
    max_output_tokens: int = 1_024
    timeout_ms: int = 30_000
    # Phase F (H1/H2): trusted-proxy allowlist + signed-user-token secret forwarded
    # to the gateway config. Defaults keep both seams fail-closed (no trusted proxy,
    # no signed user tokens) until an operator wires real values.
    trusted_proxy_ips: tuple[str, ...] = ()
    auth_secret: str | None = None
    route_policy_config: StagingRoutePolicyConfig = field(
        default_factory=StagingRoutePolicyConfig
    )
    service_name: str = "q44-public-chat-staging-http-wrapper"

    def __post_init__(self) -> None:
        validate_internal_bind_host(self.bind_host)
        if not _is_loopback_host(self.bind_host) and not self.allow_private_internal_bind:
            raise ValueError("private internal bind requires allow_private_internal_bind")
        if not 0 <= self.port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        if self.path_prefix and not self.path_prefix.startswith("/"):
            raise ValueError("path_prefix must be empty or start with /")
        if self.public_service_enabled:
            raise ValueError(REASON_PUBLIC_SERVICE_FORBIDDEN)
        if self.live_reward_enabled:
            raise ValueError(REASON_LIVE_REWARD_FORBIDDEN)
        if self.payout_executor_enabled:
            raise ValueError(REASON_PAYOUT_EXECUTOR_FORBIDDEN)
        if self.route_policy_config.anonymous_free_chat_limit_per_hour != 10:
            raise ValueError("anonymous free chat must remain 10/hour")
        if self.api_key_requests_per_hour <= 0:
            raise ValueError("api_key_requests_per_hour must be positive")
        if self.worker_queue_capacity <= 0:
            raise ValueError("worker_queue_capacity must be positive")
        if self.worker_transport_enabled and not self.staging_contract_enabled:
            raise ValueError(REASON_PUBLIC_HTTP_WORKER_TRANSPORT_REQUIRES_CONTRACT)

    def worker_transport_harness_config(self) -> WorkerTransportHarnessConfig:
        """Build the internal worker-transport harness config (opt-in, default OFF).

        Honors the ``worker_transport_enabled`` opt-in to flip the co-located
        staging queue on (``enabled=True``, ``kill_switch_unavailable=False``)
        without touching any forbidden flag. Stays fail-closed when the opt-in
        is off.
        """
        return WorkerTransportHarnessConfig.colocated_staging(
            enabled=self.worker_transport_enabled,
            queue_capacity=self.worker_queue_capacity,
        )

    def to_gateway_config(self) -> PublicChatGatewayConfig:
        return PublicChatGatewayConfig(
            staging_contract_enabled=self.staging_contract_enabled,
            public_service_enabled=False,
            local_contract_only=True,
            free_requests_per_hour=10,
            api_key_requests_per_hour=self.api_key_requests_per_hour,
            max_prompt_chars=self.max_prompt_chars,
            max_input_tokens=self.max_input_tokens,
            default_max_output_tokens=self.default_max_output_tokens,
            max_output_tokens=self.max_output_tokens,
            timeout_ms=self.timeout_ms,
            trusted_proxy_ips=self.trusted_proxy_ips,
            auth_secret=self.auth_secret,
        )


@dataclass(slots=True)
class PublicChatStagingHttpWrapper:
    config: PublicChatStagingHttpWrapperConfig = field(
        default_factory=PublicChatStagingHttpWrapperConfig
    )
    worker_harness: ApiChatWorkerTransportHarness | None = None
    rate_limiter: Any | None = None
    gateway: PublicChatApiGateway = field(init=False)
    route_policy: StagingRoutePolicy = field(init=False)
    audit_store: JsonlPublicChatAuditStore | None = field(init=False, default=None)

    def __post_init__(self) -> None:
        if self.worker_harness is None:
            # Default OFF unless the explicit worker_transport_enabled opt-in is
            # set on the config; never sets a forbidden flag. When enabled, the
            # dispatcher is also turned on (no workers registered yet — a
            # co-located worker registers itself before processing).
            enabled = self.config.worker_transport_enabled
            self.worker_harness = ApiChatWorkerTransportHarness(
                config=self.config.worker_transport_harness_config(),
                dispatcher=ApiChatWorkerBridgeDispatcher(
                    enabled=enabled,
                    kill_switch_unavailable=not enabled,
                    queue_capacity=self.config.worker_queue_capacity,
                ),
            )
        limiter = self.rate_limiter
        if limiter is None:
            limiter = (
                SQLitePublicChatRateLimitStore(self.config.rate_limit_store_path)
                if self.config.rate_limit_store_path is not None
                else InMemoryApiChatRateLimiter()
            )
        self.rate_limiter = limiter
        self.gateway = PublicChatApiGateway(
            config=self.config.to_gateway_config(),
            worker_harness=self.worker_harness,
            rate_limiter=limiter,
        )
        self.route_policy = StagingRoutePolicy(self.config.route_policy_config)
        if self.config.audit_log_path is not None:
            self.audit_store = JsonlPublicChatAuditStore(self.config.audit_log_path)

    def build_http_server(self) -> HTTPServer:
        if not self.config.enabled:
            raise RuntimeError(REASON_PUBLIC_HTTP_WRAPPER_DISABLED)
        return HTTPServer((self.config.bind_host, self.config.port), self.build_handler_class())

    def build_handler_class(self) -> type[BaseHTTPRequestHandler]:
        wrapper = self

        class AlicePublicChatStagingHttpHandler(BaseHTTPRequestHandler):
            server_version = "AlicePublicChatStagingHttpWrapper/1.0"

            def do_GET(self) -> None:
                self._handle()

            def do_POST(self) -> None:
                self._handle()

            def log_message(self, format: str, *args: object) -> None:
                return

            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length", "0") or "0")
                response = wrapper.handle(
                    StagingHttpRequest(
                        method=self.command,
                        path=self.path,
                        headers={key: value for key, value in self.headers.items()},
                        body=self.rfile.read(length) if length else b"",
                    ),
                    client_host=self.client_address[0],
                )
                body = response.to_json_bytes()
                self.send_response(response.status_code)
                for key, value in _json_headers(response.headers, len(body)).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

        return AlicePublicChatStagingHttpHandler

    def handle(
        self,
        request: StagingHttpRequest,
        *,
        client_host: str | None = None,
    ) -> StagingHttpResponse:
        observed_at = utc_now()
        method = request.method.upper()
        path = self._contract_path(request.path)
        route_decision = self.route_policy.evaluate(
            method=method,
            path=path,
            headers=request.headers,
            key_present=_request_key_present(request.headers),
        )

        payload: Mapping[str, object] | None = None
        prompt_digest: str | None = None
        if route_decision.admitted:
            if path in _CHAT_ROUTES and not self.config.enabled:
                response = _http_error(
                    503,
                    code=REASON_PUBLIC_HTTP_WRAPPER_DISABLED,
                    message="public chat staging HTTP wrapper is disabled",
                    headers=route_decision.headers,
                    metadata=self._base_metadata(fail_closed=True),
                )
            elif method == "POST" and path in _CHAT_ROUTES:
                decoded = _decode_json_object(request.body)
                if not isinstance(decoded, Mapping):
                    response = _http_error(
                        400,
                        code=REASON_PUBLIC_HTTP_INVALID_JSON,
                        message="request body must be a JSON object",
                        headers=route_decision.headers,
                        metadata=self._base_metadata(fail_closed=True),
                    )
                else:
                    payload = decoded
                    prompt_digest = _prompt_hash_from_payload(path, payload)
                    response = self._from_gateway_response(
                        self.gateway.handle(
                            PublicChatGatewayRequest(
                                method=method,
                                path=path,
                                headers=request.headers,
                                body=payload,
                                observed_at=observed_at,
                                # H1: the real connection peer is the authoritative
                                # network anchor; x-alice-client-ip is ignored.
                                declared_peer_ip=client_host,
                            )
                        ),
                        route_headers=route_decision.headers,
                    )
            else:
                response = self._from_gateway_response(
                    self.gateway.handle(
                        PublicChatGatewayRequest(
                            method=method,
                            path=path,
                            headers=request.headers,
                            observed_at=observed_at,
                            declared_peer_ip=client_host,
                        )
                    ),
                    route_headers=route_decision.headers,
                )
                if path == "/health":
                    response = self._with_wrapper_health(response)
                elif path == "/limits":
                    response = self._with_wrapper_limits(response)
        else:
            response = _http_error(
                route_decision.status_code,
                code=route_decision.reason_code,
                message="request rejected by staging route policy",
                headers=route_decision.headers,
                metadata={**self._base_metadata(fail_closed=True), **route_decision.metadata},
            )

        prompt_digest = prompt_digest or _prompt_hash_from_response(response)
        self._record_audit(
            request=request,
            response=response,
            observed_at=observed_at,
            prompt_hash_value=prompt_digest,
            client_host=client_host,
        )
        return response

    def close(self) -> None:
        close_limiter = getattr(self.rate_limiter, "close", None)
        if close_limiter is not None:
            close_limiter()
        if self.audit_store is not None:
            self.audit_store.close()

    def _contract_path(self, raw_path: str) -> str:
        parsed = urlsplit(raw_path)
        path = parsed.path or "/"
        prefix = self.config.path_prefix.rstrip("/")
        if prefix:
            if path == prefix:
                return "/"
            if path.startswith(f"{prefix}/"):
                return path[len(prefix) :] or "/"
        return path

    def _from_gateway_response(
        self,
        response: Any,
        *,
        route_headers: Mapping[str, str],
    ) -> StagingHttpResponse:
        return StagingHttpResponse(
            status_code=response.status_code,
            headers={**route_headers, **response.headers},
            body=response.body,
        )

    def _with_wrapper_health(self, response: StagingHttpResponse) -> StagingHttpResponse:
        body = dict(response.body)
        body.update(self._health_metadata())
        body["ok"] = bool(body.get("ok")) and self.config.enabled
        return StagingHttpResponse(
            status_code=response.status_code,
            headers={
                **response.headers,
                "X-Alice-Staging-Internal-Only": "true",
                "X-Alice-Public-Service-Enabled": "false",
            },
            body=body,
        )

    def _with_wrapper_limits(self, response: StagingHttpResponse) -> StagingHttpResponse:
        body = dict(response.body)
        body.update(
            {
                "http_wrapper_contract_version": (
                    PUBLIC_CHAT_STAGING_HTTP_WRAPPER_CONTRACT_VERSION
                ),
                "staging_http_wrapper_enabled": self.config.enabled,
                "bind_host": self.config.bind_host,
                "localhost_only_default": self.config.bind_host == "127.0.0.1",
                "allow_private_internal_bind": self.config.allow_private_internal_bind,
                "rate_limit_store_configured": self.config.rate_limit_store_path is not None,
                "audit_log_configured": self.config.audit_log_path is not None,
                "public_service_enabled": False,
                "live_reward_enabled": False,
                "payout_executor_enabled": False,
            }
        )
        return StagingHttpResponse(
            status_code=response.status_code,
            headers=response.headers,
            body=body,
        )

    def _health_metadata(self) -> dict[str, object]:
        return {
            "http_wrapper_contract_version": PUBLIC_CHAT_STAGING_HTTP_WRAPPER_CONTRACT_VERSION,
            "public_gateway_contract_version": PUBLIC_CHAT_GATEWAY_CONTRACT_VERSION,
            "service": self.config.service_name,
            "staging_http_wrapper_enabled": self.config.enabled,
            "bind_host": self.config.bind_host,
            "localhost_only_default": self.config.bind_host == "127.0.0.1",
            "allow_private_internal_bind": self.config.allow_private_internal_bind,
            "long_running_service_started": False,
            "rate_limit_store_configured": self.config.rate_limit_store_path is not None,
            "audit_log_configured": self.config.audit_log_path is not None,
            "public_service_enabled": False,
            "public_unauthenticated_api_enabled": False,
            "live_reward_enabled": False,
            "payout_executor_enabled": False,
            "paid_acu": "0",
            "raw_prompt_persisted": False,
            "raw_api_key_persisted": False,
            "raw_response_persisted": False,
            "route_policy": self.route_policy.metadata(),
            "endpoints": (
                "GET /health",
                "GET /limits",
                "POST /chat",
                "POST /v1/chat/completions",
            ),
        }

    def _base_metadata(self, *, fail_closed: bool) -> dict[str, object]:
        return {
            "http_wrapper_contract_version": PUBLIC_CHAT_STAGING_HTTP_WRAPPER_CONTRACT_VERSION,
            "staging_http_wrapper_enabled": self.config.enabled,
            "staging_contract_enabled": self.config.staging_contract_enabled,
            "public_service_enabled": False,
            "public_unauthenticated_api_enabled": False,
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
        }

    def _record_audit(
        self,
        *,
        request: StagingHttpRequest,
        response: StagingHttpResponse,
        observed_at: datetime,
        prompt_hash_value: str | None,
        client_host: str | None,
    ) -> None:
        if self.audit_store is None:
            return
        self.audit_store.record(
            method=request.method.upper(),
            path=request.path,
            headers=request.headers,
            status_code=response.status_code,
            reason_code=_reason_code_from_body(response.body),
            prompt_hash_value=prompt_hash_value,
            observed_at=observed_at,
            client_host=client_host,
        )


@dataclass(slots=True)
class SQLitePublicChatRateLimitStore:
    path: Path
    _connection: sqlite3.Connection = field(init=False, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._connection = sqlite3.connect(
                str(self.path),
                isolation_level=None,
                check_same_thread=False,
            )
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._initialize()
        except sqlite3.DatabaseError as exc:
            raise RuntimeError(REASON_PUBLIC_HTTP_RATE_STORE_UNAVAILABLE) from exc

    def consume(
        self,
        *,
        buckets: tuple[ApiChatRateLimitBucket, ...],
        observed_at: datetime,
    ) -> ApiChatRateLimitDecision:
        validate_aware_timestamp("observed_at", observed_at)
        if not buckets:
            raise ValueError("rate limit buckets must not be empty")

        with self._lock:
            retained_by_key = {
                bucket.limit_key: self._retained_locked(bucket.limit_key, observed_at)
                for bucket in buckets
            }
            rejected = _first_rejected_bucket(buckets, retained_by_key, observed_at)
            if rejected is not None:
                bucket, retry_after = rejected
                return ApiChatRateLimitDecision(
                    admitted=False,
                    reason_code=bucket.reason_code,
                    limit_name=bucket.name,
                    retry_after_seconds=retry_after,
                    remaining_requests=0,
                )
            try:
                for bucket in buckets:
                    self._connection.execute(
                        """
                        INSERT INTO public_chat_rate_limits(limit_key, observed_at)
                        VALUES (?, ?)
                        """,
                        (bucket.limit_key, observed_at.isoformat()),
                    )
            except sqlite3.DatabaseError as exc:
                raise RuntimeError(REASON_PUBLIC_HTTP_RATE_STORE_UNAVAILABLE) from exc

        remaining = min(
            bucket.requests_per_hour - len(retained_by_key[bucket.limit_key])
            for bucket in buckets
        )
        return ApiChatRateLimitDecision(
            admitted=True,
            reason_code=REASON_RATE_LIMIT_ADMITTED,
            remaining_requests=max(remaining, 0),
        )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _initialize(self) -> None:
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS public_chat_rate_limits (
                limit_key TEXT NOT NULL,
                observed_at TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_public_chat_rate_limits_key_time
            ON public_chat_rate_limits(limit_key, observed_at)
            """
        )

    def _retained_locked(self, limit_key: str, observed_at: datetime) -> list[datetime]:
        cutoff = observed_at - timedelta(hours=1)
        try:
            self._connection.execute(
                """
                DELETE FROM public_chat_rate_limits
                WHERE limit_key = ? AND observed_at <= ?
                """,
                (limit_key, cutoff.isoformat()),
            )
            rows = self._connection.execute(
                """
                SELECT observed_at
                FROM public_chat_rate_limits
                WHERE limit_key = ?
                ORDER BY observed_at
                """,
                (limit_key,),
            ).fetchall()
        except sqlite3.DatabaseError as exc:
            raise RuntimeError(REASON_PUBLIC_HTTP_RATE_STORE_UNAVAILABLE) from exc
        return [datetime.fromisoformat(row[0]) for row in rows]


@dataclass(slots=True)
class JsonlPublicChatAuditStore:
    path: Path
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self._lock = threading.RLock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.path.touch(exist_ok=True)
        except OSError as exc:
            raise RuntimeError(REASON_PUBLIC_HTTP_AUDIT_STORE_UNAVAILABLE) from exc

    def record(
        self,
        *,
        method: str,
        path: str,
        headers: Mapping[str, str],
        status_code: int,
        reason_code: str,
        prompt_hash_value: str | None,
        observed_at: datetime,
        client_host: str | None,
    ) -> None:
        validate_aware_timestamp("observed_at", observed_at)
        normalized = {key.lower(): value for key, value in headers.items()}
        payload: dict[str, object] = {
            "contract_version": PUBLIC_CHAT_STAGING_HTTP_WRAPPER_CONTRACT_VERSION,
            "observed_at": observed_at.isoformat(),
            "method": method,
            "path": _safe_path(path),
            "status_code": status_code,
            "reason_code": reason_code,
            "client_host_hash": _hash_optional(client_host),
            "client_ip_hash": _hash_optional(normalized.get("x-alice-client-ip")),
            "api_key_handle_present": bool(normalized.get("x-alice-api-key-handle")),
            "api_key_hash_present": bool(normalized.get("x-alice-api-key-hash")),
            "api_key_id_present": bool(normalized.get("x-alice-api-key-id")),
            "authorization_present": bool(normalized.get("authorization")),
            "raw_prompt_persisted": False,
            "raw_api_key_persisted": False,
            "public_service_enabled": False,
            "live_reward_enabled": False,
            "payout_executor_enabled": False,
            "paid_acu": "0",
        }
        if prompt_hash_value is not None:
            payload["prompt_hash"] = prompt_hash_value
        try:
            line = json.dumps(payload, sort_keys=True, separators=(",", ":"))
            with self._lock, self.path.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")
        except OSError as exc:
            raise RuntimeError(REASON_PUBLIC_HTTP_AUDIT_STORE_UNAVAILABLE) from exc

    def close(self) -> None:
        return


def _decode_json_object(body: bytes) -> object:
    if not body:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _prompt_hash_from_payload(path: str, payload: Mapping[str, object]) -> str | None:
    if path == "/chat":
        prompt = payload.get("message", payload.get("prompt"))
        if isinstance(prompt, str) and prompt.strip():
            return prompt_hash(f"user: {prompt}")
        return None
    messages = payload.get("messages")
    if not isinstance(messages, list | tuple):
        return None
    rendered: list[str] = []
    for message in messages:
        if not isinstance(message, Mapping):
            return None
        role = message.get("role")
        content = message.get("content")
        if not isinstance(role, str) or not isinstance(content, str):
            return None
        rendered.append(f"{role}: {content}")
    if not rendered:
        return None
    return prompt_hash("\n".join(rendered))


def _prompt_hash_from_response(response: StagingHttpResponse) -> str | None:
    metadata = response.body.get("metadata")
    if isinstance(metadata, Mapping):
        value = metadata.get("prompt_hash")
        if isinstance(value, str) and len(value) == 64:
            return value
    return None


def _request_key_present(headers: Mapping[str, str]) -> bool:
    normalized = {key.lower(): value for key, value in headers.items()}
    return bool(
        _optional_header(normalized, "x-alice-api-key-handle")
        or _optional_header(normalized, "x-alice-api-key-id")
        or _optional_header(normalized, "x-alice-api-key-hash")
        or _optional_header(normalized, "authorization")
    )


def _optional_header(headers: Mapping[str, str], name: str) -> str | None:
    value = headers.get(name)
    if value is None or not value.strip():
        return None
    return value.strip()


def _reason_code_from_body(body: Mapping[str, object]) -> str:
    error = body.get("error")
    if isinstance(error, Mapping):
        code = error.get("code")
        if isinstance(code, str) and code:
            return code
    code = body.get("reason_code")
    if isinstance(code, str) and code:
        return code
    return "api_chat_public_http_ok"


def _http_error(
    status_code: int,
    *,
    code: str,
    message: str,
    headers: Mapping[str, str] | None = None,
    metadata: dict[str, object] | None = None,
) -> StagingHttpResponse:
    body: dict[str, Any] = {
        "error": {
            "message": message,
            "type": _http_error_type(status_code),
            "code": code,
        }
    }
    if metadata is not None:
        body["metadata"] = metadata
    return StagingHttpResponse(status_code=status_code, headers=dict(headers or {}), body=body)


def _http_error_type(status_code: int) -> str:
    if status_code == 401:
        return "authentication_error"
    if status_code == 403:
        return "permission_error"
    if status_code == 429:
        return "rate_limit_error"
    if status_code >= 500:
        return "service_unavailable"
    return "invalid_request_error"


def _first_rejected_bucket(
    buckets: tuple[ApiChatRateLimitBucket, ...],
    retained_by_key: dict[str, list[datetime]],
    observed_at: datetime,
) -> tuple[ApiChatRateLimitBucket, int] | None:
    for bucket in buckets:
        retained = retained_by_key[bucket.limit_key]
        if len(retained) >= bucket.requests_per_hour:
            return bucket, _retry_after_seconds(oldest=retained[0], observed_at=observed_at)
    return None


def _retry_after_seconds(*, oldest: datetime, observed_at: datetime) -> int:
    retry_after = (oldest + timedelta(hours=1) - observed_at).total_seconds()
    return max(1, math.ceil(retry_after))


def _json_headers(headers: Mapping[str, str], content_length: int) -> dict[str, str]:
    response_headers = dict(headers)
    response_headers.setdefault("Content-Type", "application/json")
    response_headers["Content-Length"] = str(content_length)
    return response_headers


def _safe_path(path: str) -> str:
    parsed = urlsplit(path)
    return parsed.path or "/"


def _hash_optional(value: str | None) -> str | None:
    if value is None or not value.strip():
        return None
    return "sha256:" + hashlib.sha256(value.strip().encode("utf-8")).hexdigest()[:16]


def _is_loopback_host(bind_host: str) -> bool:
    return ip_address(bind_host).is_loopback

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler
from typing import Any
from urllib.parse import urlsplit

from alice_acp.api_chat import ApiChatLocalBackend
from alice_acp.api_chat.types import DEFAULT_CHAT_MODEL_ID
from alice_acp.api_chat.validators import validate_sha256
from alice_acp.api_chat_gateway.adapter import OpenAICompatibleChatGateway
from alice_acp.api_chat_gateway.key_registry import (
    REASON_API_KEY_RATE_LIMIT,
    ApiChatGatewayKeyAdmission,
    ApiChatGatewayKeyRegistry,
)
from alice_acp.api_chat_gateway.route_policy import (
    StagingRoutePolicy,
    StagingRoutePolicyConfig,
    validate_internal_bind_host,
)
from alice_acp.api_chat_gateway.types import (
    OPENAI_CHAT_GATEWAY_SERVICE,
    GatewayRequestContext,
    GatewayResponse,
    ModelRegistry,
)

CHAT_ROUTE_ALIASES = {
    "/api/chat": "/chat",
}


@dataclass(frozen=True, slots=True)
class StagingGatewayHarnessConfig:
    enabled: bool = False
    bind_host: str = "127.0.0.1"
    contract_path_prefix: str = ""
    service_name: str = "q22c-api-chat-staging-http-gateway"
    kill_switch_unavailable: bool = False
    route_policy_config: StagingRoutePolicyConfig = field(
        default_factory=StagingRoutePolicyConfig
    )

    def __post_init__(self) -> None:
        validate_internal_bind_host(self.bind_host)
        if self.contract_path_prefix and not self.contract_path_prefix.startswith("/"):
            raise ValueError("contract_path_prefix must be empty or start with /")


@dataclass(frozen=True, slots=True)
class StagingHttpRequest:
    method: str
    path: str
    headers: Mapping[str, str] = field(default_factory=dict)
    body: bytes = b""


@dataclass(frozen=True, slots=True)
class StagingHttpResponse:
    status_code: int
    headers: dict[str, str]
    body: dict[str, Any]

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300

    def to_json_bytes(self) -> bytes:
        return json.dumps(self.body, sort_keys=True, separators=(",", ":")).encode("utf-8")


@dataclass(slots=True)
class StagingApiChatHttpHarness:
    config: StagingGatewayHarnessConfig = field(default_factory=StagingGatewayHarnessConfig)
    gateway: OpenAICompatibleChatGateway = field(default_factory=OpenAICompatibleChatGateway)
    key_registry: ApiChatGatewayKeyRegistry = field(default_factory=ApiChatGatewayKeyRegistry)
    route_policy: StagingRoutePolicy = field(init=False)

    def __post_init__(self) -> None:
        self.route_policy = StagingRoutePolicy(self.config.route_policy_config)

    @classmethod
    def from_backend(
        cls,
        backend: ApiChatLocalBackend,
        *,
        config: StagingGatewayHarnessConfig | None = None,
        model_registry: ModelRegistry | None = None,
        key_registry: ApiChatGatewayKeyRegistry | None = None,
    ) -> StagingApiChatHttpHarness:
        gateway = OpenAICompatibleChatGateway(
            backend=backend,
            model_registry=model_registry or ModelRegistry(),
        )
        return cls(
            config=config or StagingGatewayHarnessConfig(),
            gateway=gateway,
            key_registry=key_registry or ApiChatGatewayKeyRegistry(),
        )

    def handle(self, request: StagingHttpRequest) -> StagingHttpResponse:
        method = request.method.upper()
        path = self._contract_path(request.path)
        route_decision = self.route_policy.evaluate(
            method=method,
            path=path,
            headers=request.headers,
            key_present=_request_key_present(request.headers),
        )
        if not route_decision.admitted:
            return _http_error(
                route_decision.status_code,
                code=route_decision.reason_code,
                message="request rejected by staging route policy",
                headers=route_decision.headers,
                metadata=route_decision.metadata,
            )

        if path == "/health":
            if method != "GET":
                return _http_error(
                    405,
                    code="method_not_allowed",
                    message="method not allowed",
                    headers=route_decision.headers,
                )
            return self._health(route_headers=route_decision.headers)

        if path == "/v1/models":
            if method != "GET":
                return _http_error(
                    405,
                    code="method_not_allowed",
                    message="method not allowed",
                    headers=route_decision.headers,
                )
            return _from_gateway_response(
                self.gateway.list_models(),
                route_headers=route_decision.headers,
            )

        if path == "/chat":
            if method != "POST":
                return _http_error(
                    405,
                    code="method_not_allowed",
                    message="method not allowed",
                    headers=route_decision.headers,
                )
            payload = _decode_json_object(request.body)
            if not isinstance(payload, dict):
                return _http_error(
                    400,
                    code="invalid_json",
                    message="request body must be a JSON object",
                    headers=route_decision.headers,
                    metadata=self._base_metadata(),
                )
            try:
                chat_payload = _chat_payload_to_openai_payload(payload)
                context = self._validated_context_from_headers(request.headers)
            except ApiChatKeyAdmissionError as exc:
                return _http_error(
                    exc.status_code,
                    code=exc.admission.reason_code,
                    message="API key is not admitted by the staging key registry",
                    headers=_key_admission_headers(route_decision.headers, exc.admission),
                    metadata=self._key_admission_metadata(exc.admission),
                )
            except ValueError as exc:
                return _http_error(
                    400,
                    code="invalid_request",
                    message=str(exc),
                    headers=route_decision.headers,
                    metadata=self._base_metadata(),
                )
            response = self.gateway.create_chat_completion(chat_payload, context=context)
            return _from_gateway_response(response, route_headers=route_decision.headers)

        if path == "/v1/chat/completions":
            if method != "POST":
                return _http_error(
                    405,
                    code="method_not_allowed",
                    message="method not allowed",
                    headers=route_decision.headers,
                )
            payload = _decode_json_object(request.body)
            if not isinstance(payload, dict):
                return _http_error(
                    400,
                    code="invalid_json",
                    message="request body must be a JSON object",
                    headers=route_decision.headers,
                    metadata=self._base_metadata(),
                )
            try:
                context = self._validated_context_from_headers(request.headers)
            except ApiChatKeyAdmissionError as exc:
                return _http_error(
                    exc.status_code,
                    code=exc.admission.reason_code,
                    message="API key is not admitted by the staging key registry",
                    headers=_key_admission_headers(route_decision.headers, exc.admission),
                    metadata=self._key_admission_metadata(exc.admission),
                )
            except ValueError as exc:
                return _http_error(
                    400,
                    code="invalid_request",
                    message=str(exc),
                    headers=route_decision.headers,
                    metadata=self._base_metadata(),
                )
            response = self.gateway.create_chat_completion(payload, context=context)
            return _from_gateway_response(response, route_headers=route_decision.headers)

        return _http_error(
            404,
            code="route_not_found",
            message="route not found",
            headers=route_decision.headers,
        )

    def build_handler_class(self) -> type[BaseHTTPRequestHandler]:
        harness = self

        class AliceStagingApiChatHandler(BaseHTTPRequestHandler):
            server_version = "AliceApiChatStagingHarness/1.0"

            def do_GET(self) -> None:
                self._handle()

            def do_POST(self) -> None:
                self._handle()

            def log_message(self, format: str, *args: object) -> None:
                return

            def _handle(self) -> None:
                length = int(self.headers.get("Content-Length", "0") or "0")
                response = harness.handle(
                    StagingHttpRequest(
                        method=self.command,
                        path=self.path,
                        headers={key: value for key, value in self.headers.items()},
                        body=self.rfile.read(length) if length else b"",
                    )
                )
                body = response.to_json_bytes()
                self.send_response(response.status_code)
                for key, value in _json_headers(response.headers, len(body)).items():
                    self.send_header(key, value)
                self.end_headers()
                self.wfile.write(body)

        return AliceStagingApiChatHandler

    def _contract_path(self, raw_path: str) -> str:
        parsed = urlsplit(raw_path)
        path = parsed.path or "/"
        prefix = self.config.contract_path_prefix.rstrip("/")
        if prefix:
            if path == prefix:
                return "/"
            if path.startswith(f"{prefix}/"):
                return _canonical_contract_path(path[len(prefix) :])
        return _canonical_contract_path(path)

    def _health(self, *, route_headers: Mapping[str, str]) -> StagingHttpResponse:
        backend_health = self.gateway.backend.health()
        service_available = not self.config.kill_switch_unavailable
        return StagingHttpResponse(
            status_code=200,
            headers={
                **route_headers,
                "X-Alice-Staging-Internal-Only": "true",
                "X-Alice-Long-Running-Service-Started": "false",
            },
            body={
                "ok": backend_health.get("ok") is True and service_available,
                "service": self.config.service_name,
                "gateway_service": OPENAI_CHAT_GATEWAY_SERVICE,
                "staging_internal_only": True,
                "staging_http_enabled": self.config.enabled,
                "long_running_service_started": False,
                "bind_host": self.config.bind_host,
                "service_available": service_available,
                "kill_switch_unavailable": self.config.kill_switch_unavailable,
                "public_service_enabled": False,
                "live_reward_enabled": backend_health.get("live_reward_enabled", False),
                "payout_executor_enabled": backend_health.get("payout_executor_enabled", False),
                "raw_prompt_persisted": False,
                "raw_api_key_persisted": False,
                "foundation_mock_revenue_separated": True,
                "key_registry_configured": self.key_registry.configured,
                "route_policy": self.route_policy.metadata(),
                "model_routing": self.gateway.model_router.summary(),
                "endpoints": (
                    "GET /health",
                    "GET /v1/models",
                    "POST /api/chat",
                    "POST /chat",
                    "POST /v1/chat/completions",
                ),
                "backend": backend_health,
            },
        )

    def _base_metadata(self) -> dict[str, object]:
        return {
            "service": self.config.service_name,
            "staging_internal_only": True,
            "public_service_enabled": False,
            "live_reward_enabled": False,
            "payout_executor_enabled": False,
            "paid_acu": "0",
        }

    def _validated_context_from_headers(
        self,
        headers: Mapping[str, str],
    ) -> GatewayRequestContext:
        context = _context_from_headers(
            headers,
            allow_authorization_bearer=not self.key_registry.configured,
        )
        if not self.key_registry.configured:
            return context
        admission = self.key_registry.admit(
            key_hash=context.api_key_hash,
            key_id=context.api_key_id,
            observed_at=context.observed_at,
            required=True,
        )
        if not admission.admitted:
            raise ApiChatKeyAdmissionError(admission)
        assert admission.key_record is not None
        return GatewayRequestContext(
            client_ip=context.client_ip,
            user_agent=context.user_agent,
            observed_at=context.observed_at,
            api_key_id=admission.key_record.key_id,
            api_key_hash=admission.key_record.key_hash,
            user_id=context.user_id,
        )

    def _key_admission_metadata(
        self,
        admission: ApiChatGatewayKeyAdmission,
    ) -> dict[str, object]:
        metadata = self._base_metadata()
        metadata.update(
            {
                "key_registry_configured": self.key_registry.configured,
                "key_admitted": admission.admitted,
                "key_status": admission.key_record.status
                if admission.key_record is not None
                else None,
                "retry_after": admission.retry_after_seconds,
                "raw_api_key_persisted": False,
                "route_policy": self.route_policy.metadata(),
            }
        )
        return metadata


class ApiChatKeyAdmissionError(ValueError):
    def __init__(self, admission: ApiChatGatewayKeyAdmission) -> None:
        super().__init__(admission.reason_code)
        self.admission = admission

    @property
    def status_code(self) -> int:
        if self.admission.reason_code == REASON_API_KEY_RATE_LIMIT:
            return 429
        return 401


def _context_from_headers(
    headers: Mapping[str, str],
    *,
    allow_authorization_bearer: bool = True,
) -> GatewayRequestContext:
    normalized = {key.lower(): value for key, value in headers.items()}
    api_key_hash = _api_key_hash_from_headers(
        normalized,
        allow_authorization_bearer=allow_authorization_bearer,
    )
    api_key_id = _optional_header(normalized, "x-alice-api-key-id")
    user_id = _optional_header(normalized, "x-alice-user-id")
    client_ip = _optional_header(normalized, "x-alice-client-ip") or "127.0.0.1"
    user_agent = _optional_header(normalized, "user-agent") or (
        "alice-q22c-staging-http-harness/1.0"
    )
    return GatewayRequestContext(
        client_ip=client_ip,
        user_agent=user_agent,
        api_key_id=api_key_id,
        api_key_hash=api_key_hash,
        user_id=user_id,
    )


def _api_key_hash_from_headers(
    headers: Mapping[str, str],
    *,
    allow_authorization_bearer: bool,
) -> str | None:
    explicit_hash = _optional_header(headers, "x-alice-api-key-hash")
    if explicit_hash is not None:
        validate_sha256(explicit_hash, field_name="x-alice-api-key-hash")
        return explicit_hash

    authorization = _optional_header(headers, "authorization")
    if authorization is None:
        return None
    if not allow_authorization_bearer:
        raise ValueError("raw bearer API keys are forbidden; use x-alice-api-key-hash")
    scheme, _, credential = authorization.partition(" ")
    if scheme.lower() != "bearer" or not credential:
        return None
    return hashlib.sha256(credential.encode("utf-8")).hexdigest()


def _optional_header(headers: Mapping[str, str], name: str) -> str | None:
    value = headers.get(name)
    if value is None or not value.strip():
        return None
    return value.strip()


def _decode_json_object(body: bytes) -> object:
    if not body:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None


def _chat_payload_to_openai_payload(payload: Mapping[str, object]) -> dict[str, object]:
    if any(field in payload for field in ("api_key", "access_token", "token")):
        raise ValueError("raw API credentials must be supplied through headers only")

    if "messages" in payload:
        openai_payload = dict(payload)
        openai_payload.setdefault("model", DEFAULT_CHAT_MODEL_ID)
        return openai_payload

    prompt = payload.get("message", payload.get("prompt"))
    if not isinstance(prompt, str) or not prompt.strip():
        raise ValueError("message or prompt must be a non-empty string")

    model = payload.get("model", DEFAULT_CHAT_MODEL_ID)
    if not isinstance(model, str) or not model.strip():
        raise ValueError("model must be a non-empty string")

    openai_payload: dict[str, object] = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }
    metadata = payload.get("metadata")
    if metadata is not None:
        if not isinstance(metadata, Mapping):
            raise ValueError("metadata must be an object")
        openai_payload["metadata"] = dict(metadata)
    mode = payload.get("mode")
    if mode is not None:
        if not isinstance(mode, str) or not mode.strip():
            raise ValueError("mode must be a non-empty string")
        openai_payload["metadata"] = {**dict(openai_payload.get("metadata", {})), "mode": mode}
    max_tokens = payload.get("max_tokens")
    if max_tokens is not None:
        openai_payload["max_tokens"] = max_tokens
    user = payload.get("user")
    if user is not None:
        openai_payload["user"] = user
    return openai_payload


def _from_gateway_response(
    response: GatewayResponse,
    *,
    route_headers: Mapping[str, str],
) -> StagingHttpResponse:
    return StagingHttpResponse(
        status_code=response.status_code,
        headers={**route_headers, **response.headers},
        body=response.body,
    )


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
    if status_code == 429:
        return "rate_limit_error"
    if status_code == 403:
        return "permission_error"
    return "invalid_request_error"


def _json_headers(headers: Mapping[str, str], content_length: int) -> dict[str, str]:
    response_headers = dict(headers)
    response_headers.setdefault("Content-Type", "application/json")
    response_headers["Content-Length"] = str(content_length)
    return response_headers


def _request_key_present(headers: Mapping[str, str]) -> bool:
    normalized = {key.lower(): value for key, value in headers.items()}
    return bool(
        _optional_header(normalized, "x-alice-api-key-hash")
        or _optional_header(normalized, "authorization")
    )


def _key_admission_headers(
    route_headers: Mapping[str, str],
    admission: ApiChatGatewayKeyAdmission,
) -> dict[str, str]:
    headers = {
        **route_headers,
        "WWW-Authenticate": 'Bearer realm="alice-api-chat-staging"',
    }
    if admission.retry_after_seconds is not None:
        headers["Retry-After"] = str(admission.retry_after_seconds)
    return headers


def _canonical_contract_path(path: str) -> str:
    return CHAT_ROUTE_ALIASES.get(path, path)

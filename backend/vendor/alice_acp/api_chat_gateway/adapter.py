from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from alice_acp.api_chat import (
    STATUS_QUEUED,
    STATUS_REJECTED,
    ApiChatLocalBackend,
    ApiChatRateLimitPolicy,
    ApiChatRequest,
    ApiChatResult,
)
from alice_acp.api_chat.abuse import InMemoryApiChatAbuseGuard
from alice_acp.api_chat.contracts import stable_hash
from alice_acp.api_chat.scheduler import ApiChatInferenceScheduler
from alice_acp.api_chat.types import (
    ApiChatGatewayConfig,
    ApiChatGatewayMode,
    ApiChatGatewayRequest,
    ApiChatModelClass,
    ApiChatSchedulerRequest,
)
from alice_acp.api_chat_gateway.model_routing import (
    ApiChatModelRouteScheduler,
    ModelRouteDecision,
    ModelRouteRateLimitResult,
    model_route_request_for_chat,
)
from alice_acp.api_chat_gateway.types import (
    OPENAI_CHAT_GATEWAY_SERVICE,
    ChatCompletionRequest,
    ChatMessage,
    GatewayConfig,
    GatewayMode,
    GatewayRequestContext,
    GatewayResponse,
    ModelRegistry,
)


@dataclass(slots=True)
class OpenAICompatibleChatGateway:
    config: GatewayConfig = field(default_factory=GatewayConfig)
    backend: ApiChatLocalBackend = field(default_factory=ApiChatLocalBackend)
    model_registry: ModelRegistry = field(default_factory=ModelRegistry)
    scheduler: ApiChatInferenceScheduler = field(default_factory=ApiChatInferenceScheduler)
    model_router: ApiChatModelRouteScheduler = field(
        default_factory=ApiChatModelRouteScheduler
    )
    abuse_config: ApiChatGatewayConfig = field(default_factory=ApiChatGatewayConfig)
    abuse_guard: InMemoryApiChatAbuseGuard = field(default_factory=InMemoryApiChatAbuseGuard)

    def list_models(self) -> GatewayResponse:
        return GatewayResponse(
            status_code=200,
            headers={"X-Alice-Local-Contract-Only": "true"},
            body=self.model_registry.to_openai_list(),
        )

    def create_chat_completion(
        self,
        payload: dict[str, object] | ChatCompletionRequest,
        *,
        context: GatewayRequestContext | None = None,
    ) -> GatewayResponse:
        request_context = context or GatewayRequestContext()
        if not self.config.backend_available:
            return _error_response(
                503,
                code="backend_unavailable",
                message="API chat backend unavailable",
                metadata=self._base_metadata(),
            )

        try:
            request = ChatCompletionRequest.from_payload(payload)
        except ValueError as exc:
            return _error_response(
                400,
                code="invalid_request",
                message=str(exc),
                metadata=self._base_metadata(),
            )

        if request.stream:
            return _error_response(
                400,
                code="stream_not_supported_in_local_contract",
                message="stream=true is not supported by the local gateway contract",
                metadata=self._base_metadata(mode=request.metadata.mode),
            )
        if not self.model_registry.contains(request.model):
            return _error_response(
                400,
                code="model_not_found",
                message="requested model is not in the local registry",
                metadata=self._base_metadata(mode=request.metadata.mode),
            )
        if self.config.require_api_key and request_context.backend_api_key_id is None:
            return _error_response(
                401,
                code="api_key_required",
                message="API key id or hash is required",
                headers={"WWW-Authenticate": 'Bearer realm="alice-api-chat-local"'},
                metadata=self._base_metadata(mode=request.metadata.mode),
            )

        health = self.backend.health()
        if health.get("ok") is not True:
            return _error_response(
                503,
                code="backend_unavailable",
                message="API chat backend unavailable",
                metadata=self._base_metadata(mode=request.metadata.mode),
            )

        rendered_prompt = render_chat_prompt(request.messages)
        abuse_decision = self.abuse_guard.assess(
            ApiChatGatewayRequest(
                request_kind="api"
                if request_context.backend_api_key_id is not None
                else "chat",
                prompt=rendered_prompt,
                client_ip=request_context.client_ip,
                mode=_api_chat_mode(request.metadata.mode),
                requested_model_class=_requested_model_class(request),
                requested_at=request_context.observed_at,
                user_id=request_context.user_id or request.user,
                api_key_id=request_context.backend_api_key_id,
            ),
            config=self.abuse_config,
        )
        if not abuse_decision.admitted:
            return _error_response(
                400,
                code=abuse_decision.reason_code,
                message="request rejected by abuse guard",
                metadata={
                    **self._base_metadata(mode=request.metadata.mode),
                    "prompt_hash": abuse_decision.prompt_hash,
                    "raw_prompt_persisted": False,
                    "raw_response_persisted": False,
                },
            )

        prompt_digest = stable_hash({"messages": rendered_prompt})
        model_route_decision = None
        scheduler_decision = None
        if self.model_router.enabled:
            model_route_decision = self.model_router.route(
                model_route_request_for_chat(
                    request_id=_scheduler_request_id(request, request_context),
                    mode=request.metadata.mode,
                    requested_model_class=_requested_model_class(request),
                    prompt_hash=prompt_digest,
                    observed_at=request_context.observed_at,
                    identity_kind=_identity_kind(request, request_context),
                )
            )
            if model_route_decision.status == "rejected":
                return _error_response(
                    503,
                    code=model_route_decision.reason_code,
                    message="no loaded or cached Alice model route is available",
                    metadata={
                        **self._base_metadata(mode=request.metadata.mode),
                        "model_route": model_route_decision.to_public_dict(),
                    },
                )
        elif self.scheduler.devices:
            scheduler_decision = self.scheduler.schedule(
                ApiChatSchedulerRequest(
                    request_id=_scheduler_request_id(request, request_context),
                    mode=_api_chat_mode(request.metadata.mode),
                    requested_model_class=_requested_model_class(request),
                    prompt_hash=prompt_digest,
                )
            )
            if scheduler_decision.status == "rejected":
                return _error_response(
                    503,
                    code=scheduler_decision.reason_code,
                    message="no eligible Alice inference device is available",
                    metadata={
                        **self._base_metadata(mode=request.metadata.mode),
                        "scheduler": _scheduler_metadata(scheduler_decision),
                    },
                )

        response_content = _contract_response_content(
            mode=request.metadata.mode,
            max_tokens=request.max_tokens,
        )
        input_tokens = estimate_chat_input_tokens(request.messages)
        output_tokens = estimate_text_tokens(response_content)
        backend_user_id = (
            None
            if request_context.backend_api_key_id is not None
            else request_context.user_id or request.user
        )
        backend_request = ApiChatRequest(
            prompt=rendered_prompt,
            client_ip=request_context.client_ip,
            user_agent=request_context.user_agent,
            requested_at=request_context.observed_at,
            model_id=request.model,
            user_id=backend_user_id,
            api_key_id=request_context.backend_api_key_id,
            input_tokens=input_tokens,
            max_output_tokens=request.max_tokens or 256,
        )

        try:
            result = self.backend.handle_chat(
                backend_request,
                output_tokens=output_tokens,
                latency_ms=0,
                queue=(
                    scheduler_decision is not None and scheduler_decision.status == "queued"
                )
                or (
                    model_route_decision is not None
                    and model_route_decision.status == "queued"
                ),
            )
        except RuntimeError:
            return _error_response(
                503,
                code="backend_unavailable",
                message="API chat backend unavailable",
                metadata=self._base_metadata(mode=request.metadata.mode),
            )

        headers = _rate_limit_headers(result, self.backend.config.rate_limit_policy)
        if result.status == STATUS_REJECTED:
            return _error_response(
                429,
                code=result.reason_code,
                message="rate limit exceeded",
                headers=headers,
                retry_after=result.retry_after_seconds,
                metadata=self._metadata_for_result(result, request.metadata.mode),
            )

        if result.status == STATUS_QUEUED:
            response = GatewayResponse(
                status_code=202,
                headers=headers,
                body={
                    "id": result.request_id,
                    "object": "chat.completion.queued",
                    "created": int(result.record.requested_at.timestamp()),
                    "model": result.record.model_id,
                    "status": "queued",
                    "choices": [],
                    "usage": {
                        "prompt_tokens": result.record.usage.input_tokens,
                        "completion_tokens": 0,
                        "total_tokens": result.record.usage.input_tokens,
                    },
                    "metadata": self._metadata_for_result(result, request.metadata.mode),
                },
            )
            if scheduler_decision is not None:
                response.body["metadata"]["scheduler"] = _scheduler_metadata(scheduler_decision)
            if model_route_decision is not None:
                response.body["metadata"]["model_route"] = _model_route_metadata(
                    model_route_decision
                )
            return response

        response = GatewayResponse(
            status_code=200,
            headers=headers,
            body={
                "id": result.request_id,
                "object": "chat.completion",
                "created": int(result.record.requested_at.timestamp()),
                "model": result.record.model_id,
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": response_content},
                        "finish_reason": "stop",
                    }
                ],
                "usage": {
                    "prompt_tokens": result.record.usage.input_tokens,
                    "completion_tokens": result.record.usage.output_tokens,
                    "total_tokens": result.record.usage.total_tokens,
                },
                "metadata": self._metadata_for_result(result, request.metadata.mode),
            },
        )
        if scheduler_decision is not None:
            response.body["metadata"]["scheduler"] = _scheduler_metadata(scheduler_decision)
        if model_route_decision is not None:
            response.body["metadata"]["model_route"] = _model_route_metadata(
                model_route_decision
            )
        return response

    def _base_metadata(self, *, mode: GatewayMode = "Auto") -> dict[str, object]:
        health = self.backend.health()
        return {
            "service": OPENAI_CHAT_GATEWAY_SERVICE,
            "contract_version": self.config.contract_version,
            "backend_contract_version": health.get("contract_version"),
            "mode": mode,
            "local_contract_only": self.config.local_contract_only,
            "public_service_enabled": self.config.public_service_enabled,
            "live_reward_enabled": health.get("live_reward_enabled", False),
            "payout_executor_enabled": health.get("payout_executor_enabled", False),
            "paid_acu": "0",
            "model_routing": self.model_router.summary(),
        }

    def _metadata_for_result(
        self,
        result: ApiChatResult,
        mode: GatewayMode,
    ) -> dict[str, object]:
        metadata = self._base_metadata(mode=mode)
        metadata.update(
            {
                "request_id": result.request_id,
                "request_hash": result.record.request_hash,
                "prompt_hash": result.record.prompt_hash,
                "raw_prompt_persisted": False,
                "raw_response_persisted": False,
                "foundation_revenue_recorded": result.foundation_revenue is not None,
                "rate_limit": _rate_limit_metadata(
                    result,
                    self.backend.config.rate_limit_policy,
                ),
            }
        )
        metadata["rate_limit_result"] = _rate_limit_result_metadata(result, metadata["rate_limit"])
        return metadata


def render_chat_prompt(messages: tuple[ChatMessage, ...]) -> str:
    return "\n".join(f"{message.role}: {message.content}" for message in messages)


def estimate_chat_input_tokens(messages: tuple[ChatMessage, ...]) -> int:
    return sum(4 + estimate_text_tokens(message.content) for message in messages)


def estimate_text_tokens(text: str) -> int:
    return max(1, len(text.split()))


def _contract_response_content(*, mode: GatewayMode, max_tokens: int | None) -> str:
    if max_tokens is not None and max_tokens <= 1:
        return "Accepted."
    if mode == "Fast":
        return "Accepted."
    if mode in {"Roleplay", "RP Lite", "RP Pro"}:
        return "Alice local roleplay contract response accepted."
    if mode == "Best":
        return "Alice local best-effort contract response accepted."
    return "Alice local contract response accepted."


def _api_chat_mode(mode: GatewayMode) -> ApiChatGatewayMode:
    return {
        "Auto": "auto",
        "Fast": "fast",
        "Standard": "standard",
        "Roleplay": "roleplay",
        "Best": "best",
        "RP Lite": "rp_lite",
        "RP Pro": "rp_pro",
    }[mode]


def _requested_model_class(request: ChatCompletionRequest) -> ApiChatModelClass:
    model_map: dict[str, ApiChatModelClass] = {
        "alice-lite-4b@contract": "alice_lite_4b",
        "alice-standard-9b@contract": "alice_standard_9b",
        "alice-pro-27b@contract": "alice_pro_27b",
        "alice-pro-35b-moe@contract": "alice_pro_35b_moe",
        "alice-rp-lite-9b@contract": "rp_lite_9b",
        "alice-rp-pro-27b@contract": "rp_pro_27b",
    }
    if request.model in model_map:
        return model_map[request.model]
    if request.metadata.mode == "RP Pro":
        return "rp_pro_27b"
    if request.metadata.mode in {"Roleplay", "RP Lite"}:
        return "rp_lite_9b"
    if request.metadata.mode == "Best":
        return "alice_pro_35b_moe"
    if request.metadata.mode == "Standard":
        return "alice_standard_9b"
    if request.metadata.mode == "Fast":
        return "alice_lite_4b"
    return "auto"


def _scheduler_request_id(
    request: ChatCompletionRequest,
    context: GatewayRequestContext,
) -> str:
    digest = stable_hash(
        {
            "model": request.model,
            "mode": request.metadata.mode,
            "client_ip": context.client_ip,
            "observed_at": context.observed_at,
        }
    )
    return f"chat-sched-{digest[:24]}"


def _scheduler_metadata(decision: object) -> dict[str, object]:
    return {
        "status": decision.status,
        "reason_code": decision.reason_code,
        "selected_device_id": decision.selected_device_id,
        "selected_miner_id": decision.selected_miner_id,
        "model_lane": decision.model_lane,
        "runtime": decision.runtime,
        "fallback_from": decision.fallback_from,
        "downgrade_applied": decision.downgrade_applied,
        "demand_driven": decision.demand_driven,
        "should_throttle_mining": decision.should_throttle_mining,
        "background_inference_started": decision.background_inference_started,
    }


def _model_route_metadata(decision: ModelRouteDecision) -> dict[str, object]:
    return decision.to_public_dict()


def _identity_kind(
    request: ChatCompletionRequest,
    context: GatewayRequestContext,
) -> str:
    if context.backend_api_key_id is not None:
        return "api_key"
    if context.user_id is not None or request.user is not None:
        return "user_id"
    return "anonymous"


def _error_response(
    status_code: int,
    *,
    code: str,
    message: str,
    headers: dict[str, str] | None = None,
    retry_after: int | None = None,
    metadata: dict[str, object] | None = None,
) -> GatewayResponse:
    error: dict[str, object] = {
        "message": message,
        "type": _error_type(status_code),
        "code": code,
    }
    if retry_after is not None:
        error["retry_after"] = retry_after
    body: dict[str, Any] = {"error": error}
    if metadata is not None:
        body["metadata"] = metadata
    response_headers = dict(headers or {})
    if retry_after is not None:
        response_headers["Retry-After"] = str(retry_after)
    return GatewayResponse(status_code=status_code, headers=response_headers, body=body)


def _error_type(status_code: int) -> str:
    if status_code == 401:
        return "authentication_error"
    if status_code == 429:
        return "rate_limit_error"
    if status_code == 503:
        return "service_unavailable_error"
    return "invalid_request_error"


def _rate_limit_headers(
    result: ApiChatResult,
    policy: ApiChatRateLimitPolicy,
) -> dict[str, str]:
    metadata = _rate_limit_metadata(result, policy)
    headers = {
        "X-RateLimit-Limit-Requests": str(metadata["limit_requests_per_hour"]),
        "X-RateLimit-Remaining-Requests": str(metadata["remaining_requests"]),
        "X-RateLimit-Remaining-Burst": str(metadata["remaining_burst"]),
    }
    if metadata["retry_after"] is not None:
        headers["Retry-After"] = str(metadata["retry_after"])
    return headers


def _rate_limit_metadata(
    result: ApiChatResult,
    policy: ApiChatRateLimitPolicy,
) -> dict[str, object]:
    decision = result.abuse_decision
    if decision is None:
        return {
            "identity_kind": "unknown",
            "limit_requests_per_hour": 0,
            "remaining_requests": 0,
            "remaining_burst": 0,
            "retry_after": result.retry_after_seconds,
        }

    if decision.subject.identity_kind == "api_key":
        hourly_limit = policy.api_key_requests_per_hour
    elif decision.subject.identity_kind == "user_id":
        hourly_limit = policy.user_requests_per_hour
    else:
        hourly_limit = policy.free_requests_per_hour

    return {
        "identity_kind": decision.subject.identity_kind,
        "limit_requests_per_hour": hourly_limit,
        "remaining_requests": decision.remaining_hourly,
        "remaining_burst": decision.remaining_burst,
        "retry_after": result.retry_after_seconds,
    }


def _rate_limit_result_metadata(
    result: ApiChatResult,
    metadata: dict[str, object],
) -> dict[str, object]:
    return ModelRouteRateLimitResult.from_metadata(
        admitted=result.status != STATUS_REJECTED,
        reason_code=result.reason_code,
        metadata=metadata,
    ).to_public_dict()

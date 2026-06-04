from __future__ import annotations

from dataclasses import dataclass, field

from alice_acp.api_chat.abuse import InMemoryApiChatAbuseGuard
from alice_acp.api_chat.contracts import request_id_for_hash, stable_hash
from alice_acp.api_chat.rate_limit import (
    InMemoryApiChatRateLimiter,
    hashed_bucket_value,
)
from alice_acp.api_chat.scheduler import ApiChatInferenceScheduler
from alice_acp.api_chat.types import (
    ApiChatGatewayConfig,
    ApiChatGatewayDecision,
    ApiChatGatewayRequest,
    ApiChatRateLimitBucket,
    ApiChatSchedulerRequest,
)

REASON_GATEWAY_ADMITTED = "api_chat_gateway_admitted"
REASON_GATEWAY_QUEUED = "api_chat_gateway_queued"
REASON_API_KEY_MISSING = "api_chat_api_key_missing"
REASON_API_KEY_DISABLED = "api_chat_api_key_disabled"
REASON_API_KEY_UNKNOWN = "api_chat_api_key_unknown"
REASON_FREE_CHAT_RATE_LIMIT = "api_chat_free_chat_hourly_limit_exceeded"
REASON_API_KEY_RATE_LIMIT = "api_chat_api_key_hourly_limit_exceeded"
REASON_IP_RATE_LIMIT = "api_chat_ip_hourly_limit_exceeded"
REASON_USER_RATE_LIMIT = "api_chat_user_hourly_limit_exceeded"


@dataclass(slots=True)
class ApiChatGatewayContract:
    config: ApiChatGatewayConfig = field(default_factory=ApiChatGatewayConfig)
    scheduler: ApiChatInferenceScheduler = field(default_factory=ApiChatInferenceScheduler)
    rate_limiter: InMemoryApiChatRateLimiter = field(default_factory=InMemoryApiChatRateLimiter)
    abuse_guard: InMemoryApiChatAbuseGuard = field(default_factory=InMemoryApiChatAbuseGuard)

    def handle(self, request: ApiChatGatewayRequest) -> ApiChatGatewayDecision:
        request_hash = _gateway_request_hash(request)
        abuse = self.abuse_guard.assess(request, config=self.config)
        if not abuse.admitted:
            return ApiChatGatewayDecision(
                status="rejected",
                reason_code=abuse.reason_code,
                request_hash=request_hash,
                prompt_hash=abuse.prompt_hash,
                abuse=abuse,
            )

        key_policy = None
        if request.request_kind == "api":
            if request.api_key_id is None:
                return ApiChatGatewayDecision(
                    status="rejected",
                    reason_code=REASON_API_KEY_MISSING,
                    request_hash=request_hash,
                    prompt_hash=abuse.prompt_hash,
                    abuse=abuse,
                )
            key_policy = self.config.api_key_policy(request.api_key_id)
            if key_policy is None:
                return ApiChatGatewayDecision(
                    status="rejected",
                    reason_code=REASON_API_KEY_UNKNOWN,
                    request_hash=request_hash,
                    prompt_hash=abuse.prompt_hash,
                    abuse=abuse,
                )
            if not key_policy.enabled:
                return ApiChatGatewayDecision(
                    status="rejected",
                    reason_code=REASON_API_KEY_DISABLED,
                    request_hash=request_hash,
                    prompt_hash=abuse.prompt_hash,
                    abuse=abuse,
                )

        rate_limit = self.rate_limiter.consume(
            buckets=_rate_limit_buckets(request, self.config, key_requests_per_hour=(
                key_policy.requests_per_hour if key_policy is not None else None
            )),
            observed_at=request.requested_at,
        )
        if not rate_limit.admitted:
            return ApiChatGatewayDecision(
                status="rejected",
                reason_code=rate_limit.reason_code,
                request_hash=request_hash,
                prompt_hash=abuse.prompt_hash,
                rate_limit=rate_limit,
                abuse=abuse,
                retry_after_seconds=rate_limit.retry_after_seconds,
            )

        request_id = request_id_for_hash(request_hash)
        scheduler_decision = self.scheduler.schedule(
            ApiChatSchedulerRequest(
                request_id=request_id,
                mode=request.mode,
                requested_model_class=request.requested_model_class,
                prompt_hash=abuse.prompt_hash,
            )
        )
        if scheduler_decision.status == "admitted":
            reason_code = REASON_GATEWAY_ADMITTED
        elif scheduler_decision.status == "queued":
            reason_code = REASON_GATEWAY_QUEUED
        else:
            reason_code = scheduler_decision.reason_code
        return ApiChatGatewayDecision(
            status=scheduler_decision.status,
            reason_code=reason_code,
            request_hash=request_hash,
            prompt_hash=abuse.prompt_hash,
            rate_limit=rate_limit,
            abuse=abuse,
            scheduler=scheduler_decision,
        )


def _rate_limit_buckets(
    request: ApiChatGatewayRequest,
    config: ApiChatGatewayConfig,
    *,
    key_requests_per_hour: int | None,
) -> tuple[ApiChatRateLimitBucket, ...]:
    buckets: list[ApiChatRateLimitBucket] = []
    if request.request_kind == "chat":
        buckets.append(
            ApiChatRateLimitBucket(
                name="free_chat_ip",
                key_hash=hashed_bucket_value("client_ip", request.client_ip),
                requests_per_hour=config.rate_policy.free_chat_requests_per_hour,
                reason_code=REASON_FREE_CHAT_RATE_LIMIT,
            )
        )
        if request.user_id is not None:
            buckets.append(
                ApiChatRateLimitBucket(
                    name="free_chat_user",
                    key_hash=hashed_bucket_value("user_id", request.user_id),
                    requests_per_hour=config.rate_policy.free_chat_requests_per_hour,
                    reason_code=REASON_FREE_CHAT_RATE_LIMIT,
                )
            )
        return tuple(buckets)

    if request.api_key_id is None:
        raise ValueError("api request must have api_key_id before rate limiting")
    buckets.append(
        ApiChatRateLimitBucket(
            name="api_key",
            key_hash=hashed_bucket_value("api_key_id", request.api_key_id),
            requests_per_hour=key_requests_per_hour
            or config.rate_policy.api_key_default_requests_per_hour,
            reason_code=REASON_API_KEY_RATE_LIMIT,
        )
    )
    buckets.append(
        ApiChatRateLimitBucket(
            name="api_ip",
            key_hash=hashed_bucket_value("client_ip", request.client_ip),
            requests_per_hour=config.rate_policy.ip_requests_per_hour,
            reason_code=REASON_IP_RATE_LIMIT,
        )
    )
    if request.user_id is not None:
        buckets.append(
            ApiChatRateLimitBucket(
                name="api_user",
                key_hash=hashed_bucket_value("user_id", request.user_id),
                requests_per_hour=config.rate_policy.user_requests_per_hour,
                reason_code=REASON_USER_RATE_LIMIT,
            )
        )
    return tuple(buckets)


def _gateway_request_hash(request: ApiChatGatewayRequest) -> str:
    return stable_hash(
        {
            "request_kind": request.request_kind,
            "prompt_hash": stable_hash({"prompt": request.prompt}),
            "client_ip_hash": hashed_bucket_value("client_ip", request.client_ip),
            "user_id_hash": hashed_bucket_value("user_id", request.user_id)
            if request.user_id is not None
            else None,
            "api_key_id_hash": hashed_bucket_value("api_key_id", request.api_key_id)
            if request.api_key_id is not None
            else None,
            "mode": request.mode,
            "requested_model_class": request.requested_model_class,
            "requested_at": request.requested_at,
        }
    )

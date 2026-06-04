from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal

from alice_acp.api_chat.contracts import (
    prompt_hash,
    request_hash,
    request_id_for_hash,
    resolve_abuse_subject,
    resolve_anonymous_abuse_subject,
    revenue_id_for_request,
)
from alice_acp.api_chat.store import InMemoryApiChatStore
from alice_acp.api_chat.types import (
    REASON_CHAT_COMPLETED,
    REASON_CHAT_FAILED,
    REASON_CHAT_QUEUED,
    STATUS_COMPLETED,
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_REJECTED,
    ApiChatAbuseDecision,
    ApiChatAbuseSubject,
    ApiChatBackendConfig,
    ApiChatRateLimitPolicy,
    ApiChatRecord,
    ApiChatRequest,
    ApiChatResult,
    ApiChatUsage,
    FoundationRevenueMockRecord,
    utc_now,
)


@dataclass(slots=True)
class ApiChatLocalBackend:
    config: ApiChatBackendConfig = field(default_factory=ApiChatBackendConfig)
    store: InMemoryApiChatStore = field(default_factory=InMemoryApiChatStore)

    def health(self) -> dict[str, object]:
        return {
            "ok": True,
            "service": "q22-api-chat-local-contract",
            "contract_version": self.config.contract_version,
            "local_contract_only": self.config.local_contract_only,
            "public_service_enabled": self.config.public_service_enabled,
            "persist_raw_prompt": self.config.persist_raw_prompt,
            "live_reward_enabled": self.config.live_reward_enabled,
            "payout_executor_enabled": self.config.payout_executor_enabled,
        }

    def admit(self, request: ApiChatRequest) -> ApiChatResult:
        subject = resolve_abuse_subject(request)
        checks = _admission_checks(
            request=request,
            primary_subject=subject,
            policy=self.config.rate_limit_policy,
        )
        decisions = []
        for check in checks:
            abuse_decision = self.store.evaluate_admission(
                subject=check.subject,
                policy=check.policy,
                observed_at=request.requested_at,
            )
            decisions.append(abuse_decision)
            if not abuse_decision.admitted:
                return self._record_admission_decision(
                    request=request,
                    subject=abuse_decision.subject,
                    abuse_decision=abuse_decision,
                )

        abuse_decision = decisions[-1]
        digest = request_hash(request, subject)
        request_id = request_id_for_hash(digest)
        status = "admitted"
        record = ApiChatRecord(
            request_id=request_id,
            request_hash=digest,
            prompt_hash=prompt_hash(request.prompt),
            status=status,
            reason_code=abuse_decision.reason_code,
            subject=subject,
            model_id=request.model_id,
            usage=ApiChatUsage(input_tokens=request.input_tokens, output_tokens=0),
            latency_ms=0,
            requested_at=request.requested_at,
            recorded_at=request.requested_at,
        )
        for check in checks:
            self.store.mark_admitted(subject=check.subject, observed_at=request.requested_at)
        stored = self.store.add_record(record)
        return ApiChatResult(
            status=stored.status,
            reason_code=stored.reason_code,
            request_id=stored.request_id,
            record=stored,
            abuse_decision=abuse_decision,
            retry_after_seconds=abuse_decision.retry_after_seconds,
        )

    def _record_admission_decision(
        self,
        *,
        request: ApiChatRequest,
        subject: ApiChatAbuseSubject,
        abuse_decision: ApiChatAbuseDecision,
    ) -> ApiChatResult:
        digest = request_hash(request, subject)
        request_id = request_id_for_hash(digest)
        record = ApiChatRecord(
            request_id=request_id,
            request_hash=digest,
            prompt_hash=prompt_hash(request.prompt),
            status=STATUS_REJECTED,
            reason_code=abuse_decision.reason_code,
            subject=subject,
            model_id=request.model_id,
            usage=ApiChatUsage(input_tokens=request.input_tokens, output_tokens=0),
            latency_ms=0,
            requested_at=request.requested_at,
            recorded_at=request.requested_at,
        )
        stored = self.store.add_record(record)
        return ApiChatResult(
            status=stored.status,
            reason_code=stored.reason_code,
            request_id=stored.request_id,
            record=stored,
            abuse_decision=abuse_decision,
            retry_after_seconds=abuse_decision.retry_after_seconds,
        )

    def queue_request(
        self,
        request_id: str,
        *,
        observed_at: datetime | None = None,
        reason_code: str = REASON_CHAT_QUEUED,
        abuse_decision: ApiChatAbuseDecision | None = None,
    ) -> ApiChatResult:
        record = self.store.transition_record(
            request_id=request_id,
            status=STATUS_QUEUED,
            reason_code=reason_code,
            observed_at=observed_at or utc_now(),
        )
        return ApiChatResult(
            status=record.status,
            reason_code=record.reason_code,
            request_id=record.request_id,
            record=record,
            abuse_decision=abuse_decision,
        )

    def complete_request(
        self,
        request_id: str,
        *,
        usage: ApiChatUsage,
        latency_ms: int,
        observed_at: datetime | None = None,
        simulated_foundation_revenue: Decimal = Decimal("0"),
        abuse_decision: ApiChatAbuseDecision | None = None,
    ) -> ApiChatResult:
        completed_at = observed_at or utc_now()
        record = self.store.transition_record(
            request_id=request_id,
            status=STATUS_COMPLETED,
            reason_code=REASON_CHAT_COMPLETED,
            usage=usage,
            latency_ms=latency_ms,
            observed_at=completed_at,
        )
        revenue_record = None
        if simulated_foundation_revenue > Decimal("0"):
            revenue_record = self.store.add_foundation_revenue(
                FoundationRevenueMockRecord(
                    revenue_id=revenue_id_for_request(
                        request_id=request_id,
                        amount=simulated_foundation_revenue,
                        source=self.config.revenue_source,
                    ),
                    request_id=request_id,
                    amount=simulated_foundation_revenue,
                    source=self.config.revenue_source,
                    recorded_at=completed_at,
                )
            )
        return ApiChatResult(
            status=record.status,
            reason_code=record.reason_code,
            request_id=record.request_id,
            record=record,
            abuse_decision=abuse_decision,
            foundation_revenue=revenue_record,
        )

    def fail_request(
        self,
        request_id: str,
        *,
        reason_code: str = REASON_CHAT_FAILED,
        latency_ms: int = 0,
        observed_at: datetime | None = None,
        abuse_decision: ApiChatAbuseDecision | None = None,
    ) -> ApiChatResult:
        record = self.store.transition_record(
            request_id=request_id,
            status=STATUS_FAILED,
            reason_code=reason_code,
            latency_ms=latency_ms,
            observed_at=observed_at or utc_now(),
        )
        return ApiChatResult(
            status=record.status,
            reason_code=record.reason_code,
            request_id=record.request_id,
            record=record,
            abuse_decision=abuse_decision,
        )

    def handle_chat(
        self,
        request: ApiChatRequest,
        *,
        output_tokens: int = 0,
        latency_ms: int = 0,
        queue: bool = False,
        fail_reason_code: str | None = None,
    ) -> ApiChatResult:
        admitted = self.admit(request)
        if not admitted.accepted:
            return admitted
        if queue:
            return self.queue_request(
                admitted.request_id,
                observed_at=request.requested_at,
                abuse_decision=admitted.abuse_decision,
            )
        if fail_reason_code is not None:
            return self.fail_request(
                admitted.request_id,
                reason_code=fail_reason_code,
                latency_ms=latency_ms,
                observed_at=request.requested_at,
                abuse_decision=admitted.abuse_decision,
            )
        return self.complete_request(
            admitted.request_id,
            usage=ApiChatUsage(input_tokens=request.input_tokens, output_tokens=output_tokens),
            latency_ms=latency_ms,
            observed_at=request.requested_at,
            simulated_foundation_revenue=request.simulated_foundation_revenue,
            abuse_decision=admitted.abuse_decision,
        )


@dataclass(frozen=True, slots=True)
class _AdmissionCheck:
    subject: ApiChatAbuseSubject
    policy: ApiChatRateLimitPolicy


def _admission_checks(
    *,
    request: ApiChatRequest,
    primary_subject: ApiChatAbuseSubject,
    policy: ApiChatRateLimitPolicy,
) -> tuple[_AdmissionCheck, ...]:
    if request.user_id is None or request.api_key_id is not None:
        return (_AdmissionCheck(subject=primary_subject, policy=policy),)

    ip_subject = resolve_anonymous_abuse_subject(request)
    free_user_policy = replace(
        policy,
        user_requests_per_hour=policy.free_requests_per_hour,
        user_burst_requests=policy.free_burst_requests,
        user_burst_window=policy.free_burst_window,
    )
    return (
        _AdmissionCheck(subject=ip_subject, policy=policy),
        _AdmissionCheck(subject=primary_subject, policy=free_user_policy),
    )

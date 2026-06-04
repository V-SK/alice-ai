from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta

from alice_acp.api_chat.types import (
    REASON_API_KEY_BURST_LIMIT,
    REASON_API_KEY_RATE_LIMIT,
    REASON_CHAT_ADMITTED,
    REASON_FREE_BURST_LIMIT,
    REASON_FREE_RATE_LIMIT,
    REASON_USER_BURST_LIMIT,
    REASON_USER_RATE_LIMIT,
    STATUS_COMPLETED,
    STATUS_FAILED,
    ApiChatAbuseDecision,
    ApiChatAbuseSubject,
    ApiChatLifecycleEvent,
    ApiChatRateLimitPolicy,
    ApiChatRecord,
    ApiChatUsage,
    FoundationRevenueMockRecord,
    LifecycleStatus,
)
from alice_acp.api_chat.validators import validate_aware_timestamp


@dataclass(slots=True)
class InMemoryApiChatStore:
    records: dict[str, ApiChatRecord] = field(default_factory=dict)
    lifecycle_events: list[ApiChatLifecycleEvent] = field(default_factory=list)
    foundation_revenue_records: dict[str, FoundationRevenueMockRecord] = field(
        default_factory=dict
    )
    _admissions_by_limit_key: dict[str, list[datetime]] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)

    def evaluate_admission(
        self,
        *,
        subject: ApiChatAbuseSubject,
        policy: ApiChatRateLimitPolicy,
        observed_at: datetime,
    ) -> ApiChatAbuseDecision:
        validate_aware_timestamp("observed_at", observed_at)
        hourly_limit, burst_limit, burst_window, hourly_reason, burst_reason = _limit_config(
            subject, policy
        )
        with self._lock:
            retained = self._retained_admissions(subject.limit_key, observed_at)
            if len(retained) >= hourly_limit:
                retry_after = _retry_after_seconds(
                    oldest=retained[0],
                    observed_at=observed_at,
                    window=timedelta(hours=1),
                )
                return ApiChatAbuseDecision(
                    admitted=False,
                    subject=subject,
                    reason_code=hourly_reason,
                    denied_reasons=(hourly_reason,),
                    retry_after_seconds=retry_after,
                )

            burst_cutoff = observed_at - burst_window
            burst = [timestamp for timestamp in retained if timestamp > burst_cutoff]
            if len(burst) >= burst_limit:
                retry_after = _retry_after_seconds(
                    oldest=burst[0],
                    observed_at=observed_at,
                    window=burst_window,
                )
                return ApiChatAbuseDecision(
                    admitted=False,
                    subject=subject,
                    reason_code=burst_reason,
                    denied_reasons=(burst_reason,),
                    retry_after_seconds=retry_after,
                    remaining_hourly=max(hourly_limit - len(retained), 0),
                )

            return ApiChatAbuseDecision(
                admitted=True,
                subject=subject,
                reason_code=REASON_CHAT_ADMITTED,
                remaining_hourly=max(hourly_limit - len(retained) - 1, 0),
                remaining_burst=max(burst_limit - len(burst) - 1, 0),
            )

    def mark_admitted(self, *, subject: ApiChatAbuseSubject, observed_at: datetime) -> None:
        validate_aware_timestamp("observed_at", observed_at)
        with self._lock:
            retained = self._retained_admissions(subject.limit_key, observed_at)
            retained.append(observed_at)
            self._admissions_by_limit_key[subject.limit_key] = retained

    def add_record(self, record: ApiChatRecord) -> ApiChatRecord:
        with self._lock:
            self.records[record.request_id] = record
            self.lifecycle_events.append(
                ApiChatLifecycleEvent(
                    request_id=record.request_id,
                    status=record.status,
                    reason_code=record.reason_code,
                    recorded_at=record.recorded_at,
                )
            )
        return record

    def transition_record(
        self,
        *,
        request_id: str,
        status: LifecycleStatus,
        reason_code: str,
        observed_at: datetime,
        usage: ApiChatUsage | None = None,
        latency_ms: int | None = None,
    ) -> ApiChatRecord:
        validate_aware_timestamp("observed_at", observed_at)
        with self._lock:
            existing = self.records[request_id]
            record = replace(
                existing,
                status=status,
                reason_code=reason_code,
                usage=usage if usage is not None else existing.usage,
                latency_ms=latency_ms if latency_ms is not None else existing.latency_ms,
                completed_at=observed_at
                if status in {STATUS_COMPLETED, STATUS_FAILED}
                else existing.completed_at,
            )
            self.records[request_id] = record
            self.lifecycle_events.append(
                ApiChatLifecycleEvent(
                    request_id=request_id,
                    status=status,
                    reason_code=reason_code,
                    recorded_at=observed_at,
                )
            )
        return record

    def add_foundation_revenue(
        self, record: FoundationRevenueMockRecord
    ) -> FoundationRevenueMockRecord:
        with self._lock:
            self.foundation_revenue_records[record.revenue_id] = record
        return record

    def lifecycle_for(self, request_id: str) -> tuple[ApiChatLifecycleEvent, ...]:
        return tuple(event for event in self.lifecycle_events if event.request_id == request_id)

    def _retained_admissions(self, limit_key: str, observed_at: datetime) -> list[datetime]:
        cutoff = observed_at - timedelta(hours=1)
        retained = [
            timestamp
            for timestamp in self._admissions_by_limit_key.get(limit_key, [])
            if timestamp > cutoff
        ]
        self._admissions_by_limit_key[limit_key] = retained
        return retained


def _limit_config(
    subject: ApiChatAbuseSubject,
    policy: ApiChatRateLimitPolicy,
) -> tuple[int, int, timedelta, str, str]:
    if subject.identity_kind == "user_id":
        return (
            policy.user_requests_per_hour,
            policy.user_burst_requests,
            policy.user_burst_window,
            REASON_USER_RATE_LIMIT,
            REASON_USER_BURST_LIMIT,
        )
    if subject.identity_kind == "api_key":
        return (
            policy.api_key_requests_per_hour,
            policy.api_key_burst_requests,
            policy.api_key_burst_window,
            REASON_API_KEY_RATE_LIMIT,
            REASON_API_KEY_BURST_LIMIT,
        )
    return (
        policy.free_requests_per_hour,
        policy.free_burst_requests,
        policy.free_burst_window,
        REASON_FREE_RATE_LIMIT,
        REASON_FREE_BURST_LIMIT,
    )


def _retry_after_seconds(*, oldest: datetime, observed_at: datetime, window: timedelta) -> int:
    retry_after = (oldest + window - observed_at).total_seconds()
    return max(1, math.ceil(retry_after))

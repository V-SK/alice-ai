from __future__ import annotations

import hashlib
import math
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from alice_acp.api_chat.types import (
    ApiChatRateLimitBucket,
    ApiChatRateLimitDecision,
)
from alice_acp.api_chat.validators import validate_aware_timestamp

REASON_RATE_LIMIT_ADMITTED = "api_chat_rate_limit_admitted"


@dataclass(slots=True)
class InMemoryApiChatRateLimiter:
    _admissions_by_limit_key: dict[str, list[datetime]] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)

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
                bucket.limit_key: self._retained(bucket.limit_key, observed_at)
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

            for bucket in buckets:
                retained = retained_by_key[bucket.limit_key]
                retained.append(observed_at)
                self._admissions_by_limit_key[bucket.limit_key] = retained

            remaining = min(
                bucket.requests_per_hour - len(retained_by_key[bucket.limit_key])
                for bucket in buckets
            )
            return ApiChatRateLimitDecision(
                admitted=True,
                reason_code=REASON_RATE_LIMIT_ADMITTED,
                remaining_requests=max(remaining, 0),
            )

    def _retained(self, limit_key: str, observed_at: datetime) -> list[datetime]:
        cutoff = observed_at - timedelta(hours=1)
        retained = [
            timestamp
            for timestamp in self._admissions_by_limit_key.get(limit_key, [])
            if timestamp > cutoff
        ]
        self._admissions_by_limit_key[limit_key] = retained
        return retained


def hashed_bucket_value(kind: str, value: str) -> str:
    if not kind or not value:
        raise ValueError("bucket hash inputs must be non-empty")
    return hashlib.sha256(f"{kind}:{value}".encode()).hexdigest()


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

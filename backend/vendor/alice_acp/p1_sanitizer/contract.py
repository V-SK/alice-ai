from __future__ import annotations

from alice_acp.p1_sanitizer.types import SanitizerResult, StreamingPolicy

P1_SANITIZER_FAIL_CLOSED = "P1_SANITIZER_FAIL_CLOSED"
AFTER_DELIVERY_SANITIZATION_NOT_ALLOWED = "AFTER_DELIVERY_SANITIZATION_NOT_ALLOWED"


def validate_streaming_policy(streaming_policy: StreamingPolicy) -> None:
    if streaming_policy == "after_delivery_only":
        raise ValueError(AFTER_DELIVERY_SANITIZATION_NOT_ALLOWED)


def fail_closed_result(
    request_id: str,
    reason_code: str = P1_SANITIZER_FAIL_CLOSED,
) -> SanitizerResult:
    return SanitizerResult(
        request_id=request_id,
        decision="hold",
        reason_code=reason_code,
    )

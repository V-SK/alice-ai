from __future__ import annotations

from dataclasses import dataclass

from alice_acp.p1_sanitizer.contract import (
    AFTER_DELIVERY_SANITIZATION_NOT_ALLOWED,
    P1_SANITIZER_FAIL_CLOSED,
    fail_closed_result,
    validate_streaming_policy,
)
from alice_acp.p1_sanitizer.types import SanitizerResult, StreamingPolicy

P1_SANITIZER_UNAVAILABLE = "P1_SANITIZER_UNAVAILABLE"
P1_SANITIZER_TIMEOUT = "P1_SANITIZER_TIMEOUT"
P1_SANITIZER_PARSE_ERROR = "P1_SANITIZER_PARSE_ERROR"
P1_SANITIZER_CONTRACT_READY = "P1_SANITIZER_CONTRACT_READY"


@dataclass(frozen=True, slots=True)
class P1SanitizerReadiness:
    input_result: SanitizerResult
    output_result: SanitizerResult
    streaming_policy_ok: bool
    p1_live_reward_ready: bool
    reason_codes: tuple[str, ...]


def evaluate_p1_sanitizer_readiness(
    *,
    request_id: str,
    input_available: bool,
    output_available: bool,
    streaming_policy: StreamingPolicy,
    timed_out: bool = False,
    parse_error: bool = False,
) -> P1SanitizerReadiness:
    reason_codes: list[str] = []
    streaming_policy_ok = True
    try:
        validate_streaming_policy(streaming_policy)
    except ValueError:
        streaming_policy_ok = False
        reason_codes.append(AFTER_DELIVERY_SANITIZATION_NOT_ALLOWED)

    if not input_available or not output_available:
        reason_codes.append(P1_SANITIZER_UNAVAILABLE)
    if timed_out:
        reason_codes.append(P1_SANITIZER_TIMEOUT)
    if parse_error:
        reason_codes.append(P1_SANITIZER_PARSE_ERROR)
    contract_ready = (
        streaming_policy_ok
        and input_available
        and output_available
        and not timed_out
        and not parse_error
    )
    if not reason_codes:
        reason_codes.append(
            P1_SANITIZER_CONTRACT_READY if contract_ready else P1_SANITIZER_FAIL_CLOSED
        )
    decision = "pass" if contract_ready else "hold"
    input_result = SanitizerResult(
        request_id=f"{request_id}-input",
        decision=decision,
        reason_code=P1_SANITIZER_CONTRACT_READY if decision == "pass" else reason_codes[0],
    )
    output_result = (
        SanitizerResult(
            request_id=f"{request_id}-output",
            decision="pass",
            reason_code=P1_SANITIZER_CONTRACT_READY,
        )
        if contract_ready
        else fail_closed_result(f"{request_id}-output", reason_codes[0])
    )
    return P1SanitizerReadiness(
        input_result=input_result,
        output_result=output_result,
        streaming_policy_ok=streaming_policy_ok,
        p1_live_reward_ready=False,
        reason_codes=tuple(dict.fromkeys(reason_codes)),
    )

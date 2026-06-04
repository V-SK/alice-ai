from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SanitizerDecision = Literal["pass", "reject", "sanitize", "hold", "error", "redact"]
StreamingPolicy = Literal["buffer_and_sanitize", "sliding_window", "after_delivery_only"]


@dataclass(frozen=True, slots=True)
class SanitizerTimeoutPolicy:
    timeout_ms: int
    fail_closed: bool = True

    def __post_init__(self) -> None:
        if self.timeout_ms <= 0:
            raise ValueError("timeout_ms must be positive")
        if not self.fail_closed:
            raise ValueError("P1 sanitizer timeout policy must fail closed")


@dataclass(frozen=True, slots=True)
class P1InputSanitizerRequest:
    request_id: str
    prompt_ref: str
    source_route_class: str
    policy_version: str
    timeout_policy: SanitizerTimeoutPolicy
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.request_id or not self.prompt_ref:
            raise ValueError("input sanitizer request ids must be non-empty")
        if not self.source_route_class or not self.policy_version:
            raise ValueError("input sanitizer policy fields must be non-empty")


@dataclass(frozen=True, slots=True)
class P1OutputSanitizerRequest:
    request_id: str
    output_ref: str
    streaming_policy: StreamingPolicy
    source_route_class: str
    policy_version: str
    timeout_policy: SanitizerTimeoutPolicy
    segment_index: int | None = None
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.request_id or not self.output_ref:
            raise ValueError("output sanitizer request ids must be non-empty")
        if not self.source_route_class or not self.policy_version:
            raise ValueError("output sanitizer policy fields must be non-empty")
        if self.segment_index is not None and self.segment_index < 0:
            raise ValueError("segment_index must be non-negative")


@dataclass(frozen=True, slots=True)
class SanitizerResult:
    request_id: str
    decision: SanitizerDecision
    reason_code: str
    sanitized_ref: str | None = None
    evidence_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.request_id or not self.reason_code:
            raise ValueError("sanitizer result fields must be non-empty")
        if self.decision == "sanitize" and not self.sanitized_ref:
            raise ValueError("sanitize decision requires sanitized_ref")

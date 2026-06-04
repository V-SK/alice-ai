from __future__ import annotations

from dataclasses import dataclass

from alice_acp.p1_sanitizer.readiness import P1SanitizerReadiness

P1_HARD_EXCLUSION_FLAGS = frozenset(
    {
        "uploaded_file",
        "account_memory",
        "private_conversation_history",
        "secret_tool_output",
        "codebase_attachment",
        "private_document",
        "access_token",
        "private_key",
        "legal_sensitive",
        "medical_sensitive",
        "financial_sensitive",
        "raw_api_account_metadata",
    }
)


@dataclass(frozen=True, slots=True)
class P1SanitizerProductionGateReport:
    excluded_flags: tuple[str, ...]
    reason_codes: tuple[str, ...]
    sanitizer_contract_ready: bool
    offline_eval_passed: bool
    default_off_rollout: bool
    p1_live_reward_ready: bool = False


def evaluate_p1_sanitizer_production_gate(
    readiness: P1SanitizerReadiness,
    *,
    content_flags: tuple[str, ...],
    offline_eval_passed: bool,
    default_off_rollout: bool = True,
) -> P1SanitizerProductionGateReport:
    excluded_flags = tuple(sorted(set(content_flags) & P1_HARD_EXCLUSION_FLAGS))
    reason_codes: list[str] = []
    if excluded_flags:
        reason_codes.append("P1_HARD_EXCLUSION_PRESENT")
    if not readiness.streaming_policy_ok:
        reason_codes.append("P1_STREAMING_POLICY_NOT_ALLOWED")
    if readiness.input_result.decision != "pass":
        reason_codes.append("P1_INPUT_SANITIZER_NOT_PASSING")
    if readiness.output_result.decision != "pass":
        reason_codes.append("P1_OUTPUT_SANITIZER_NOT_PASSING")
    if not offline_eval_passed:
        reason_codes.append("P1_OFFLINE_EVAL_NOT_PASSED")
    if not default_off_rollout:
        reason_codes.append("P1_ROLLOUT_NOT_DEFAULT_OFF")

    contract_ready = (
        readiness.streaming_policy_ok
        and readiness.input_result.decision == "pass"
        and readiness.output_result.decision == "pass"
    )
    return P1SanitizerProductionGateReport(
        excluded_flags=excluded_flags,
        reason_codes=tuple(reason_codes),
        sanitizer_contract_ready=contract_ready,
        offline_eval_passed=offline_eval_passed,
        default_off_rollout=default_off_rollout,
        p1_live_reward_ready=False,
    )

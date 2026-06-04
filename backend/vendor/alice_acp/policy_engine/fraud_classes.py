from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Literal

from alice_acp.policy_engine.tranche import SECONDS_24H, SECONDS_48H, SECONDS_72H

SECONDS_7D = 7 * 24 * 60 * 60

CACHED_ANSWER_REPLAY = "cached_answer_replay"
RUNTIME_INTEGRITY_FRAUD = "runtime_integrity_fraud"
P1_SANITIZER_BYPASS = "P1_sanitizer_bypass"
POOL_SHARE_FRAUD = "pool_share_fraud"
MODEL_SUBSTITUTION_35B_PLUS = "model_substitution_35B_plus"
SPECULATIVE_ROUTE_FRAUD = "speculative_route_fraud"
REQUESTER_MINER_COLLUSION = "requester_miner_collusion"
CLUSTER_SYBIL = "cluster_sybil"
QUALITY_DRIFT = "quality_drift"

R6_GPU_MINING = "R6_gpu_mining"
R7_CPU_XMR = "R7_cpu_xmr"
R2_R3_9B_P0 = "R2_R3_9B_P0"
R2_R3_P1 = "R2_R3_P1"
R3_35B_PLUS = "R3_35B_plus"
R8_COMMUNITY_VALIDATION = "R8_community_validation"

P1_CORRELATION_HIGH_REQUIRES_UNDER_REVIEW = "P1_CORRELATION_HIGH_REQUIRES_UNDER_REVIEW"

EvaluationPhase = Literal["admission", "settlement"]
RuntimeIntegrityStatus = Literal["unknown", "pass", "fail"]

FRAUD_CLASS_WINDOWS = {
    CACHED_ANSWER_REPLAY: SECONDS_24H,
    RUNTIME_INTEGRITY_FRAUD: SECONDS_24H,
    P1_SANITIZER_BYPASS: SECONDS_24H,
    POOL_SHARE_FRAUD: SECONDS_24H,
    MODEL_SUBSTITUTION_35B_PLUS: SECONDS_48H,
    SPECULATIVE_ROUTE_FRAUD: SECONDS_72H,
    REQUESTER_MINER_COLLUSION: SECONDS_7D,
    CLUSTER_SYBIL: SECONDS_7D,
}

ROUTE_FRAUD_CLASS_RULES: dict[str, dict[str, tuple[str, ...]]] = {
    R6_GPU_MINING: {
        "always": (POOL_SHARE_FRAUD, RUNTIME_INTEGRITY_FRAUD),
        "conditional": (CLUSTER_SYBIL,),
    },
    R7_CPU_XMR: {
        "always": (POOL_SHARE_FRAUD, RUNTIME_INTEGRITY_FRAUD),
        "conditional": (CLUSTER_SYBIL,),
    },
    R2_R3_9B_P0: {
        "always": (CACHED_ANSWER_REPLAY, RUNTIME_INTEGRITY_FRAUD),
        "conditional": (),
        "excluded": (QUALITY_DRIFT,),
    },
    R2_R3_P1: {
        "always": (P1_SANITIZER_BYPASS, CACHED_ANSWER_REPLAY, RUNTIME_INTEGRITY_FRAUD),
        "conditional": (REQUESTER_MINER_COLLUSION, CLUSTER_SYBIL),
    },
    R3_35B_PLUS: {
        "always": (
            MODEL_SUBSTITUTION_35B_PLUS,
            SPECULATIVE_ROUTE_FRAUD,
            RUNTIME_INTEGRITY_FRAUD,
            CACHED_ANSWER_REPLAY,
        ),
        "conditional": (REQUESTER_MINER_COLLUSION, CLUSTER_SYBIL),
    },
    R8_COMMUNITY_VALIDATION: {
        "always": (RUNTIME_INTEGRITY_FRAUD,),
        "conditional": (CLUSTER_SYBIL,),
    },
}


@dataclass(frozen=True, slots=True)
class RiskSignalContext:
    route_source_class: str
    evaluation_phase: EvaluationPhase = "settlement"
    mode: str = ""
    model_tier: str = ""
    privacy_class: str = ""
    p1: bool = False
    cluster_status: str = "none"
    requester_miner_correlation_score: Decimal | None = None
    runtime_integrity_status: RuntimeIntegrityStatus = "unknown"
    speculative_route_signal: bool = False
    quality_drift_signal: bool = False


@dataclass(frozen=True, slots=True)
class FraudClassApplicability:
    fraud_class: str
    window_seconds: int
    reason_code: str

    def as_payload(self) -> dict[str, Any]:
        return {
            "fraud_class": self.fraud_class,
            "window_seconds": self.window_seconds,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True, slots=True)
class RiskAssessment:
    applicable_classes: tuple[FraudClassApplicability, ...]
    max_window_seconds: int
    requires_under_review: bool = False
    reason_code: str | None = None

    def applicable_payload(self) -> list[dict[str, Any]]:
        return [item.as_payload() for item in self.applicable_classes]


def fraud_class_window_seconds(fraud_class: str) -> int | None:
    return FRAUD_CLASS_WINDOWS.get(fraud_class)


def assess_admission_risk(context: RiskSignalContext) -> RiskAssessment:
    return assess_risk(
        RiskSignalContext(
            route_source_class=context.route_source_class,
            evaluation_phase="admission",
            mode=context.mode,
            model_tier=context.model_tier,
            privacy_class=context.privacy_class,
            p1=context.p1,
            cluster_status=context.cluster_status,
            requester_miner_correlation_score=context.requester_miner_correlation_score,
            runtime_integrity_status=context.runtime_integrity_status,
            speculative_route_signal=context.speculative_route_signal,
            quality_drift_signal=context.quality_drift_signal,
        )
    )


def assess_settlement_risk(context: RiskSignalContext) -> RiskAssessment:
    return assess_risk(
        RiskSignalContext(
            route_source_class=context.route_source_class,
            evaluation_phase="settlement",
            mode=context.mode,
            model_tier=context.model_tier,
            privacy_class=context.privacy_class,
            p1=context.p1,
            cluster_status=context.cluster_status,
            requester_miner_correlation_score=context.requester_miner_correlation_score,
            runtime_integrity_status=context.runtime_integrity_status,
            speculative_route_signal=context.speculative_route_signal,
            quality_drift_signal=context.quality_drift_signal,
        )
    )


def assess_risk(context: RiskSignalContext) -> RiskAssessment:
    if context.route_source_class not in ROUTE_FRAUD_CLASS_RULES:
        raise ValueError(f"unknown route_source_class {context.route_source_class}")

    if context.route_source_class == R2_R3_P1:
        p1_assessment = _assess_p1_post_quarantine(context)
        if p1_assessment is not None:
            return p1_assessment

    if (
        context.route_source_class == R2_R3_P1
        and context.requester_miner_correlation_score is not None
        and context.requester_miner_correlation_score > Decimal("0.5")
    ):
        return RiskAssessment(
            applicable_classes=(),
            max_window_seconds=0,
            requires_under_review=True,
            reason_code=P1_CORRELATION_HIGH_REQUIRES_UNDER_REVIEW,
        )

    class_reasons = _route_class_reasons(context)
    deduped = _dedupe_runtime_integrity(context, class_reasons)
    applicable = tuple(
        FraudClassApplicability(
            fraud_class=fraud_class,
            window_seconds=FRAUD_CLASS_WINDOWS[fraud_class],
            reason_code=reason,
        )
        for fraud_class, reason in sorted(deduped.items())
        if fraud_class in FRAUD_CLASS_WINDOWS
    )
    max_window = max((item.window_seconds for item in applicable), default=0)
    return RiskAssessment(applicable_classes=applicable, max_window_seconds=max_window)


def _assess_p1_post_quarantine(context: RiskSignalContext) -> RiskAssessment | None:
    score = context.requester_miner_correlation_score
    if score is None or context.evaluation_phase == "admission":
        return None
    if score > Decimal("0.5"):
        return RiskAssessment(
            applicable_classes=(),
            max_window_seconds=0,
            requires_under_review=True,
            reason_code=P1_CORRELATION_HIGH_REQUIRES_UNDER_REVIEW,
        )

    class_reasons = {
        P1_SANITIZER_BYPASS: "p1_post_quarantine_required",
        CACHED_ANSWER_REPLAY: "p1_post_quarantine_required",
    }
    if context.runtime_integrity_status != "pass":
        class_reasons[RUNTIME_INTEGRITY_FRAUD] = "runtime_integrity_not_cleared"
    if score >= Decimal("0.3"):
        class_reasons[REQUESTER_MINER_COLLUSION] = "p1_correlation_0_3_to_0_5"

    applicable = tuple(
        FraudClassApplicability(
            fraud_class=fraud_class,
            window_seconds=FRAUD_CLASS_WINDOWS[fraud_class],
            reason_code=reason,
        )
        for fraud_class, reason in sorted(class_reasons.items())
    )
    return RiskAssessment(
        applicable_classes=applicable,
        max_window_seconds=max(item.window_seconds for item in applicable),
    )


def _route_class_reasons(context: RiskSignalContext) -> dict[str, str]:
    rules = ROUTE_FRAUD_CLASS_RULES[context.route_source_class]
    class_reasons = {
        fraud_class: "route_always_applicable"
        for fraud_class in rules.get("always", ())
        if fraud_class != QUALITY_DRIFT
    }
    if context.evaluation_phase == "admission":
        for fraud_class in rules.get("conditional", ()):
            if _admission_assumes_conditional_class(context, fraud_class):
                class_reasons[fraud_class] = "admission_assumed_conditional"
        return class_reasons

    for fraud_class in rules.get("conditional", ()):
        if fraud_class == CLUSTER_SYBIL and _cluster_requires_sybil_hold(context.cluster_status):
            class_reasons[fraud_class] = "cluster_status_requires_hold"
        if (
            fraud_class == REQUESTER_MINER_COLLUSION
            and context.requester_miner_correlation_score is not None
            and context.requester_miner_correlation_score >= Decimal("0.3")
        ):
            class_reasons[fraud_class] = "requester_miner_correlation_active"

    if context.route_source_class == R3_35B_PLUS and not context.speculative_route_signal:
        class_reasons.pop(SPECULATIVE_ROUTE_FRAUD, None)
    return class_reasons


def _admission_assumes_conditional_class(
    context: RiskSignalContext,
    fraud_class: str,
) -> bool:
    if fraud_class == CLUSTER_SYBIL:
        return _cluster_requires_sybil_hold(context.cluster_status)
    if fraud_class == REQUESTER_MINER_COLLUSION:
        return context.route_source_class == R2_R3_P1
    return False


def _dedupe_runtime_integrity(
    context: RiskSignalContext,
    class_reasons: dict[str, str],
) -> dict[str, str]:
    deduped = dict(class_reasons)
    if context.runtime_integrity_status == "pass":
        deduped.pop(RUNTIME_INTEGRITY_FRAUD, None)
    elif context.runtime_integrity_status == "fail":
        deduped[RUNTIME_INTEGRITY_FRAUD] = "runtime_integrity_failed"
    return deduped


def _cluster_requires_sybil_hold(cluster_status: str) -> bool:
    return cluster_status in {
        "candidate_cluster_high",
        "confirmed_cluster_no_fraud",
        "confirmed_fraud_cluster",
    }

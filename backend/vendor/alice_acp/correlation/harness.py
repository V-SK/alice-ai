from __future__ import annotations

from dataclasses import dataclass

from alice_acp.correlation.types import CorrelationScoreInput, correlation_signal
from alice_acp.policy_engine import (
    R2_R3_P1,
    RiskAssessment,
    RiskSignal,
    assess_settlement_risk,
    risk_signals_to_context,
)


@dataclass(frozen=True, slots=True)
class CorrelationPolicyEvaluation:
    signal: RiskSignal
    assessment: RiskAssessment
    ledger_mutated: bool = False


def evaluate_correlation_for_policy(
    score_input: CorrelationScoreInput,
    *,
    route_source_class: str = R2_R3_P1,
) -> CorrelationPolicyEvaluation:
    signal = correlation_signal(score_input)
    context = risk_signals_to_context(
        route_source_class=route_source_class,
        signals=(signal,),
    )
    return CorrelationPolicyEvaluation(
        signal=signal,
        assessment=assess_settlement_risk(context),
        ledger_mutated=False,
    )

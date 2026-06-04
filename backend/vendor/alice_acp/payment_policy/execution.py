from __future__ import annotations

from dataclasses import dataclass

from alice_acp.payment_policy.r3_gate import evaluate_r3_eligibility, payment_finality_signal
from alice_acp.payment_policy.types import CreditProvenance, R3EligibilityResult
from alice_acp.policy_engine import RiskSignal


@dataclass(frozen=True, slots=True)
class PaymentPolicyExecution:
    eligibility: R3EligibilityResult
    risk_signal: RiskSignal
    reward_amount_mutated: bool = False


def execute_payment_finality_policy(
    provenance: CreditProvenance,
    *,
    other_risk_gates_pass: bool,
) -> PaymentPolicyExecution:
    return PaymentPolicyExecution(
        eligibility=evaluate_r3_eligibility(
            provenance,
            other_risk_gates_pass=other_risk_gates_pass,
        ),
        risk_signal=payment_finality_signal(provenance),
        reward_amount_mutated=False,
    )

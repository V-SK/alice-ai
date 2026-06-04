from __future__ import annotations

from decimal import Decimal

from alice_acp.payment_policy.types import CreditProvenance, R3EligibilityResult
from alice_acp.policy_engine import RiskSignal


def evaluate_r3_eligibility(
    provenance: CreditProvenance,
    *,
    other_risk_gates_pass: bool,
) -> R3EligibilityResult:
    if provenance.chargeback_observed:
        return R3EligibilityResult(
            eligible=False,
            finality_level=provenance.finality_level,
            reason_code="CHARGEBACK_REVOKES_R3_ELIGIBILITY",
            cap_treatment="not_eligible",
            revokes_future_r3=True,
        )
    if provenance.finality_level == "level_1_authorized":
        return R3EligibilityResult(
            eligible=False,
            finality_level=provenance.finality_level,
            reason_code="PAYMENT_NOT_CAPTURED",
            cap_treatment="not_eligible",
        )
    if provenance.finality_level == "level_2_captured":
        return R3EligibilityResult(
            eligible=False,
            finality_level=provenance.finality_level,
            reason_code="PAYMENT_CAPTURED_R2_LIKE_CAP",
            cap_treatment="r2_like_cap",
        )
    if provenance.credit_type == "promotional" and not provenance.policy_approved_promotional_r3:
        return R3EligibilityResult(
            eligible=False,
            finality_level=provenance.finality_level,
            reason_code="PROMOTIONAL_CREDIT_R2_LIKE_CAP",
            cap_treatment="r2_like_cap",
        )
    if not other_risk_gates_pass:
        return R3EligibilityResult(
            eligible=False,
            finality_level=provenance.finality_level,
            reason_code="RISK_GATES_NOT_PASSED",
            cap_treatment="not_eligible",
        )
    return R3EligibilityResult(
        eligible=True,
        finality_level=provenance.finality_level,
        reason_code="R3_PAYMENT_PROVENANCE_ELIGIBLE",
        cap_treatment="r3_eligible",
    )


def payment_finality_signal(provenance: CreditProvenance) -> RiskSignal:
    return RiskSignal(
        signal_id=provenance.provenance_id,
        signal_type="payment_finality",
        producer="alice_acp.payment_policy.r3_gate",
        policy_version=provenance.policy_version,
        observed_at=provenance.observed_at,
        admission_id=provenance.admission_id,
        attempt_id=provenance.attempt_id,
        route_contract_id=provenance.route_contract_id,
        confidence=Decimal("1"),
        severity="high" if provenance.chargeback_observed else "info",
        reason_code="PAYMENT_FINALITY_SIGNAL",
        evidence_refs=provenance.evidence_refs,
        payload={
            "payment_finality_level": provenance.finality_level,
            "credit_type": provenance.credit_type,
            "chargeback_observed": provenance.chargeback_observed,
        },
    )

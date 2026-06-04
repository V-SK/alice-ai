from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from alice_acp.payment_policy.evidence import PaymentEvidenceBindingReport
from alice_acp.test_harness.phase_e_ha_evidence import PhaseEHAEvidenceReport

PHASE_E_REASON_WHY_NOT_LIVE = (
    "no_payout_executor_implementation",
    "no_live_reward_execution_approval",
    "no_production_HA_drill",
    "no_production_P1_sanitizer",
    "no_production_verifier_fleet",
    "no_live_payment_processor",
    "no_external_audit_signoff",
    "no_four_week_shadow_window",
    "evidence_registry_is_local_only",
)


@dataclass(frozen=True, slots=True)
class PhaseEReadinessReport:
    ha_evidence: PhaseEHAEvidenceReport
    payment_evidence: PaymentEvidenceBindingReport
    evidence_reason_codes: tuple[str, ...]
    reason_why_not_live: tuple[str, ...] = PHASE_E_REASON_WHY_NOT_LIVE
    evidence_backed_local_readiness: bool = False
    can_start_live_rewards: bool = False
    can_start_payout_executor: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def build_phase_e_readiness_report(
    *,
    ha_evidence: PhaseEHAEvidenceReport,
    payment_evidence: PaymentEvidenceBindingReport,
) -> PhaseEReadinessReport:
    reason_codes = tuple(
        dict.fromkeys((*ha_evidence.reason_codes, *payment_evidence.reason_codes))
    )
    return PhaseEReadinessReport(
        ha_evidence=ha_evidence,
        payment_evidence=payment_evidence,
        evidence_reason_codes=reason_codes,
        evidence_backed_local_readiness=not reason_codes,
        can_start_live_rewards=False,
        can_start_payout_executor=False,
    )

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from alice_acp.mttd.operations import SeededMTTDOperationalReport
from alice_acp.p1_sanitizer.policy import P1SanitizerProductionGateReport
from alice_acp.payment_policy.processor_contract import PaymentProcessorReadinessReport
from alice_acp.test_harness.phase_d_benchmark import PhaseDBenchmarkGateReport
from alice_acp.test_harness.phase_d_ha_readiness import ABRSHAReadinessReport
from alice_acp.verifier.readiness import VerifierFleetReadinessReport

PHASE_D_REASON_WHY_NOT_LIVE = (
    "no_payout_executor_implementation",
    "no_live_reward_execution_approval",
    "no_production_HA_drill",
    "no_production_P1_sanitizer",
    "no_production_verifier_fleet",
    "no_live_payment_processor",
    "no_external_audit_signoff",
    "no_four_week_shadow_window",
)


@dataclass(frozen=True, slots=True)
class PayoutExecutorDesignGateReport:
    design_doc_exists: bool
    threat_model_defined: bool
    idempotency_plan_defined: bool
    approval_gate_defined: bool
    no_clawback_preserved: bool
    freeze_runbook_defined: bool
    reason_codes: tuple[str, ...]
    payout_executor_ready: bool = False


@dataclass(frozen=True, slots=True)
class MVP3BLiveRewardGateReport:
    four_week_shadow_window_complete: bool
    demand_threshold_met: bool
    supply_threshold_met: bool
    no_unresolved_p0_incident: bool
    rollback_command_tested: bool
    external_review_complete: bool
    reason_codes: tuple[str, ...]
    can_start_live_rewards: bool = False


@dataclass(frozen=True, slots=True)
class PhaseDReadinessReport:
    ha: ABRSHAReadinessReport
    benchmark: PhaseDBenchmarkGateReport
    verifier: VerifierFleetReadinessReport
    seeded_mttd: SeededMTTDOperationalReport
    p1_sanitizer: P1SanitizerProductionGateReport
    payment_processor: PaymentProcessorReadinessReport
    payout_executor: PayoutExecutorDesignGateReport
    mvp3b_gate: MVP3BLiveRewardGateReport
    reason_why_not_live: tuple[str, ...] = PHASE_D_REASON_WHY_NOT_LIVE
    can_start_phase_d_planning: bool = True
    can_start_phase_d_implementation: bool = False
    can_start_live_rewards: bool = False
    can_start_payout_executor: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def evaluate_payout_executor_design_gate(
    *,
    design_doc_exists: bool,
    threat_model_defined: bool,
    idempotency_plan_defined: bool,
    approval_gate_defined: bool,
    no_clawback_preserved: bool,
    freeze_runbook_defined: bool,
) -> PayoutExecutorDesignGateReport:
    reason_codes: list[str] = []
    if not design_doc_exists:
        reason_codes.append("PAYOUT_EXECUTOR_DESIGN_DOC_MISSING")
    if not threat_model_defined:
        reason_codes.append("PAYOUT_EXECUTOR_THREAT_MODEL_MISSING")
    if not idempotency_plan_defined:
        reason_codes.append("PAYOUT_EXECUTOR_IDEMPOTENCY_PLAN_MISSING")
    if not approval_gate_defined:
        reason_codes.append("PAYOUT_EXECUTOR_APPROVAL_GATE_MISSING")
    if not no_clawback_preserved:
        reason_codes.append("NO_CLAWBACK_NOT_PRESERVED")
    if not freeze_runbook_defined:
        reason_codes.append("PAYOUT_FREEZE_RUNBOOK_MISSING")
    return PayoutExecutorDesignGateReport(
        design_doc_exists=design_doc_exists,
        threat_model_defined=threat_model_defined,
        idempotency_plan_defined=idempotency_plan_defined,
        approval_gate_defined=approval_gate_defined,
        no_clawback_preserved=no_clawback_preserved,
        freeze_runbook_defined=freeze_runbook_defined,
        reason_codes=tuple(reason_codes),
        payout_executor_ready=False,
    )


def evaluate_mvp3b_live_reward_gate(
    *,
    four_week_shadow_window_complete: bool,
    demand_threshold_met: bool,
    supply_threshold_met: bool,
    no_unresolved_p0_incident: bool,
    rollback_command_tested: bool,
    external_review_complete: bool,
) -> MVP3BLiveRewardGateReport:
    reason_codes: list[str] = []
    if not four_week_shadow_window_complete:
        reason_codes.append("FOUR_WEEK_SHADOW_WINDOW_INCOMPLETE")
    if not demand_threshold_met:
        reason_codes.append("DEMAND_THRESHOLD_NOT_MET")
    if not supply_threshold_met:
        reason_codes.append("SUPPLY_THRESHOLD_NOT_MET")
    if not no_unresolved_p0_incident:
        reason_codes.append("UNRESOLVED_P0_INCIDENT_PRESENT")
    if not rollback_command_tested:
        reason_codes.append("ROLLBACK_COMMAND_NOT_TESTED")
    if not external_review_complete:
        reason_codes.append("EXTERNAL_REVIEW_NOT_COMPLETE")
    return MVP3BLiveRewardGateReport(
        four_week_shadow_window_complete=four_week_shadow_window_complete,
        demand_threshold_met=demand_threshold_met,
        supply_threshold_met=supply_threshold_met,
        no_unresolved_p0_incident=no_unresolved_p0_incident,
        rollback_command_tested=rollback_command_tested,
        external_review_complete=external_review_complete,
        reason_codes=tuple(reason_codes),
        can_start_live_rewards=False,
    )


def build_phase_d_readiness_report(
    *,
    ha: ABRSHAReadinessReport,
    benchmark: PhaseDBenchmarkGateReport,
    verifier: VerifierFleetReadinessReport,
    seeded_mttd: SeededMTTDOperationalReport,
    p1_sanitizer: P1SanitizerProductionGateReport,
    payment_processor: PaymentProcessorReadinessReport,
    payout_executor: PayoutExecutorDesignGateReport,
    mvp3b_gate: MVP3BLiveRewardGateReport,
) -> PhaseDReadinessReport:
    return PhaseDReadinessReport(
        ha=ha,
        benchmark=benchmark,
        verifier=verifier,
        seeded_mttd=seeded_mttd,
        p1_sanitizer=p1_sanitizer,
        payment_processor=payment_processor,
        payout_executor=payout_executor,
        mvp3b_gate=mvp3b_gate,
        can_start_phase_d_planning=True,
        can_start_phase_d_implementation=False,
        can_start_live_rewards=False,
        can_start_payout_executor=False,
    )

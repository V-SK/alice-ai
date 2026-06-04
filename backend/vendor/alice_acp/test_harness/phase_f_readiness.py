from __future__ import annotations

from dataclasses import asdict, dataclass
from decimal import Decimal
from typing import Any

from alice_acp.test_harness.phase_e_readiness import PhaseEReadinessReport
from alice_acp.test_harness.phase_f_benchmark_evidence import PhaseFBenchmarkEvidenceReport
from alice_acp.test_harness.phase_f_evidence_bridge import PhaseFEvidenceBridgeReport
from alice_acp.test_harness.phase_f_ha_custody import PhaseFHAReadinessReport
from alice_acp.test_harness.shadow_window import PhaseFShadowWindowEvidenceReport

PHASE_F_REASON_WHY_NOT_LIVE = (
    "evidence_custody_is_local_only",
    "no_live_reward_execution_approval",
    "no_payout_executor_implementation",
    "no_production_HA_drill",
    "no_representative_external_benchmark_for_live",
    "no_four_week_production_shadow_window",
    "no_production_verifier_fleet",
    "no_production_P1_sanitizer",
    "no_live_payment_processor",
    "no_external_audit_signoff_for_live",
)


@dataclass(frozen=True, slots=True)
class PhaseFReadinessReport:
    phase_e_report: PhaseEReadinessReport
    ha_custody: PhaseFHAReadinessReport
    benchmark_evidence: PhaseFBenchmarkEvidenceReport
    shadow_window: PhaseFShadowWindowEvidenceReport
    evidence_bridge: PhaseFEvidenceBridgeReport
    reason_codes: tuple[str, ...]
    reason_why_not_live: tuple[str, ...] = PHASE_F_REASON_WHY_NOT_LIVE
    evidence_custody_ready: bool = False
    can_start_live_rewards: bool = False
    can_start_payout_executor: bool = False
    production_ha_ready: bool = False
    production_benchmark_ready: bool = False
    live_payment_processor_ready: bool = False

    def as_dict(self) -> dict[str, Any]:
        return _json_ready(asdict(self))


def build_phase_f_readiness_report(
    *,
    phase_e_report: PhaseEReadinessReport,
    ha_custody: PhaseFHAReadinessReport,
    benchmark_evidence: PhaseFBenchmarkEvidenceReport,
    shadow_window: PhaseFShadowWindowEvidenceReport,
    evidence_bridge: PhaseFEvidenceBridgeReport,
) -> PhaseFReadinessReport:
    reason_codes: list[str] = []

    if not phase_e_report.evidence_backed_local_readiness:
        reason_codes.append("PHASE_E_LOCAL_EVIDENCE_GATE_BLOCKED")
        reason_codes.extend(phase_e_report.evidence_reason_codes)
    if phase_e_report.can_start_live_rewards:
        reason_codes.append("PHASE_E_LIVE_REWARD_READY_NOT_ALLOWED")
    if phase_e_report.can_start_payout_executor:
        reason_codes.append("PHASE_E_PAYOUT_EXECUTOR_READY_NOT_ALLOWED")

    if not ha_custody.ha_custody_ready:
        reason_codes.append("PHASE_F_HA_CUSTODY_GATE_BLOCKED")
        reason_codes.extend(ha_custody.reason_codes)
    if ha_custody.live_reward_ready:
        reason_codes.append("PHASE_F_HA_LIVE_REWARD_READY_NOT_ALLOWED")
    if ha_custody.production_ha_ready:
        reason_codes.append("PHASE_F_PRODUCTION_HA_READY_NOT_ALLOWED")

    if not benchmark_evidence.representative_benchmark_evidence_ready:
        reason_codes.append("PHASE_F_BENCHMARK_EVIDENCE_GATE_BLOCKED")
        reason_codes.extend(benchmark_evidence.reason_codes)
    if benchmark_evidence.live_reward_ready:
        reason_codes.append("PHASE_F_BENCHMARK_LIVE_REWARD_READY_NOT_ALLOWED")
    if benchmark_evidence.production_benchmark_ready:
        reason_codes.append("PHASE_F_PRODUCTION_BENCHMARK_READY_NOT_ALLOWED")

    if not shadow_window.shadow_window_evidence_ready:
        reason_codes.append("PHASE_F_SHADOW_WINDOW_EVIDENCE_GATE_BLOCKED")
        reason_codes.extend(shadow_window.reason_codes)
    if shadow_window.live_reward_ready:
        reason_codes.append("PHASE_F_SHADOW_LIVE_REWARD_READY_NOT_ALLOWED")
    if shadow_window.payout_executor_ready:
        reason_codes.append("PHASE_F_SHADOW_PAYOUT_EXECUTOR_READY_NOT_ALLOWED")

    if not evidence_bridge.evidence_bridge_ready:
        reason_codes.append("PHASE_F_EVIDENCE_BRIDGE_GATE_BLOCKED")
        reason_codes.extend(evidence_bridge.reason_codes)
    if evidence_bridge.live_reward_ready:
        reason_codes.append("PHASE_F_BRIDGE_LIVE_REWARD_READY_NOT_ALLOWED")
    if evidence_bridge.live_payment_processor_ready:
        reason_codes.append("PHASE_F_BRIDGE_LIVE_PAYMENT_PROCESSOR_READY_NOT_ALLOWED")
    if evidence_bridge.payout_executor_ready:
        reason_codes.append("PHASE_F_BRIDGE_PAYOUT_EXECUTOR_READY_NOT_ALLOWED")

    deduped_reason_codes = tuple(dict.fromkeys(reason_codes))
    return PhaseFReadinessReport(
        phase_e_report=phase_e_report,
        ha_custody=ha_custody,
        benchmark_evidence=benchmark_evidence,
        shadow_window=shadow_window,
        evidence_bridge=evidence_bridge,
        reason_codes=deduped_reason_codes,
        evidence_custody_ready=not deduped_reason_codes,
        can_start_live_rewards=False,
        can_start_payout_executor=False,
        production_ha_ready=False,
        production_benchmark_ready=False,
        live_payment_processor_ready=False,
    )


def _json_ready(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, tuple):
        return [_json_ready(item) for item in value]
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    return value

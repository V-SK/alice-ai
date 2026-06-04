from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from alice_acp.test_harness.phase_f_benchmark_evidence import PhaseFBenchmarkEvidenceReport
from alice_acp.test_harness.phase_f_evidence_bridge import PhaseFEvidenceBridgeReport
from alice_acp.test_harness.phase_f_ha_custody import PhaseFHAReadinessReport
from alice_acp.test_harness.phase_g_common import (
    dedupe_reason_codes,
    json_ready_dataclass,
    require_non_empty,
    require_ref,
)
from alice_acp.test_harness.phase_g_external_custody import (
    PhaseGExternalCustodyReviewReport,
)
from alice_acp.test_harness.phase_g_signing_authority import (
    PhaseGSigningAuthorityReviewReport,
)
from alice_acp.test_harness.shadow_window import PhaseFShadowWindowEvidenceReport

PhaseGReviewerVerdict = Literal["pass", "fail"]


@dataclass(frozen=True, slots=True)
class PhaseGProductionHAReviewPacket:
    phase_f_ha_report: PhaseFHAReadinessReport
    external_custody_report: PhaseGExternalCustodyReviewReport
    signing_authority_report: PhaseGSigningAuthorityReviewReport
    drill_execution_approval_ref: str
    operator_runbook_review_ref: str
    rollback_freeze_decision_ref: str
    unresolved_p0_count: int
    post_drill_reviewer_verdict: PhaseGReviewerVerdict

    def __post_init__(self) -> None:
        for field_name in _HA_EVIDENCE_REF_FIELDS:
            _require_ref_when_present(getattr(self, field_name), field_name=field_name)
        if self.unresolved_p0_count < 0:
            raise ValueError("unresolved_p0_count must be non-negative")


@dataclass(frozen=True, slots=True)
class PhaseGProductionHAReviewReport:
    reason_codes: tuple[str, ...]
    external_review_ready: bool = False
    production_ha_ready: bool = False
    live_reward_ready: bool = False
    payout_executor_ready: bool = False

    def as_dict(self) -> dict[str, Any]:
        return json_ready_dataclass(self)


@dataclass(frozen=True, slots=True)
class PhaseGBenchmarkReviewPacket:
    phase_f_benchmark_report: PhaseFBenchmarkEvidenceReport
    external_custody_report: PhaseGExternalCustodyReviewReport
    signing_authority_report: PhaseGSigningAuthorityReviewReport
    hardware_review_ref: str
    postgres_config_review_ref: str
    workload_review_ref: str
    retryable_separation_review_ref: str
    invariant_counter_review_ref: str
    environment_drift_statement_ref: str
    reviewer_approval_ref: str
    environment_drift_detected: bool = False

    def __post_init__(self) -> None:
        for field_name in _BENCHMARK_EVIDENCE_REF_FIELDS:
            _require_ref_when_present(getattr(self, field_name), field_name=field_name)
        _require_approval_when_present(
            self.reviewer_approval_ref,
            field_name="reviewer_approval_ref",
        )


@dataclass(frozen=True, slots=True)
class PhaseGBenchmarkReviewReport:
    reason_codes: tuple[str, ...]
    representative_benchmark_review_ready: bool = False
    production_benchmark_ready: bool = False
    live_reward_ready: bool = False
    payout_executor_ready: bool = False

    def as_dict(self) -> dict[str, Any]:
        return json_ready_dataclass(self)


@dataclass(frozen=True, slots=True)
class PhaseGShadowWindowReviewPacket:
    phase_f_shadow_report: PhaseFShadowWindowEvidenceReport
    external_custody_report: PhaseGExternalCustodyReviewReport
    signing_authority_report: PhaseGSigningAuthorityReviewReport
    incident_triage_review_ref: str
    no_payout_invocation_review_ref: str
    no_paid_mutation_review_ref: str
    demand_supply_review_ref: str
    rollback_freeze_review_ref: str
    reviewer_approval_ref: str

    def __post_init__(self) -> None:
        for field_name in _SHADOW_EVIDENCE_REF_FIELDS:
            _require_ref_when_present(getattr(self, field_name), field_name=field_name)
        _require_approval_when_present(
            self.reviewer_approval_ref,
            field_name="reviewer_approval_ref",
        )


@dataclass(frozen=True, slots=True)
class PhaseGShadowWindowReviewReport:
    reason_codes: tuple[str, ...]
    shadow_window_review_ready: bool = False
    live_reward_ready: bool = False
    payout_executor_ready: bool = False

    def as_dict(self) -> dict[str, Any]:
        return json_ready_dataclass(self)


@dataclass(frozen=True, slots=True)
class PhaseGComponentEvidenceReviewPacket:
    phase_f_bridge_report: PhaseFEvidenceBridgeReport
    external_custody_report: PhaseGExternalCustodyReviewReport
    signing_authority_report: PhaseGSigningAuthorityReviewReport
    verifier_operating_model_review_ref: str
    p1_implementation_plan_review_ref: str
    payment_integration_plan_review_ref: str
    secret_handling_policy_review_ref: str
    rollback_freeze_review_ref: str
    reviewer_approval_ref: str
    raw_secret_material_present: bool = False

    def __post_init__(self) -> None:
        for field_name in _COMPONENT_EVIDENCE_REF_FIELDS:
            _require_ref_when_present(getattr(self, field_name), field_name=field_name)
        _require_approval_when_present(
            self.reviewer_approval_ref,
            field_name="reviewer_approval_ref",
        )


@dataclass(frozen=True, slots=True)
class PhaseGComponentEvidenceReviewReport:
    reason_codes: tuple[str, ...]
    component_evidence_review_ready: bool = False
    live_reward_ready: bool = False
    live_payment_processor_ready: bool = False
    payout_executor_ready: bool = False

    def as_dict(self) -> dict[str, Any]:
        return json_ready_dataclass(self)


_HA_EVIDENCE_REF_FIELDS = (
    "drill_execution_approval_ref",
    "operator_runbook_review_ref",
    "rollback_freeze_decision_ref",
)

_BENCHMARK_EVIDENCE_REF_FIELDS = (
    "hardware_review_ref",
    "postgres_config_review_ref",
    "workload_review_ref",
    "retryable_separation_review_ref",
    "invariant_counter_review_ref",
    "environment_drift_statement_ref",
)

_SHADOW_EVIDENCE_REF_FIELDS = (
    "incident_triage_review_ref",
    "no_payout_invocation_review_ref",
    "no_paid_mutation_review_ref",
    "demand_supply_review_ref",
    "rollback_freeze_review_ref",
)

_COMPONENT_EVIDENCE_REF_FIELDS = (
    "verifier_operating_model_review_ref",
    "p1_implementation_plan_review_ref",
    "payment_integration_plan_review_ref",
    "secret_handling_policy_review_ref",
    "rollback_freeze_review_ref",
)


def evaluate_phase_g_production_ha_review(
    packet: PhaseGProductionHAReviewPacket,
) -> PhaseGProductionHAReviewReport:
    reason_codes: list[str] = []
    if not packet.phase_f_ha_report.ha_custody_ready:
        reason_codes.append("PHASE_G_HA_PHASE_F_CUSTODY_GATE_BLOCKED")
        reason_codes.extend(packet.phase_f_ha_report.reason_codes)
    if packet.phase_f_ha_report.production_ha_ready:
        reason_codes.append("PHASE_G_HA_PHASE_F_PRODUCTION_READY_NOT_ALLOWED")
    _append_dependency_reason_codes(
        packet.external_custody_report,
        packet.signing_authority_report,
        reason_codes,
    )
    _append_missing_ref_reason_codes(packet, _HA_EVIDENCE_REF_FIELDS, "PHASE_G_HA", reason_codes)
    if packet.unresolved_p0_count:
        reason_codes.append("PHASE_G_HA_UNRESOLVED_P0_INCIDENT")
    if packet.post_drill_reviewer_verdict != "pass":
        reason_codes.append("PHASE_G_HA_REVIEWER_VERDICT_FAILED")

    deduped_reason_codes = dedupe_reason_codes(reason_codes)
    return PhaseGProductionHAReviewReport(
        reason_codes=deduped_reason_codes,
        external_review_ready=not deduped_reason_codes,
        production_ha_ready=False,
        live_reward_ready=False,
        payout_executor_ready=False,
    )


def evaluate_phase_g_benchmark_review(
    packet: PhaseGBenchmarkReviewPacket,
) -> PhaseGBenchmarkReviewReport:
    reason_codes: list[str] = []
    if not packet.phase_f_benchmark_report.representative_benchmark_evidence_ready:
        reason_codes.append("PHASE_G_BENCHMARK_PHASE_F_EVIDENCE_GATE_BLOCKED")
        reason_codes.extend(packet.phase_f_benchmark_report.reason_codes)
    if packet.phase_f_benchmark_report.production_benchmark_ready:
        reason_codes.append("PHASE_G_BENCHMARK_PHASE_F_PRODUCTION_READY_NOT_ALLOWED")
    _append_dependency_reason_codes(
        packet.external_custody_report,
        packet.signing_authority_report,
        reason_codes,
    )
    _append_missing_ref_reason_codes(
        packet,
        _BENCHMARK_EVIDENCE_REF_FIELDS,
        "PHASE_G_BENCHMARK",
        reason_codes,
    )
    if not require_non_empty(packet.reviewer_approval_ref, field_name="reviewer_approval_ref"):
        reason_codes.append("PHASE_G_BENCHMARK_REVIEWER_APPROVAL_REF_MISSING")
    if packet.environment_drift_detected:
        reason_codes.append("PHASE_G_BENCHMARK_ENVIRONMENT_DRIFT_DETECTED")

    deduped_reason_codes = dedupe_reason_codes(reason_codes)
    return PhaseGBenchmarkReviewReport(
        reason_codes=deduped_reason_codes,
        representative_benchmark_review_ready=not deduped_reason_codes,
        production_benchmark_ready=False,
        live_reward_ready=False,
        payout_executor_ready=False,
    )


def evaluate_phase_g_shadow_window_review(
    packet: PhaseGShadowWindowReviewPacket,
) -> PhaseGShadowWindowReviewReport:
    reason_codes: list[str] = []
    if not packet.phase_f_shadow_report.shadow_window_evidence_ready:
        reason_codes.append("PHASE_G_SHADOW_PHASE_F_EVIDENCE_GATE_BLOCKED")
        reason_codes.extend(packet.phase_f_shadow_report.reason_codes)
    if packet.phase_f_shadow_report.live_reward_ready:
        reason_codes.append("PHASE_G_SHADOW_PHASE_F_LIVE_READY_NOT_ALLOWED")
    if packet.phase_f_shadow_report.payout_executor_ready:
        reason_codes.append("PHASE_G_SHADOW_PHASE_F_PAYOUT_READY_NOT_ALLOWED")
    _append_dependency_reason_codes(
        packet.external_custody_report,
        packet.signing_authority_report,
        reason_codes,
    )
    _append_missing_ref_reason_codes(
        packet,
        _SHADOW_EVIDENCE_REF_FIELDS,
        "PHASE_G_SHADOW",
        reason_codes,
    )
    if not require_non_empty(packet.reviewer_approval_ref, field_name="reviewer_approval_ref"):
        reason_codes.append("PHASE_G_SHADOW_REVIEWER_APPROVAL_REF_MISSING")

    deduped_reason_codes = dedupe_reason_codes(reason_codes)
    return PhaseGShadowWindowReviewReport(
        reason_codes=deduped_reason_codes,
        shadow_window_review_ready=not deduped_reason_codes,
        live_reward_ready=False,
        payout_executor_ready=False,
    )


def evaluate_phase_g_component_evidence_review(
    packet: PhaseGComponentEvidenceReviewPacket,
) -> PhaseGComponentEvidenceReviewReport:
    reason_codes: list[str] = []
    if not packet.phase_f_bridge_report.evidence_bridge_ready:
        reason_codes.append("PHASE_G_COMPONENT_PHASE_F_BRIDGE_GATE_BLOCKED")
        reason_codes.extend(packet.phase_f_bridge_report.reason_codes)
    if packet.phase_f_bridge_report.live_reward_ready:
        reason_codes.append("PHASE_G_COMPONENT_PHASE_F_LIVE_READY_NOT_ALLOWED")
    if packet.phase_f_bridge_report.live_payment_processor_ready:
        reason_codes.append("PHASE_G_COMPONENT_PHASE_F_PAYMENT_READY_NOT_ALLOWED")
    if packet.phase_f_bridge_report.payout_executor_ready:
        reason_codes.append("PHASE_G_COMPONENT_PHASE_F_PAYOUT_READY_NOT_ALLOWED")
    _append_dependency_reason_codes(
        packet.external_custody_report,
        packet.signing_authority_report,
        reason_codes,
    )
    _append_missing_ref_reason_codes(
        packet,
        _COMPONENT_EVIDENCE_REF_FIELDS,
        "PHASE_G_COMPONENT",
        reason_codes,
    )
    if not require_non_empty(packet.reviewer_approval_ref, field_name="reviewer_approval_ref"):
        reason_codes.append("PHASE_G_COMPONENT_REVIEWER_APPROVAL_REF_MISSING")
    if packet.raw_secret_material_present:
        reason_codes.append("PHASE_G_COMPONENT_RAW_SECRET_MATERIAL_PRESENT")

    deduped_reason_codes = dedupe_reason_codes(reason_codes)
    return PhaseGComponentEvidenceReviewReport(
        reason_codes=deduped_reason_codes,
        component_evidence_review_ready=not deduped_reason_codes,
        live_reward_ready=False,
        live_payment_processor_ready=False,
        payout_executor_ready=False,
    )


def _append_dependency_reason_codes(
    custody_report: PhaseGExternalCustodyReviewReport,
    signing_report: PhaseGSigningAuthorityReviewReport,
    reason_codes: list[str],
) -> None:
    if not custody_report.external_custody_review_ready:
        reason_codes.append("PHASE_G_EXTERNAL_CUSTODY_REVIEW_GATE_BLOCKED")
        reason_codes.extend(custody_report.reason_codes)
    if custody_report.production_storage_authority_ready:
        reason_codes.append("PHASE_G_EXTERNAL_CUSTODY_PRODUCTION_READY_NOT_ALLOWED")
    if not signing_report.signing_authority_review_ready:
        reason_codes.append("PHASE_G_SIGNING_AUTHORITY_REVIEW_GATE_BLOCKED")
        reason_codes.extend(signing_report.reason_codes)
    if signing_report.live_reward_ready:
        reason_codes.append("PHASE_G_SIGNING_LIVE_REWARD_READY_NOT_ALLOWED")
    if signing_report.payout_executor_ready:
        reason_codes.append("PHASE_G_SIGNING_PAYOUT_READY_NOT_ALLOWED")


def _append_missing_ref_reason_codes(
    packet: object,
    field_names: tuple[str, ...],
    prefix: str,
    reason_codes: list[str],
) -> None:
    for field_name in field_names:
        if not require_non_empty(getattr(packet, field_name), field_name=field_name):
            reason_codes.append(f"{prefix}_{field_name.upper()}_MISSING")


def _require_ref_when_present(ref: str, *, field_name: str) -> None:
    if ref.strip():
        require_ref(ref, field_name=field_name, scheme="evidence")


def _require_approval_when_present(ref: str, *, field_name: str) -> None:
    if ref.strip():
        require_ref(ref, field_name=field_name, scheme="approval")

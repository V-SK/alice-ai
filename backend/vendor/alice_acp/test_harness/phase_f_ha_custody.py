from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from alice_acp.evidence import (
    EvidenceCustodyRecord,
    LocalEvidenceRegistry,
    SignedApprovalEnvelope,
    validate_evidence_custody,
    validate_signed_approval_envelope,
)
from alice_acp.evidence.registry import EvidenceRecordNotFoundError
from alice_acp.evidence.types import parse_evidence_ref
from alice_acp.test_harness.phase_d_ha_readiness import ABRSHADrillEvidence
from alice_acp.test_harness.phase_e_ha_evidence import (
    PhaseEHAEvidenceReport,
    validate_ha_drill_evidence,
)


@dataclass(frozen=True, slots=True)
class PhaseFHAEvidencePacket:
    phase_d_evidence: ABRSHADrillEvidence
    drill_runbook_ref: str
    environment_topology_ref: str
    failover_timeline_ref: str
    split_brain_prevention_ref: str
    zero_data_loss_ref: str
    reservation_counter_ref: str
    negative_budget_counter_ref: str
    idempotent_retry_consistency_ref: str
    failover_duration_ref: str
    p99_recovery_latency_ref: str

    def __post_init__(self) -> None:
        for ref in _packet_evidence_refs(self):
            parsed = parse_evidence_ref(ref)
            if parsed.scheme != "evidence":
                raise ValueError("Phase F HA packet refs must use evidence://")


@dataclass(frozen=True, slots=True)
class PhaseFHAReadinessReport:
    phase_e_report: PhaseEHAEvidenceReport
    subject: str
    reason_codes: tuple[str, ...]
    ha_custody_ready: bool = False
    live_reward_ready: bool = False
    production_ha_ready: bool = False


@dataclass(frozen=True, slots=True)
class _RequiredCustodyRef:
    field_name: str
    artifact_type: str
    label: str

    @property
    def missing_code(self) -> str:
        return f"PHASE_F_HA_{self.label}_CUSTODY_MISSING"

    @property
    def invalid_code(self) -> str:
        return f"PHASE_F_HA_{self.label}_CUSTODY_INVALID"

    @property
    def signoff_missing_code(self) -> str:
        return f"PHASE_F_HA_SIGNOFF_MISSING_{self.label}_BINDING"


REQUIRED_HA_CUSTODY_REFS: tuple[_RequiredCustodyRef, ...] = (
    _RequiredCustodyRef("drill_runbook_ref", "ha_drill_runbook", "DRILL_RUNBOOK"),
    _RequiredCustodyRef(
        "environment_topology_ref",
        "ha_environment_topology",
        "ENVIRONMENT_TOPOLOGY",
    ),
    _RequiredCustodyRef("failover_timeline_ref", "ha_failover_timeline", "FAILOVER_TIMELINE"),
    _RequiredCustodyRef(
        "split_brain_prevention_ref",
        "ha_split_brain_prevention_proof",
        "SPLIT_BRAIN_PREVENTION",
    ),
    _RequiredCustodyRef("zero_data_loss_ref", "ha_zero_data_loss_proof", "ZERO_DATA_LOSS"),
    _RequiredCustodyRef(
        "reservation_counter_ref",
        "ha_reservation_counter_proof",
        "RESERVATION_COUNTERS",
    ),
    _RequiredCustodyRef(
        "negative_budget_counter_ref",
        "ha_negative_budget_counter_proof",
        "NEGATIVE_BUDGET_COUNTER",
    ),
    _RequiredCustodyRef(
        "idempotent_retry_consistency_ref",
        "ha_idempotent_retry_consistency",
        "IDEMPOTENT_RETRY_CONSISTENCY",
    ),
    _RequiredCustodyRef(
        "failover_duration_ref",
        "ha_failover_duration_measurement",
        "FAILOVER_DURATION",
    ),
    _RequiredCustodyRef(
        "p99_recovery_latency_ref",
        "ha_p99_recovery_latency_measurement",
        "P99_RECOVERY_LATENCY",
    ),
)


def validate_phase_f_ha_custody_packet(
    packet: PhaseFHAEvidencePacket,
    registry: LocalEvidenceRegistry,
    custody_records: tuple[EvidenceCustodyRecord, ...],
    signed_approval: SignedApprovalEnvelope | None,
    *,
    subject: str,
    now: datetime | None = None,
) -> PhaseFHAReadinessReport:
    phase_e_report = validate_ha_drill_evidence(
        packet.phase_d_evidence,
        registry,
        subject=subject,
        now=now,
    )
    reason_codes: list[str] = list(phase_e_report.reason_codes)
    if not phase_e_report.evidence_backed_ha_ready:
        reason_codes.append("PHASE_E_HA_EVIDENCE_GATE_BLOCKED")

    custody_by_ref = {record.artifact_ref: record for record in custody_records}
    approved_refs = _approved_ref_hashes(signed_approval)

    for required in REQUIRED_HA_CUSTODY_REFS:
        ref = getattr(packet, required.field_name)
        custody = custody_by_ref.get(ref)
        if custody is None:
            reason_codes.append(required.missing_code)
        else:
            custody_report = validate_evidence_custody(
                custody,
                registry,
                subject=subject,
                artifact_type=required.artifact_type,
                now=now,
            )
            if not custody_report.custody_ready:
                reason_codes.append(required.invalid_code)
                reason_codes.extend(custody_report.reason_codes)
        if ref not in approved_refs:
            reason_codes.append(required.signoff_missing_code)
        else:
            _append_hash_mismatch_if_needed(
                registry,
                ref,
                approved_refs[ref],
                reason_codes,
                mismatch_code="PHASE_F_HA_SIGNOFF_HASH_MISMATCH",
            )

    for label, ref in _phase_e_ref_bindings(packet):
        if ref is None:
            continue
        if ref not in approved_refs:
            reason_codes.append(f"PHASE_F_HA_SIGNOFF_MISSING_{label}_BINDING")
        else:
            _append_hash_mismatch_if_needed(
                registry,
                ref,
                approved_refs[ref],
                reason_codes,
                mismatch_code="PHASE_F_HA_SIGNOFF_HASH_MISMATCH",
            )

    if signed_approval is None:
        reason_codes.append("PHASE_F_HA_SIGNED_APPROVAL_MISSING")
    else:
        if signed_approval.approval_ref != packet.phase_d_evidence.external_approval_ref:
            reason_codes.append("PHASE_F_HA_SIGNED_APPROVAL_REF_MISMATCH")
        signoff_report = validate_signed_approval_envelope(
            signed_approval,
            registry,
            subject=subject,
            approval_scope="ha_drill_signoff",
            now=now,
        )
        reason_codes.extend(signoff_report.reason_codes)

    return PhaseFHAReadinessReport(
        phase_e_report=phase_e_report,
        subject=subject,
        reason_codes=tuple(dict.fromkeys(reason_codes)),
        ha_custody_ready=not reason_codes,
        live_reward_ready=False,
        production_ha_ready=False,
    )


def _packet_evidence_refs(packet: PhaseFHAEvidencePacket) -> tuple[str, ...]:
    return tuple(getattr(packet, required.field_name) for required in REQUIRED_HA_CUSTODY_REFS)


def _phase_e_ref_bindings(packet: PhaseFHAEvidencePacket) -> tuple[tuple[str, str | None], ...]:
    return (
        ("DRILL_ARTIFACT", packet.phase_d_evidence.drill_artifact_ref),
        ("DRILL_ENVIRONMENT", packet.phase_d_evidence.drill_environment_ref),
    )


def _approved_ref_hashes(approval: SignedApprovalEnvelope | None) -> dict[str, str]:
    if approval is None:
        return {}
    return dict(
        zip(
            approval.approved_evidence_refs,
            approval.approved_content_sha256,
            strict=True,
        )
    )


def _append_hash_mismatch_if_needed(
    registry: LocalEvidenceRegistry,
    ref: str,
    approved_hash: str,
    reason_codes: list[str],
    *,
    mismatch_code: str,
) -> None:
    try:
        record = registry.require(ref)
    except EvidenceRecordNotFoundError:
        return
    except ValueError:
        return
    if record.content_sha256 != approved_hash:
        reason_codes.append(mismatch_code)

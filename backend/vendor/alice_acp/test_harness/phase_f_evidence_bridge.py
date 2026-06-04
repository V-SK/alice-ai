from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from alice_acp.evidence import (
    EvidenceCustodyRecord,
    LocalEvidenceRegistry,
    validate_evidence_custody,
)
from alice_acp.evidence.types import parse_evidence_ref
from alice_acp.mttd import SeededMTTDOperationalReport
from alice_acp.p1_sanitizer import P1SanitizerProductionGateReport
from alice_acp.payment_policy import PaymentEvidenceBindingReport
from alice_acp.verifier import VerifierFleetReadinessReport


@dataclass(frozen=True, slots=True)
class PhaseFEvidenceBridgePacket:
    verifier_report: VerifierFleetReadinessReport
    seeded_mttd_report: SeededMTTDOperationalReport
    p1_report: P1SanitizerProductionGateReport
    payment_report: PaymentEvidenceBindingReport
    verifier_backlog_metrics_ref: str
    verifier_p95_mttd_metrics_ref: str
    seeded_mttd_operational_ref: str
    p1_offline_eval_ref: str
    p1_streaming_policy_ref: str
    payment_processor_contract_ref: str
    payment_finality_mapping_ref: str
    payment_chargeback_runbook_ref: str
    payment_reconciliation_evidence_ref: str
    payment_secret_reference_metadata_ref: str
    payment_secret_ref: str

    def __post_init__(self) -> None:
        for ref in _packet_evidence_refs(self):
            parsed = parse_evidence_ref(ref)
            if parsed.scheme != "evidence":
                raise ValueError("Phase F bridge evidence refs must use evidence://")
        parsed_secret = parse_evidence_ref(self.payment_secret_ref)
        if parsed_secret.scheme != "secret-ref":
            raise ValueError("payment_secret_ref must use secret-ref://")


@dataclass(frozen=True, slots=True)
class PhaseFEvidenceBridgeReport:
    reason_codes: tuple[str, ...]
    evidence_bridge_ready: bool = False
    live_reward_ready: bool = False
    live_payment_processor_ready: bool = False
    payout_executor_ready: bool = False


@dataclass(frozen=True, slots=True)
class _RequiredBridgeCustodyRef:
    field_name: str
    artifact_type: str
    label: str

    @property
    def missing_code(self) -> str:
        return f"PHASE_F_BRIDGE_{self.label}_CUSTODY_MISSING"

    @property
    def invalid_code(self) -> str:
        return f"PHASE_F_BRIDGE_{self.label}_CUSTODY_INVALID"


REQUIRED_BRIDGE_CUSTODY_REFS: tuple[_RequiredBridgeCustodyRef, ...] = (
    _RequiredBridgeCustodyRef(
        "verifier_backlog_metrics_ref",
        "verifier_backlog_metrics",
        "VERIFIER_BACKLOG",
    ),
    _RequiredBridgeCustodyRef(
        "verifier_p95_mttd_metrics_ref",
        "verifier_p95_mttd_metrics",
        "VERIFIER_P95_MTTD",
    ),
    _RequiredBridgeCustodyRef(
        "seeded_mttd_operational_ref",
        "seeded_mttd_operational_evidence",
        "SEEDED_MTTD",
    ),
    _RequiredBridgeCustodyRef(
        "p1_offline_eval_ref",
        "p1_sanitizer_offline_eval",
        "P1_OFFLINE_EVAL",
    ),
    _RequiredBridgeCustodyRef(
        "p1_streaming_policy_ref",
        "p1_sanitizer_streaming_policy",
        "P1_STREAMING_POLICY",
    ),
    _RequiredBridgeCustodyRef(
        "payment_processor_contract_ref",
        "payment_processor_contract",
        "PAYMENT_PROCESSOR_CONTRACT",
    ),
    _RequiredBridgeCustodyRef(
        "payment_finality_mapping_ref",
        "payment_finality_mapping",
        "PAYMENT_FINALITY_MAPPING",
    ),
    _RequiredBridgeCustodyRef(
        "payment_chargeback_runbook_ref",
        "payment_chargeback_runbook",
        "PAYMENT_CHARGEBACK_RUNBOOK",
    ),
    _RequiredBridgeCustodyRef(
        "payment_reconciliation_evidence_ref",
        "payment_reconciliation_evidence",
        "PAYMENT_RECONCILIATION",
    ),
    _RequiredBridgeCustodyRef(
        "payment_secret_reference_metadata_ref",
        "payment_secret_reference_metadata",
        "PAYMENT_SECRET_REFERENCE_METADATA",
    ),
)


def validate_phase_f_evidence_bridge(
    packet: PhaseFEvidenceBridgePacket,
    registry: LocalEvidenceRegistry,
    custody_records: tuple[EvidenceCustodyRecord, ...],
    *,
    subject: str,
    now: datetime | None = None,
) -> PhaseFEvidenceBridgeReport:
    reason_codes: list[str] = []
    _append_underlying_gate_reason_codes(packet, reason_codes)

    custody_by_ref = {record.artifact_ref: record for record in custody_records}
    for required in REQUIRED_BRIDGE_CUSTODY_REFS:
        ref = getattr(packet, required.field_name)
        custody = custody_by_ref.get(ref)
        if custody is None:
            reason_codes.append(required.missing_code)
            continue
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

    return PhaseFEvidenceBridgeReport(
        reason_codes=tuple(dict.fromkeys(reason_codes)),
        evidence_bridge_ready=not reason_codes,
        live_reward_ready=False,
        live_payment_processor_ready=False,
        payout_executor_ready=False,
    )


def _packet_evidence_refs(packet: PhaseFEvidenceBridgePacket) -> tuple[str, ...]:
    return tuple(getattr(packet, required.field_name) for required in REQUIRED_BRIDGE_CUSTODY_REFS)


def _append_underlying_gate_reason_codes(
    packet: PhaseFEvidenceBridgePacket,
    reason_codes: list[str],
) -> None:
    if not packet.verifier_report.verifier_fleet_ready:
        reason_codes.append("VERIFIER_READINESS_GATE_BLOCKED")
        reason_codes.extend(packet.verifier_report.reason_codes)
    if packet.verifier_report.live_reward_ready:
        reason_codes.append("VERIFIER_LIVE_REWARD_READY_NOT_ALLOWED")

    if packet.seeded_mttd_report.reason_codes:
        reason_codes.append("SEEDED_MTTD_GATE_BLOCKED")
        reason_codes.extend(packet.seeded_mttd_report.reason_codes)
    if packet.seeded_mttd_report.public_miner_bucket_debits:
        reason_codes.append("SEEDED_MTTD_PUBLIC_MINER_BUCKET_DEBIT")
    if packet.seeded_mttd_report.live_enforcement_ready:
        reason_codes.append("SEEDED_MTTD_LIVE_ENFORCEMENT_NOT_ALLOWED")

    if packet.p1_report.reason_codes:
        reason_codes.append("P1_SANITIZER_GATE_BLOCKED")
        reason_codes.extend(packet.p1_report.reason_codes)
    if packet.p1_report.p1_live_reward_ready:
        reason_codes.append("P1_LIVE_REWARD_READY_NOT_ALLOWED")

    if not packet.payment_report.processor_evidence_ready:
        reason_codes.append("PAYMENT_EVIDENCE_GATE_BLOCKED")
        reason_codes.extend(packet.payment_report.reason_codes)
    if packet.payment_report.live_payment_processor_ready:
        reason_codes.append("LIVE_PAYMENT_PROCESSOR_READY_NOT_ALLOWED")
    if packet.payment_report.payout_executor_ready:
        reason_codes.append("PAYOUT_EXECUTOR_READY_NOT_ALLOWED")

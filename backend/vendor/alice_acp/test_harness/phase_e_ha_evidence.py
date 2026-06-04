from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from alice_acp.evidence import (
    ApprovalRecord,
    ArtifactManifest,
    EvidenceRecord,
    EvidenceRecordNotFoundError,
    LocalEvidenceRegistry,
    approval_record_from_manifest,
    validate_external_approval,
    verify_record_manifest,
)
from alice_acp.evidence.types import parse_evidence_ref
from alice_acp.test_harness.phase_d_ha_readiness import (
    ABRSHADrillEvidence,
    ABRSHAReadinessReport,
    evaluate_abrs_ha_drill,
)

HA_DRILL_ARTIFACT_TYPE = "ha_drill_artifact"
HA_DRILL_ENVIRONMENT_TYPE = "ha_drill_environment"
HA_EXTERNAL_APPROVAL_TYPE = "ha_external_approval"


@dataclass(frozen=True, slots=True)
class PhaseEHAEvidenceReport:
    phase_d_report: ABRSHAReadinessReport
    subject: str
    reason_codes: tuple[str, ...]
    evidence_backed_ha_ready: bool = False
    live_reward_ready: bool = False


def validate_ha_drill_evidence(
    evidence: ABRSHADrillEvidence,
    registry: LocalEvidenceRegistry,
    *,
    subject: str,
    now: datetime | None = None,
) -> PhaseEHAEvidenceReport:
    phase_d_report = evaluate_abrs_ha_drill(evidence)
    reason_codes: list[str] = list(phase_d_report.reason_codes)
    if not phase_d_report.production_ha_ready:
        reason_codes.append("PHASE_D_HA_GATE_BLOCKED")

    _require_verified_manifest(
        registry,
        evidence.drill_artifact_ref,
        subject=subject,
        artifact_type=HA_DRILL_ARTIFACT_TYPE,
        missing_code="HA_DRILL_ARTIFACT_NOT_REGISTERED",
        invalid_code="HA_DRILL_ARTIFACT_INVALID",
        expected_scheme="evidence",
        now=now,
        reason_codes=reason_codes,
    )
    _require_verified_manifest(
        registry,
        evidence.drill_environment_ref,
        subject=subject,
        artifact_type=HA_DRILL_ENVIRONMENT_TYPE,
        missing_code="HA_DRILL_ENVIRONMENT_NOT_REGISTERED",
        invalid_code="HA_DRILL_ENVIRONMENT_INVALID",
        expected_scheme="evidence",
        now=now,
        reason_codes=reason_codes,
    )
    approval_manifest = _require_verified_manifest(
        registry,
        evidence.external_approval_ref,
        subject=subject,
        artifact_type=HA_EXTERNAL_APPROVAL_TYPE,
        missing_code="EXTERNAL_HA_APPROVAL_NOT_REGISTERED",
        invalid_code="EXTERNAL_HA_APPROVAL_INVALID",
        expected_scheme="approval",
        now=now,
        reason_codes=reason_codes,
    )
    if approval_manifest is not None:
        approval = _approval_from_manifest(approval_manifest, reason_codes)
        if approval is not None:
            approval_report = validate_external_approval(
                approval,
                registry,
                subject=subject,
                now=now,
            )
            reason_codes.extend(approval_report.reason_codes)
            related_refs = set(approval.approved_evidence_refs)
        else:
            related_refs = set()
        if evidence.drill_artifact_ref not in related_refs:
            reason_codes.append("EXTERNAL_HA_APPROVAL_MISSING_DRILL_ARTIFACT_BINDING")
        if evidence.drill_environment_ref not in related_refs:
            reason_codes.append("EXTERNAL_HA_APPROVAL_MISSING_ENVIRONMENT_BINDING")

    return PhaseEHAEvidenceReport(
        phase_d_report=phase_d_report,
        subject=subject,
        reason_codes=tuple(dict.fromkeys(reason_codes)),
        evidence_backed_ha_ready=not reason_codes,
        live_reward_ready=False,
    )


def _require_verified_manifest(
    registry: LocalEvidenceRegistry,
    ref: str | None,
    *,
    subject: str,
    artifact_type: str,
    missing_code: str,
    invalid_code: str,
    expected_scheme: str,
    now: datetime | None,
    reason_codes: list[str],
) -> ArtifactManifest | None:
    if ref is None:
        reason_codes.append(missing_code)
        return None
    try:
        if parse_evidence_ref(ref).scheme != expected_scheme:
            raise ValueError("evidence ref scheme mismatch")
        record = registry.require(ref, subject=subject, artifact_type=artifact_type, now=now)
    except EvidenceRecordNotFoundError:
        reason_codes.append(missing_code)
        return None
    except ValueError:
        reason_codes.append(invalid_code)
        return None
    return _verify_manifest(record, invalid_code=invalid_code, reason_codes=reason_codes)


def _verify_manifest(
    record: EvidenceRecord,
    *,
    invalid_code: str,
    reason_codes: list[str],
) -> ArtifactManifest | None:
    try:
        return verify_record_manifest(record)
    except ValueError:
        reason_codes.append(invalid_code)
        return None


def _approval_from_manifest(
    manifest: ArtifactManifest,
    reason_codes: list[str],
) -> ApprovalRecord | None:
    try:
        return approval_record_from_manifest(manifest)
    except ValueError:
        reason_codes.append("EXTERNAL_HA_APPROVAL_INVALID")
        return None

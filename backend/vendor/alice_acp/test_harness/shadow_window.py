from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import date, datetime
from decimal import Decimal
from typing import Any

from alice_acp.evidence import (
    EvidenceCustodyRecord,
    LocalEvidenceRegistry,
    SignedApprovalEnvelope,
    validate_evidence_custody,
    validate_signed_approval_envelope,
)
from alice_acp.evidence.registry import EvidenceRecordNotFoundError
from alice_acp.evidence.types import parse_evidence_ref, validate_aware_timestamp

FOUR_WEEK_SHADOW_WINDOW_DAYS = 28


@dataclass(frozen=True, slots=True)
class ShadowWindowDailySummary:
    summary_date: date
    summary_ref: str
    reservation_count: int
    liability_count: int
    tranche_split_count: int
    paid_acu_total: Decimal
    payout_executor_invocations: int
    estimated_base_release_acu: Decimal
    estimated_risk_reserve_acu: Decimal
    unresolved_p0_incidents: int = 0

    def __post_init__(self) -> None:
        parsed = parse_evidence_ref(self.summary_ref)
        if parsed.scheme != "evidence":
            raise ValueError("summary_ref must use evidence://")
        for field_name in (
            "reservation_count",
            "liability_count",
            "tranche_split_count",
            "payout_executor_invocations",
            "unresolved_p0_incidents",
        ):
            if getattr(self, field_name) < 0:
                raise ValueError(f"{field_name} must be non-negative")
        for field_name in (
            "paid_acu_total",
            "estimated_base_release_acu",
            "estimated_risk_reserve_acu",
        ):
            if getattr(self, field_name) < 0:
                raise ValueError(f"{field_name} must be non-negative")


@dataclass(frozen=True, slots=True)
class PhaseFShadowWindowEvidencePacket:
    window_start_at: datetime
    window_end_at: datetime
    daily_summaries: tuple[ShadowWindowDailySummary, ...]
    window_start_ref: str
    window_end_ref: str
    paid_acu_zero_assertion_ref: str
    no_payout_executor_invocation_ref: str
    demand_threshold_ref: str
    supply_threshold_ref: str
    unresolved_p0_incident_count_ref: str
    rollback_freeze_drill_ref: str
    external_review_approval_ref: str
    demand_threshold_met: bool
    supply_threshold_met: bool
    unresolved_p0_incident_count: int
    rollback_freeze_drill_passed: bool

    def __post_init__(self) -> None:
        validate_aware_timestamp("window_start_at", self.window_start_at)
        validate_aware_timestamp("window_end_at", self.window_end_at)
        if self.window_end_at <= self.window_start_at:
            raise ValueError("window_end_at must be after window_start_at")
        if self.unresolved_p0_incident_count < 0:
            raise ValueError("unresolved_p0_incident_count must be non-negative")
        for ref in _packet_static_evidence_refs(self):
            parsed = parse_evidence_ref(ref)
            if parsed.scheme != "evidence":
                raise ValueError("Phase F shadow-window refs must use evidence://")
        parsed_approval = parse_evidence_ref(self.external_review_approval_ref)
        if parsed_approval.scheme != "approval":
            raise ValueError("external_review_approval_ref must use approval://")


@dataclass(frozen=True, slots=True)
class PhaseFShadowWindowEvidenceReport:
    subject: str
    reason_codes: tuple[str, ...]
    estimated_base_release_acu: Decimal
    estimated_risk_reserve_acu: Decimal
    shadow_window_evidence_ready: bool = False
    live_reward_ready: bool = False
    payout_executor_ready: bool = False

    def as_dict(self) -> dict[str, Any]:
        return _json_ready(asdict(self))


@dataclass(frozen=True, slots=True)
class _RequiredShadowCustodyRef:
    field_name: str
    artifact_type: str
    label: str

    @property
    def missing_code(self) -> str:
        return f"PHASE_F_SHADOW_{self.label}_CUSTODY_MISSING"

    @property
    def invalid_code(self) -> str:
        return f"PHASE_F_SHADOW_{self.label}_CUSTODY_INVALID"

    @property
    def signoff_missing_code(self) -> str:
        return f"PHASE_F_SHADOW_SIGNOFF_MISSING_{self.label}_BINDING"


REQUIRED_SHADOW_WINDOW_CUSTODY_REFS: tuple[_RequiredShadowCustodyRef, ...] = (
    _RequiredShadowCustodyRef("window_start_ref", "shadow_window_start", "WINDOW_START"),
    _RequiredShadowCustodyRef("window_end_ref", "shadow_window_end", "WINDOW_END"),
    _RequiredShadowCustodyRef(
        "paid_acu_zero_assertion_ref",
        "shadow_paid_acu_zero_assertion",
        "PAID_ACU_ZERO",
    ),
    _RequiredShadowCustodyRef(
        "no_payout_executor_invocation_ref",
        "shadow_no_payout_executor_invocation",
        "NO_PAYOUT_EXECUTOR",
    ),
    _RequiredShadowCustodyRef("demand_threshold_ref", "shadow_demand_threshold", "DEMAND"),
    _RequiredShadowCustodyRef("supply_threshold_ref", "shadow_supply_threshold", "SUPPLY"),
    _RequiredShadowCustodyRef(
        "unresolved_p0_incident_count_ref",
        "shadow_unresolved_p0_incident_count",
        "UNRESOLVED_P0",
    ),
    _RequiredShadowCustodyRef(
        "rollback_freeze_drill_ref",
        "shadow_rollback_freeze_drill",
        "ROLLBACK_FREEZE",
    ),
)


def validate_phase_f_shadow_window_evidence(
    packet: PhaseFShadowWindowEvidencePacket,
    registry: LocalEvidenceRegistry,
    custody_records: tuple[EvidenceCustodyRecord, ...],
    signed_approval: SignedApprovalEnvelope | None,
    *,
    subject: str,
    now: datetime | None = None,
) -> PhaseFShadowWindowEvidenceReport:
    reason_codes: list[str] = []
    if now is not None:
        validate_aware_timestamp("now", now)
    _append_window_shape_reason_codes(packet, reason_codes)
    _append_shadow_accounting_reason_codes(packet, reason_codes)

    custody_by_ref = {record.artifact_ref: record for record in custody_records}
    approved_refs = _approved_ref_hashes(signed_approval)

    for required in REQUIRED_SHADOW_WINDOW_CUSTODY_REFS:
        ref = getattr(packet, required.field_name)
        _append_custody_reason_codes(
            ref,
            required.artifact_type,
            required.missing_code,
            required.invalid_code,
            registry,
            custody_by_ref,
            reason_codes,
            subject=subject,
            now=now,
        )
        _append_signoff_binding_reason_codes(
            ref,
            required.signoff_missing_code,
            registry,
            approved_refs,
            reason_codes,
        )

    for summary in packet.daily_summaries:
        _append_custody_reason_codes(
            summary.summary_ref,
            "shadow_window_daily_summary",
            "PHASE_F_SHADOW_DAILY_SUMMARY_CUSTODY_MISSING",
            "PHASE_F_SHADOW_DAILY_SUMMARY_CUSTODY_INVALID",
            registry,
            custody_by_ref,
            reason_codes,
            subject=subject,
            now=now,
        )
        _append_signoff_binding_reason_codes(
            summary.summary_ref,
            "PHASE_F_SHADOW_SIGNOFF_MISSING_DAILY_SUMMARY_BINDING",
            registry,
            approved_refs,
            reason_codes,
        )

    if signed_approval is None:
        reason_codes.append("PHASE_F_SHADOW_SIGNED_APPROVAL_MISSING")
    else:
        if signed_approval.approval_ref != packet.external_review_approval_ref:
            reason_codes.append("PHASE_F_SHADOW_SIGNED_APPROVAL_REF_MISMATCH")
        signoff_report = validate_signed_approval_envelope(
            signed_approval,
            registry,
            subject=subject,
            approval_scope="shadow_window_signoff",
            now=now,
        )
        reason_codes.extend(signoff_report.reason_codes)

    return PhaseFShadowWindowEvidenceReport(
        subject=subject,
        reason_codes=tuple(dict.fromkeys(reason_codes)),
        estimated_base_release_acu=sum(
            (summary.estimated_base_release_acu for summary in packet.daily_summaries),
            Decimal("0"),
        ),
        estimated_risk_reserve_acu=sum(
            (summary.estimated_risk_reserve_acu for summary in packet.daily_summaries),
            Decimal("0"),
        ),
        shadow_window_evidence_ready=not reason_codes,
        live_reward_ready=False,
        payout_executor_ready=False,
    )


def _packet_static_evidence_refs(packet: PhaseFShadowWindowEvidencePacket) -> tuple[str, ...]:
    return tuple(
        getattr(packet, required.field_name) for required in REQUIRED_SHADOW_WINDOW_CUSTODY_REFS
    )


def _append_window_shape_reason_codes(
    packet: PhaseFShadowWindowEvidencePacket,
    reason_codes: list[str],
) -> None:
    observed_days = (packet.window_end_at.date() - packet.window_start_at.date()).days
    if observed_days < FOUR_WEEK_SHADOW_WINDOW_DAYS:
        reason_codes.append("SHADOW_WINDOW_LESS_THAN_FOUR_WEEKS")
    if len(packet.daily_summaries) < FOUR_WEEK_SHADOW_WINDOW_DAYS:
        reason_codes.append("SHADOW_WINDOW_DAILY_SUMMARY_MISSING")
    expected_dates = {
        date.fromordinal(packet.window_start_at.date().toordinal() + offset)
        for offset in range(FOUR_WEEK_SHADOW_WINDOW_DAYS)
    }
    observed_dates = {summary.summary_date for summary in packet.daily_summaries}
    if not expected_dates.issubset(observed_dates):
        reason_codes.append("SHADOW_WINDOW_DAILY_SUMMARY_GAP")


def _append_shadow_accounting_reason_codes(
    packet: PhaseFShadowWindowEvidencePacket,
    reason_codes: list[str],
) -> None:
    if any(summary.paid_acu_total != Decimal("0") for summary in packet.daily_summaries):
        reason_codes.append("SHADOW_WINDOW_PAID_ACU_NONZERO")
    if any(summary.payout_executor_invocations for summary in packet.daily_summaries):
        reason_codes.append("SHADOW_WINDOW_PAYOUT_EXECUTOR_INVOKED")
    if packet.unresolved_p0_incident_count:
        reason_codes.append("SHADOW_WINDOW_UNRESOLVED_P0_INCIDENT")
    if any(summary.unresolved_p0_incidents for summary in packet.daily_summaries):
        reason_codes.append("SHADOW_WINDOW_DAILY_UNRESOLVED_P0_INCIDENT")
    if not packet.rollback_freeze_drill_passed:
        reason_codes.append("SHADOW_WINDOW_ROLLBACK_FREEZE_DRILL_MISSING")
    if not packet.demand_threshold_met:
        reason_codes.append("SHADOW_WINDOW_DEMAND_THRESHOLD_MISSING")
    if not packet.supply_threshold_met:
        reason_codes.append("SHADOW_WINDOW_SUPPLY_THRESHOLD_MISSING")


def _append_custody_reason_codes(
    ref: str,
    artifact_type: str,
    missing_code: str,
    invalid_code: str,
    registry: LocalEvidenceRegistry,
    custody_by_ref: dict[str, EvidenceCustodyRecord],
    reason_codes: list[str],
    *,
    subject: str,
    now: datetime | None,
) -> None:
    custody = custody_by_ref.get(ref)
    if custody is None:
        reason_codes.append(missing_code)
        return
    custody_report = validate_evidence_custody(
        custody,
        registry,
        subject=subject,
        artifact_type=artifact_type,
        now=now,
    )
    if not custody_report.custody_ready:
        reason_codes.append(invalid_code)
        reason_codes.extend(custody_report.reason_codes)


def _append_signoff_binding_reason_codes(
    ref: str,
    missing_code: str,
    registry: LocalEvidenceRegistry,
    approved_refs: dict[str, str],
    reason_codes: list[str],
) -> None:
    if ref not in approved_refs:
        reason_codes.append(missing_code)
        return
    try:
        record = registry.require(ref)
    except EvidenceRecordNotFoundError:
        return
    except ValueError:
        return
    if record.content_sha256 != approved_refs[ref]:
        reason_codes.append("PHASE_F_SHADOW_SIGNOFF_HASH_MISMATCH")


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


def _json_ready(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_json_ready(item) for item in value]
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    return value

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from alice_acp.test_harness.phase_g_common import (
    dedupe_reason_codes,
    json_ready_dataclass,
    require_non_empty,
    require_ref,
)

PhaseGProviderKind = Literal[
    "external_archive",
    "external_audit_packet",
    "cold_archive",
    "immutable_log",
]


@dataclass(frozen=True, slots=True)
class PhaseGExternalCustodyProviderProfile:
    provider_kind: PhaseGProviderKind
    provider_identity_ref: str
    custody_policy_ref: str
    retention_policy_ref: str
    immutability_mechanism_ref: str
    redaction_policy_ref: str
    access_review_ref: str
    deletion_freeze_policy_ref: str
    reviewer_approval_ref: str
    production_authority_claimed: bool = False

    def __post_init__(self) -> None:
        for field_name in _EVIDENCE_REF_FIELDS:
            _require_ref_when_present(
                getattr(self, field_name),
                field_name=field_name,
                scheme="evidence",
            )
        _require_ref_when_present(
            self.reviewer_approval_ref,
            field_name="reviewer_approval_ref",
            scheme="approval",
        )


@dataclass(frozen=True, slots=True)
class PhaseGExternalCustodyReviewReport:
    provider_profile: PhaseGExternalCustodyProviderProfile
    reason_codes: tuple[str, ...]
    external_custody_review_ready: bool = False
    live_reward_ready: bool = False
    payout_executor_ready: bool = False
    production_storage_authority_ready: bool = False

    def as_dict(self) -> dict[str, Any]:
        return json_ready_dataclass(self)


_EVIDENCE_REF_FIELDS = (
    "provider_identity_ref",
    "custody_policy_ref",
    "retention_policy_ref",
    "immutability_mechanism_ref",
    "redaction_policy_ref",
    "access_review_ref",
    "deletion_freeze_policy_ref",
)

_MISSING_REF_REASON = {
    "provider_identity_ref": "PHASE_G_CUSTODY_PROVIDER_IDENTITY_REF_MISSING",
    "custody_policy_ref": "PHASE_G_CUSTODY_POLICY_REF_MISSING",
    "retention_policy_ref": "PHASE_G_RETENTION_POLICY_REF_MISSING",
    "immutability_mechanism_ref": "PHASE_G_IMMUTABILITY_MECHANISM_REF_MISSING",
    "redaction_policy_ref": "PHASE_G_REDACTION_POLICY_REF_MISSING",
    "access_review_ref": "PHASE_G_ACCESS_REVIEW_REF_MISSING",
    "deletion_freeze_policy_ref": "PHASE_G_DELETION_FREEZE_POLICY_REF_MISSING",
    "reviewer_approval_ref": "PHASE_G_CUSTODY_REVIEWER_APPROVAL_REF_MISSING",
}


def evaluate_phase_g_external_custody_provider(
    profile: PhaseGExternalCustodyProviderProfile,
) -> PhaseGExternalCustodyReviewReport:
    reason_codes: list[str] = []

    for field_name, reason_code in _MISSING_REF_REASON.items():
        if not require_non_empty(getattr(profile, field_name), field_name=field_name):
            reason_codes.append(reason_code)

    if profile.production_authority_claimed:
        reason_codes.append("PHASE_G_CUSTODY_PRODUCTION_AUTHORITY_NOT_ALLOWED")

    deduped_reason_codes = dedupe_reason_codes(reason_codes)
    return PhaseGExternalCustodyReviewReport(
        provider_profile=profile,
        reason_codes=deduped_reason_codes,
        external_custody_review_ready=not deduped_reason_codes,
        live_reward_ready=False,
        payout_executor_ready=False,
        production_storage_authority_ready=False,
    )


def _require_ref_when_present(ref: str, *, field_name: str, scheme: str) -> None:
    if ref.strip():
        require_ref(ref, field_name=field_name, scheme=scheme)  # type: ignore[arg-type]

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Literal

from alice_acp.test_harness.phase_g_common import (
    dedupe_reason_codes,
    json_ready_dataclass,
    require_non_empty,
    require_ref,
)

PhaseGApprovalScope = Literal[
    "external_audit",
    "ha_drill_review",
    "benchmark_review",
    "shadow_window_review",
    "component_readiness_review",
    "readiness_freeze",
]


@dataclass(frozen=True, slots=True)
class PhaseGSigningAuthorityReview:
    signer_identity_ref: str
    reviewer_identity_ref: str
    subject_owner_identity_ref: str
    reviewer_role: str
    approval_scope: PhaseGApprovalScope
    key_custody_policy_ref: str
    revocation_policy_ref: str
    separation_of_duties_ref: str
    signature_evidence_ref: str
    approval_envelope_ref: str
    live_reward_authority_claimed: bool = False
    payout_authority_claimed: bool = False

    def __post_init__(self) -> None:
        _require_ref_when_present(
            self.signer_identity_ref,
            field_name="signer_identity_ref",
            scheme="approval",
        )
        _require_ref_when_present(
            self.reviewer_identity_ref,
            field_name="reviewer_identity_ref",
            scheme="approval",
        )
        _require_ref_when_present(
            self.subject_owner_identity_ref,
            field_name="subject_owner_identity_ref",
            scheme="approval",
        )
        for field_name in _EVIDENCE_REF_FIELDS:
            _require_ref_when_present(
                getattr(self, field_name),
                field_name=field_name,
                scheme="evidence",
            )
        _require_ref_when_present(
            self.approval_envelope_ref,
            field_name="approval_envelope_ref",
            scheme="approval",
        )


@dataclass(frozen=True, slots=True)
class PhaseGSigningAuthorityReviewReport:
    signing_authority: PhaseGSigningAuthorityReview
    reason_codes: tuple[str, ...]
    signing_authority_review_ready: bool = False
    live_reward_ready: bool = False
    payout_executor_ready: bool = False

    def as_dict(self) -> dict[str, Any]:
        return json_ready_dataclass(self)


_EVIDENCE_REF_FIELDS = (
    "key_custody_policy_ref",
    "revocation_policy_ref",
    "separation_of_duties_ref",
    "signature_evidence_ref",
)

_MISSING_REF_REASON = {
    "signer_identity_ref": "PHASE_G_SIGNER_IDENTITY_REF_MISSING",
    "reviewer_identity_ref": "PHASE_G_REVIEWER_IDENTITY_REF_MISSING",
    "subject_owner_identity_ref": "PHASE_G_SUBJECT_OWNER_IDENTITY_REF_MISSING",
    "key_custody_policy_ref": "PHASE_G_KEY_CUSTODY_POLICY_REF_MISSING",
    "revocation_policy_ref": "PHASE_G_REVOCATION_POLICY_REF_MISSING",
    "separation_of_duties_ref": "PHASE_G_SEPARATION_OF_DUTIES_REF_MISSING",
    "signature_evidence_ref": "PHASE_G_SIGNATURE_EVIDENCE_REF_MISSING",
    "approval_envelope_ref": "PHASE_G_APPROVAL_ENVELOPE_REF_MISSING",
}


def evaluate_phase_g_signing_authority(
    review: PhaseGSigningAuthorityReview,
) -> PhaseGSigningAuthorityReviewReport:
    reason_codes: list[str] = []

    for field_name, reason_code in _MISSING_REF_REASON.items():
        if not require_non_empty(getattr(review, field_name), field_name=field_name):
            reason_codes.append(reason_code)

    if (
        review.approval_scope == "external_audit"
        and review.reviewer_identity_ref == review.subject_owner_identity_ref
    ):
        reason_codes.append("PHASE_G_REVIEWER_INDEPENDENCE_NOT_PROVEN")
    if review.live_reward_authority_claimed:
        reason_codes.append("PHASE_G_SIGNING_LIVE_REWARD_AUTHORITY_NOT_ALLOWED")
    if review.payout_authority_claimed:
        reason_codes.append("PHASE_G_SIGNING_PAYOUT_AUTHORITY_NOT_ALLOWED")

    deduped_reason_codes = dedupe_reason_codes(reason_codes)
    return PhaseGSigningAuthorityReviewReport(
        signing_authority=review,
        reason_codes=deduped_reason_codes,
        signing_authority_review_ready=not deduped_reason_codes,
        live_reward_ready=False,
        payout_executor_ready=False,
    )


def _require_ref_when_present(ref: str, *, field_name: str, scheme: str) -> None:
    if ref.strip():
        require_ref(ref, field_name=field_name, scheme=scheme)  # type: ignore[arg-type]

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from alice_acp.audit import canonical_json
from alice_acp.evidence.registry import EvidenceRecordNotFoundError, LocalEvidenceRegistry
from alice_acp.evidence.types import (
    ensure_no_production_alice_reference,
    ensure_no_raw_secret,
    parse_evidence_ref,
    validate_aware_timestamp,
    validate_sha256,
)

ApprovalScope = Literal[
    "owner_approval",
    "external_audit_signoff",
    "ha_drill_signoff",
    "benchmark_signoff",
    "shadow_window_signoff",
]

SUPPORTED_APPROVAL_SCOPES: frozenset[str] = frozenset(
    {
        "owner_approval",
        "external_audit_signoff",
        "ha_drill_signoff",
        "benchmark_signoff",
        "shadow_window_signoff",
    }
)


@dataclass(frozen=True, slots=True)
class SignedApprovalEnvelope:
    approval_ref: str
    approval_scope: ApprovalScope
    subject: str
    issuer: str
    signer_identity_ref: str
    approved_evidence_refs: tuple[str, ...]
    approved_content_sha256: tuple[str, ...]
    signature_artifact_ref: str | None
    approved_at: datetime
    expires_at: datetime | None = None
    approval_decision: str | bool = "approved"
    live_reward_authority_claimed: bool = False
    payout_authority_claimed: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.approval_decision, bool):
            raise ValueError("signed approval envelope must not be boolean-only")
        if self.approval_decision != "approved":
            raise ValueError("approval_decision must be approved")
        parsed_approval = parse_evidence_ref(self.approval_ref)
        if parsed_approval.scheme != "approval":
            raise ValueError("approval_ref must use approval://")
        if self.approval_scope not in SUPPORTED_APPROVAL_SCOPES:
            raise ValueError("approval_scope is unsupported")
        required = (
            self.subject,
            self.issuer,
            self.signer_identity_ref,
        )
        if any(not value or not value.strip() for value in required):
            raise ValueError("signed approval envelope fields must be non-empty")
        signer_ref = parse_evidence_ref(self.signer_identity_ref)
        if signer_ref.scheme == "evidence":
            raise ValueError("signer_identity_ref must not use evidence://")
        if self.signature_artifact_ref is not None:
            signature_ref = parse_evidence_ref(self.signature_artifact_ref)
            if signature_ref.scheme != "evidence":
                raise ValueError("signature_artifact_ref must use evidence://")
        validate_aware_timestamp("approved_at", self.approved_at)
        if self.expires_at is not None:
            validate_aware_timestamp("expires_at", self.expires_at)
            if self.expires_at <= self.approved_at:
                raise ValueError("expires_at must be after approved_at")
        if not self.approved_evidence_refs:
            raise ValueError("signed approval envelope must bind evidence refs")
        if len(self.approved_evidence_refs) != len(self.approved_content_sha256):
            raise ValueError("approved evidence refs and hashes must align")
        for ref in self.approved_evidence_refs:
            parsed = parse_evidence_ref(ref)
            if parsed.scheme != "evidence":
                raise ValueError("approved_evidence_refs must use evidence://")
        for digest in self.approved_content_sha256:
            validate_sha256(digest, field_name="approved_content_sha256")
        if self.live_reward_authority_claimed or self.payout_authority_claimed:
            raise ValueError("local signed approval envelope must not claim production authority")
        for value in (
            self.approval_ref,
            self.approval_scope,
            self.subject,
            self.issuer,
            self.signer_identity_ref,
            self.signature_artifact_ref or "",
            self.approval_decision,
            *self.approved_evidence_refs,
            *self.approved_content_sha256,
        ):
            ensure_no_raw_secret(value)
            ensure_no_production_alice_reference(value)

    @property
    def fingerprint(self) -> str:
        return canonical_json(
            {
                "approval_ref": self.approval_ref,
                "approval_scope": self.approval_scope,
                "subject": self.subject,
                "issuer": self.issuer,
                "signer_identity_ref": self.signer_identity_ref,
                "approved_evidence_refs": self.approved_evidence_refs,
                "approved_content_sha256": self.approved_content_sha256,
                "signature_artifact_ref": self.signature_artifact_ref,
                "approved_at": self.approved_at.isoformat(),
                "expires_at": self.expires_at.isoformat() if self.expires_at else None,
                "approval_decision": self.approval_decision,
                "live_reward_authority_claimed": self.live_reward_authority_claimed,
                "payout_authority_claimed": self.payout_authority_claimed,
            }
        )


@dataclass(frozen=True, slots=True)
class SignedApprovalValidationReport:
    approval_ref: str
    reason_codes: tuple[str, ...]
    approval_ready: bool = False
    live_reward_ready: bool = False
    payout_ready: bool = False


def validate_signed_approval_envelope(
    envelope: SignedApprovalEnvelope,
    registry: LocalEvidenceRegistry,
    *,
    subject: str,
    approval_scope: str | None = None,
    now: datetime | None = None,
) -> SignedApprovalValidationReport:
    reason_codes: list[str] = []
    if envelope.subject != subject:
        reason_codes.append("SIGNOFF_SUBJECT_MISMATCH")
    if approval_scope is not None and envelope.approval_scope != approval_scope:
        reason_codes.append("SIGNOFF_SCOPE_MISMATCH")
    if envelope.signature_artifact_ref is None:
        reason_codes.append("SIGNOFF_SIGNATURE_ARTIFACT_MISSING")
    if now is not None:
        validate_aware_timestamp("now", now)
        if envelope.expires_at is not None and envelope.expires_at <= now:
            reason_codes.append("SIGNOFF_EXPIRED")

    for ref, digest in zip(
        envelope.approved_evidence_refs,
        envelope.approved_content_sha256,
        strict=True,
    ):
        try:
            record = registry.require(ref, subject=subject, now=now)
        except EvidenceRecordNotFoundError:
            reason_codes.append("SIGNOFF_EVIDENCE_RECORD_NOT_FOUND")
            continue
        except ValueError:
            reason_codes.append("SIGNOFF_EVIDENCE_RECORD_INVALID")
            continue
        if record.content_sha256 != digest:
            reason_codes.append("SIGNOFF_EVIDENCE_HASH_MISMATCH")

    if envelope.signature_artifact_ref is not None:
        try:
            registry.require(
                envelope.signature_artifact_ref,
                subject=subject,
                artifact_type="signature_artifact",
                now=now,
            )
        except EvidenceRecordNotFoundError:
            reason_codes.append("SIGNOFF_SIGNATURE_ARTIFACT_NOT_FOUND")
        except ValueError:
            reason_codes.append("SIGNOFF_SIGNATURE_ARTIFACT_INVALID")

    return SignedApprovalValidationReport(
        approval_ref=envelope.approval_ref,
        reason_codes=tuple(dict.fromkeys(reason_codes)),
        approval_ready=not reason_codes,
        live_reward_ready=False,
        payout_ready=False,
    )

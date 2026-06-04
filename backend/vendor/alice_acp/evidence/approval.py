from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime

from alice_acp.evidence.artifacts import ArtifactManifest
from alice_acp.evidence.registry import LocalEvidenceRegistry
from alice_acp.evidence.types import (
    ensure_no_raw_secret,
    parse_evidence_ref,
    validate_aware_timestamp,
    validate_sha256,
)


@dataclass(frozen=True, slots=True)
class ApprovalRecord:
    approval_ref: str
    subject: str
    issuer: str
    approved_at: datetime
    approved_evidence_refs: tuple[str, ...]
    approved_content_sha256: tuple[str, ...]
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        parsed = parse_evidence_ref(self.approval_ref)
        if parsed.scheme != "approval":
            raise ValueError("approval_ref must use approval://")
        if not self.subject or not self.issuer:
            raise ValueError("approval subject and issuer must be non-empty")
        validate_aware_timestamp("approved_at", self.approved_at)
        if self.expires_at is not None:
            validate_aware_timestamp("expires_at", self.expires_at)
            if self.expires_at <= self.approved_at:
                raise ValueError("expires_at must be after approved_at")
        if not self.approved_evidence_refs:
            raise ValueError("approval must bind at least one evidence ref")
        if len(self.approved_evidence_refs) != len(self.approved_content_sha256):
            raise ValueError("approval evidence refs and hashes must align")
        for ref in self.approved_evidence_refs:
            parse_evidence_ref(ref)
        for digest in self.approved_content_sha256:
            validate_sha256(digest, field_name="approved_content_sha256")
        for value in (
            self.approval_ref,
            self.subject,
            self.issuer,
            *self.approved_evidence_refs,
        ):
            ensure_no_raw_secret(value)


@dataclass(frozen=True, slots=True)
class ApprovalValidationReport:
    approval_ref: str
    reason_codes: tuple[str, ...]
    approval_ready: bool


def validate_external_approval(
    approval: ApprovalRecord,
    registry: LocalEvidenceRegistry,
    *,
    subject: str,
    now: datetime | None = None,
) -> ApprovalValidationReport:
    reason_codes: list[str] = []
    if approval.subject != subject:
        reason_codes.append("APPROVAL_SUBJECT_MISMATCH")
    if now is not None:
        validate_aware_timestamp("now", now)
        if approval.expires_at is not None and approval.expires_at <= now:
            reason_codes.append("APPROVAL_EXPIRED")

    for ref, digest in zip(
        approval.approved_evidence_refs,
        approval.approved_content_sha256,
        strict=True,
    ):
        try:
            record = registry.require(ref, subject=subject, now=now)
        except ValueError:
            reason_codes.append("APPROVAL_EVIDENCE_REF_INVALID")
            continue
        if record.content_sha256 != digest:
            reason_codes.append("APPROVAL_EVIDENCE_HASH_MISMATCH")

    return ApprovalValidationReport(
        approval_ref=approval.approval_ref,
        reason_codes=tuple(reason_codes),
        approval_ready=not reason_codes,
    )


def approval_record_from_manifest(manifest: ArtifactManifest) -> ApprovalRecord:
    if parse_evidence_ref(manifest.ref).scheme != "approval":
        raise ValueError("approval manifest ref must use approval://")
    manifest.verify_hash()
    payload = json.loads(manifest.content)
    if not isinstance(payload, dict):
        raise ValueError("approval manifest content must be a JSON object")

    approval = ApprovalRecord(
        approval_ref=str(payload.get("approval_ref", manifest.ref)),
        subject=str(payload.get("subject", manifest.subject)),
        issuer=str(payload.get("issuer", manifest.issuer)),
        approved_at=datetime.fromisoformat(str(payload["approved_at"])),
        approved_evidence_refs=tuple(str(ref) for ref in payload["approved_evidence_refs"]),
        approved_content_sha256=tuple(str(digest) for digest in payload["approved_content_sha256"]),
        expires_at=(
            datetime.fromisoformat(str(payload["expires_at"]))
            if payload.get("expires_at") is not None
            else None
        ),
    )
    if approval.approval_ref != manifest.ref:
        raise ValueError("approval manifest ref mismatch")
    if approval.subject != manifest.subject:
        raise ValueError("approval manifest subject mismatch")
    if approval.issuer != manifest.issuer:
        raise ValueError("approval manifest issuer mismatch")
    if tuple(approval.approved_evidence_refs) != manifest.related_refs:
        raise ValueError("approval manifest related refs mismatch")
    return approval

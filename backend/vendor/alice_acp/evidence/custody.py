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

CustodyStorageClass = Literal[
    "local_fixture",
    "external_archive",
    "external_audit_packet",
    "cold_archive",
    "immutable_log",
]

SUPPORTED_CUSTODY_STORAGE_CLASSES: frozenset[str] = frozenset(
    {
        "local_fixture",
        "external_archive",
        "external_audit_packet",
        "cold_archive",
        "immutable_log",
    }
)


@dataclass(frozen=True, slots=True)
class EvidenceCustodyRecord:
    artifact_ref: str
    artifact_type: str
    subject: str
    issuer: str
    content_sha256: str
    storage_class: CustodyStorageClass
    storage_location_hint: str
    immutability_proof_ref: str | None
    retention_policy: str
    captured_at: datetime
    redaction_status: str
    expires_at: datetime | None = None
    production_authority_claimed: bool = False

    def __post_init__(self) -> None:
        parsed_artifact = parse_evidence_ref(self.artifact_ref)
        if parsed_artifact.scheme != "evidence":
            raise ValueError("artifact_ref must use evidence://")
        if self.immutability_proof_ref is not None:
            parsed_proof = parse_evidence_ref(self.immutability_proof_ref)
            if parsed_proof.scheme not in {"evidence", "approval"}:
                raise ValueError("immutability_proof_ref must use evidence:// or approval://")
        if self.storage_class not in SUPPORTED_CUSTODY_STORAGE_CLASSES:
            raise ValueError("storage_class is unsupported")
        required = (
            self.artifact_type,
            self.subject,
            self.issuer,
            self.storage_location_hint,
            self.retention_policy,
            self.redaction_status,
        )
        if any(not value or not value.strip() for value in required):
            raise ValueError("custody record fields must be non-empty")
        validate_sha256(self.content_sha256)
        validate_aware_timestamp("captured_at", self.captured_at)
        if self.expires_at is not None:
            validate_aware_timestamp("expires_at", self.expires_at)
            if self.expires_at <= self.captured_at:
                raise ValueError("expires_at must be after captured_at")
        if self.production_authority_claimed:
            raise ValueError("local custody record must not claim production authority")
        _ensure_not_alice_live_path(self.storage_location_hint)
        for value in (
            self.artifact_ref,
            self.artifact_type,
            self.subject,
            self.issuer,
            self.content_sha256,
            self.storage_class,
            self.storage_location_hint,
            self.immutability_proof_ref or "",
            self.retention_policy,
            self.redaction_status,
        ):
            ensure_no_raw_secret(value)
            ensure_no_production_alice_reference(value)

    @property
    def fingerprint(self) -> str:
        return canonical_json(
            {
                "artifact_ref": self.artifact_ref,
                "artifact_type": self.artifact_type,
                "subject": self.subject,
                "issuer": self.issuer,
                "content_sha256": self.content_sha256,
                "storage_class": self.storage_class,
                "storage_location_hint": self.storage_location_hint,
                "immutability_proof_ref": self.immutability_proof_ref,
                "retention_policy": self.retention_policy,
                "captured_at": self.captured_at.isoformat(),
                "redaction_status": self.redaction_status,
                "expires_at": self.expires_at.isoformat() if self.expires_at else None,
                "production_authority_claimed": self.production_authority_claimed,
            }
        )


@dataclass(frozen=True, slots=True)
class CustodyValidationReport:
    artifact_ref: str
    reason_codes: tuple[str, ...]
    custody_ready: bool = False
    production_authority_ready: bool = False


def validate_evidence_custody(
    custody: EvidenceCustodyRecord,
    registry: LocalEvidenceRegistry,
    *,
    subject: str | None = None,
    artifact_type: str | None = None,
    now: datetime | None = None,
) -> CustodyValidationReport:
    reason_codes: list[str] = []
    if custody.immutability_proof_ref is None:
        reason_codes.append("CUSTODY_IMMUTABILITY_PROOF_MISSING")
    if now is not None:
        validate_aware_timestamp("now", now)
        if custody.expires_at is not None and custody.expires_at <= now:
            reason_codes.append("CUSTODY_EXPIRED")
    if subject is not None and custody.subject != subject:
        reason_codes.append("CUSTODY_SUBJECT_MISMATCH")
    if artifact_type is not None and custody.artifact_type != artifact_type:
        reason_codes.append("CUSTODY_ARTIFACT_TYPE_MISMATCH")

    try:
        record = registry.require(
            custody.artifact_ref,
            subject=subject or custody.subject,
            artifact_type=artifact_type or custody.artifact_type,
            now=now,
        )
    except EvidenceRecordNotFoundError:
        reason_codes.append("CUSTODY_EVIDENCE_RECORD_NOT_FOUND")
    except ValueError:
        reason_codes.append("CUSTODY_EVIDENCE_RECORD_INVALID")
    else:
        if record.content_sha256 != custody.content_sha256:
            reason_codes.append("CUSTODY_CONTENT_HASH_MISMATCH")
        if record.subject != custody.subject:
            reason_codes.append("CUSTODY_RECORD_SUBJECT_MISMATCH")
        if record.artifact_type != custody.artifact_type:
            reason_codes.append("CUSTODY_RECORD_ARTIFACT_TYPE_MISMATCH")

    return CustodyValidationReport(
        artifact_ref=custody.artifact_ref,
        reason_codes=tuple(dict.fromkeys(reason_codes)),
        custody_ready=not reason_codes,
        production_authority_ready=False,
    )


def _ensure_not_alice_live_path(value: str) -> None:
    parts = value.replace("\\", "/").split("/")
    if "alice_live" in parts:
        raise ValueError("custody location must not reference alice_live")

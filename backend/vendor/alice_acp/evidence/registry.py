from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from alice_acp.audit import canonical_json
from alice_acp.evidence.types import (
    ensure_no_production_alice_reference,
    ensure_no_raw_secret,
    parse_evidence_ref,
    validate_aware_timestamp,
    validate_sha256,
)


class EvidenceRegistryError(ValueError):
    reason_code = "EVIDENCE_REGISTRY_ERROR"


class EvidenceRecordMismatchError(EvidenceRegistryError):
    reason_code = "EVIDENCE_RECORD_PAYLOAD_MISMATCH"

    def __init__(self, ref: str) -> None:
        super().__init__(f"evidence record {ref} already exists with different payload")
        self.ref = ref


class EvidenceRecordNotFoundError(EvidenceRegistryError):
    reason_code = "EVIDENCE_RECORD_NOT_FOUND"

    def __init__(self, ref: str) -> None:
        super().__init__(f"evidence record {ref} was not found")
        self.ref = ref


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    ref: str
    artifact_type: str
    subject: str
    issuer: str
    created_at: datetime
    content_sha256: str
    source_kind: str
    storage_location_hint: str
    redaction_status: str
    expires_at: datetime | None = None
    related_refs: tuple[str, ...] = ()
    metadata: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        parse_evidence_ref(self.ref)
        required = (
            self.artifact_type,
            self.subject,
            self.issuer,
            self.source_kind,
            self.storage_location_hint,
            self.redaction_status,
        )
        if any(not value or not value.strip() for value in required):
            raise ValueError("evidence record fields must be non-empty")
        validate_aware_timestamp("created_at", self.created_at)
        if self.expires_at is not None:
            validate_aware_timestamp("expires_at", self.expires_at)
            if self.expires_at <= self.created_at:
                raise ValueError("expires_at must be after created_at")
        validate_sha256(self.content_sha256)
        for related_ref in self.related_refs:
            parse_evidence_ref(related_ref)
        for value in (
            self.ref,
            self.artifact_type,
            self.subject,
            self.issuer,
            self.source_kind,
            self.storage_location_hint,
            self.redaction_status,
            *self.related_refs,
            *self.metadata.values(),
        ):
            ensure_no_raw_secret(value)
            ensure_no_production_alice_reference(value)

    @property
    def fingerprint(self) -> str:
        return canonical_json(
            {
                "ref": self.ref,
                "artifact_type": self.artifact_type,
                "subject": self.subject,
                "issuer": self.issuer,
                "created_at": self.created_at.isoformat(),
                "content_sha256": self.content_sha256,
                "source_kind": self.source_kind,
                "storage_location_hint": self.storage_location_hint,
                "redaction_status": self.redaction_status,
                "expires_at": self.expires_at.isoformat() if self.expires_at else None,
                "related_refs": list(self.related_refs),
                "metadata": self.metadata,
            }
        )


class LocalEvidenceRegistry:
    def __init__(self, records: tuple[EvidenceRecord, ...] = ()) -> None:
        self._records: dict[str, EvidenceRecord] = {}
        self._append_order: list[str] = []
        for record in records:
            self.append(record)

    def append(self, record: EvidenceRecord) -> bool:
        existing = self._records.get(record.ref)
        if existing is None:
            self._records[record.ref] = record
            self._append_order.append(record.ref)
            return True
        if existing.fingerprint == record.fingerprint:
            return False
        raise EvidenceRecordMismatchError(record.ref)

    def get(self, ref: str) -> EvidenceRecord | None:
        parse_evidence_ref(ref)
        return self._records.get(ref)

    def require(
        self,
        ref: str,
        *,
        subject: str | None = None,
        artifact_type: str | None = None,
        now: datetime | None = None,
    ) -> EvidenceRecord:
        record = self.get(ref)
        if record is None:
            raise EvidenceRecordNotFoundError(ref)
        if subject is not None and record.subject != subject:
            raise ValueError("evidence record subject mismatch")
        if artifact_type is not None and record.artifact_type != artifact_type:
            raise ValueError("evidence record artifact type mismatch")
        if now is not None:
            validate_aware_timestamp("now", now)
            if record.expires_at is not None and record.expires_at <= now:
                raise ValueError("evidence record is expired")
        return record

    @property
    def records(self) -> tuple[EvidenceRecord, ...]:
        return tuple(self._records[ref] for ref in self._append_order)

    def __len__(self) -> int:
        return len(self._records)

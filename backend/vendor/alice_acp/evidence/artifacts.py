from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from alice_acp.evidence.registry import EvidenceRecord
from alice_acp.evidence.types import (
    ensure_no_production_alice_reference,
    ensure_no_raw_secret,
    parse_evidence_ref,
    validate_aware_timestamp,
    validate_sha256,
)


@dataclass(frozen=True, slots=True)
class ArtifactManifest:
    ref: str
    artifact_type: str
    subject: str
    issuer: str
    created_at: datetime
    content: str
    content_sha256: str
    source_kind: str
    storage_location_hint: str
    redaction_status: str
    expires_at: datetime | None = None
    related_refs: tuple[str, ...] = ()
    metadata: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        parse_evidence_ref(self.ref)
        validate_aware_timestamp("created_at", self.created_at)
        if self.expires_at is not None:
            validate_aware_timestamp("expires_at", self.expires_at)
        validate_sha256(self.content_sha256)
        if not self.redaction_status:
            raise ValueError("redaction_status must be non-empty")
        ensure_no_raw_secret(self.content, field_name="artifact content")
        ensure_no_raw_secret(self.storage_location_hint, field_name="storage_location_hint")
        ensure_no_production_alice_reference(self.content, field_name="artifact content")
        ensure_no_production_alice_reference(
            self.storage_location_hint,
            field_name="storage_location_hint",
        )

    @property
    def computed_sha256(self) -> str:
        return hashlib.sha256(self.content.encode("utf-8")).hexdigest()

    def verify_hash(self) -> None:
        if self.computed_sha256 != self.content_sha256:
            raise ValueError("artifact content_sha256 mismatch")

    def to_record(self) -> EvidenceRecord:
        self.verify_hash()
        return EvidenceRecord(
            ref=self.ref,
            artifact_type=self.artifact_type,
            subject=self.subject,
            issuer=self.issuer,
            created_at=self.created_at,
            content_sha256=self.content_sha256,
            source_kind=self.source_kind,
            storage_location_hint=self.storage_location_hint,
            redaction_status=self.redaction_status,
            expires_at=self.expires_at,
            related_refs=self.related_refs,
            metadata=self.metadata,
        )


def artifact_manifest_from_mapping(payload: dict[str, Any]) -> ArtifactManifest:
    return ArtifactManifest(
        ref=str(payload["ref"]),
        artifact_type=str(payload["artifact_type"]),
        subject=str(payload["subject"]),
        issuer=str(payload["issuer"]),
        created_at=datetime.fromisoformat(str(payload["created_at"])),
        content=str(payload["content"]),
        content_sha256=str(payload["content_sha256"]),
        source_kind=str(payload["source_kind"]),
        storage_location_hint=str(payload["storage_location_hint"]),
        redaction_status=str(payload["redaction_status"]),
        expires_at=(
            datetime.fromisoformat(str(payload["expires_at"]))
            if payload.get("expires_at") is not None
            else None
        ),
        related_refs=tuple(str(ref) for ref in payload.get("related_refs", ())),
        metadata={str(key): str(value) for key, value in payload.get("metadata", {}).items()},
    )


def load_artifact_manifest(
    location_hint: str | Path,
    *,
    base_path: str | Path | None = None,
) -> ArtifactManifest:
    location = str(location_hint)
    if not location.strip():
        raise ValueError("artifact manifest location must be non-empty")
    ensure_no_raw_secret(location, field_name="artifact manifest location")
    ensure_no_production_alice_reference(location, field_name="artifact manifest location")
    path = Path(location)
    if not path.is_absolute():
        base = Path.cwd() if base_path is None else Path(base_path)
        path = base / path
    if "alice_live" in path.parts:
        raise ValueError("artifact manifest must not reference alice_live")
    if not path.is_file():
        raise ValueError("artifact manifest was not found")
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("artifact manifest payload must be a JSON object")
    return artifact_manifest_from_mapping(payload)


def verify_record_manifest(
    record: EvidenceRecord,
    *,
    base_path: str | Path | None = None,
) -> ArtifactManifest:
    manifest = load_artifact_manifest(record.storage_location_hint, base_path=base_path)
    manifest_record = manifest.to_record()
    if manifest_record.fingerprint != record.fingerprint:
        raise ValueError("artifact manifest does not match registry record")
    return manifest

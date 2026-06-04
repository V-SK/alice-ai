from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, Literal

from alice_acp.evidence.types import (
    ensure_no_production_alice_reference,
    ensure_no_raw_secret,
    parse_evidence_ref,
    validate_aware_timestamp,
    validate_sha256,
)

PRLPoolExportSourceType = Literal["manual", "fixture", "api_export", "file_export"]
PRLPoolExportCustodyStatus = Literal["ready", "under_review", "rejected"]

PRL_POOL_EXPORT_CUSTODY_CONFIRMED = "PRL_POOL_EXPORT_CUSTODY_CONFIRMED"
PRL_POOL_EXPORT_POOL_MISMATCH = "PRL_POOL_EXPORT_POOL_MISMATCH"
PRL_POOL_EXPORT_SESSION_MISMATCH = "PRL_POOL_EXPORT_SESSION_MISMATCH"
PRL_POOL_EXPORT_WORKER_MISMATCH = "PRL_POOL_EXPORT_WORKER_MISMATCH"
PRL_POOL_EXPORT_COLLECTION_ADDRESS_MISMATCH = "PRL_POOL_EXPORT_COLLECTION_ADDRESS_MISMATCH"
PRL_POOL_EXPORT_ACCEPTED_HASH_ABSENT = "PRL_POOL_EXPORT_ACCEPTED_HASH_ABSENT"
PRL_POOL_EXPORT_REJECTED_HASH_ABSENT = "PRL_POOL_EXPORT_REJECTED_HASH_ABSENT"
PRL_POOL_EXPORT_REJECTED_HASH_MARKED_ACCEPTED = (
    "PRL_POOL_EXPORT_REJECTED_HASH_MARKED_ACCEPTED"
)
PRL_POOL_EXPORT_COUNT_MISMATCH = "PRL_POOL_EXPORT_COUNT_MISMATCH"
PRL_POOL_EXPORT_HASH_OVERLAP = "PRL_POOL_EXPORT_HASH_OVERLAP"
PRL_POOL_EXPORT_DUPLICATE_HASH = "PRL_POOL_EXPORT_DUPLICATE_HASH"
PRL_POOL_EXPORT_STALE = "PRL_POOL_EXPORT_STALE"
PRL_POOL_EXPORT_FUTURE = "PRL_POOL_EXPORT_FUTURE"
PRL_POOL_EXPORT_OPERATOR_REF_MISSING = "PRL_POOL_EXPORT_OPERATOR_REF_MISSING"
PRL_POOL_EXPORT_RETENTION_REF_MISSING = "PRL_POOL_EXPORT_RETENTION_REF_MISSING"

DEFAULT_MAX_EXPORT_AGE = timedelta(hours=24)
DEFAULT_MAX_FUTURE_SKEW = timedelta(seconds=60)

_SUPPORTED_EXPORT_SOURCE_TYPES = frozenset({"manual", "fixture", "api_export", "file_export"})


@dataclass(frozen=True, slots=True)
class PRLPoolExportSnapshot:
    pool_id: str
    export_source_type: PRLPoolExportSourceType
    worker_name: str
    session_id: str
    collection_address: str
    generated_at: datetime
    accepted_share_count: int
    rejected_share_count: int
    accepted_share_hashes: tuple[str, ...]
    rejected_share_hashes: tuple[str, ...]
    export_payload_hash: str
    source_url_or_ref: str

    def __post_init__(self) -> None:
        required = (
            self.pool_id,
            self.export_source_type,
            self.worker_name,
            self.session_id,
            self.collection_address,
            self.export_payload_hash,
            self.source_url_or_ref,
        )
        if any(not value for value in required):
            raise ValueError("PRL pool export snapshot fields must be non-empty")
        if self.export_source_type not in _SUPPORTED_EXPORT_SOURCE_TYPES:
            raise ValueError(
                "export_source_type must be manual, fixture, api_export, or file_export"
            )
        validate_aware_timestamp("generated_at", self.generated_at)
        _validate_nonnegative_int("accepted_share_count", self.accepted_share_count)
        _validate_nonnegative_int("rejected_share_count", self.rejected_share_count)
        validate_sha256(self.export_payload_hash, field_name="export_payload_hash")
        for field_name, value in (
            ("pool_id", self.pool_id),
            ("export_source_type", self.export_source_type),
            ("worker_name", self.worker_name),
            ("session_id", self.session_id),
            ("collection_address", self.collection_address),
            ("source_url_or_ref", self.source_url_or_ref),
        ):
            _validate_public_string(field_name, value)
        _validate_hash_tuple("accepted_share_hashes", self.accepted_share_hashes)
        _validate_hash_tuple("rejected_share_hashes", self.rejected_share_hashes)


@dataclass(frozen=True, slots=True)
class PRLPoolExportCustodyEnvelope:
    snapshot: PRLPoolExportSnapshot
    custody_ref: str
    captured_at: datetime
    operator_ref: str | None = None
    retention_ref: str | None = None

    def __post_init__(self) -> None:
        validate_aware_timestamp("captured_at", self.captured_at)
        parsed_custody = parse_evidence_ref(self.custody_ref)
        if parsed_custody.scheme != "evidence":
            raise ValueError("custody_ref must use evidence://")
        if self.operator_ref is not None:
            parsed_operator = parse_evidence_ref(self.operator_ref)
            if parsed_operator.scheme != "approval":
                raise ValueError("operator_ref must use approval://")
        if self.retention_ref is not None:
            parsed_retention = parse_evidence_ref(self.retention_ref)
            if parsed_retention.scheme not in {"evidence", "approval"}:
                raise ValueError("retention_ref must use evidence:// or approval://")
        for field_name, value in (
            ("custody_ref", self.custody_ref),
            ("operator_ref", self.operator_ref or ""),
            ("retention_ref", self.retention_ref or ""),
        ):
            _validate_public_string(field_name, value)


@dataclass(frozen=True, slots=True)
class PRLPoolExportValidationResult:
    status: PRLPoolExportCustodyStatus
    custody_ready: bool
    production_readiness_ready: bool
    reason_codes: tuple[str, ...]
    snapshot_canonical_hash: str
    envelope_canonical_hash: str
    missing_accepted_share_hashes: tuple[str, ...] = ()
    rejected_snapshot_mismatch_hashes: tuple[str, ...] = ()

    @property
    def nonrewardable(self) -> bool:
        return not self.custody_ready


def canonical_pool_export_snapshot_payload(snapshot: PRLPoolExportSnapshot) -> str:
    return _canonical_json(_snapshot_payload(snapshot))


def canonical_pool_export_snapshot_hash(snapshot: PRLPoolExportSnapshot) -> str:
    return hashlib.sha256(
        canonical_pool_export_snapshot_payload(snapshot).encode("utf-8")
    ).hexdigest()


def canonical_pool_export_envelope_payload(envelope: PRLPoolExportCustodyEnvelope) -> str:
    payload = {
        "captured_at": envelope.captured_at.isoformat(),
        "custody_ref": envelope.custody_ref,
        "operator_ref": envelope.operator_ref,
        "retention_ref": envelope.retention_ref,
        "snapshot": _snapshot_payload(envelope.snapshot),
        "snapshot_canonical_hash": canonical_pool_export_snapshot_hash(envelope.snapshot),
    }
    return _canonical_json(payload)


def canonical_pool_export_envelope_hash(envelope: PRLPoolExportCustodyEnvelope) -> str:
    return hashlib.sha256(
        canonical_pool_export_envelope_payload(envelope).encode("utf-8")
    ).hexdigest()


def validate_prl_pool_export_custody(
    envelope: PRLPoolExportCustodyEnvelope,
    *,
    expected_pool_id: str,
    expected_session_id: str,
    expected_worker_name: str,
    expected_collection_address: str,
    accepted_canonical_hashes: tuple[str, ...],
    rejected_canonical_hashes: tuple[str, ...] = (),
    observed_at: datetime,
    max_export_age: timedelta = DEFAULT_MAX_EXPORT_AGE,
    max_future_skew: timedelta = DEFAULT_MAX_FUTURE_SKEW,
    production_readiness_required: bool = True,
) -> PRLPoolExportValidationResult:
    validate_aware_timestamp("observed_at", observed_at)
    _validate_public_string("expected_pool_id", expected_pool_id)
    _validate_public_string("expected_session_id", expected_session_id)
    _validate_public_string("expected_worker_name", expected_worker_name)
    _validate_public_string("expected_collection_address", expected_collection_address)
    expected_accepted = _validate_hash_tuple(
        "accepted_canonical_hashes", accepted_canonical_hashes
    )
    expected_rejected = _validate_hash_tuple(
        "rejected_canonical_hashes", rejected_canonical_hashes
    )

    snapshot = envelope.snapshot
    rejected_reasons: list[str] = []
    under_review_reasons: list[str] = []
    missing_accepted_hashes: list[str] = []
    rejected_mismatch_hashes: list[str] = []

    if snapshot.pool_id != expected_pool_id:
        rejected_reasons.append(PRL_POOL_EXPORT_POOL_MISMATCH)
    if snapshot.session_id != expected_session_id:
        rejected_reasons.append(PRL_POOL_EXPORT_SESSION_MISMATCH)
    if snapshot.worker_name != expected_worker_name:
        rejected_reasons.append(PRL_POOL_EXPORT_WORKER_MISMATCH)
    if snapshot.collection_address != expected_collection_address:
        rejected_reasons.append(PRL_POOL_EXPORT_COLLECTION_ADDRESS_MISMATCH)

    accepted_snapshot_hashes = set(snapshot.accepted_share_hashes)
    rejected_snapshot_hashes = set(snapshot.rejected_share_hashes)
    if len(accepted_snapshot_hashes) != len(snapshot.accepted_share_hashes):
        rejected_reasons.append(PRL_POOL_EXPORT_DUPLICATE_HASH)
    if len(rejected_snapshot_hashes) != len(snapshot.rejected_share_hashes):
        rejected_reasons.append(PRL_POOL_EXPORT_DUPLICATE_HASH)
    if accepted_snapshot_hashes & rejected_snapshot_hashes:
        rejected_reasons.append(PRL_POOL_EXPORT_HASH_OVERLAP)
    if snapshot.accepted_share_count != len(snapshot.accepted_share_hashes):
        rejected_reasons.append(PRL_POOL_EXPORT_COUNT_MISMATCH)
    if snapshot.rejected_share_count != len(snapshot.rejected_share_hashes):
        rejected_reasons.append(PRL_POOL_EXPORT_COUNT_MISMATCH)

    for proof_hash in expected_accepted:
        if proof_hash not in accepted_snapshot_hashes:
            missing_accepted_hashes.append(proof_hash)
            under_review_reasons.append(PRL_POOL_EXPORT_ACCEPTED_HASH_ABSENT)

    for proof_hash in expected_rejected:
        if proof_hash in accepted_snapshot_hashes:
            rejected_mismatch_hashes.append(proof_hash)
            rejected_reasons.append(PRL_POOL_EXPORT_REJECTED_HASH_MARKED_ACCEPTED)
        elif proof_hash not in rejected_snapshot_hashes:
            rejected_mismatch_hashes.append(proof_hash)
            under_review_reasons.append(PRL_POOL_EXPORT_REJECTED_HASH_ABSENT)

    if snapshot.generated_at > observed_at + max_future_skew:
        under_review_reasons.append(PRL_POOL_EXPORT_FUTURE)
    elif observed_at - snapshot.generated_at > max_export_age:
        under_review_reasons.append(PRL_POOL_EXPORT_STALE)

    if production_readiness_required:
        if envelope.operator_ref is None:
            rejected_reasons.append(PRL_POOL_EXPORT_OPERATOR_REF_MISSING)
        if envelope.retention_ref is None:
            rejected_reasons.append(PRL_POOL_EXPORT_RETENTION_REF_MISSING)

    reason_codes = tuple(dict.fromkeys((*rejected_reasons, *under_review_reasons)))
    if rejected_reasons:
        status: PRLPoolExportCustodyStatus = "rejected"
    elif under_review_reasons:
        status = "under_review"
    else:
        status = "ready"
        reason_codes = (PRL_POOL_EXPORT_CUSTODY_CONFIRMED,)

    custody_ready = status == "ready"
    return PRLPoolExportValidationResult(
        status=status,
        custody_ready=custody_ready,
        production_readiness_ready=custody_ready and production_readiness_required,
        reason_codes=reason_codes,
        snapshot_canonical_hash=canonical_pool_export_snapshot_hash(snapshot),
        envelope_canonical_hash=canonical_pool_export_envelope_hash(envelope),
        missing_accepted_share_hashes=tuple(missing_accepted_hashes),
        rejected_snapshot_mismatch_hashes=tuple(rejected_mismatch_hashes),
    )


def _snapshot_payload(snapshot: PRLPoolExportSnapshot) -> dict[str, object]:
    return {
        "accepted_share_count": snapshot.accepted_share_count,
        "accepted_share_hashes": snapshot.accepted_share_hashes,
        "collection_address": snapshot.collection_address,
        "export_payload_hash": snapshot.export_payload_hash,
        "export_source_type": snapshot.export_source_type,
        "generated_at": snapshot.generated_at.isoformat(),
        "pool_id": snapshot.pool_id,
        "rejected_share_count": snapshot.rejected_share_count,
        "rejected_share_hashes": snapshot.rejected_share_hashes,
        "session_id": snapshot.session_id,
        "source_url_or_ref": snapshot.source_url_or_ref,
        "worker_name": snapshot.worker_name,
    }


def _canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(
        {key: _canonical_value(value) for key, value in payload.items()},
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def _canonical_value(value: object) -> object:
    if isinstance(value, float):
        raise TypeError("PRL pool export canonical JSON must not contain float values")
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, tuple):
        return [_canonical_value(item) for item in value]
    if isinstance(value, list):
        return [_canonical_value(item) for item in value]
    if isinstance(value, dict):
        return {key: _canonical_value(value[key]) for key in sorted(value)}
    return value


def _validate_nonnegative_int(field_name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name} must be an integer")
    if value < 0:
        raise ValueError(f"{field_name} must be non-negative")


def _validate_hash_tuple(field_name: str, values: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(values, tuple):
        raise TypeError(f"{field_name} must be a tuple")
    for value in values:
        validate_sha256(value, field_name=field_name)
    return values


def _validate_public_string(field_name: str, value: str) -> None:
    ensure_no_raw_secret(value, field_name=field_name)
    ensure_no_production_alice_reference(value, field_name=field_name)

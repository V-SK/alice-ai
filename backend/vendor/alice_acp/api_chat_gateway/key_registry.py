from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from alice_acp.api_chat.types import utc_now, validate_public_identifier
from alice_acp.api_chat.validators import (
    ensure_no_raw_secret,
    validate_aware_timestamp,
    validate_sha256,
)

ApiChatGatewayKeyStatus = Literal["active", "revoked", "disabled"]

REASON_API_KEY_ADMITTED = "api_chat_gateway_key_admitted"
REASON_API_KEY_MISSING = "api_chat_gateway_key_missing"
REASON_API_KEY_UNKNOWN = "api_chat_gateway_key_unknown"
REASON_API_KEY_REVOKED = "api_chat_gateway_key_revoked"
REASON_API_KEY_DISABLED = "api_chat_gateway_key_disabled"
REASON_API_KEY_RATE_LIMIT = "api_chat_gateway_key_hourly_limit_exceeded"

_VALID_KEY_STATUSES: tuple[ApiChatGatewayKeyStatus, ...] = (
    "active",
    "revoked",
    "disabled",
)
_FORBIDDEN_RAW_KEY_FIELDS = frozenset(
    {
        "api_key",
        "api_key_value",
        "bearer",
        "plaintext",
        "private_key",
        "raw_key",
        "secret",
        "seed",
        "token",
    }
)


@dataclass(frozen=True, slots=True)
class ApiChatGatewayKeyRecord:
    key_id: str
    key_hash: str
    status: ApiChatGatewayKeyStatus = "active"
    limit_per_hour: int = 120
    created_at: datetime = field(default_factory=utc_now)
    revoked_at: datetime | None = None
    owner_ref: str = "owner-unassigned"
    scopes: tuple[str, ...] = ("chat.completions",)

    def __post_init__(self) -> None:
        object.__setattr__(self, "key_id", canonical_api_key_id(self.key_id))
        object.__setattr__(self, "key_hash", self.key_hash.strip().lower())
        validate_sha256(self.key_hash, field_name="key_hash")
        if self.status not in _VALID_KEY_STATUSES:
            raise ValueError("key status must be active, revoked, or disabled")
        if self.limit_per_hour <= 0:
            raise ValueError("limit_per_hour must be positive")
        validate_aware_timestamp("created_at", self.created_at)
        if self.revoked_at is not None:
            validate_aware_timestamp("revoked_at", self.revoked_at)
        if self.status == "revoked" and self.revoked_at is None:
            raise ValueError("revoked_at is required for revoked keys")
        if self.status != "revoked" and self.revoked_at is not None:
            raise ValueError("revoked_at is only allowed for revoked keys")
        validate_public_identifier("owner_ref", self.owner_ref)
        if not self.scopes:
            raise ValueError("scopes must not be empty")
        for scope in self.scopes:
            validate_public_identifier("scope", scope)

    @classmethod
    def from_mapping(cls, payload: dict[str, object]) -> ApiChatGatewayKeyRecord:
        forbidden = _FORBIDDEN_RAW_KEY_FIELDS.intersection(payload)
        if forbidden:
            names = ", ".join(sorted(forbidden))
            raise ValueError(f"raw API key material is forbidden: {names}")
        scopes = payload.get("scopes", ("chat.completions",))
        if not isinstance(scopes, tuple):
            if not isinstance(scopes, list):
                raise ValueError("scopes must be a list or tuple")
            scopes = tuple(scopes)
        return cls(
            key_id=_required_str(payload, "key_id"),
            key_hash=_required_str(payload, "key_hash"),
            status=payload.get("status", "active"),  # type: ignore[arg-type]
            limit_per_hour=_required_int(payload, "limit_per_hour", default=120),
            created_at=_required_datetime(payload, "created_at", default=utc_now()),
            revoked_at=_optional_datetime(payload, "revoked_at"),
            owner_ref=_required_str(payload, "owner_ref", default="owner-unassigned"),
            scopes=tuple(_required_scope(scope) for scope in scopes),
        )

    @property
    def active(self) -> bool:
        return self.status == "active"

    def public_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "key_id": self.key_id,
            "key_hash": self.key_hash,
            "status": self.status,
            "limit_per_hour": self.limit_per_hour,
            "created_at": self.created_at.isoformat(),
            "owner_ref": self.owner_ref,
            "scopes": self.scopes,
        }
        if self.revoked_at is not None:
            payload["revoked_at"] = self.revoked_at.isoformat()
        return payload


@dataclass(frozen=True, slots=True)
class ApiChatGatewayKeyAdmission:
    admitted: bool
    reason_code: str
    key_record: ApiChatGatewayKeyRecord | None = None
    retry_after_seconds: int | None = None


@dataclass(slots=True)
class ApiChatGatewayKeyRegistry:
    records: tuple[ApiChatGatewayKeyRecord, ...] = ()
    _by_hash: dict[str, ApiChatGatewayKeyRecord] = field(init=False, repr=False)
    _by_id: dict[str, ApiChatGatewayKeyRecord] = field(init=False, repr=False)
    _admissions_by_key_hash: dict[str, list[datetime]] = field(
        default_factory=dict,
        init=False,
        repr=False,
    )
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)

    def __post_init__(self) -> None:
        by_hash: dict[str, ApiChatGatewayKeyRecord] = {}
        by_id: dict[str, ApiChatGatewayKeyRecord] = {}
        for record in self.records:
            if record.key_hash in by_hash:
                raise ValueError("key registry contains duplicate key_hash")
            if record.key_id in by_id:
                raise ValueError("key registry contains duplicate key_id")
            by_hash[record.key_hash] = record
            by_id[record.key_id] = record
        self._by_hash = by_hash
        self._by_id = by_id

    @property
    def configured(self) -> bool:
        return bool(self.records)

    @classmethod
    def from_mappings(
        cls,
        payloads: tuple[dict[str, object], ...],
    ) -> ApiChatGatewayKeyRegistry:
        return cls(
            records=tuple(
                ApiChatGatewayKeyRecord.from_mapping(payload) for payload in payloads
            )
        )

    def validate(
        self,
        *,
        key_hash: str | None,
        key_id: str | None = None,
        required: bool = True,
    ) -> ApiChatGatewayKeyAdmission:
        if key_hash is None:
            if required:
                return ApiChatGatewayKeyAdmission(False, REASON_API_KEY_MISSING)
            return ApiChatGatewayKeyAdmission(True, REASON_API_KEY_ADMITTED)
        normalized_hash = canonical_api_key_hash(key_hash)
        normalized_id = canonical_api_key_id(key_id) if key_id is not None else None
        record = self._by_hash.get(normalized_hash)
        if record is None:
            return ApiChatGatewayKeyAdmission(False, REASON_API_KEY_UNKNOWN)
        if normalized_id is not None and record.key_id != normalized_id:
            return ApiChatGatewayKeyAdmission(False, REASON_API_KEY_UNKNOWN)
        if record.status == "revoked":
            return ApiChatGatewayKeyAdmission(False, REASON_API_KEY_REVOKED, record)
        if record.status == "disabled":
            return ApiChatGatewayKeyAdmission(False, REASON_API_KEY_DISABLED, record)
        return ApiChatGatewayKeyAdmission(True, REASON_API_KEY_ADMITTED, record)

    def admit(
        self,
        *,
        key_hash: str | None,
        key_id: str | None = None,
        observed_at: datetime,
        required: bool = True,
    ) -> ApiChatGatewayKeyAdmission:
        validate_aware_timestamp("observed_at", observed_at)
        admission = self.validate(key_hash=key_hash, key_id=key_id, required=required)
        if not admission.admitted or admission.key_record is None:
            return admission

        record = admission.key_record
        with self._lock:
            retained = self._retained_admissions_locked(record.key_hash, observed_at)
            if len(retained) >= record.limit_per_hour:
                return ApiChatGatewayKeyAdmission(
                    admitted=False,
                    reason_code=REASON_API_KEY_RATE_LIMIT,
                    key_record=record,
                    retry_after_seconds=_retry_after_seconds(
                        oldest=retained[0],
                        observed_at=observed_at,
                    ),
                )
            retained.append(observed_at)
            self._admissions_by_key_hash[record.key_hash] = retained
        return admission

    def _retained_admissions_locked(
        self,
        key_hash: str,
        observed_at: datetime,
    ) -> list[datetime]:
        cutoff = observed_at - timedelta(hours=1)
        retained = [
            timestamp
            for timestamp in self._admissions_by_key_hash.get(key_hash, [])
            if timestamp > cutoff
        ]
        self._admissions_by_key_hash[key_hash] = retained
        return retained


def canonical_api_key_id(value: str | None) -> str:
    if value is None:
        raise ValueError("key_id must be non-empty")
    canonical = value.strip().lower()
    validate_public_identifier("key_id", canonical)
    if not canonical.startswith("ak_"):
        raise ValueError("key_id must start with ak_")
    if canonical != value.strip():
        raise ValueError("key_id must be canonical lowercase")
    ensure_no_raw_secret(canonical, field_name="key_id")
    return canonical


def canonical_api_key_hash(value: str) -> str:
    canonical = value.strip().lower()
    validate_sha256(canonical, field_name="key_hash")
    if canonical != value.strip():
        raise ValueError("key_hash must be canonical lowercase sha256")
    return canonical


def _required_str(payload: dict[str, object], field_name: str, default: str | None = None) -> str:
    value = payload.get(field_name, default)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    ensure_no_raw_secret(value, field_name=field_name)
    return value


def _required_int(payload: dict[str, object], field_name: str, *, default: int) -> int:
    value = payload.get(field_name, default)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer")
    return value


def _required_datetime(
    payload: dict[str, object],
    field_name: str,
    *,
    default: datetime,
) -> datetime:
    value = payload.get(field_name, default)
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime")
    return value


def _optional_datetime(payload: dict[str, object], field_name: str) -> datetime | None:
    value = payload.get(field_name)
    if value is None:
        return None
    if not isinstance(value, datetime):
        raise ValueError(f"{field_name} must be a datetime")
    return value


def _required_scope(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("scope must be a non-empty string")
    ensure_no_raw_secret(value, field_name="scope")
    return value


def _retry_after_seconds(*, oldest: datetime, observed_at: datetime) -> int:
    retry_after = (oldest + timedelta(hours=1) - observed_at).total_seconds()
    return max(1, math.ceil(retry_after))

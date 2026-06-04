from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

from alice_acp.api_chat.types import utc_now, validate_public_identifier
from alice_acp.api_chat.validators import (
    ensure_no_raw_secret,
    validate_aware_timestamp,
    validate_sha256,
)

API_CHAT_STORAGE_CONTRACT_VERSION = "a3-api-chat-durable-storage-contract-v1"
ANONYMOUS_FREE_LIMIT_PER_HOUR = 10

REASON_STORAGE_CONTRACT_DEFAULT_OFF = "api_chat_storage_contract_default_off"
REASON_STORAGE_CONTRACT_READY = "api_chat_storage_contract_ready"
REASON_ANONYMOUS_FREE_BUCKET = "api_chat_anonymous_free_10_per_hour"
REASON_RAW_PRIVATE_PAYLOAD_FORBIDDEN = "api_chat_storage_raw_private_payload_forbidden"

ApiKeyHashStatus = Literal["active", "disabled", "revoked"]
RateLimitSubjectKind = Literal["anonymous", "api_key", "user_id", "worker"]
QueueResultStatus = Literal["acked", "nacked", "retry", "stale"]

_VALID_API_KEY_HASH_STATUSES: tuple[ApiKeyHashStatus, ...] = (
    "active",
    "disabled",
    "revoked",
)
_VALID_RATE_LIMIT_SUBJECT_KINDS: tuple[RateLimitSubjectKind, ...] = (
    "anonymous",
    "api_key",
    "user_id",
    "worker",
)
_VALID_QUEUE_RESULT_STATUSES: tuple[QueueResultStatus, ...] = (
    "acked",
    "nacked",
    "retry",
    "stale",
)
_FORBIDDEN_RAW_FIELD_NAMES = frozenset(
    {
        "api_key",
        "api_key_value",
        "authorization",
        "bearer",
        "completion",
        "messages",
        "output",
        "password",
        "private_key",
        "prompt",
        "raw_api_key",
        "raw_completion",
        "raw_key",
        "raw_output",
        "raw_prompt",
        "raw_token",
        "request_body",
        "response_body",
        "secret",
        "seed",
        "token",
    }
)


class ApiChatDurableStorage(Protocol):
    def upsert_api_key_hash(self, record: ApiKeyHashRecordDTO) -> None: ...

    def get_api_key_hash(self, key_hash: str) -> ApiKeyHashRecordDTO | None: ...

    def put_rate_limit_bucket(self, bucket: RateLimitBucketDTO) -> None: ...

    def append_audit_event(self, event: RedactedAuditEventDTO) -> None: ...

    def put_worker_heartbeat(self, heartbeat: WorkerHeartbeatStorageDTO) -> None: ...

    def put_queue_lease(self, lease: QueueLeaseStorageDTO) -> None: ...

    def put_queue_result(self, result: QueueResultStorageDTO) -> None: ...


@dataclass(frozen=True, slots=True)
class ApiChatStorageContractConfig:
    storage_writes_enabled: bool = False
    public_service_enabled: bool = False
    raw_prompt_persistence_enabled: bool = False
    raw_completion_persistence_enabled: bool = False
    raw_api_key_persistence_enabled: bool = False
    raw_token_persistence_enabled: bool = False

    def __post_init__(self) -> None:
        if (
            self.public_service_enabled
            or self.raw_prompt_persistence_enabled
            or self.raw_completion_persistence_enabled
            or self.raw_api_key_persistence_enabled
            or self.raw_token_persistence_enabled
        ):
            raise ValueError(REASON_RAW_PRIVATE_PAYLOAD_FORBIDDEN)

    @property
    def reason_code(self) -> str:
        if self.storage_writes_enabled:
            return REASON_STORAGE_CONTRACT_READY
        return REASON_STORAGE_CONTRACT_DEFAULT_OFF

    def to_public_dict(self) -> dict[str, object]:
        return {
            "contract_version": API_CHAT_STORAGE_CONTRACT_VERSION,
            "storage_writes_enabled": self.storage_writes_enabled,
            "public_service_enabled": False,
            "anonymous_free_limit_per_hour": ANONYMOUS_FREE_LIMIT_PER_HOUR,
            "raw_prompt_persisted": False,
            "raw_completion_persisted": False,
            "raw_api_key_persisted": False,
            "raw_token_persisted": False,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True, slots=True)
class ApiKeyHashRecordDTO:
    key_id: str
    key_hash: str
    owner_ref: str
    scopes: tuple[str, ...]
    status: ApiKeyHashStatus
    created_at: datetime
    updated_at: datetime
    revoked_at: datetime | None = None
    expires_at: datetime | None = None

    def __post_init__(self) -> None:
        validate_public_identifier("key_id", self.key_id)
        if not self.key_id.startswith("ak_"):
            raise ValueError("key_id must start with ak_")
        validate_sha256(self.key_hash, field_name="key_hash")
        validate_public_identifier("owner_ref", self.owner_ref)
        scopes = tuple(dict.fromkeys(self.scopes))
        if not scopes:
            raise ValueError("api key scopes must not be empty")
        for scope in scopes:
            validate_public_identifier("scope", scope)
            ensure_no_raw_secret(scope, field_name="scope")
        if self.status not in _VALID_API_KEY_HASH_STATUSES:
            raise ValueError("api key hash status is unsupported")
        validate_aware_timestamp("created_at", self.created_at)
        validate_aware_timestamp("updated_at", self.updated_at)
        if self.updated_at < self.created_at:
            raise ValueError("updated_at must be after created_at")
        if self.revoked_at is not None:
            validate_aware_timestamp("revoked_at", self.revoked_at)
        if self.expires_at is not None:
            validate_aware_timestamp("expires_at", self.expires_at)
        if self.status == "revoked" and self.revoked_at is None:
            raise ValueError("revoked api key hash requires revoked_at")
        if self.status != "revoked" and self.revoked_at is not None:
            raise ValueError("revoked_at is only allowed for revoked api key hashes")
        object.__setattr__(self, "scopes", scopes)

    def to_storage_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "key_id": self.key_id,
            "key_hash": self.key_hash,
            "owner_ref": self.owner_ref,
            "scopes": self.scopes,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
        }
        if self.revoked_at is not None:
            payload["revoked_at"] = self.revoked_at.isoformat()
        if self.expires_at is not None:
            payload["expires_at"] = self.expires_at.isoformat()
        return payload


@dataclass(frozen=True, slots=True)
class RateLimitBucketDTO:
    subject_hash: str
    subject_kind: RateLimitSubjectKind
    window_start: datetime
    limit: int
    used: int
    reset_at: datetime
    reason_code: str

    def __post_init__(self) -> None:
        validate_sha256(self.subject_hash, field_name="subject_hash")
        if self.subject_kind not in _VALID_RATE_LIMIT_SUBJECT_KINDS:
            raise ValueError("rate limit subject kind is unsupported")
        validate_aware_timestamp("window_start", self.window_start)
        validate_aware_timestamp("reset_at", self.reset_at)
        if self.reset_at <= self.window_start:
            raise ValueError("reset_at must be after window_start")
        if self.limit <= 0:
            raise ValueError("rate limit must be positive")
        if self.used < 0:
            raise ValueError("rate limit used count must be non-negative")
        validate_public_identifier("reason_code", self.reason_code)

    @classmethod
    def anonymous_free(
        cls,
        *,
        subject_hash: str,
        window_start: datetime,
        used: int,
        reset_at: datetime,
    ) -> RateLimitBucketDTO:
        return cls(
            subject_hash=subject_hash,
            subject_kind="anonymous",
            window_start=window_start,
            limit=ANONYMOUS_FREE_LIMIT_PER_HOUR,
            used=used,
            reset_at=reset_at,
            reason_code=REASON_ANONYMOUS_FREE_BUCKET,
        )

    def to_storage_dict(self) -> dict[str, object]:
        return {
            "subject_hash": self.subject_hash,
            "subject_kind": self.subject_kind,
            "window_start": self.window_start.isoformat(),
            "limit": self.limit,
            "used": self.used,
            "reset_at": self.reset_at.isoformat(),
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True, slots=True)
class RedactedAuditEventDTO:
    event_id: str
    event_type: str
    subject_hash: str
    reason_code: str
    occurred_at: datetime
    metadata: Mapping[str, object] = field(default_factory=dict)

    def __post_init__(self) -> None:
        validate_public_identifier("event_id", self.event_id)
        validate_public_identifier("event_type", self.event_type)
        validate_sha256(self.subject_hash, field_name="subject_hash")
        validate_public_identifier("reason_code", self.reason_code)
        validate_aware_timestamp("occurred_at", self.occurred_at)
        _validate_metadata(self.metadata)

    def to_storage_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "event_type": self.event_type,
            "subject_hash": self.subject_hash,
            "reason_code": self.reason_code,
            "occurred_at": self.occurred_at.isoformat(),
            "metadata": dict(self.metadata),
        }


@dataclass(frozen=True, slots=True)
class WorkerHeartbeatStorageDTO:
    miner_id: str
    device_id: str
    loaded_models: tuple[str, ...]
    cached_models: tuple[str, ...]
    memory_free_gb: int
    memory_total_gb: int
    queue_depth: int
    last_seen_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        validate_public_identifier("miner_id", self.miner_id)
        validate_public_identifier("device_id", self.device_id)
        loaded = _canonical_public_identifiers("loaded_model", self.loaded_models)
        cached = _canonical_public_identifiers("cached_model", self.cached_models)
        if self.memory_free_gb < 0:
            raise ValueError("memory_free_gb must be non-negative")
        if self.memory_total_gb <= 0:
            raise ValueError("memory_total_gb must be positive")
        if self.memory_free_gb > self.memory_total_gb:
            raise ValueError("memory_free_gb must not exceed memory_total_gb")
        if self.queue_depth < 0:
            raise ValueError("queue_depth must be non-negative")
        validate_aware_timestamp("last_seen_at", self.last_seen_at)
        object.__setattr__(self, "loaded_models", loaded)
        object.__setattr__(self, "cached_models", cached)

    def to_storage_dict(self) -> dict[str, object]:
        return {
            "miner_id": self.miner_id,
            "device_id": self.device_id,
            "loaded_models": self.loaded_models,
            "cached_models": self.cached_models,
            "memory_free_gb": self.memory_free_gb,
            "memory_total_gb": self.memory_total_gb,
            "queue_depth": self.queue_depth,
            "last_seen_at": self.last_seen_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class QueueLeaseStorageDTO:
    lease_id: str
    queue_item_id: str
    worker_id: str
    prompt_hash: str
    leased_at: datetime
    expires_at: datetime
    ack_count: int = 0
    nack_count: int = 0
    retry_count: int = 0
    stale_count: int = 0

    def __post_init__(self) -> None:
        validate_public_identifier("lease_id", self.lease_id)
        validate_public_identifier("queue_item_id", self.queue_item_id)
        validate_public_identifier("worker_id", self.worker_id)
        validate_sha256(self.prompt_hash, field_name="prompt_hash")
        validate_aware_timestamp("leased_at", self.leased_at)
        validate_aware_timestamp("expires_at", self.expires_at)
        if self.expires_at <= self.leased_at:
            raise ValueError("queue lease expires_at must be after leased_at")
        _validate_non_negative_counters(
            ack_count=self.ack_count,
            nack_count=self.nack_count,
            retry_count=self.retry_count,
            stale_count=self.stale_count,
        )

    def to_storage_dict(self) -> dict[str, object]:
        return {
            "lease_id": self.lease_id,
            "queue_item_id": self.queue_item_id,
            "worker_id": self.worker_id,
            "prompt_hash": self.prompt_hash,
            "leased_at": self.leased_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "ack_count": self.ack_count,
            "nack_count": self.nack_count,
            "retry_count": self.retry_count,
            "stale_count": self.stale_count,
        }


@dataclass(frozen=True, slots=True)
class QueueResultStorageDTO:
    result_id: str
    queue_item_id: str
    lease_id: str
    status: QueueResultStatus
    prompt_hash: str
    output_hash: str | None
    reason_code: str
    completed_at: datetime
    ack_count: int = 0
    nack_count: int = 0
    retry_count: int = 0
    stale_count: int = 0

    def __post_init__(self) -> None:
        validate_public_identifier("result_id", self.result_id)
        validate_public_identifier("queue_item_id", self.queue_item_id)
        validate_public_identifier("lease_id", self.lease_id)
        if self.status not in _VALID_QUEUE_RESULT_STATUSES:
            raise ValueError("queue result status is unsupported")
        validate_sha256(self.prompt_hash, field_name="prompt_hash")
        if self.output_hash is not None:
            validate_sha256(self.output_hash, field_name="output_hash")
        if self.status == "acked" and self.output_hash is None:
            raise ValueError("acked queue result requires output_hash")
        validate_public_identifier("reason_code", self.reason_code)
        validate_aware_timestamp("completed_at", self.completed_at)
        _validate_non_negative_counters(
            ack_count=self.ack_count,
            nack_count=self.nack_count,
            retry_count=self.retry_count,
            stale_count=self.stale_count,
        )

    def to_storage_dict(self) -> dict[str, object]:
        return {
            "result_id": self.result_id,
            "queue_item_id": self.queue_item_id,
            "lease_id": self.lease_id,
            "status": self.status,
            "prompt_hash": self.prompt_hash,
            "output_hash": self.output_hash,
            "reason_code": self.reason_code,
            "completed_at": self.completed_at.isoformat(),
            "ack_count": self.ack_count,
            "nack_count": self.nack_count,
            "retry_count": self.retry_count,
            "stale_count": self.stale_count,
        }


def api_chat_storage_contract_summary(
    config: ApiChatStorageContractConfig | None = None,
) -> dict[str, object]:
    return (config or ApiChatStorageContractConfig()).to_public_dict()


def _canonical_public_identifiers(
    field_name: str,
    values: tuple[str, ...],
) -> tuple[str, ...]:
    canonical = tuple(dict.fromkeys(values))
    for value in canonical:
        validate_public_identifier(field_name, value)
        ensure_no_raw_secret(value, field_name=field_name)
    return canonical


def _validate_metadata(metadata: Mapping[str, object]) -> None:
    for key, value in metadata.items():
        _validate_metadata_key(str(key))
        _validate_metadata_value(str(key), value)


def _validate_metadata_key(key: str) -> None:
    normalized = key.lower()
    if normalized in _FORBIDDEN_RAW_FIELD_NAMES:
        raise ValueError(REASON_RAW_PRIVATE_PAYLOAD_FORBIDDEN)
    validate_public_identifier("metadata_key", normalized)


def _validate_metadata_value(field_name: str, value: object) -> None:
    if isinstance(value, str):
        ensure_no_raw_secret(value, field_name=field_name)
        return
    if isinstance(value, bool | int | float) or value is None:
        return
    if isinstance(value, tuple | list):
        for item in value:
            _validate_metadata_value(field_name, item)
        return
    if isinstance(value, Mapping):
        _validate_metadata(value)
        return
    raise ValueError("audit metadata values must be scalar, list, or mapping")


def _validate_non_negative_counters(**counters: int) -> None:
    for field_name, value in counters.items():
        if value < 0:
            raise ValueError(f"{field_name} must be non-negative")

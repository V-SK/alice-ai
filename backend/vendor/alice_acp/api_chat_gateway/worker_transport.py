from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal

from alice_acp.api_chat.types import utc_now, validate_public_identifier
from alice_acp.api_chat.validators import (
    ensure_no_raw_secret,
    validate_aware_timestamp,
    validate_sha256,
)

WORKER_TRANSPORT_CONTRACT_VERSION = "api-chat-worker-internal-transport-contract-v1"

REASON_WORKER_AUTH_RAW_SECRET_FORBIDDEN = "api_chat_worker_auth_raw_secret_forbidden"
REASON_WORKER_AUTH_SCOPE_DENIED = "api_chat_worker_auth_scope_denied"
REASON_WORKER_TRANSPORT_FORBIDDEN = "api_chat_worker_transport_network_forbidden"

WorkerAuthScope = Literal["dispatch", "lease", "complete", "retry", "fail", "heartbeat"]
WorkerTransportAction = Literal[
    "dispatch",
    "lease",
    "complete",
    "retry",
    "fail",
    "heartbeat",
]

VALID_WORKER_AUTH_SCOPES: tuple[WorkerAuthScope, ...] = (
    "dispatch",
    "lease",
    "complete",
    "retry",
    "fail",
    "heartbeat",
)
VALID_WORKER_TRANSPORT_ACTIONS: tuple[WorkerTransportAction, ...] = (
    "dispatch",
    "lease",
    "complete",
    "retry",
    "fail",
    "heartbeat",
)

_FORBIDDEN_RAW_AUTH_FIELDS = frozenset(
    {
        "api_key",
        "authorization",
        "bearer",
        "bearer_token",
        "password",
        "private_key",
        "raw_secret",
        "raw_token",
        "secret",
        "token",
        "access_token",
    }
)


@dataclass(frozen=True, slots=True)
class WorkerAuthHandleDTO:
    worker_id: str
    passport_id: str
    key_id: str
    key_hash: str
    scopes: tuple[WorkerAuthScope, ...]
    authenticated_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        validate_public_identifier("worker_id", self.worker_id)
        validate_public_identifier("passport_id", self.passport_id)
        validate_public_identifier("key_id", self.key_id)
        validate_sha256(self.key_hash, field_name="key_hash")
        scopes = tuple(dict.fromkeys(self.scopes))
        if not scopes:
            raise ValueError("worker auth scopes must not be empty")
        for scope in scopes:
            if scope not in VALID_WORKER_AUTH_SCOPES:
                raise ValueError("worker auth scope is unsupported")
            ensure_no_raw_secret(scope, field_name="worker_auth_scope")
        validate_aware_timestamp("authenticated_at", self.authenticated_at)
        object.__setattr__(self, "scopes", scopes)

    @classmethod
    def from_payload(cls, payload: Mapping[str, object]) -> WorkerAuthHandleDTO:
        _reject_raw_auth_material(payload)
        scopes = payload.get("scopes")
        if not isinstance(scopes, Sequence) or isinstance(scopes, str | bytes):
            raise ValueError("worker auth scopes must be an array")
        return cls(
            worker_id=_required_str(payload, "worker_id"),
            passport_id=_required_str(payload, "passport_id"),
            key_id=_required_str(payload, "key_id"),
            key_hash=_required_str(payload, "key_hash"),
            scopes=tuple(_scope_from_payload(scope) for scope in scopes),
        )

    def allows(self, action: WorkerTransportAction) -> bool:
        if action not in VALID_WORKER_TRANSPORT_ACTIONS:
            raise ValueError("worker transport action is unsupported")
        return action in self.scopes

    def require_scope(self, action: WorkerTransportAction) -> None:
        if not self.allows(action):
            raise ValueError(REASON_WORKER_AUTH_SCOPE_DENIED)

    def to_public_dict(self) -> dict[str, object]:
        return {
            "worker_id": self.worker_id,
            "passport_id": self.passport_id,
            "key_id": self.key_id,
            "key_hash": self.key_hash,
            "scopes": self.scopes,
            "authenticated_at": self.authenticated_at.isoformat(),
            "raw_token_persisted": False,
            "raw_secret_persisted": False,
        }


@dataclass(frozen=True, slots=True)
class InternalWorkerTransportDTO:
    transport_id: str
    action: WorkerTransportAction
    auth_handle: WorkerAuthHandleDTO
    job_id: str | None = None
    lease_id: str | None = None
    payload_hash: str | None = None
    created_at: datetime = field(default_factory=utc_now)
    internal_only: bool = True
    network_transport_enabled: bool = False
    remote_model_call_performed: bool = False
    public_service_enabled: bool = False
    production_api_claim: bool = False

    def __post_init__(self) -> None:
        validate_public_identifier("transport_id", self.transport_id)
        if self.action not in VALID_WORKER_TRANSPORT_ACTIONS:
            raise ValueError("worker transport action is unsupported")
        self.auth_handle.require_scope(self.action)
        if self.job_id is not None:
            validate_public_identifier("job_id", self.job_id)
        if self.lease_id is not None:
            validate_public_identifier("lease_id", self.lease_id)
        if self.payload_hash is not None:
            validate_sha256(self.payload_hash, field_name="payload_hash")
        validate_aware_timestamp("created_at", self.created_at)
        if (
            not self.internal_only
            or self.network_transport_enabled
            or self.remote_model_call_performed
            or self.public_service_enabled
            or self.production_api_claim
        ):
            raise ValueError(REASON_WORKER_TRANSPORT_FORBIDDEN)

    def to_public_dict(self) -> dict[str, object]:
        return {
            "contract_version": WORKER_TRANSPORT_CONTRACT_VERSION,
            "transport_id": self.transport_id,
            "action": self.action,
            "auth_handle": self.auth_handle.to_public_dict(),
            "job_id": self.job_id,
            "lease_id": self.lease_id,
            "payload_hash": self.payload_hash,
            "created_at": self.created_at.isoformat(),
            "internal_only": True,
            "network_transport_enabled": False,
            "remote_model_call_performed": False,
            "public_service_enabled": False,
            "production_api_claim": False,
        }


def _reject_raw_auth_material(payload: Mapping[str, object]) -> None:
    forbidden = _FORBIDDEN_RAW_AUTH_FIELDS.intersection(
        str(field).lower() for field in payload
    )
    if forbidden:
        raise ValueError(REASON_WORKER_AUTH_RAW_SECRET_FORBIDDEN)
    for field_name, value in payload.items():
        if isinstance(value, str):
            ensure_no_raw_secret(value, field_name=str(field_name))


def _required_str(payload: Mapping[str, object], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _scope_from_payload(value: object) -> WorkerAuthScope:
    if value not in VALID_WORKER_AUTH_SCOPES:
        raise ValueError("worker auth scope is unsupported")
    return value

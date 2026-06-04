from __future__ import annotations

import base64
import binascii
import hashlib
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Literal, Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from alice_acp.evidence.types import ensure_no_raw_secret, validate_aware_timestamp, validate_sha256

SESSION_REQUEST_PAYLOAD_VERSION = "alice-mining-session-request-payload-v1"
SESSION_REQUEST_FIELDS = (
    "passport_id",
    "device_id",
    "session_nonce",
    "requested_algorithm",
    "requested_pool_id",
    "timestamp",
    "policy_version",
    "payload_version",
)
SESSION_REQUEST_SIGNATURE_DOMAIN = b"alice-mining-session-request-v1\n"
SESSION_REQUEST_SIGNATURE_SCHEME = "ed25519-session-request-sha256-v1"

ED25519_PUBLIC_KEY_ALGORITHM = "ed25519"
PUBLIC_KEY_STATUS_ACTIVE = "active"
PUBLIC_KEY_STATUS_REVOKED = "revoked"
PUBLIC_KEY_STATUS_ROTATED = "rotated"
SUPPORTED_SESSION_REQUEST_ALGORITHMS = ("PRL_POUW", "RVN_KAWPOW")

C22_REGISTRY_UNAVAILABLE = "C22_REGISTRY_UNAVAILABLE"
C22_REGISTRY_SOURCE_NOT_AUTHORITATIVE = "C22_REGISTRY_SOURCE_NOT_AUTHORITATIVE"
C22_REGISTRY_STALE = "C22_REGISTRY_STALE"
C22_REGISTRY_REVOCATION_SOURCE_MISSING = "C22_REGISTRY_REVOCATION_SOURCE_MISSING"
C22_PUBLIC_KEY_UNKNOWN = "C22_PUBLIC_KEY_UNKNOWN"
C22_PUBLIC_KEY_PASSPORT_MISMATCH = "C22_PUBLIC_KEY_PASSPORT_MISMATCH"
C22_PUBLIC_KEY_DEVICE_MISMATCH = "C22_PUBLIC_KEY_DEVICE_MISMATCH"
C22_PUBLIC_KEY_REVOKED = "C22_PUBLIC_KEY_REVOKED"
C22_PUBLIC_KEY_ROTATED = "C22_PUBLIC_KEY_ROTATED"
C22_PUBLIC_KEY_NOT_YET_VALID = "C22_PUBLIC_KEY_NOT_YET_VALID"
C22_PUBLIC_KEY_EXPIRED = "C22_PUBLIC_KEY_EXPIRED"
C22_PUBLIC_KEY_ALGORITHM_UNSUPPORTED = "C22_PUBLIC_KEY_ALGORITHM_UNSUPPORTED"
C22_SESSION_PAYLOAD_VERSION_UNSUPPORTED = "C22_SESSION_PAYLOAD_VERSION_UNSUPPORTED"
C22_SESSION_REQUEST_HASH_MISMATCH = "C22_SESSION_REQUEST_HASH_MISMATCH"
C22_SESSION_REQUEST_SIGNATURE_SCHEME_UNSUPPORTED = (
    "C22_SESSION_REQUEST_SIGNATURE_SCHEME_UNSUPPORTED"
)
C22_SESSION_REQUEST_SIGNATURE_INVALID = "C22_SESSION_REQUEST_SIGNATURE_INVALID"
C22_SESSION_REQUEST_TIMESTAMP_STALE = "C22_SESSION_REQUEST_TIMESTAMP_STALE"
C22_SESSION_REQUEST_TIMESTAMP_IN_FUTURE = "C22_SESSION_REQUEST_TIMESTAMP_IN_FUTURE"
C22_SESSION_REQUEST_ALGORITHM_UNSUPPORTED = "C22_SESSION_REQUEST_ALGORITHM_UNSUPPORTED"
C22_SESSION_REPLAY_STORE_NOT_CONFIGURED = "C22_SESSION_REPLAY_STORE_NOT_CONFIGURED"
C22_SESSION_NONCE_REPLAYED = "C22_SESSION_NONCE_REPLAYED"
C22_SESSION_NONCE_STALE = "C22_SESSION_NONCE_STALE"

PublicKeyStatus = Literal["active", "revoked", "rotated"]

_PUBLIC_TEXT_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,191}$")
_PRIVATE_KEY_MATERIAL_PATTERN = re.compile(
    "|".join(
        (
            "-----BE" + "GIN [A-Z0-9 ]*PRIVATE " + "KEY-----",
            "OPENSSH PRIVATE " + "KEY",
            "PRIVATE " + "KEY",
        )
    ),
    re.IGNORECASE,
)
_RAW_TOKEN_MATERIAL_PATTERN = re.compile(
    "|".join(
        (
            "sk" + r"-[A-Za-z0-9_-]+",
            "ghp" + r"_[A-Za-z0-9_]+",
            "hf" + r"_[A-Za-z0-9_]+",
            "AK" + r"IA[A-Z0-9]{16}",
            "SECRET" + "=",
            "TOKEN" + "=",
        )
    )
)
_FORBIDDEN_RAW_RECORD_FIELDS = frozenset(
    {
        "ed25519_private_key",
        "mnemonic",
        "private_key",
        "private_key_pem",
        "raw_private_key",
        "raw_seed",
        "raw_signature_material",
        "raw_token",
        "secret",
        "seed",
        "seed_phrase",
        "signing_key",
        "token",
        "wallet_private_key",
    }
)


@dataclass(frozen=True, slots=True)
class PassportPublicKeyRecord:
    passport_id: str
    device_id: str
    key_id: str
    public_key_pem: str
    registry_revision: str
    registered_at: datetime
    not_before: datetime
    algorithm: str = ED25519_PUBLIC_KEY_ALGORITHM
    status: PublicKeyStatus = PUBLIC_KEY_STATUS_ACTIVE
    not_after: datetime | None = None
    revoked_at: datetime | None = None
    rotated_to_key_id: str | None = None
    revocation_source_ref: str | None = None

    def __post_init__(self) -> None:
        for field_name, value in (
            ("passport_id", self.passport_id),
            ("device_id", self.device_id),
            ("key_id", self.key_id),
            ("registry_revision", self.registry_revision),
        ):
            _validate_public_identifier(field_name, value)
        if self.algorithm != ED25519_PUBLIC_KEY_ALGORITHM:
            raise ValueError("passport public key algorithm must be ed25519")
        if self.status not in (
            PUBLIC_KEY_STATUS_ACTIVE,
            PUBLIC_KEY_STATUS_REVOKED,
            PUBLIC_KEY_STATUS_ROTATED,
        ):
            raise ValueError("passport public key status must be active, revoked, or rotated")
        _validate_public_key_pem(self.public_key_pem)
        validate_aware_timestamp("registered_at", self.registered_at)
        validate_aware_timestamp("not_before", self.not_before)
        if self.not_after is not None:
            validate_aware_timestamp("not_after", self.not_after)
            if self.not_after <= self.not_before:
                raise ValueError("not_after must be after not_before")
        if self.revoked_at is not None:
            validate_aware_timestamp("revoked_at", self.revoked_at)
        if self.rotated_to_key_id is not None:
            _validate_public_identifier("rotated_to_key_id", self.rotated_to_key_id)
            if self.rotated_to_key_id == self.key_id:
                raise ValueError("rotated_to_key_id must differ from key_id")
        if self.revocation_source_ref is not None:
            _validate_public_identifier("revocation_source_ref", self.revocation_source_ref)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> PassportPublicKeyRecord:
        forbidden = _FORBIDDEN_RAW_RECORD_FIELDS.intersection(payload)
        if forbidden:
            names = ", ".join(sorted(forbidden))
            raise ValueError(
                f"raw key material is forbidden in passport public key record: {names}"
            )
        return cls(
            passport_id=_required_str(payload, "passport_id"),
            device_id=_required_str(payload, "device_id"),
            key_id=_required_str(payload, "key_id"),
            public_key_pem=_required_public_key_pem(payload, "public_key_pem"),
            registry_revision=_required_revision(payload, "registry_revision"),
            registered_at=_required_datetime(payload, "registered_at"),
            not_before=_required_datetime(payload, "not_before"),
            algorithm=_required_str(payload, "algorithm"),
            status=_required_str(payload, "status"),  # type: ignore[arg-type]
            not_after=_optional_datetime(payload, "not_after"),
            revoked_at=_optional_datetime(payload, "revoked_at"),
            rotated_to_key_id=_optional_str(payload, "rotated_to_key_id"),
            revocation_source_ref=_optional_str(payload, "revocation_source_ref"),
        )

    def public_dict(self) -> dict[str, object]:
        return {
            "algorithm": self.algorithm,
            "device_id": self.device_id,
            "key_id": self.key_id,
            "not_after": _iso_or_none(self.not_after),
            "not_before": self.not_before.isoformat(),
            "passport_id": self.passport_id,
            "public_key_pem": self.public_key_pem,
            "registered_at": self.registered_at.isoformat(),
            "registry_revision": self.registry_revision,
            "revocation_source_ref": self.revocation_source_ref,
            "revoked_at": _iso_or_none(self.revoked_at),
            "rotated_to_key_id": self.rotated_to_key_id,
            "status": self.status,
        }

    def ed25519_public_key(self) -> Ed25519PublicKey:
        loaded = serialization.load_pem_public_key(self.public_key_pem.encode("ascii"))
        if not isinstance(loaded, Ed25519PublicKey):
            raise ValueError("public_key_pem must encode an Ed25519 public key")
        return loaded


@dataclass(frozen=True, slots=True)
class RegistrySnapshotMetadata:
    source_ref: str
    registry_revision: str
    authoritative: bool
    loaded_at: datetime
    expires_at: datetime
    revocation_source_ref: str | None
    available: bool = True
    unavailable_reason: str | None = None

    def __post_init__(self) -> None:
        _validate_public_identifier("source_ref", self.source_ref)
        _validate_public_identifier("registry_revision", self.registry_revision)
        if not isinstance(self.authoritative, bool):
            raise ValueError("authoritative must be a boolean")
        if not isinstance(self.available, bool):
            raise ValueError("available must be a boolean")
        validate_aware_timestamp("loaded_at", self.loaded_at)
        validate_aware_timestamp("expires_at", self.expires_at)
        if self.expires_at <= self.loaded_at:
            raise ValueError("expires_at must be after loaded_at")
        if self.revocation_source_ref is not None:
            _validate_public_identifier("revocation_source_ref", self.revocation_source_ref)
        if self.unavailable_reason is not None:
            _validate_public_identifier("unavailable_reason", self.unavailable_reason)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> RegistrySnapshotMetadata:
        return cls(
            source_ref=_required_str(payload, "source_ref"),
            registry_revision=_required_revision(payload, "registry_revision"),
            authoritative=_required_bool(payload, "authoritative"),
            loaded_at=_required_datetime(payload, "loaded_at"),
            expires_at=_required_datetime(payload, "expires_at"),
            revocation_source_ref=_optional_str(payload, "revocation_source_ref"),
            available=_optional_bool(payload, "available", default=True),
            unavailable_reason=_optional_str(payload, "unavailable_reason"),
        )

    def public_dict(self) -> dict[str, object]:
        return {
            "authoritative": self.authoritative,
            "available": self.available,
            "expires_at": self.expires_at.isoformat(),
            "loaded_at": self.loaded_at.isoformat(),
            "registry_revision": self.registry_revision,
            "revocation_source_ref": self.revocation_source_ref,
            "source_ref": self.source_ref,
            "unavailable_reason": self.unavailable_reason,
        }


@dataclass(frozen=True, slots=True)
class RegistryResolutionResult:
    accepted: bool
    reason_code: str | None = None
    record: PassportPublicKeyRecord | None = None
    registry_revision: str | None = None


class PassportPublicKeyRegistryProtocol(Protocol):
    def resolve(
        self,
        *,
        passport_id: str,
        device_id: str,
        key_id: str,
        observed_at: datetime,
    ) -> RegistryResolutionResult:
        ...


@dataclass(slots=True)
class InMemoryPassportPublicKeyRegistry:
    metadata: RegistrySnapshotMetadata
    records: tuple[PassportPublicKeyRecord, ...] = ()
    _by_key_id: dict[str, PassportPublicKeyRecord] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        if not isinstance(self.metadata, RegistrySnapshotMetadata):
            raise ValueError("metadata must be RegistrySnapshotMetadata")
        by_key_id: dict[str, PassportPublicKeyRecord] = {}
        for record in self.records:
            if record.key_id in by_key_id:
                raise ValueError("passport public key registry contains duplicate key_id")
            by_key_id[record.key_id] = record
        self._by_key_id = by_key_id

    def resolve(
        self,
        *,
        passport_id: str,
        device_id: str,
        key_id: str,
        observed_at: datetime,
    ) -> RegistryResolutionResult:
        validate_aware_timestamp("observed_at", observed_at)
        health = _metadata_health(self.metadata, observed_at)
        if health is not None:
            return RegistryResolutionResult(
                False,
                health,
                registry_revision=self.metadata.registry_revision,
            )
        if not self.records:
            return RegistryResolutionResult(
                False,
                C22_REGISTRY_UNAVAILABLE,
                registry_revision=self.metadata.registry_revision,
            )
        _validate_public_identifier("passport_id", passport_id)
        _validate_public_identifier("device_id", device_id)
        _validate_public_identifier("key_id", key_id)

        record = self._by_key_id.get(key_id)
        if record is None:
            return RegistryResolutionResult(
                False,
                C22_PUBLIC_KEY_UNKNOWN,
                registry_revision=self.metadata.registry_revision,
            )
        if record.passport_id != passport_id:
            return RegistryResolutionResult(
                False,
                C22_PUBLIC_KEY_PASSPORT_MISMATCH,
                record,
                self.metadata.registry_revision,
            )
        if record.device_id != device_id:
            return RegistryResolutionResult(
                False,
                C22_PUBLIC_KEY_DEVICE_MISMATCH,
                record,
                self.metadata.registry_revision,
            )
        if record.revocation_source_ref is None:
            return RegistryResolutionResult(
                False,
                C22_REGISTRY_REVOCATION_SOURCE_MISSING,
                record,
                self.metadata.registry_revision,
            )
        if record.status == PUBLIC_KEY_STATUS_REVOKED or record.revoked_at is not None:
            return RegistryResolutionResult(
                False,
                C22_PUBLIC_KEY_REVOKED,
                record,
                self.metadata.registry_revision,
            )
        if record.status == PUBLIC_KEY_STATUS_ROTATED or record.rotated_to_key_id is not None:
            return RegistryResolutionResult(
                False,
                C22_PUBLIC_KEY_ROTATED,
                record,
                self.metadata.registry_revision,
            )
        if observed_at < record.not_before:
            return RegistryResolutionResult(
                False,
                C22_PUBLIC_KEY_NOT_YET_VALID,
                record,
                self.metadata.registry_revision,
            )
        if record.not_after is not None and observed_at > record.not_after:
            return RegistryResolutionResult(
                False,
                C22_PUBLIC_KEY_EXPIRED,
                record,
                self.metadata.registry_revision,
            )
        if record.algorithm != ED25519_PUBLIC_KEY_ALGORITHM:
            return RegistryResolutionResult(
                False,
                C22_PUBLIC_KEY_ALGORITHM_UNSUPPORTED,
                record,
                self.metadata.registry_revision,
            )
        return RegistryResolutionResult(
            True,
            record=record,
            registry_revision=self.metadata.registry_revision,
        )


@dataclass(frozen=True, slots=True)
class LocalJsonPassportPublicKeyRegistry:
    path: Path

    def resolve(
        self,
        *,
        passport_id: str,
        device_id: str,
        key_id: str,
        observed_at: datetime,
    ) -> RegistryResolutionResult:
        try:
            registry = self.load()
        except (OSError, TypeError, ValueError, json.JSONDecodeError):
            return RegistryResolutionResult(False, C22_REGISTRY_UNAVAILABLE)
        return registry.resolve(
            passport_id=passport_id,
            device_id=device_id,
            key_id=key_id,
            observed_at=observed_at,
        )

    def load(self) -> InMemoryPassportPublicKeyRegistry:
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        if not isinstance(payload, Mapping):
            raise ValueError("local JSON registry payload must be an object")
        metadata_payload = payload.get("metadata")
        if not isinstance(metadata_payload, Mapping):
            raise ValueError("local JSON registry metadata must be an object")
        records_payload = payload.get("records")
        if not isinstance(records_payload, list):
            raise ValueError("local JSON registry records must be a list")
        return InMemoryPassportPublicKeyRegistry(
            metadata=RegistrySnapshotMetadata.from_mapping(metadata_payload),
            records=tuple(
                PassportPublicKeyRecord.from_mapping(record) for record in records_payload
            ),
        )


@dataclass(frozen=True, slots=True)
class MinerSessionRequestPayload:
    passport_id: str
    device_id: str
    session_nonce: str
    requested_algorithm: str
    requested_pool_id: str
    timestamp: str
    policy_version: str
    payload_version: str = SESSION_REQUEST_PAYLOAD_VERSION

    def __post_init__(self) -> None:
        if self.payload_version != SESSION_REQUEST_PAYLOAD_VERSION:
            raise ValueError(C22_SESSION_PAYLOAD_VERSION_UNSUPPORTED)
        for field_name, value in (
            ("passport_id", self.passport_id),
            ("device_id", self.device_id),
            ("session_nonce", self.session_nonce),
            ("requested_pool_id", self.requested_pool_id),
            ("policy_version", self.policy_version),
            ("payload_version", self.payload_version),
        ):
            _validate_public_identifier(field_name, value)
        _validate_public_identifier("requested_algorithm", self.requested_algorithm)
        if self.requested_algorithm not in SUPPORTED_SESSION_REQUEST_ALGORITHMS:
            raise ValueError(C22_SESSION_REQUEST_ALGORITHM_UNSUPPORTED)
        _coerce_datetime(self.timestamp, "timestamp")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> MinerSessionRequestPayload:
        missing = sorted(set(SESSION_REQUEST_FIELDS) - set(payload))
        extra = sorted(set(payload) - set(SESSION_REQUEST_FIELDS))
        if missing:
            raise ValueError(f"missing_session_request_fields:{','.join(missing)}")
        if extra:
            raise ValueError(f"unexpected_session_request_fields:{','.join(extra)}")
        return cls(
            passport_id=_required_str(payload, "passport_id"),
            device_id=_required_str(payload, "device_id"),
            session_nonce=_required_str(payload, "session_nonce"),
            requested_algorithm=_required_str(payload, "requested_algorithm"),
            requested_pool_id=_required_str(payload, "requested_pool_id"),
            timestamp=_required_str(payload, "timestamp"),
            policy_version=_required_str(payload, "policy_version"),
            payload_version=_required_str(payload, "payload_version"),
        )

    @property
    def timestamp_at(self) -> datetime:
        return _coerce_datetime(self.timestamp, "timestamp")

    def canonical_fields(self) -> dict[str, str]:
        return {
            "passport_id": self.passport_id,
            "device_id": self.device_id,
            "session_nonce": self.session_nonce,
            "requested_algorithm": self.requested_algorithm,
            "requested_pool_id": self.requested_pool_id,
            "timestamp": self.timestamp,
            "policy_version": self.policy_version,
            "payload_version": self.payload_version,
        }


@dataclass(frozen=True, slots=True)
class MinerSessionRequestSignature:
    key_id: str
    signature: str
    scheme: str = SESSION_REQUEST_SIGNATURE_SCHEME

    def __post_init__(self) -> None:
        _validate_public_identifier("key_id", self.key_id)
        _validate_public_identifier("scheme", self.scheme)
        _validate_signature_b64(self.signature)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> MinerSessionRequestSignature:
        return cls(
            key_id=_required_str(payload, "key_id"),
            signature=_required_str(payload, "signature"),
            scheme=_required_str(payload, "scheme"),
        )


@dataclass(frozen=True, slots=True)
class SessionRequestVerificationPolicy:
    max_request_age: timedelta = timedelta(minutes=10)
    max_future_skew: timedelta = timedelta(seconds=60)

    def __post_init__(self) -> None:
        if self.max_request_age.total_seconds() <= 0:
            raise ValueError("max_request_age must be positive")
        if self.max_future_skew.total_seconds() < 0:
            raise ValueError("max_future_skew must be non-negative")


DEFAULT_SESSION_REQUEST_VERIFICATION_POLICY = SessionRequestVerificationPolicy()


@dataclass(frozen=True, slots=True)
class SessionNonceReplayResult:
    accepted: bool
    reason_code: str | None = None


class SessionNonceReplayStoreProtocol(Protocol):
    def mark_seen(
        self,
        payload: MinerSessionRequestPayload,
        *,
        observed_at: datetime,
    ) -> SessionNonceReplayResult:
        ...


@dataclass(slots=True)
class InMemorySessionNonceReplayStore:
    seen_nonces: set[tuple[str, str, str]] = field(default_factory=set)
    latest_timestamp: dict[tuple[str, str], datetime] = field(default_factory=dict)

    def mark_seen(
        self,
        payload: MinerSessionRequestPayload,
        *,
        observed_at: datetime,
    ) -> SessionNonceReplayResult:
        validate_aware_timestamp("observed_at", observed_at)
        scope = (payload.passport_id, payload.device_id)
        timestamp = payload.timestamp_at
        latest = self.latest_timestamp.get(scope)
        if latest is not None and timestamp < latest:
            return SessionNonceReplayResult(False, C22_SESSION_NONCE_STALE)

        nonce_key = (*scope, payload.session_nonce)
        if nonce_key in self.seen_nonces:
            return SessionNonceReplayResult(False, C22_SESSION_NONCE_REPLAYED)

        self.seen_nonces.add(nonce_key)
        if latest is None or timestamp > latest:
            self.latest_timestamp[scope] = timestamp
        return SessionNonceReplayResult(True)


@dataclass(frozen=True, slots=True)
class MinerSessionRequestVerificationResult:
    accepted: bool
    reason_code: str | None
    passport_id: str | None = None
    device_id: str | None = None
    key_id: str | None = None
    payload_hash: str | None = None
    registry_revision: str | None = None


def canonical_session_request_payload_json(
    payload: MinerSessionRequestPayload | Mapping[str, object],
) -> str:
    active_payload = (
        payload
        if isinstance(payload, MinerSessionRequestPayload)
        else MinerSessionRequestPayload.from_mapping(payload)
    )
    return json.dumps(
        active_payload.canonical_fields(),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def session_request_payload_hash(payload: MinerSessionRequestPayload | Mapping[str, object]) -> str:
    canonical = canonical_session_request_payload_json(payload).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


def session_request_signature_message(payload_hash: str) -> bytes:
    validate_sha256(payload_hash, field_name="payload_hash")
    return SESSION_REQUEST_SIGNATURE_DOMAIN + payload_hash.encode("ascii")


def verify_authoritative_miner_session_request(
    payload: MinerSessionRequestPayload | Mapping[str, object],
    *,
    payload_hash: str,
    signature: MinerSessionRequestSignature | Mapping[str, object],
    registry: PassportPublicKeyRegistryProtocol,
    replay_store: SessionNonceReplayStoreProtocol | None,
    observed_at: datetime,
    policy: SessionRequestVerificationPolicy | None = None,
) -> MinerSessionRequestVerificationResult:
    validate_aware_timestamp("observed_at", observed_at)
    active_policy = policy or DEFAULT_SESSION_REQUEST_VERIFICATION_POLICY
    try:
        active_payload = (
            payload
            if isinstance(payload, MinerSessionRequestPayload)
            else MinerSessionRequestPayload.from_mapping(payload)
        )
        active_signature = (
            signature
            if isinstance(signature, MinerSessionRequestSignature)
            else MinerSessionRequestSignature.from_mapping(signature)
        )
        expected_hash = session_request_payload_hash(active_payload)
    except ValueError as exc:
        return MinerSessionRequestVerificationResult(False, str(exc))

    base_result = {
        "passport_id": active_payload.passport_id,
        "device_id": active_payload.device_id,
        "key_id": active_signature.key_id,
        "payload_hash": payload_hash,
    }
    if replay_store is None:
        return MinerSessionRequestVerificationResult(
            False,
            C22_SESSION_REPLAY_STORE_NOT_CONFIGURED,
            **base_result,
        )
    if payload_hash != expected_hash:
        return MinerSessionRequestVerificationResult(
            False,
            C22_SESSION_REQUEST_HASH_MISMATCH,
            **base_result,
        )
    if active_signature.scheme != SESSION_REQUEST_SIGNATURE_SCHEME:
        return MinerSessionRequestVerificationResult(
            False,
            C22_SESSION_REQUEST_SIGNATURE_SCHEME_UNSUPPORTED,
            **base_result,
        )

    timestamp_result = _validate_request_timestamp(
        active_payload.timestamp_at,
        observed_at=observed_at,
        policy=active_policy,
    )
    if timestamp_result is not None:
        return MinerSessionRequestVerificationResult(False, timestamp_result, **base_result)

    resolution = registry.resolve(
        passport_id=active_payload.passport_id,
        device_id=active_payload.device_id,
        key_id=active_signature.key_id,
        observed_at=observed_at,
    )
    if not resolution.accepted or resolution.record is None:
        return MinerSessionRequestVerificationResult(
            False,
            resolution.reason_code,
            registry_revision=resolution.registry_revision,
            **base_result,
        )
    record = resolution.record
    record_revision = resolution.registry_revision or record.registry_revision
    if active_payload.timestamp_at < record.not_before:
        return MinerSessionRequestVerificationResult(
            False,
            C22_PUBLIC_KEY_NOT_YET_VALID,
            registry_revision=record_revision,
            **base_result,
        )
    if record.not_after is not None and active_payload.timestamp_at > record.not_after:
        return MinerSessionRequestVerificationResult(
            False,
            C22_PUBLIC_KEY_EXPIRED,
            registry_revision=record_revision,
            **base_result,
        )

    try:
        record.ed25519_public_key().verify(
            _decode_ed25519_signature(active_signature.signature),
            session_request_signature_message(payload_hash),
        )
    except (InvalidSignature, ValueError):
        return MinerSessionRequestVerificationResult(
            False,
            C22_SESSION_REQUEST_SIGNATURE_INVALID,
            registry_revision=record_revision,
            **base_result,
        )

    replay = replay_store.mark_seen(active_payload, observed_at=observed_at)
    if not replay.accepted:
        return MinerSessionRequestVerificationResult(
            False,
            replay.reason_code,
            registry_revision=record_revision,
            **base_result,
        )

    return MinerSessionRequestVerificationResult(
        True,
        None,
        registry_revision=record_revision,
        **base_result,
    )


def _metadata_health(metadata: RegistrySnapshotMetadata, observed_at: datetime) -> str | None:
    if not metadata.available:
        return C22_REGISTRY_UNAVAILABLE
    if not metadata.authoritative:
        return C22_REGISTRY_SOURCE_NOT_AUTHORITATIVE
    if metadata.revocation_source_ref is None:
        return C22_REGISTRY_REVOCATION_SOURCE_MISSING
    if observed_at > metadata.expires_at:
        return C22_REGISTRY_STALE
    return None


def _validate_request_timestamp(
    timestamp: datetime,
    *,
    observed_at: datetime,
    policy: SessionRequestVerificationPolicy,
) -> str | None:
    if timestamp > observed_at + policy.max_future_skew:
        return C22_SESSION_REQUEST_TIMESTAMP_IN_FUTURE
    if observed_at - timestamp > policy.max_request_age:
        return C22_SESSION_REQUEST_TIMESTAMP_STALE
    return None


def _decode_ed25519_signature(signature_b64: str) -> bytes:
    try:
        decoded = base64.b64decode(signature_b64.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError) as exc:
        raise ValueError("signature must be strict base64") from exc
    if len(decoded) != 64:
        raise ValueError("signature must decode to a 64-byte Ed25519 signature")
    return decoded


def _validate_signature_b64(value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("signature must be a non-empty string")
    ensure_no_raw_secret(value, field_name="signature")
    _decode_ed25519_signature(value)


def _validate_public_identifier(field_name: str, value: str) -> None:
    _validate_public_text(field_name, value)
    if not _PUBLIC_TEXT_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} is malformed")


def _validate_public_text(field_name: str, value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    ensure_no_raw_secret(value, field_name=field_name)
    if _PRIVATE_KEY_MATERIAL_PATTERN.search(value):
        raise ValueError(f"{field_name} must not contain private key material")


def _validate_public_key_pem(value: str) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("public_key_pem must be a non-empty PEM string")
    if _PRIVATE_KEY_MATERIAL_PATTERN.search(value):
        raise ValueError("public_key_pem must not contain private key material")
    if _RAW_TOKEN_MATERIAL_PATTERN.search(value):
        raise ValueError("public_key_pem must not contain raw token material")
    try:
        loaded = serialization.load_pem_public_key(value.encode("ascii"))
    except (TypeError, ValueError) as exc:
        raise ValueError("public_key_pem must encode a valid public key") from exc
    if not isinstance(loaded, Ed25519PublicKey):
        raise ValueError("public_key_pem must encode an Ed25519 public key")


def _required_str(payload: Mapping[str, object], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    _validate_public_text(field_name, value)
    return value


def _required_public_key_pem(payload: Mapping[str, object], field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    return value


def _optional_str(payload: Mapping[str, object], field_name: str) -> str | None:
    value = payload.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string when provided")
    _validate_public_text(field_name, value)
    return value


def _required_revision(payload: Mapping[str, object], field_name: str) -> str:
    value = payload.get(field_name)
    if isinstance(value, bool) or value is None:
        raise ValueError(f"{field_name} must be a string or integer revision")
    if not isinstance(value, str | int):
        raise ValueError(f"{field_name} must be a string or integer revision")
    revision = str(value)
    _validate_public_identifier(field_name, revision)
    return revision


def _required_bool(payload: Mapping[str, object], field_name: str) -> bool:
    value = payload.get(field_name)
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be a boolean")
    return value


def _optional_bool(
    payload: Mapping[str, object],
    field_name: str,
    *,
    default: bool,
) -> bool:
    value = payload.get(field_name, default)
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be a boolean")
    return value


def _required_datetime(payload: Mapping[str, object], field_name: str) -> datetime:
    value = payload.get(field_name)
    if value is None:
        raise ValueError(f"{field_name} must be a datetime")
    return _coerce_datetime(value, field_name)


def _optional_datetime(payload: Mapping[str, object], field_name: str) -> datetime | None:
    value = payload.get(field_name)
    if value is None:
        return None
    return _coerce_datetime(value, field_name)


def _coerce_datetime(value: object, field_name: str) -> datetime:
    if isinstance(value, datetime):
        validate_aware_timestamp(field_name, value)
        return value.astimezone(UTC)
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"{field_name} must be an ISO datetime") from exc
        validate_aware_timestamp(field_name, parsed)
        return parsed.astimezone(UTC)
    raise ValueError(f"{field_name} must be a datetime")


def _iso_or_none(value: datetime | None) -> str | None:
    return value.isoformat() if value is not None else None

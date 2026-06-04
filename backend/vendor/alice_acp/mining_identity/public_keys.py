from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Literal, Protocol

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from alice_acp.evidence.types import ensure_no_raw_secret, validate_aware_timestamp

PublicKeyAlgorithm = Literal["ed25519"]
PublicKeyStatus = Literal["active", "revoked", "rotated"]

ED25519_ALGORITHM: PublicKeyAlgorithm = "ed25519"
PUBLIC_KEY_ACTIVE: PublicKeyStatus = "active"
PUBLIC_KEY_REVOKED: PublicKeyStatus = "revoked"
PUBLIC_KEY_ROTATED: PublicKeyStatus = "rotated"

PUBLIC_KEY_REGISTRY_NOT_CONFIGURED = "PUBLIC_KEY_REGISTRY_NOT_CONFIGURED"
PUBLIC_KEY_UNKNOWN = "PUBLIC_KEY_UNKNOWN"
PUBLIC_KEY_SOURCE_NOT_AUTHORITATIVE = "PUBLIC_KEY_SOURCE_NOT_AUTHORITATIVE"
PUBLIC_KEY_PASSPORT_MISMATCH = "PUBLIC_KEY_PASSPORT_MISMATCH"
PUBLIC_KEY_DEVICE_MISMATCH = "PUBLIC_KEY_DEVICE_MISMATCH"
PUBLIC_KEY_REVOKED_REASON = "PUBLIC_KEY_REVOKED"
PUBLIC_KEY_ROTATED_REASON = "PUBLIC_KEY_ROTATED"
PUBLIC_KEY_NOT_YET_VALID = "PUBLIC_KEY_NOT_YET_VALID"
PUBLIC_KEY_EXPIRED = "PUBLIC_KEY_EXPIRED"
PUBLIC_KEY_ALGORITHM_UNSUPPORTED = "PUBLIC_KEY_ALGORITHM_UNSUPPORTED"

_PUBLIC_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,191}$")
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
_FORBIDDEN_RAW_KEY_FIELDS = frozenset(
    {
        "ed25519_private_key",
        "private_key",
        "private_key_pem",
        "raw_private_key",
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
class PublicKeyRegistrySource:
    source_ref: str
    authoritative: bool
    imported_at: datetime | None = None
    metadata: Mapping[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        _validate_public_text("source_ref", self.source_ref)
        if not isinstance(self.authoritative, bool):
            raise ValueError("source authoritative flag must be a boolean")
        if self.imported_at is not None:
            validate_aware_timestamp("imported_at", self.imported_at)
        if not isinstance(self.metadata, Mapping):
            raise ValueError("source metadata must be a mapping")
        for key, value in self.metadata.items():
            _validate_public_text("source metadata key", key)
            _validate_public_text("source metadata value", value)

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> PublicKeyRegistrySource:
        forbidden = _FORBIDDEN_RAW_KEY_FIELDS.intersection(payload)
        if forbidden:
            names = ", ".join(sorted(forbidden))
            raise ValueError(f"raw key material is forbidden in source metadata: {names}")
        metadata = payload.get("metadata", {})
        if not isinstance(metadata, Mapping):
            raise ValueError("source metadata must be a mapping")
        return cls(
            source_ref=_required_str(payload, "source_ref"),
            authoritative=_required_bool(payload, "authoritative"),
            imported_at=_optional_datetime(payload, "imported_at"),
            metadata={
                _required_mapping_str(key, "source metadata key"): _required_mapping_str(
                    value,
                    "source metadata value",
                )
                for key, value in metadata.items()
            },
        )


@dataclass(frozen=True, slots=True)
class PublicKeyRegistryEntry:
    passport_id: str
    device_id: str
    key_id: str
    algorithm: PublicKeyAlgorithm
    public_key_pem: str
    status: PublicKeyStatus
    not_before: datetime
    not_after: datetime | None
    registry_revision: int
    source_metadata: PublicKeyRegistrySource

    def __post_init__(self) -> None:
        _validate_public_identifier("passport_id", self.passport_id)
        _validate_public_identifier("device_id", self.device_id)
        _validate_public_identifier("key_id", self.key_id)
        if self.algorithm != ED25519_ALGORITHM:
            raise ValueError("public key algorithm must be ed25519")
        if self.status not in (PUBLIC_KEY_ACTIVE, PUBLIC_KEY_REVOKED, PUBLIC_KEY_ROTATED):
            raise ValueError("public key status must be active, revoked, or rotated")
        _validate_public_key_pem(self.public_key_pem)
        validate_aware_timestamp("not_before", self.not_before)
        if self.not_after is not None:
            validate_aware_timestamp("not_after", self.not_after)
            if self.not_after <= self.not_before:
                raise ValueError("not_after must be after not_before")
        if (
            not isinstance(self.registry_revision, int)
            or isinstance(self.registry_revision, bool)
            or self.registry_revision <= 0
        ):
            raise ValueError("registry_revision must be a positive integer")
        if not isinstance(self.source_metadata, PublicKeyRegistrySource):
            raise ValueError("source_metadata must be a PublicKeyRegistrySource")

    @classmethod
    def from_mapping(cls, payload: Mapping[str, object]) -> PublicKeyRegistryEntry:
        forbidden = _FORBIDDEN_RAW_KEY_FIELDS.intersection(payload)
        if forbidden:
            names = ", ".join(sorted(forbidden))
            raise ValueError(f"raw key material is forbidden in public key registry: {names}")
        source = payload.get("source_metadata")
        if isinstance(source, PublicKeyRegistrySource):
            source_metadata = source
        elif isinstance(source, Mapping):
            source_metadata = PublicKeyRegistrySource.from_mapping(source)
        else:
            raise ValueError("source_metadata must be provided")
        return cls(
            passport_id=_required_str(payload, "passport_id"),
            device_id=_required_str(payload, "device_id"),
            key_id=_required_str(payload, "key_id"),
            algorithm=_required_str(payload, "algorithm"),  # type: ignore[arg-type]
            public_key_pem=_required_str(payload, "public_key_pem"),
            status=_required_str(payload, "status"),  # type: ignore[arg-type]
            not_before=_required_datetime(payload, "not_before"),
            not_after=_optional_datetime(payload, "not_after"),
            registry_revision=_required_int(payload, "registry_revision"),
            source_metadata=source_metadata,
        )

    @property
    def signer_ref(self) -> str:
        return public_key_signer_ref(
            passport_id=self.passport_id,
            device_id=self.device_id,
            key_id=self.key_id,
        )

    def ed25519_public_key(self) -> Ed25519PublicKey:
        loaded = serialization.load_pem_public_key(self.public_key_pem.encode("ascii"))
        if not isinstance(loaded, Ed25519PublicKey):
            raise ValueError("public_key_pem must encode an Ed25519 public key")
        return loaded

    def public_dict(self) -> dict[str, object]:
        return {
            "passport_id": self.passport_id,
            "device_id": self.device_id,
            "key_id": self.key_id,
            "algorithm": self.algorithm,
            "public_key_pem": self.public_key_pem,
            "status": self.status,
            "not_before": self.not_before.isoformat(),
            "not_after": self.not_after.isoformat() if self.not_after is not None else None,
            "registry_revision": self.registry_revision,
            "source_metadata": {
                "source_ref": self.source_metadata.source_ref,
                "authoritative": self.source_metadata.authoritative,
                "imported_at": (
                    self.source_metadata.imported_at.isoformat()
                    if self.source_metadata.imported_at is not None
                    else None
                ),
                "metadata": dict(self.source_metadata.metadata),
            },
        }


@dataclass(frozen=True, slots=True)
class PublicKeyResolutionResult:
    accepted: bool
    reason_code: str | None = None
    entry: PublicKeyRegistryEntry | None = None


class PublicKeyRegistryProtocol(Protocol):
    @property
    def configured(self) -> bool:
        ...

    def resolve(
        self,
        *,
        key_id: str,
        passport_id: str,
        device_id: str,
        observed_at: datetime,
    ) -> PublicKeyResolutionResult:
        ...


@dataclass(slots=True)
class LocalPublicKeyRegistry:
    records: tuple[PublicKeyRegistryEntry, ...] = ()
    require_authoritative_source: bool = True
    _by_key_id: dict[str, PublicKeyRegistryEntry] = field(init=False, repr=False)

    def __post_init__(self) -> None:
        by_key_id: dict[str, PublicKeyRegistryEntry] = {}
        for record in self.records:
            if record.key_id in by_key_id:
                raise ValueError("public key registry contains duplicate key_id")
            by_key_id[record.key_id] = record
        self._by_key_id = by_key_id

    @property
    def configured(self) -> bool:
        return bool(self.records)

    @classmethod
    def from_mappings(
        cls,
        payloads: tuple[Mapping[str, object], ...],
        *,
        require_authoritative_source: bool = True,
    ) -> LocalPublicKeyRegistry:
        return cls(
            records=tuple(PublicKeyRegistryEntry.from_mapping(payload) for payload in payloads),
            require_authoritative_source=require_authoritative_source,
        )

    def resolve(
        self,
        *,
        key_id: str,
        passport_id: str,
        device_id: str,
        observed_at: datetime,
    ) -> PublicKeyResolutionResult:
        validate_aware_timestamp("observed_at", observed_at)
        if not self.configured:
            return PublicKeyResolutionResult(False, PUBLIC_KEY_REGISTRY_NOT_CONFIGURED)
        _validate_public_identifier("key_id", key_id)
        _validate_public_identifier("passport_id", passport_id)
        _validate_public_identifier("device_id", device_id)

        entry = self._by_key_id.get(key_id)
        if entry is None:
            return PublicKeyResolutionResult(False, PUBLIC_KEY_UNKNOWN)
        if entry.passport_id != passport_id:
            return PublicKeyResolutionResult(False, PUBLIC_KEY_PASSPORT_MISMATCH, entry)
        if entry.device_id != device_id:
            return PublicKeyResolutionResult(False, PUBLIC_KEY_DEVICE_MISMATCH, entry)
        if self.require_authoritative_source and not entry.source_metadata.authoritative:
            return PublicKeyResolutionResult(False, PUBLIC_KEY_SOURCE_NOT_AUTHORITATIVE, entry)
        if entry.status == PUBLIC_KEY_REVOKED:
            return PublicKeyResolutionResult(False, PUBLIC_KEY_REVOKED_REASON, entry)
        if entry.status == PUBLIC_KEY_ROTATED:
            return PublicKeyResolutionResult(False, PUBLIC_KEY_ROTATED_REASON, entry)
        if observed_at < entry.not_before:
            return PublicKeyResolutionResult(False, PUBLIC_KEY_NOT_YET_VALID, entry)
        if entry.not_after is not None and observed_at > entry.not_after:
            return PublicKeyResolutionResult(False, PUBLIC_KEY_EXPIRED, entry)
        if entry.algorithm != ED25519_ALGORITHM:
            return PublicKeyResolutionResult(False, PUBLIC_KEY_ALGORITHM_UNSUPPORTED, entry)
        return PublicKeyResolutionResult(True, entry=entry)


@dataclass(frozen=True, slots=True)
class ProductionPublicKeyRegistryAdapter:
    """Fail-closed placeholder until a production registry backend is configured."""

    @property
    def configured(self) -> bool:
        return False

    def resolve(
        self,
        *,
        key_id: str,
        passport_id: str,
        device_id: str,
        observed_at: datetime,
    ) -> PublicKeyResolutionResult:
        validate_aware_timestamp("observed_at", observed_at)
        _validate_public_text("key_id", key_id)
        _validate_public_text("passport_id", passport_id)
        _validate_public_text("device_id", device_id)
        return PublicKeyResolutionResult(False, PUBLIC_KEY_REGISTRY_NOT_CONFIGURED)


def public_key_signer_ref(*, passport_id: str, device_id: str, key_id: str) -> str:
    _validate_public_identifier("passport_id", passport_id)
    _validate_public_identifier("device_id", device_id)
    _validate_public_identifier("key_id", key_id)
    return f"mining-public-key://{passport_id}/{device_id}/{key_id}"


def _validate_public_identifier(field_name: str, value: str) -> None:
    _validate_public_text(field_name, value)
    if not _PUBLIC_ID_PATTERN.fullmatch(value):
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
    if field_name != "public_key_pem":
        _validate_public_text(field_name, value)
    return value


def _required_mapping_str(value: object, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field_name} must be a non-empty string")
    _validate_public_text(field_name, value)
    return value


def _required_bool(payload: Mapping[str, object], field_name: str) -> bool:
    value = payload.get(field_name)
    if not isinstance(value, bool):
        raise ValueError(f"{field_name} must be a boolean")
    return value


def _required_int(payload: Mapping[str, object], field_name: str) -> int:
    value = payload.get(field_name)
    if not isinstance(value, int) or isinstance(value, bool):
        raise ValueError(f"{field_name} must be an integer")
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
        return value
    if isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError as exc:
            raise ValueError(f"{field_name} must be an ISO datetime") from exc
        validate_aware_timestamp(field_name, parsed)
        return parsed
    raise ValueError(f"{field_name} must be a datetime")

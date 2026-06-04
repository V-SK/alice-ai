from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime

from cryptography.exceptions import InvalidSignature

from alice_acp.evidence.types import ensure_no_raw_secret, validate_aware_timestamp, validate_sha256
from alice_acp.mining_identity.public_keys import (
    PUBLIC_KEY_EXPIRED,
    PUBLIC_KEY_NOT_YET_VALID,
    PublicKeyRegistryProtocol,
)
from alice_acp.mining_session.contracts import validate_signed_mining_session
from alice_acp.mining_session.types import SignedMiningSession

ED25519_SESSION_SIGNATURE_SCHEME = "alice_ed25519_session_v1"
ED25519_SESSION_SIGNATURE_DOMAIN = "alice-acp:mining-session:ed25519:v1"

SESSION_KEY_ID_MISSING = "SESSION_KEY_ID_MISSING"
SESSION_KEY_ID_MISMATCH = "SESSION_KEY_ID_MISMATCH"
SESSION_SIGNATURE_MISSING = "SESSION_SIGNATURE_MISSING"
SESSION_SIGNATURE_SCHEME_UNSUPPORTED = "SESSION_SIGNATURE_SCHEME_UNSUPPORTED"
SESSION_SIGNATURE_INVALID = "SESSION_SIGNATURE_INVALID"


@dataclass(frozen=True, slots=True)
class SessionSignatureVerificationResult:
    accepted: bool
    session_id: str
    reason_code: str | None = None
    key_id: str | None = None
    registry_revision: int | None = None

    @property
    def rewardable(self) -> bool:
        return self.accepted


def domain_separated_session_message(payload_digest: str) -> bytes:
    validate_sha256(payload_digest, field_name="payload_digest")
    return (
        f"{ED25519_SESSION_SIGNATURE_DOMAIN}\n"
        f"payload_sha256={payload_digest}"
    ).encode("ascii")


def verify_ed25519_signed_mining_session(
    session: SignedMiningSession,
    *,
    registry: PublicKeyRegistryProtocol,
    observed_at: datetime,
    expected_device_id: str,
    expected_collection_address: str | None = None,
    expected_worker_id: str | None = None,
    expected_passport_id: str | None = None,
    expected_attempt_id: str | None = None,
    expected_pool_id: str | None = None,
) -> SessionSignatureVerificationResult:
    validate_aware_timestamp("observed_at", observed_at)
    ensure_no_raw_secret(expected_device_id, field_name="expected_device_id")

    if not registry.configured:
        return _rejected(session, "PUBLIC_KEY_REGISTRY_NOT_CONFIGURED")

    envelope = session.signature
    if envelope.key_id is None:
        return _rejected(session, SESSION_KEY_ID_MISSING)
    if envelope.signature_b64 is None:
        return _rejected(session, SESSION_SIGNATURE_MISSING, key_id=envelope.key_id)
    if envelope.signature_scheme != ED25519_SESSION_SIGNATURE_SCHEME:
        return _rejected(
            session,
            SESSION_SIGNATURE_SCHEME_UNSUPPORTED,
            key_id=envelope.key_id,
        )

    session_contract_result = validate_signed_mining_session(
        session,
        observed_at=observed_at,
        expected_collection_address=expected_collection_address,
        expected_worker_id=expected_worker_id,
        expected_passport_id=expected_passport_id,
        expected_attempt_id=expected_attempt_id,
        expected_pool_id=expected_pool_id,
    )
    if not session_contract_result.accepted:
        return _rejected(session, session_contract_result.reason_code, key_id=envelope.key_id)

    resolution = registry.resolve(
        key_id=envelope.key_id,
        passport_id=session.passport_id,
        device_id=expected_device_id,
        observed_at=observed_at,
    )
    if not resolution.accepted or resolution.entry is None:
        return _rejected(session, resolution.reason_code, key_id=envelope.key_id)

    entry = resolution.entry
    if envelope.signer_ref != entry.signer_ref:
        return _rejected(
            session,
            SESSION_KEY_ID_MISMATCH,
            key_id=envelope.key_id,
            registry_revision=entry.registry_revision,
        )
    if envelope.signed_at < entry.not_before:
        return _rejected(
            session,
            PUBLIC_KEY_NOT_YET_VALID,
            key_id=envelope.key_id,
            registry_revision=entry.registry_revision,
        )
    if entry.not_after is not None and envelope.signed_at > entry.not_after:
        return _rejected(
            session,
            PUBLIC_KEY_EXPIRED,
            key_id=envelope.key_id,
            registry_revision=entry.registry_revision,
        )

    try:
        signature = _decode_ed25519_signature(envelope.signature_b64)
        entry.ed25519_public_key().verify(
            signature,
            domain_separated_session_message(envelope.payload_digest),
        )
    except (InvalidSignature, ValueError):
        return _rejected(
            session,
            SESSION_SIGNATURE_INVALID,
            key_id=envelope.key_id,
            registry_revision=entry.registry_revision,
        )

    return SessionSignatureVerificationResult(
        accepted=True,
        session_id=session.session_id,
        key_id=envelope.key_id,
        registry_revision=entry.registry_revision,
    )


def _decode_ed25519_signature(signature_b64: str) -> bytes:
    ensure_no_raw_secret(signature_b64, field_name="signature_b64")
    try:
        decoded = base64.b64decode(signature_b64.encode("ascii"), validate=True)
    except (binascii.Error, UnicodeEncodeError) as exc:
        raise ValueError("signature_b64 must be strict base64") from exc
    if len(decoded) != 64:
        raise ValueError("signature_b64 must decode to a 64-byte Ed25519 signature")
    return decoded


def _rejected(
    session: SignedMiningSession,
    reason_code: str | None,
    *,
    key_id: str | None = None,
    registry_revision: int | None = None,
) -> SessionSignatureVerificationResult:
    return SessionSignatureVerificationResult(
        accepted=False,
        session_id=session.session_id,
        reason_code=reason_code,
        key_id=key_id,
        registry_revision=registry_revision,
    )

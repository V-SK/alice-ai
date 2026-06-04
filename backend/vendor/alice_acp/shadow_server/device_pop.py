"""Device proof-of-possession + device registry for shadow session issuance.

Phase E (C2): ``ShadowRewardLedger.issue_session`` historically trusted the
``passport_id`` / ``device_id`` request strings verbatim, so anyone could mint a
session for any identity (sybil / impersonation).  This module adds a
device-signed *proof-of-possession* (PoP) over a server-issued nonce plus a
``DeviceRegistry`` Protocol seam that the ledger consults *fail-closed*: an
unregistered identity, an absent PoP, or a bad signature is REJECTED.

CREDIT-ONLY: nothing here sets any reward/payout/chain flag.  It only adds
admission rigor in front of the existing (still intact) ledger guards.

The Ed25519 verification idiom (domain-separated message, strict 64-byte base64
signature, ``Ed25519PublicKey.verify``) deliberately mirrors the existing
``mining_identity`` primitives (``session_verify`` /
``public_key_registry``).  Those primitives are bound to the heavyweight
``SignedMiningSession`` / mining-only ``MinerSessionRequestPayload`` contracts
(RVN/PRL pool fields, mining algorithms) and are therefore *not* a clean fit for
the shadow server's generic session-issuance path, which also issues AI
inference sessions.  We reuse the cryptographic *pattern* rather than forcing
mining-only contract fields onto the shadow path.
"""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from datetime import datetime
from typing import Protocol

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from alice_acp.evidence.types import validate_aware_timestamp

# Domain separation tag — keeps a device-PoP signature from ever being valid as
# any other Ed25519 message in the system (session-request, mining-session, ...).
DEVICE_POP_SIGNATURE_DOMAIN = b"alice-acp:shadow-session:device-pop:v1\n"
DEVICE_POP_SIGNATURE_SCHEME = "ed25519-shadow-device-pop-v1"

# Phase H_a: a DISTINCT domain for enrollment (``POST /device/register``), so a
# captured issuance PoP can never be replayed as an enrollment PoP or vice versa.
DEVICE_ENROLLMENT_SIGNATURE_DOMAIN = b"alice-acp:shadow-device:enrollment-pop:v1\n"
DEVICE_ENROLLMENT_SIGNATURE_SCHEME = "ed25519-shadow-device-enrollment-v1"

DEVICE_STATUS_ACTIVE = "active"
DEVICE_STATUS_REVOKED = "revoked"

# Reason codes (returned by issue_session via SessionIssueResult.reason_code).
REASON_DEVICE_POP_REQUIRED = "device_pop_required"
REASON_DEVICE_POP_SCHEME_UNSUPPORTED = "device_pop_scheme_unsupported"
REASON_DEVICE_POP_INVALID = "device_pop_signature_invalid"
REASON_DEVICE_POP_KEY_MALFORMED = "device_pop_public_key_malformed"
REASON_DEVICE_REGISTRY_UNAVAILABLE = "device_registry_unavailable"
REASON_DEVICE_UNREGISTERED = "device_unregistered"
REASON_DEVICE_KEY_MISMATCH = "device_public_key_mismatch"
REASON_DEVICE_REVOKED = "device_revoked"
REASON_ISSUANCE_RATE_LIMITED = "session_issuance_rate_limited"
# Phase H_a enrollment (POST /device/register).
REASON_ENROLLMENT_CLOSED = "device_enrollment_closed"
REASON_ENROLLMENT_POP_INVALID = "device_enrollment_pop_invalid"
REASON_ENROLLMENT_POP_SCHEME_UNSUPPORTED = "device_enrollment_pop_scheme_unsupported"
REASON_ENROLLMENT_KEY_MISMATCH = "device_enrollment_public_key_mismatch"


@dataclass(frozen=True, slots=True)
class DeviceProofOfPossession:
    """A device-signed proof that the issuance requester holds the device key.

    ``device_public_key_b64`` is the *raw* 32-byte Ed25519 public key, base64
    encoded (NOT a PEM — PEM ``BEGIN PUBLIC KEY`` text trips the repo's raw
    secret guard, and the raw form is smaller on the wire).  ``signature_b64``
    is the strict-base64 Ed25519 signature over
    :func:`device_pop_signature_message`.
    """

    device_public_key_b64: str
    signature_b64: str
    scheme: str = DEVICE_POP_SIGNATURE_SCHEME


@dataclass(frozen=True, slots=True)
class DeviceRegistration:
    passport_id: str
    device_id: str
    device_public_key_b64: str
    status: str = DEVICE_STATUS_ACTIVE


@dataclass(frozen=True, slots=True)
class DeviceRegistryResult:
    accepted: bool
    reason_code: str | None = None
    registration: DeviceRegistration | None = None


class DeviceRegistryUnavailable(RuntimeError):
    """Raised by a registry backend when it cannot answer (fail-closed)."""


class DeviceRegistry(Protocol):
    def resolve(
        self,
        *,
        passport_id: str,
        device_id: str,
        observed_at: datetime,
    ) -> DeviceRegistryResult:
        ...


@dataclass(slots=True)
class FakeDeviceRegistry:
    """Deterministic in-memory registry for tests and in-process harnesses.

    Fail-closed by construction: only the exact ``(passport_id, device_id)``
    tuples that were registered resolve; everything else is
    ``REASON_DEVICE_UNREGISTERED``.
    """

    registrations: dict[tuple[str, str], DeviceRegistration]

    def __init__(self, registrations: tuple[DeviceRegistration, ...] = ()) -> None:
        self.registrations = {}
        for registration in registrations:
            self.register(registration)

    def register(self, registration: DeviceRegistration) -> None:
        self.registrations[(registration.passport_id, registration.device_id)] = registration

    def resolve(
        self,
        *,
        passport_id: str,
        device_id: str,
        observed_at: datetime,
    ) -> DeviceRegistryResult:
        validate_aware_timestamp("observed_at", observed_at)
        registration = self.registrations.get((passport_id, device_id))
        if registration is None:
            return DeviceRegistryResult(False, REASON_DEVICE_UNREGISTERED)
        if registration.status == DEVICE_STATUS_REVOKED:
            return DeviceRegistryResult(False, REASON_DEVICE_REVOKED, registration)
        return DeviceRegistryResult(True, registration=registration)


@dataclass(frozen=True, slots=True)
class RejectingDeviceRegistry:
    """Fail-closed production default: no identity is ever registered.

    Until a real device registry backend is wired (owner input needed), every
    issuance request is rejected as ``REASON_DEVICE_UNREGISTERED`` — the path
    never auto-passes.
    """

    def resolve(
        self,
        *,
        passport_id: str,
        device_id: str,
        observed_at: datetime,
    ) -> DeviceRegistryResult:
        validate_aware_timestamp("observed_at", observed_at)
        return DeviceRegistryResult(False, REASON_DEVICE_UNREGISTERED)


def device_pop_signature_message(
    *,
    passport_id: str,
    device_id: str,
    lane: str,
    session_kind: str,
    issuance_nonce: str,
) -> bytes:
    """Build the domain-separated message a device must sign at issuance.

    Binding passport/device/lane/session_kind into the signed message means a
    captured PoP cannot be replayed to mint a session for a *different* identity
    or lane, and the per-issuance ``issuance_nonce`` (the same nonce that seeds
    the session_id + the C3 HMAC signature) makes each PoP single-use.
    """

    parts = (
        DEVICE_POP_SIGNATURE_DOMAIN.decode("ascii").rstrip("\n"),
        f"passport_id={passport_id}",
        f"device_id={device_id}",
        f"lane={lane}",
        f"session_kind={session_kind}",
        f"issuance_nonce={issuance_nonce}",
    )
    return ("\n".join(parts) + "\n").encode("utf-8")


def load_ed25519_public_key_b64(device_public_key_b64: str) -> Ed25519PublicKey:
    raw = base64.b64decode(device_public_key_b64.encode("ascii"), validate=True)
    if len(raw) != 32:
        raise ValueError("device public key must decode to 32 raw Ed25519 bytes")
    return Ed25519PublicKey.from_public_bytes(raw)


def _decode_signature(signature_b64: str) -> bytes:
    decoded = base64.b64decode(signature_b64.encode("ascii"), validate=True)
    if len(decoded) != 64:
        raise ValueError("signature must decode to a 64-byte Ed25519 signature")
    return decoded


def verify_device_proof_of_possession(
    pop: DeviceProofOfPossession,
    *,
    registry: DeviceRegistry,
    passport_id: str,
    device_id: str,
    lane: str,
    session_kind: str,
    issuance_nonce: str,
    observed_at: datetime,
) -> DeviceRegistryResult:
    """Fail-closed device PoP verification.

    Returns an accepted result (carrying the matched registration) ONLY when:
      1. the registry resolves ``(passport_id, device_id)`` to an active key,
      2. the PoP public key matches the registered key (no key substitution),
      3. the signature scheme is the expected device-PoP scheme, and
      4. the Ed25519 signature over the issuance-bound message verifies.
    Any failure returns ``accepted=False`` with a specific reason code; nothing
    auto-passes.
    """

    if pop.scheme != DEVICE_POP_SIGNATURE_SCHEME:
        return DeviceRegistryResult(False, REASON_DEVICE_POP_SCHEME_UNSUPPORTED)

    try:
        resolution = registry.resolve(
            passport_id=passport_id,
            device_id=device_id,
            observed_at=observed_at,
        )
    except DeviceRegistryUnavailable:
        return DeviceRegistryResult(False, REASON_DEVICE_REGISTRY_UNAVAILABLE)
    if not resolution.accepted or resolution.registration is None:
        return resolution

    registration = resolution.registration
    # Constant-work string compare is fine here: the registered key is public.
    if registration.device_public_key_b64 != pop.device_public_key_b64:
        return DeviceRegistryResult(False, REASON_DEVICE_KEY_MISMATCH, registration)

    try:
        public_key = load_ed25519_public_key_b64(registration.device_public_key_b64)
    except (binascii.Error, ValueError):
        return DeviceRegistryResult(False, REASON_DEVICE_POP_KEY_MALFORMED, registration)

    message = device_pop_signature_message(
        passport_id=passport_id,
        device_id=device_id,
        lane=lane,
        session_kind=session_kind,
        issuance_nonce=issuance_nonce,
    )
    try:
        public_key.verify(_decode_signature(pop.signature_b64), message)
    except (InvalidSignature, binascii.Error, ValueError):
        return DeviceRegistryResult(False, REASON_DEVICE_POP_INVALID, registration)

    return DeviceRegistryResult(True, registration=registration)


def device_enrollment_signature_message(
    *,
    passport_id: str,
    device_id: str,
    device_public_key_b64: str,
    enrollment_nonce: str,
) -> bytes:
    """Build the domain-separated message a device must sign to ENROLL.

    Binds passport/device/public-key/nonce: the signature proves the enroller
    holds the private key for the public key it is registering (key-possession),
    a captured enrollment PoP cannot be replayed for a different identity/key,
    and the server-issued single-use ``enrollment_nonce`` makes it single-use.
    The enrollment domain is distinct from the issuance domain, so the two PoPs
    are never interchangeable.
    """

    parts = (
        DEVICE_ENROLLMENT_SIGNATURE_DOMAIN.decode("ascii").rstrip("\n"),
        f"passport_id={passport_id}",
        f"device_id={device_id}",
        f"device_public_key_b64={device_public_key_b64}",
        f"enrollment_nonce={enrollment_nonce}",
    )
    return ("\n".join(parts) + "\n").encode("utf-8")


def verify_device_enrollment_proof_of_possession(
    pop: DeviceProofOfPossession,
    *,
    passport_id: str,
    device_id: str,
    enrollment_nonce: str,
) -> str | None:
    """Fail-closed enrollment key-possession check (no registry lookup).

    Unlike issuance, enrollment is the moment the ``(passport, device) -> key``
    binding is CREATED, so there is no prior registration to resolve against.
    Instead we verify that the Ed25519 signature over the enrollment-bound
    message validates under the *presented* public key — proving the enroller
    actually holds the private half of the key it is registering.

    Returns ``None`` on success, otherwise a specific reason code. The PoP's
    ``device_public_key_b64`` must equal ``pop.device_public_key_b64`` (the
    public key being enrolled); the caller passes that same value as the key to
    register. Nothing auto-passes.
    """

    if pop.scheme != DEVICE_ENROLLMENT_SIGNATURE_SCHEME:
        return REASON_ENROLLMENT_POP_SCHEME_UNSUPPORTED
    try:
        public_key = load_ed25519_public_key_b64(pop.device_public_key_b64)
    except (binascii.Error, ValueError):
        return REASON_DEVICE_POP_KEY_MALFORMED
    message = device_enrollment_signature_message(
        passport_id=passport_id,
        device_id=device_id,
        device_public_key_b64=pop.device_public_key_b64,
        enrollment_nonce=enrollment_nonce,
    )
    try:
        public_key.verify(_decode_signature(pop.signature_b64), message)
    except (InvalidSignature, binascii.Error, ValueError):
        return REASON_ENROLLMENT_POP_INVALID
    return None

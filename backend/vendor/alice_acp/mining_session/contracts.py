from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from datetime import datetime
from typing import Any

from alice_acp.evidence.types import validate_aware_timestamp
from alice_acp.mining_session.types import (
    ALICE_REWARDED_MINING_MODE,
    SUPPORTED_MINING_ALGORITHMS,
    SessionValidationResult,
    SignatureEnvelope,
    SignedMiningSession,
)

SESSION_EXPIRED = "SESSION_EXPIRED"
SESSION_SIGNATURE_MISMATCH = "SESSION_SIGNATURE_MISMATCH"
SESSION_ROUTE_MUTATION = "SESSION_ROUTE_MUTATION"
SESSION_POOL_MUTATION = "SESSION_POOL_MUTATION"
SESSION_WORKER_MISMATCH = "SESSION_WORKER_MISMATCH"
SESSION_PASSPORT_MISMATCH = "SESSION_PASSPORT_MISMATCH"
SESSION_COLLECTION_ADDRESS_MISMATCH = "SESSION_COLLECTION_ADDRESS_MISMATCH"
DIRECT_POOL_MODE_REJECTED = "DIRECT_POOL_MODE_REJECTED"

SESSION_PAYLOAD_FIELDS = (
    "alice_collection_address",
    "algorithm",
    "attempt_id",
    "expires_at",
    "issued_at",
    "mode",
    "passport_id",
    "pool_id",
    "route_policy_version",
    "session_id",
    "session_policy_version",
    "worker_id",
)


def canonical_session_payload(fields: Mapping[str, Any]) -> str:
    payload = {name: _canonical_value(fields[name]) for name in SESSION_PAYLOAD_FIELDS}
    return json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def session_payload_digest(fields: Mapping[str, Any]) -> str:
    return hashlib.sha256(canonical_session_payload(fields).encode("utf-8")).hexdigest()


def signature_envelope_for_session_fields(
    fields: Mapping[str, Any],
    *,
    signer_ref: str = "approval://alice-mining/session-signer",
    signature_ref: str = "approval://alice-mining/session-signature",
    signed_at: datetime,
) -> SignatureEnvelope:
    return SignatureEnvelope(
        signer_ref=signer_ref,
        payload_digest=session_payload_digest(fields),
        signed_at=signed_at,
        signature_ref=signature_ref,
    )


def validate_signed_mining_session(
    session: SignedMiningSession,
    *,
    observed_at: datetime,
    expected_collection_address: str | None = None,
    expected_worker_id: str | None = None,
    expected_passport_id: str | None = None,
    expected_attempt_id: str | None = None,
    expected_pool_id: str | None = None,
) -> SessionValidationResult:
    validate_aware_timestamp("observed_at", observed_at)
    if session.mode != ALICE_REWARDED_MINING_MODE:
        return _invalid(session, DIRECT_POOL_MODE_REJECTED)
    # Milestone 0 (D1): the algorithm lock is widened from RVN-only to the three
    # supported share-hash legs (RVN/KawPoW, XMR/RandomX, LTC/Scrypt). A truly
    # unknown algorithm is still a route mutation (fail-closed).
    if session.algorithm not in SUPPORTED_MINING_ALGORITHMS:
        return _invalid(session, SESSION_ROUTE_MUTATION)
    if observed_at > session.expires_at:
        return _invalid(session, SESSION_EXPIRED)
    if session.signature.payload_digest != session_payload_digest(_session_fields(session)):
        return _invalid(session, SESSION_SIGNATURE_MISMATCH)
    if expected_collection_address is not None:
        if expected_collection_address != session.alice_collection_address:
            return SessionValidationResult(
                status="no_reward",
                session_id=session.session_id,
                reason_code=SESSION_COLLECTION_ADDRESS_MISMATCH,
            )
    if expected_worker_id is not None and expected_worker_id != session.worker_id:
        return _invalid(session, SESSION_WORKER_MISMATCH)
    if expected_passport_id is not None and expected_passport_id != session.passport_id:
        return _invalid(session, SESSION_PASSPORT_MISMATCH)
    if expected_attempt_id is not None and expected_attempt_id != session.attempt_id:
        return _invalid(session, SESSION_ROUTE_MUTATION)
    if expected_pool_id is not None and expected_pool_id != session.pool_id:
        return _invalid(session, SESSION_POOL_MUTATION)
    return SessionValidationResult(status="valid", session_id=session.session_id)


def _session_fields(session: SignedMiningSession) -> dict[str, Any]:
    return {
        "alice_collection_address": session.alice_collection_address,
        "algorithm": session.algorithm,
        "attempt_id": session.attempt_id,
        "expires_at": session.expires_at,
        "issued_at": session.issued_at,
        "mode": session.mode,
        "passport_id": session.passport_id,
        "pool_id": session.pool_id,
        "route_policy_version": session.route_policy_version,
        "session_id": session.session_id,
        "session_policy_version": session.session_policy_version,
        "worker_id": session.worker_id,
    }


def _canonical_value(value: object) -> object:
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _invalid(session: SignedMiningSession, reason_code: str) -> SessionValidationResult:
    return SessionValidationResult(
        status="invalid",
        session_id=session.session_id,
        reason_code=reason_code,
    )

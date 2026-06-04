from __future__ import annotations

import hashlib
from datetime import datetime

from alice_acp.mining_identity.signatures import identity_payload_digest
from alice_acp.mining_identity.types import MinerIdentitySignaturePayload
from alice_acp.mining_pool.types import AcceptedShareProof
from alice_acp.mining_proofs.canonical import canonical_share_hash
from alice_acp.mining_proofs.types import MiningShareProof
from alice_acp.mining_server.types import (
    SERVER_DEVICE_NOT_ALLOWED,
    SERVER_PASSPORT_NOT_ALLOWED,
    SERVER_POOL_NOT_ALLOWED,
    AllowlistDecision,
    InternalAllowlistAdapter,
)
from alice_acp.mining_session.contracts import signature_envelope_for_session_fields
from alice_acp.mining_session.types import (
    ALICE_REWARDED_MINING_MODE,
    RVN_KAWPOW,
    SignedMiningSession,
)


def evaluate_internal_allowlist(
    allowlist: InternalAllowlistAdapter,
    *,
    passport_id: str,
    device_id: str,
    pool_id: str,
) -> AllowlistDecision:
    if passport_id not in allowlist.allowed_passports:
        return AllowlistDecision(False, SERVER_PASSPORT_NOT_ALLOWED)
    if device_id not in allowlist.allowed_devices:
        return AllowlistDecision(False, SERVER_DEVICE_NOT_ALLOWED)
    if pool_id not in allowlist.allowed_pools:
        return AllowlistDecision(False, SERVER_POOL_NOT_ALLOWED)
    return AllowlistDecision(True, "SERVER_ALLOWLIST_ACCEPTED")


def build_worker_id(*, passport_id: str, device_id: str, pool_id: str) -> str:
    digest = hashlib.sha256(f"{passport_id}|{device_id}|{pool_id}".encode()).hexdigest()
    return f"alice-rvn-{digest[:16]}"


def build_session_id(
    *,
    passport_id: str,
    device_id: str,
    pool_id: str,
    identity_digest: str,
) -> str:
    digest = hashlib.sha256(
        f"{passport_id}|{device_id}|{pool_id}|{identity_digest}".encode()
    ).hexdigest()
    return f"mining-session-{digest[:24]}"


def build_attempt_id(*, session_id: str) -> str:
    return f"{session_id}-attempt"


def issue_signed_mining_session(
    *,
    passport_id: str,
    device_id: str,
    pool_id: str,
    alice_collection_address: str,
    identity_payload: MinerIdentitySignaturePayload,
    issued_at: datetime,
    expires_at: datetime,
    route_policy_version: str,
    session_policy_version: str,
    signer_ref: str,
    signature_ref_prefix: str,
) -> tuple[SignedMiningSession, str]:
    identity_digest = identity_payload_digest(identity_payload)
    session_id = build_session_id(
        passport_id=passport_id,
        device_id=device_id,
        pool_id=pool_id,
        identity_digest=identity_digest,
    )
    worker_id = build_worker_id(
        passport_id=passport_id,
        device_id=device_id,
        pool_id=pool_id,
    )
    fields = {
        "session_id": session_id,
        "passport_id": passport_id,
        "attempt_id": build_attempt_id(session_id=session_id),
        "pool_id": pool_id,
        "algorithm": RVN_KAWPOW,
        "alice_collection_address": alice_collection_address,
        "worker_id": worker_id,
        "issued_at": issued_at,
        "expires_at": expires_at,
        "route_policy_version": route_policy_version,
        "session_policy_version": session_policy_version,
        "mode": ALICE_REWARDED_MINING_MODE,
    }
    session = SignedMiningSession(
        signature=signature_envelope_for_session_fields(
            fields,
            signer_ref=signer_ref,
            signature_ref=f"{signature_ref_prefix}/{session_id}",
            signed_at=issued_at,
        ),
        **fields,
    )
    return session, identity_digest


def accepted_share_from_mining_proof(proof: MiningShareProof) -> AcceptedShareProof:
    return AcceptedShareProof(
        pool_id=proof.pool_id,
        session_id=proof.session_id,
        worker_id=proof.worker_id,
        share_id=f"{proof.pool_job_id}:{proof.share_nonce}",
        nonce=proof.share_nonce,
        submitted_at=proof.submitted_at,
        share_difficulty=proof.share_difficulty,
        raw_ref_hash=canonical_share_hash(proof),
        evidence_ref=proof.pool_evidence_ref,
        collection_address=proof.alice_collection_address,
        algorithm=proof.algorithm,
    )

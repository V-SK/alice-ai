from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime

from alice_acp.evidence.types import validate_aware_timestamp
from alice_acp.mining_identity.types import MinerIdentitySignaturePayload, NonceVerificationResult

NONCE_REPLAYED = "NONCE_REPLAYED"
NONCE_STALE = "NONCE_STALE"
NONCE_EXPIRED = "NONCE_EXPIRED"


def canonical_identity_payload(payload: MinerIdentitySignaturePayload) -> str:
    return json.dumps(
        payload.canonical_fields(),
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    )


def identity_payload_digest(payload: MinerIdentitySignaturePayload) -> str:
    canonical = canonical_identity_payload(payload).encode("utf-8")
    return hashlib.sha256(canonical).hexdigest()


@dataclass(slots=True)
class LocalNonceVerifier:
    seen_nonces: set[tuple[str, str, str]] = field(default_factory=set)
    latest_issued_at: dict[tuple[str, str, str], datetime] = field(default_factory=dict)

    def verify_once(
        self,
        payload: MinerIdentitySignaturePayload,
        *,
        observed_at: datetime,
    ) -> NonceVerificationResult:
        validate_aware_timestamp("observed_at", observed_at)
        if observed_at > payload.expires_at:
            return NonceVerificationResult(False, NONCE_EXPIRED)

        scope = (
            payload.miner_passport_id,
            payload.miner_wallet_public_key,
            payload.replay_guard_epoch,
        )
        nonce_key = (*scope, payload.session_nonce)
        latest = self.latest_issued_at.get(scope)
        if latest is not None and payload.issued_at < latest:
            return NonceVerificationResult(False, NONCE_STALE)
        if nonce_key in self.seen_nonces:
            return NonceVerificationResult(False, NONCE_REPLAYED)

        self.seen_nonces.add(nonce_key)
        self.latest_issued_at[scope] = payload.issued_at
        return NonceVerificationResult(True)

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

from alice_acp.evidence.types import ensure_no_raw_secret, validate_aware_timestamp

IdentitySignatureIntent = Literal["miner_identity_v1"]


@dataclass(frozen=True, slots=True)
class MinerIdentitySignaturePayload:
    miner_wallet_public_key: str
    miner_passport_id: str
    session_nonce: str
    issued_at: datetime
    expires_at: datetime
    replay_guard_epoch: str
    policy_version: str
    intent: IdentitySignatureIntent = "miner_identity_v1"

    def __post_init__(self) -> None:
        required = (
            self.miner_wallet_public_key,
            self.miner_passport_id,
            self.session_nonce,
            self.replay_guard_epoch,
            self.policy_version,
            self.intent,
        )
        if any(not value for value in required):
            raise ValueError("miner identity signature payload fields must be non-empty")
        for field_name, value in (
            ("miner_wallet_public_key", self.miner_wallet_public_key),
            ("miner_passport_id", self.miner_passport_id),
            ("session_nonce", self.session_nonce),
        ):
            ensure_no_raw_secret(value, field_name=field_name)
        validate_aware_timestamp("issued_at", self.issued_at)
        validate_aware_timestamp("expires_at", self.expires_at)
        if self.expires_at <= self.issued_at:
            raise ValueError("expires_at must be after issued_at")

    def canonical_fields(self) -> dict[str, str]:
        return {
            "expires_at": self.expires_at.isoformat(),
            "intent": self.intent,
            "issued_at": self.issued_at.isoformat(),
            "miner_passport_id": self.miner_passport_id,
            "miner_wallet_public_key": self.miner_wallet_public_key,
            "policy_version": self.policy_version,
            "replay_guard_epoch": self.replay_guard_epoch,
            "session_nonce": self.session_nonce,
        }


@dataclass(frozen=True, slots=True)
class NonceVerificationResult:
    accepted: bool
    reason_code: str | None = None

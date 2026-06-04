from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

from alice_acp.mining_accounting import ZERO_ACU
from alice_acp.mining_supervisor.types import SupervisorState

EstimatedRewardStatus = Literal["shadow_only", "live_disabled"]
PoolSessionStatus = Literal["valid", "invalid", "expired"]


@dataclass(frozen=True, slots=True)
class MinerDashboardReport:
    miner_passport_id: str
    device_backend_status: str
    device_backend_reason: str
    mining_status: SupervisorState
    pool_session_status: PoolSessionStatus
    pool_session_reason: str
    accepted_shares: int
    rejected_shares: int
    duplicate_replayed_tampered_rejected_count: int
    estimated_mining_acu: Decimal
    estimated_reward_status: EstimatedRewardStatus
    ai_preemption_status: str
    ai_preemption_reason: str
    alice_collection_address: str
    paid_acu: Decimal = ZERO_ACU
    live_reward_enabled: bool = False
    miner_rvn_wallet_required: bool = False

    def __post_init__(self) -> None:
        required = (
            self.miner_passport_id,
            self.device_backend_status,
            self.device_backend_reason,
            self.mining_status,
            self.pool_session_status,
            self.pool_session_reason,
            self.estimated_reward_status,
            self.ai_preemption_status,
            self.ai_preemption_reason,
            self.alice_collection_address,
        )
        if any(not value for value in required):
            raise ValueError("dashboard report string fields must be non-empty")
        for field_name, value in (
            ("accepted_shares", self.accepted_shares),
            ("rejected_shares", self.rejected_shares),
            (
                "duplicate_replayed_tampered_rejected_count",
                self.duplicate_replayed_tampered_rejected_count,
            ),
        ):
            if value < 0:
                raise ValueError(f"{field_name} must be non-negative")
        if self.estimated_mining_acu < ZERO_ACU:
            raise ValueError("estimated_mining_acu must be non-negative")
        if self.paid_acu != ZERO_ACU:
            raise ValueError("miner dashboard reports must remain unpaid in dry-run")
        if self.live_reward_enabled:
            raise ValueError("live reward must stay disabled in dry-run")
        if self.miner_rvn_wallet_required:
            raise ValueError("miner RVN wallet must not be required for Alice sessions")

    def as_dict(self) -> dict[str, object]:
        return {
            "miner_passport_id": self.miner_passport_id,
            "device_backend_status": self.device_backend_status,
            "device_backend_reason": self.device_backend_reason,
            "mining_status": self.mining_status,
            "pool_session_status": self.pool_session_status,
            "pool_session_reason": self.pool_session_reason,
            "accepted_shares": self.accepted_shares,
            "rejected_shares": self.rejected_shares,
            "duplicate_replayed_tampered_rejected_count": (
                self.duplicate_replayed_tampered_rejected_count
            ),
            "estimated_mining_acu": str(self.estimated_mining_acu),
            "estimated_reward_status": self.estimated_reward_status,
            "ai_preemption_status": self.ai_preemption_status,
            "ai_preemption_reason": self.ai_preemption_reason,
            "alice_collection_address": self.alice_collection_address,
            "paid_acu": str(self.paid_acu),
            "live_reward_enabled": self.live_reward_enabled,
            "miner_rvn_wallet_required": self.miner_rvn_wallet_required,
        }

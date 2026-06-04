"""Local RVN/KAWPOW pool adapter contracts with synthetic share proofs."""

from alice_acp.mining_pool.adapter import (
    SHARE_ACCEPTED,
    SHARE_POOL_MISMATCH,
    SHARE_REJECTED_NOT_REWARDABLE,
    SHARE_ROUTE_MISMATCH,
    SHARE_SESSION_MISMATCH,
    SHARE_WORKER_MISMATCH,
    build_share_proof,
)
from alice_acp.mining_pool.types import AcceptedShareProof, PoolAdapterResult, PoolShareEvent

__all__ = [
    "SHARE_ACCEPTED",
    "SHARE_POOL_MISMATCH",
    "SHARE_REJECTED_NOT_REWARDABLE",
    "SHARE_ROUTE_MISMATCH",
    "SHARE_SESSION_MISMATCH",
    "SHARE_WORKER_MISMATCH",
    "AcceptedShareProof",
    "PoolAdapterResult",
    "PoolShareEvent",
    "build_share_proof",
]

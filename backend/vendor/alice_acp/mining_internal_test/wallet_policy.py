from __future__ import annotations

from alice_acp.mining_internal_test.types import (
    COLLECTION_WALLET_ADDRESS_REQUIRED,
    COLLECTION_WALLET_READY,
    COLLECTION_WALLET_SESSION_ADDRESS_MISMATCH,
    CollectionWalletDecision,
    CollectionWalletPolicy,
)
from alice_acp.mining_session.types import SignedMiningSession


def evaluate_collection_wallet_policy(
    policy: CollectionWalletPolicy,
    *,
    require_real_address: bool,
) -> CollectionWalletDecision:
    if require_real_address and not policy.has_real_collection_address:
        return CollectionWalletDecision(
            ready=False,
            reason_code=COLLECTION_WALLET_ADDRESS_REQUIRED,
        )
    return CollectionWalletDecision(ready=True, reason_code=COLLECTION_WALLET_READY)


def validate_session_collection_policy(
    policy: CollectionWalletPolicy,
    *,
    session: SignedMiningSession,
) -> CollectionWalletDecision:
    decision = evaluate_collection_wallet_policy(policy, require_real_address=True)
    if not decision.ready:
        return decision
    if policy.collection_address != session.alice_collection_address:
        return CollectionWalletDecision(
            ready=False,
            reason_code=COLLECTION_WALLET_SESSION_ADDRESS_MISMATCH,
        )
    return CollectionWalletDecision(ready=True, reason_code=COLLECTION_WALLET_READY)

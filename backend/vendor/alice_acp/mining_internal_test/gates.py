from __future__ import annotations

from alice_acp.mining_internal_test.kill_switch import evaluate_kill_switch
from alice_acp.mining_internal_test.pool_profile import evaluate_test_pool_profile
from alice_acp.mining_internal_test.types import (
    COLLECTION_WALLET_READY,
    INTERNAL_TEST_ALLOWLIST_REQUIRED,
    INTERNAL_TEST_DEVICE_NOT_ALLOWED,
    INTERNAL_TEST_DISABLED,
    INTERNAL_TEST_KILL_SWITCH_ACTIVE,
    INTERNAL_TEST_PASSPORT_NOT_ALLOWED,
    INTERNAL_TEST_POOL_NOT_ALLOWED,
    INTERNAL_TEST_READY,
    INTERNAL_TEST_REAL_POOL_DISABLED,
    TEST_POOL_PROFILE_READY,
    CollectionWalletPolicy,
    GateDecision,
    InternalMiningTestGate,
    InternalTestReadinessReport,
    KillSwitchPolicy,
    TestPoolProfile,
)
from alice_acp.mining_internal_test.wallet_policy import validate_session_collection_policy
from alice_acp.mining_session.types import SignedMiningSession


def evaluate_internal_test_gate(
    gate: InternalMiningTestGate,
    *,
    passport_id: str,
    device_id: str,
    pool_id: str,
    require_real_pool: bool,
) -> GateDecision:
    if gate.kill_switch_enabled:
        return GateDecision(allowed=False, reason_code=INTERNAL_TEST_KILL_SWITCH_ACTIVE)
    if not gate.internal_test_enabled:
        return GateDecision(allowed=False, reason_code=INTERNAL_TEST_DISABLED)
    if require_real_pool and not gate.real_pool_enabled:
        return GateDecision(allowed=False, reason_code=INTERNAL_TEST_REAL_POOL_DISABLED)
    if not gate.allowed_passports or not gate.allowed_devices or not gate.allowed_pool_ids:
        return GateDecision(allowed=False, reason_code=INTERNAL_TEST_ALLOWLIST_REQUIRED)
    if passport_id not in gate.allowed_passports:
        return GateDecision(allowed=False, reason_code=INTERNAL_TEST_PASSPORT_NOT_ALLOWED)
    if device_id not in gate.allowed_devices:
        return GateDecision(allowed=False, reason_code=INTERNAL_TEST_DEVICE_NOT_ALLOWED)
    if pool_id not in gate.allowed_pool_ids:
        return GateDecision(allowed=False, reason_code=INTERNAL_TEST_POOL_NOT_ALLOWED)
    return GateDecision(allowed=True, reason_code=INTERNAL_TEST_READY)


def evaluate_internal_test_readiness(
    *,
    gate: InternalMiningTestGate,
    wallet_policy: CollectionWalletPolicy,
    pool_profile: TestPoolProfile,
    kill_switch: KillSwitchPolicy,
    session: SignedMiningSession,
    passport_id: str,
    device_id: str,
) -> InternalTestReadinessReport:
    reason_codes: list[str] = []
    gate_decision = evaluate_internal_test_gate(
        gate,
        passport_id=passport_id,
        device_id=device_id,
        pool_id=pool_profile.pool_id,
        require_real_pool=True,
    )
    if not gate_decision.allowed:
        reason_codes.append(gate_decision.reason_code)

    pool_decision = evaluate_test_pool_profile(pool_profile, gate=gate)
    if not pool_decision.ready:
        reason_codes.append(pool_decision.reason_code)

    wallet_decision = validate_session_collection_policy(wallet_policy, session=session)
    if not wallet_decision.ready:
        reason_codes.append(wallet_decision.reason_code)

    kill_decision = evaluate_kill_switch(
        kill_switch,
        pool_id=pool_profile.pool_id,
        passport_id=passport_id,
        device_id=device_id,
    )
    if kill_decision.blocked:
        reason_codes.append(kill_decision.reason_code)

    if not reason_codes:
        reason_codes.extend(
            (
                INTERNAL_TEST_READY,
                TEST_POOL_PROFILE_READY,
                COLLECTION_WALLET_READY,
            )
        )
        return InternalTestReadinessReport(
            status="ready",
            reason_codes=tuple(reason_codes),
        )
    return InternalTestReadinessReport(
        status="blocked",
        reason_codes=tuple(reason_codes),
    )

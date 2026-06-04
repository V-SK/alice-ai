from __future__ import annotations

from alice_acp.mining_internal_test.types import (
    KILL_SWITCH_ALLOW,
    KILL_SWITCH_DEVICE_DISABLED,
    KILL_SWITCH_GLOBAL_STOP,
    KILL_SWITCH_PASSPORT_DISABLED,
    KILL_SWITCH_POOL_DISABLED,
    KillSwitchDecision,
    KillSwitchPolicy,
)


def evaluate_kill_switch(
    policy: KillSwitchPolicy,
    *,
    pool_id: str,
    passport_id: str,
    device_id: str,
) -> KillSwitchDecision:
    if policy.global_stop:
        return KillSwitchDecision(blocked=True, reason_code=KILL_SWITCH_GLOBAL_STOP)
    if pool_id in policy.disabled_pool_ids:
        return KillSwitchDecision(blocked=True, reason_code=KILL_SWITCH_POOL_DISABLED)
    if passport_id in policy.disabled_passport_ids:
        return KillSwitchDecision(blocked=True, reason_code=KILL_SWITCH_PASSPORT_DISABLED)
    if device_id in policy.disabled_device_ids:
        return KillSwitchDecision(blocked=True, reason_code=KILL_SWITCH_DEVICE_DISABLED)
    return KillSwitchDecision(blocked=False, reason_code=KILL_SWITCH_ALLOW)

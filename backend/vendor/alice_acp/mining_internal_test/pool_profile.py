from __future__ import annotations

from urllib.parse import urlparse

from alice_acp.mining_internal_test.types import (
    TEST_POOL_PROFILE_NOT_ALLOWLISTED,
    TEST_POOL_PROFILE_READY,
    TEST_POOL_PROFILE_REAL_NETWORK_REQUIRES_GATE,
    TEST_POOL_PROFILE_TEMPLATE_REQUIRED,
    InternalMiningTestGate,
    PoolProfileDecision,
    TestPoolProfile,
)

_PLACEHOLDER_MARKERS = ("<", ">", "{", "}", "$")
_LOCAL_SCHEMES = frozenset({"fixture", "template", "disabled"})


def evaluate_test_pool_profile(
    profile: TestPoolProfile,
    *,
    gate: InternalMiningTestGate,
) -> PoolProfileDecision:
    if not _is_placeholder_endpoint(profile.stratum_endpoint_template):
        return PoolProfileDecision(
            ready=False,
            reason_code=TEST_POOL_PROFILE_TEMPLATE_REQUIRED,
        )
    if profile.pool_id not in gate.allowed_pool_ids:
        return PoolProfileDecision(
            ready=False,
            reason_code=TEST_POOL_PROFILE_NOT_ALLOWLISTED,
        )
    if profile.real_network_allowed and not (
        gate.internal_test_enabled and gate.real_pool_enabled
    ):
        return PoolProfileDecision(
            ready=False,
            reason_code=TEST_POOL_PROFILE_REAL_NETWORK_REQUIRES_GATE,
        )
    return PoolProfileDecision(ready=True, reason_code=TEST_POOL_PROFILE_READY)


def _is_placeholder_endpoint(value: str) -> bool:
    parsed = urlparse(value)
    if parsed.scheme in _LOCAL_SCHEMES:
        return True
    return any(marker in value for marker in _PLACEHOLDER_MARKERS)

from __future__ import annotations

from decimal import Decimal

from alice_acp.shadow_server.types import ZERO_DECIMAL, Lane, RewardStatement


def build_reward_statement(
    *,
    passport_id: str,
    device_id: str,
    lane: Lane,
    pool_key: str,
    score_kind: str,
    device_lane_score: Decimal,
    pool_budget: Decimal,
    lane_budget: Decimal,
    denominator_score: Decimal,
    cap_policy: str,
    demand_session_id: str | None = None,
) -> RewardStatement:
    simulated_alice_credit = ZERO_DECIMAL
    if denominator_score > ZERO_DECIMAL:
        simulated_alice_credit = lane_budget * device_lane_score / denominator_score
    return RewardStatement(
        passport_id=passport_id,
        device_id=device_id,
        lane=lane,
        pool_key=pool_key,
        score_kind=score_kind,
        device_lane_score=device_lane_score,
        pool_budget=pool_budget,
        lane_budget=lane_budget,
        denominator_score=denominator_score,
        simulated_alice_credit=simulated_alice_credit,
        cap_policy=cap_policy,
        demand_session_id=demand_session_id,
    )

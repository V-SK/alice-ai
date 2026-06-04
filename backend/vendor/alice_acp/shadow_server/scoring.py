from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from decimal import Decimal
from typing import cast

from alice_acp.shadow_server.pool_budget import (
    DEFAULT_POOL_BUDGET_CONTRACT,
    GPU_LANES,
    MAIN_POOL_GPU_LANE_BUDGET_KEY,
    PoolBudgetContract,
    main_pool_lane_budget,
)
from alice_acp.shadow_server.reward_statement import build_reward_statement
from alice_acp.shadow_server.types import (
    MAIN_POOL_AI,
    POOL_MAIN,
    POOL_SCRYPT,
    POOL_XMR,
    ZERO_DECIMAL,
    Lane,
    RewardStatement,
    SettlementWindow,
    ShadowWorkRecord,
)

XMR_POOL_CAP_POLICY = "cpu_xmr_pool_uses_15_percent_budget"
# Phase C: the LTC/DOGE scrypt pool is now a REAL creditable 15% lane that pays
# its scrypt work records pro-rata out of the scrypt pool budget, exactly like
# the cpu xmr pool. Only the UNCLAIMED remainder of the scrypt budget rolls to
# reserve (see ``reserve_roll_forward`` below). The former
# ``ltc_doge_scrypt_pool_inactive_roll_forward`` cap policy (whole budget always
# to reserve) is retired.
SCRYPT_POOL_CAP_POLICY = "ltc_doge_scrypt_pool_uses_15_percent_budget"


@dataclass(frozen=True, slots=True)
class RewardScoreSettlement:
    device_credits: dict[tuple[str, str], Decimal]
    reward_statements: tuple[RewardStatement, ...]
    reserve_roll_forward: Decimal


def settle_reward_scores(
    *,
    records: Iterable[ShadowWorkRecord],
    window: SettlementWindow,
    pool_budgets: dict[str, Decimal],
    contract: PoolBudgetContract = DEFAULT_POOL_BUDGET_CONTRACT,
) -> RewardScoreSettlement:
    device_lane_scores: dict[tuple[str, str, str, str], Decimal] = defaultdict(lambda: ZERO_DECIMAL)
    lane_denominators: dict[tuple[str, str], Decimal] = defaultdict(lambda: ZERO_DECIMAL)
    score_kinds: dict[tuple[str, str, str, str], str] = {}
    demand_session_ids: dict[tuple[str, str, str, str], set[str]] = defaultdict(set)

    for record in records:
        if not window.starts_at <= record.recorded_at < window.ends_at:
            continue
        if record.verified_score <= ZERO_DECIMAL:
            continue
        key = (record.pool_key, record.lane, record.passport_id, record.device_id)
        device_lane_scores[key] += record.verified_score
        lane_denominators[(record.pool_key, record.lane)] += record.verified_score
        score_kinds.setdefault(key, record.score_kind)
        if record.demand_session_id:
            demand_session_ids[key].add(record.demand_session_id)

    # The GPU sub-budget is shared across ALL GPU lanes (RVN + Quai); "GPU present" for the
    # AI/GPU cap policy means EITHER GPU lane has score, so the gpu_score is the COMBINED
    # GPU-lane score. Each GPU lane then credits pro-rata from this single GPU budget by its
    # share of the combined GPU score (computed per-statement below), so the GPU budget is
    # never double-spent across the two GPU lanes.
    gpu_score_total = sum(
        (lane_denominators[(POOL_MAIN, gpu_lane)] for gpu_lane in GPU_LANES),
        ZERO_DECIMAL,
    )
    main_lane_budget = main_pool_lane_budget(
        main_pool_budget=pool_budgets[POOL_MAIN],
        gpu_score=gpu_score_total,
        ai_score=lane_denominators[(POOL_MAIN, MAIN_POOL_AI)],
        contract=contract,
    )

    device_credits: dict[tuple[str, str], Decimal] = {}
    statements: list[RewardStatement] = []
    scrypt_credited = ZERO_DECIMAL
    for key in sorted(device_lane_scores):
        pool_key, lane_text, passport_id, device_id = key
        lane = cast(Lane, lane_text)
        denominator = lane_denominators[(pool_key, lane_text)]
        if pool_key == POOL_MAIN:
            if lane_text in GPU_LANES:
                # Every GPU lane (RVN + Quai) draws from the SHARED GPU sub-budget and is
                # credited pro-rata against the COMBINED GPU-lane score, so the two GPU
                # lanes split the one GPU budget by their score shares (never double-spend).
                lane_budget = main_lane_budget.lane_budgets[MAIN_POOL_GPU_LANE_BUDGET_KEY]
                denominator = gpu_score_total
            else:
                lane_budget = main_lane_budget.lane_budgets[lane_text]
            cap_policy = main_lane_budget.cap_policy
        elif pool_key == POOL_XMR:
            lane_budget = pool_budgets[POOL_XMR]
            cap_policy = XMR_POOL_CAP_POLICY
        else:
            # Phase C: scrypt (LTC/DOGE) is a real creditable lane and, like the
            # cpu xmr pool, pays its work records pro-rata out of the whole
            # scrypt pool budget. (Previously this branch forced a zero lane
            # budget so scrypt never credited and the full 15% always rolled to
            # reserve.)
            lane_budget = pool_budgets[POOL_SCRYPT]
            cap_policy = SCRYPT_POOL_CAP_POLICY
        statement = build_reward_statement(
            passport_id=passport_id,
            device_id=device_id,
            lane=lane,
            pool_key=pool_key,
            score_kind=score_kinds[key],
            device_lane_score=device_lane_scores[key],
            pool_budget=pool_budgets[pool_key],
            lane_budget=lane_budget,
            denominator_score=denominator,
            cap_policy=cap_policy,
            demand_session_id=_single_demand_session_id(demand_session_ids[key]),
        )
        if statement.simulated_alice_credit > ZERO_DECIMAL:
            identity = (passport_id, device_id)
            device_credits[identity] = (
                device_credits.get(identity, ZERO_DECIMAL) + statement.simulated_alice_credit
            )
            if pool_key == POOL_SCRYPT:
                scrypt_credited += statement.simulated_alice_credit
        statements.append(statement)

    # Phase C: reserve takes ONLY the UNCLAIMED remainder of the scrypt budget.
    # When no scrypt work records claim the lane (the prior behaviour and every
    # non-scrypt window), ``scrypt_credited`` is 0 and the full 15% scrypt budget
    # rolls forward exactly as before. When scrypt miners claim the lane, that
    # claimed amount is credited to the miners (NOT rolled to reserve) and only
    # the leftover rolls forward.
    reserve_roll_forward = pool_budgets[POOL_SCRYPT] - scrypt_credited
    if reserve_roll_forward < ZERO_DECIMAL:
        reserve_roll_forward = ZERO_DECIMAL
    return RewardScoreSettlement(
        device_credits=device_credits,
        reward_statements=tuple(statements),
        reserve_roll_forward=reserve_roll_forward,
    )


def _single_demand_session_id(demand_session_ids: set[str]) -> str | None:
    if not demand_session_ids:
        return None
    if len(demand_session_ids) == 1:
        return next(iter(demand_session_ids))
    return "multiple"

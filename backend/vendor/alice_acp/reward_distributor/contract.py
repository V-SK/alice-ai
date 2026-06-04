from __future__ import annotations

from decimal import Decimal, localcontext

from alice_acp.reward_distributor.types import (
    Q16_REQUIRED_OWNER_RESERVE_BPS,
    REWARD_DISTRIBUTOR_DUPLICATE_WINDOW,
    REWARD_DISTRIBUTOR_KILL_SWITCH_ACTIVE,
    REWARD_DISTRIBUTOR_MINER_CAP_EXCEEDED,
    REWARD_DISTRIBUTOR_OWNER_RESERVE_ADDRESS_MISMATCH,
    REWARD_DISTRIBUTOR_OWNER_RESERVE_REQUIRED,
    REWARD_DISTRIBUTOR_WINDOW_CAP_EXCEEDS_SCHEDULE,
    REWARD_DISTRIBUTOR_WINDOW_SECONDS_INVALID,
    ZERO_DECIMAL,
    RewardDistributorWindowDecision,
    RewardDistributorWindowRequest,
)
from alice_acp.reward_schedule import DEFAULT_REWARD_WINDOW_SECONDS

DECIMAL_PRECISION = 80


def evaluate_reward_distributor_window(
    request: RewardDistributorWindowRequest,
) -> RewardDistributorWindowDecision:
    reserve_plan = request.emission_plan.reserve_plan
    reason_codes: list[str] = []

    duplicate_window_guard_passed = request.window_id not in request.processed_window_ids
    kill_switch_gate_passed = not request.kill_switch_active
    miner_emission_cap = request.window_plan.miner_window_emission_cap
    miner_allocated_credit = sum(
        (credit.reward_credit for credit in request.miner_credits),
        ZERO_DECIMAL,
    )
    owner_reserve_credit = _owner_reserve_credit_for_window(request)

    if request.window_plan.window_seconds != DEFAULT_REWARD_WINDOW_SECONDS:
        reason_codes.append(REWARD_DISTRIBUTOR_WINDOW_SECONDS_INVALID)
    if reserve_plan.owner_reserve_bps != Q16_REQUIRED_OWNER_RESERVE_BPS:
        reason_codes.append(REWARD_DISTRIBUTOR_OWNER_RESERVE_REQUIRED)
    if request.window_plan.owner_reserve_address != reserve_plan.owner_reserve_address:
        reason_codes.append(REWARD_DISTRIBUTOR_OWNER_RESERVE_ADDRESS_MISMATCH)
    if not duplicate_window_guard_passed:
        reason_codes.append(REWARD_DISTRIBUTOR_DUPLICATE_WINDOW)
    if not kill_switch_gate_passed:
        reason_codes.append(REWARD_DISTRIBUTOR_KILL_SWITCH_ACTIVE)
    if miner_allocated_credit > miner_emission_cap:
        reason_codes.append(REWARD_DISTRIBUTOR_MINER_CAP_EXCEEDED)
    if miner_emission_cap > reserve_plan.miner_emission_cap:
        reason_codes.append(REWARD_DISTRIBUTOR_WINDOW_CAP_EXCEEDS_SCHEDULE)

    return RewardDistributorWindowDecision(
        window_id=request.window_id,
        contract_valid=not reason_codes,
        reason_codes=tuple(reason_codes),
        window_seconds=request.window_plan.window_seconds,
        owner_reserve_address=reserve_plan.owner_reserve_address,
        owner_reserve_bps=reserve_plan.owner_reserve_bps,
        owner_reserve_credit=owner_reserve_credit,
        miner_emission_cap=miner_emission_cap,
        miner_allocated_credit=miner_allocated_credit,
        already_paid_replay_offset=reserve_plan.already_issued_rewards,
        approved_unpaid_replay_offset=reserve_plan.approved_unpaid_rewards,
        deducted_reward_offset=reserve_plan.deducted_reward_offset,
        duplicate_window_guard_passed=duplicate_window_guard_passed,
        kill_switch_gate_passed=kill_switch_gate_passed,
        chain_upgrade_required=request.chain_controls.chain_upgrade_required,
    )


def _owner_reserve_credit_for_window(request: RewardDistributorWindowRequest) -> Decimal:
    reserve_plan = request.emission_plan.reserve_plan
    if reserve_plan.owner_reserve_cap == ZERO_DECIMAL:
        return ZERO_DECIMAL
    if reserve_plan.miner_emission_cap == ZERO_DECIMAL:
        return ZERO_DECIMAL
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        return (
            request.window_plan.miner_window_emission_cap
            * reserve_plan.owner_reserve_cap
            / reserve_plan.miner_emission_cap
        )

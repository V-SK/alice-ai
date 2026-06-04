from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation, localcontext
from typing import Literal

from alice_acp.evidence.types import ensure_no_raw_secret

ZERO_DECIMAL = Decimal("0")
BPS_DENOMINATOR = 10_000
DEFAULT_OWNER_RESERVE_BPS = 2_000
DEFAULT_REWARD_WINDOW_SECONDS = 4 * 60 * 60
OWNER_RESERVE_PUBLIC_ADDRESS = "a2u85Mwf4TRgoGkPBCw8438qLVQMYctpJAFiGtVnM21RMK3rt"
DECIMAL_PRECISION = 80

DecimalInput = Decimal | int | str
ApprovedUnpaidStatus = Literal["no_approved_unpaid_offset", "approved_unpaid_offset_applied"]


@dataclass(frozen=True, slots=True)
class RewardPoolInputs:
    total_reward_pool: DecimalInput
    already_issued_rewards: DecimalInput
    approved_unpaid_rewards: DecimalInput = ZERO_DECIMAL
    owner_reserve_address: str = OWNER_RESERVE_PUBLIC_ADDRESS
    owner_reserve_bps: int = DEFAULT_OWNER_RESERVE_BPS

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "total_reward_pool",
            _coerce_decimal(self.total_reward_pool, field_name="total_reward_pool"),
        )
        object.__setattr__(
            self,
            "already_issued_rewards",
            _coerce_decimal(self.already_issued_rewards, field_name="already_issued_rewards"),
        )
        object.__setattr__(
            self,
            "approved_unpaid_rewards",
            _coerce_decimal(self.approved_unpaid_rewards, field_name="approved_unpaid_rewards"),
        )
        _validate_non_negative(self.total_reward_pool, field_name="total_reward_pool")
        _validate_non_negative(self.already_issued_rewards, field_name="already_issued_rewards")
        _validate_non_negative(self.approved_unpaid_rewards, field_name="approved_unpaid_rewards")
        _validate_owner_reserve_address(self.owner_reserve_address)
        if isinstance(self.owner_reserve_bps, bool) or not isinstance(self.owner_reserve_bps, int):
            raise TypeError("owner_reserve_bps_must_be_int")
        if not 0 <= self.owner_reserve_bps <= BPS_DENOMINATOR:
            raise ValueError("owner_reserve_bps_must_be_between_0_and_10000")
        if self.owner_reserve_bps != DEFAULT_OWNER_RESERVE_BPS:
            raise ValueError("owner_reserve_bps_must_equal_2000")


@dataclass(frozen=True, slots=True)
class RewardReservePlan:
    total_reward_pool: Decimal
    already_issued_rewards: Decimal
    approved_unpaid_rewards: Decimal
    deducted_reward_offset: Decimal
    post_issued_reward_pool: Decimal
    available_reward_pool: Decimal
    owner_reserve_bps: int
    owner_reserve_address: str
    owner_reserve_cap: Decimal
    miner_emission_cap: Decimal
    calculation_only: bool = True
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False
    chain_transfer_enabled: bool = False
    paid_acu: Decimal = ZERO_DECIMAL
    approved_unpaid_status: ApprovedUnpaidStatus = "no_approved_unpaid_offset"

    def __post_init__(self) -> None:
        _validate_disabled_flags(
            live_reward_enabled=self.live_reward_enabled,
            payout_executor_enabled=self.payout_executor_enabled,
            chain_transfer_enabled=self.chain_transfer_enabled,
        )
        if self.paid_acu != ZERO_DECIMAL:
            raise ValueError("reward_reserve_plan_paid_acu_must_remain_zero")
        if self.approved_unpaid_status not in {
            "no_approved_unpaid_offset",
            "approved_unpaid_offset_applied",
        }:
            raise ValueError("approved_unpaid_status_is_unsupported")


@dataclass(frozen=True, slots=True)
class HalvingScheduleConfig:
    emission_start: datetime
    period_seconds: int
    period_count: int
    window_seconds: int = DEFAULT_REWARD_WINDOW_SECONDS

    def __post_init__(self) -> None:
        _validate_aware_datetime(self.emission_start, field_name="emission_start")
        _validate_positive_int(self.period_seconds, field_name="period_seconds")
        _validate_positive_int(self.window_seconds, field_name="window_seconds")
        if self.period_seconds < self.window_seconds:
            raise ValueError("period_seconds_must_cover_at_least_one_window")
        _validate_positive_int(self.period_count, field_name="period_count")


@dataclass(frozen=True, slots=True)
class RewardHalvingPeriod:
    period_index: int
    starts_at: datetime
    ends_at: datetime
    halving_weight: Decimal
    emission_cap: Decimal
    full_window_emission_cap: Decimal


@dataclass(frozen=True, slots=True)
class RewardEmissionPlan:
    reserve_plan: RewardReservePlan
    schedule_config: HalvingScheduleConfig
    halving_periods: tuple[RewardHalvingPeriod, ...]
    calculation_only: bool = True
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False
    chain_transfer_enabled: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.reserve_plan, RewardReservePlan):
            raise TypeError("reserve_plan_must_be_reward_reserve_plan")
        _validate_disabled_flags(
            live_reward_enabled=self.live_reward_enabled,
            payout_executor_enabled=self.payout_executor_enabled,
            chain_transfer_enabled=self.chain_transfer_enabled,
        )


@dataclass(frozen=True, slots=True)
class WindowEmissionComponent:
    period_index: int
    overlap_seconds: int
    emission_cap: Decimal


@dataclass(frozen=True, slots=True)
class WindowEmissionPlan:
    starts_at: datetime
    ends_at: datetime
    window_seconds: int
    miner_window_emission_cap: Decimal
    components: tuple[WindowEmissionComponent, ...]
    owner_reserve_address: str
    calculation_only: bool = True
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False
    chain_transfer_enabled: bool = False

    def __post_init__(self) -> None:
        _validate_disabled_flags(
            live_reward_enabled=self.live_reward_enabled,
            payout_executor_enabled=self.payout_executor_enabled,
            chain_transfer_enabled=self.chain_transfer_enabled,
        )


def compute_reward_reserve_plan(inputs: RewardPoolInputs) -> RewardReservePlan:
    deducted_offset = inputs.already_issued_rewards + inputs.approved_unpaid_rewards
    if deducted_offset > inputs.total_reward_pool:
        raise ValueError("reward_offset_exceeds_total_pool")
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        owner_reserve_cap = (
            inputs.total_reward_pool * Decimal(inputs.owner_reserve_bps) / Decimal(BPS_DENOMINATOR)
        )
    if deducted_offset + owner_reserve_cap > inputs.total_reward_pool:
        raise ValueError("reward_offset_plus_owner_reserve_exceeds_total_pool")
    post_issued_pool = inputs.total_reward_pool - deducted_offset
    miner_emission_cap = post_issued_pool - owner_reserve_cap
    return RewardReservePlan(
        total_reward_pool=inputs.total_reward_pool,
        already_issued_rewards=inputs.already_issued_rewards,
        approved_unpaid_rewards=inputs.approved_unpaid_rewards,
        deducted_reward_offset=deducted_offset,
        post_issued_reward_pool=post_issued_pool,
        available_reward_pool=miner_emission_cap,
        owner_reserve_bps=inputs.owner_reserve_bps,
        owner_reserve_address=inputs.owner_reserve_address,
        owner_reserve_cap=owner_reserve_cap,
        miner_emission_cap=miner_emission_cap,
        approved_unpaid_status=(
            "approved_unpaid_offset_applied"
            if inputs.approved_unpaid_rewards > ZERO_DECIMAL
            else "no_approved_unpaid_offset"
        ),
    )


def build_reward_emission_plan(
    inputs: RewardPoolInputs,
    schedule_config: HalvingScheduleConfig,
) -> RewardEmissionPlan:
    reserve_plan = compute_reward_reserve_plan(inputs)
    periods = _build_halving_periods(
        miner_emission_cap=reserve_plan.miner_emission_cap,
        schedule_config=schedule_config,
    )
    return RewardEmissionPlan(
        reserve_plan=reserve_plan,
        schedule_config=schedule_config,
        halving_periods=periods,
    )


def window_emission_plan(
    emission_plan: RewardEmissionPlan,
    starts_at: datetime,
    *,
    window_seconds: int | None = None,
) -> WindowEmissionPlan:
    _validate_aware_datetime(starts_at, field_name="starts_at")
    effective_window_seconds = (
        emission_plan.schedule_config.window_seconds if window_seconds is None else window_seconds
    )
    _validate_positive_int(effective_window_seconds, field_name="window_seconds")
    ends_at = starts_at + timedelta(seconds=effective_window_seconds)
    components: list[WindowEmissionComponent] = []
    total_emission = ZERO_DECIMAL
    for period in emission_plan.halving_periods:
        overlap_seconds = _overlap_seconds(starts_at, ends_at, period.starts_at, period.ends_at)
        if overlap_seconds <= 0:
            continue
        period_seconds = _seconds_between(period.starts_at, period.ends_at)
        with localcontext() as context:
            context.prec = DECIMAL_PRECISION
            component_cap = (
                period.emission_cap * Decimal(overlap_seconds) / Decimal(period_seconds)
            )
        total_emission += component_cap
        components.append(
            WindowEmissionComponent(
                period_index=period.period_index,
                overlap_seconds=overlap_seconds,
                emission_cap=component_cap,
            )
        )
    return WindowEmissionPlan(
        starts_at=starts_at,
        ends_at=ends_at,
        window_seconds=effective_window_seconds,
        miner_window_emission_cap=total_emission,
        components=tuple(components),
        owner_reserve_address=emission_plan.reserve_plan.owner_reserve_address,
    )


def _build_halving_periods(
    *,
    miner_emission_cap: Decimal,
    schedule_config: HalvingScheduleConfig,
) -> tuple[RewardHalvingPeriod, ...]:
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        weights = tuple(
            Decimal(1) / (Decimal(2) ** period_index)
            for period_index in range(schedule_config.period_count)
        )
        total_weight = sum(weights, ZERO_DECIMAL)
        periods = []
        for period_index, weight in enumerate(weights):
            starts_at = schedule_config.emission_start + timedelta(
                seconds=schedule_config.period_seconds * period_index
            )
            ends_at = starts_at + timedelta(seconds=schedule_config.period_seconds)
            emission_cap = miner_emission_cap * weight / total_weight
            full_window_cap = (
                emission_cap
                * Decimal(schedule_config.window_seconds)
                / Decimal(schedule_config.period_seconds)
            )
            periods.append(
                RewardHalvingPeriod(
                    period_index=period_index,
                    starts_at=starts_at,
                    ends_at=ends_at,
                    halving_weight=weight,
                    emission_cap=emission_cap,
                    full_window_emission_cap=full_window_cap,
                )
            )
    return tuple(periods)


def _coerce_decimal(value: object, *, field_name: str) -> Decimal:
    if isinstance(value, bool):
        raise TypeError(f"{field_name}_must_use_decimal_or_int_not_bool")
    if isinstance(value, float):
        raise TypeError(f"{field_name}_must_use_decimal_or_int_not_float")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation as exc:
            raise ValueError(f"{field_name}_must_be_decimal_text") from exc
    raise TypeError(f"{field_name}_must_use_decimal_or_int")


def _validate_non_negative(value: Decimal, *, field_name: str) -> None:
    if value < ZERO_DECIMAL:
        raise ValueError(f"{field_name}_must_be_non_negative")


def _validate_positive_int(value: object, *, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name}_must_be_int")
    if value <= 0:
        raise ValueError(f"{field_name}_must_be_positive")


def _validate_owner_reserve_address(owner_reserve_address: str) -> None:
    if not owner_reserve_address.strip():
        raise ValueError("owner_reserve_address_required")
    if owner_reserve_address != owner_reserve_address.strip():
        raise ValueError("owner_reserve_address_must_not_have_surrounding_whitespace")
    if any(character.isspace() for character in owner_reserve_address):
        raise ValueError("owner_reserve_address_must_not_contain_whitespace")
    ensure_no_raw_secret(owner_reserve_address, field_name="owner_reserve_address")


def _validate_bool(field_name: str, value: bool) -> None:
    if not isinstance(value, bool):
        raise TypeError(f"{field_name}_must_be_bool")


def _validate_disabled_flags(
    *,
    live_reward_enabled: bool,
    payout_executor_enabled: bool,
    chain_transfer_enabled: bool,
) -> None:
    _validate_bool("live_reward_enabled", live_reward_enabled)
    _validate_bool("payout_executor_enabled", payout_executor_enabled)
    _validate_bool("chain_transfer_enabled", chain_transfer_enabled)
    if live_reward_enabled:
        raise ValueError("reward_schedule_live_reward_forbidden")
    if payout_executor_enabled:
        raise ValueError("reward_schedule_payout_executor_forbidden")
    if chain_transfer_enabled:
        raise ValueError("reward_schedule_chain_transfer_forbidden")


def _validate_aware_datetime(value: datetime, *, field_name: str) -> None:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name}_must_be_timezone_aware")


def _seconds_between(starts_at: datetime, ends_at: datetime) -> int:
    delta = ends_at - starts_at
    if delta.microseconds:
        raise ValueError("schedule_boundaries_must_align_to_whole_seconds")
    return delta.days * 86_400 + delta.seconds


def _overlap_seconds(
    starts_at: datetime,
    ends_at: datetime,
    period_starts_at: datetime,
    period_ends_at: datetime,
) -> int:
    overlap_starts_at = max(starts_at, period_starts_at)
    overlap_ends_at = min(ends_at, period_ends_at)
    if overlap_ends_at <= overlap_starts_at:
        return 0
    return _seconds_between(overlap_starts_at, overlap_ends_at)

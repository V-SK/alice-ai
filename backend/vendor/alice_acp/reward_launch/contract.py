from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext

from alice_acp.evidence.types import ensure_no_raw_secret

DecimalInput = Decimal | int

BPS_DENOMINATOR = 10_000
MINIMUM_RESERVE_BPS = 2_000
FOUR_HOUR_WINDOW_SECONDS = 4 * 60 * 60
DECIMAL_PRECISION = 80
ZERO_DECIMAL = Decimal("0")

RESERVE_RECIPIENT_ADDRESS = "a2u85Mwf4TRgoGkPBCw8438qLVQMYctpJAFiGtVnM21RMK3rt"


@dataclass(frozen=True, slots=True)
class LaunchRewardBudget:
    total_reward_pool: DecimalInput
    already_paid_rewards: DecimalInput
    reserve_recipient_address: str = RESERVE_RECIPIENT_ADDRESS
    reserve_bps: int = MINIMUM_RESERVE_BPS
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False
    chain_transfer_enabled: bool = False

    def __post_init__(self) -> None:
        total_reward_pool = _coerce_decimal(
            self.total_reward_pool,
            field_name="total_reward_pool",
        )
        already_paid_rewards = _coerce_decimal(
            self.already_paid_rewards,
            field_name="already_paid_rewards",
        )
        _validate_non_negative_decimal(total_reward_pool, field_name="total_reward_pool")
        _validate_non_negative_decimal(already_paid_rewards, field_name="already_paid_rewards")
        if already_paid_rewards > total_reward_pool:
            raise ValueError("already_paid_rewards_must_not_exceed_total_reward_pool")
        _validate_reserve_bps(self.reserve_bps)
        _validate_reserve_recipient_address(self.reserve_recipient_address)
        _validate_disabled_flags(
            live_reward_enabled=self.live_reward_enabled,
            payout_executor_enabled=self.payout_executor_enabled,
            chain_transfer_enabled=self.chain_transfer_enabled,
        )
        object.__setattr__(self, "total_reward_pool", total_reward_pool)
        object.__setattr__(self, "already_paid_rewards", already_paid_rewards)


@dataclass(frozen=True, slots=True)
class LaunchRewardBudgetPlan:
    total_reward_pool: Decimal
    already_paid_rewards: Decimal
    reserve_bps: int
    reserve_recipient_address: str
    reserve_20_percent: Decimal
    reserve_amount: Decimal
    remaining_after_paid_rewards: Decimal
    remaining_distributable_pool: Decimal
    calculation_only: bool = True
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False
    chain_transfer_enabled: bool = False
    paid_acu: Decimal = ZERO_DECIMAL
    can_start_live_rewards: bool = False
    can_start_payout_executor: bool = False

    def __post_init__(self) -> None:
        _validate_bool("calculation_only", self.calculation_only)
        if not self.calculation_only:
            raise ValueError("launch_reward_budget_calculation_only_must_remain_true")
        _validate_disabled_flags(
            live_reward_enabled=self.live_reward_enabled,
            payout_executor_enabled=self.payout_executor_enabled,
            chain_transfer_enabled=self.chain_transfer_enabled,
        )
        if self.paid_acu != ZERO_DECIMAL:
            raise ValueError("launch_reward_budget_paid_acu_must_remain_zero")
        _validate_bool("can_start_live_rewards", self.can_start_live_rewards)
        if self.can_start_live_rewards:
            raise ValueError("launch_reward_budget_live_rewards_gate_must_remain_false")
        _validate_bool("can_start_payout_executor", self.can_start_payout_executor)
        if self.can_start_payout_executor:
            raise ValueError("launch_reward_budget_payout_gate_must_remain_false")


@dataclass(frozen=True, slots=True)
class LaunchRewardEpoch:
    epoch_index: int
    epoch_seconds: int
    halving_weight: DecimalInput

    def __post_init__(self) -> None:
        _validate_non_negative_int(self.epoch_index, field_name="epoch_index")
        _validate_positive_int(self.epoch_seconds, field_name="epoch_seconds")
        if self.epoch_seconds < FOUR_HOUR_WINDOW_SECONDS:
            raise ValueError("epoch_seconds_must_cover_four_hour_window")
        halving_weight = _coerce_decimal(self.halving_weight, field_name="halving_weight")
        _validate_positive_decimal(halving_weight, field_name="halving_weight")
        object.__setattr__(self, "halving_weight", halving_weight)


@dataclass(frozen=True, slots=True)
class LaunchRewardSchedule:
    epochs: tuple[LaunchRewardEpoch, ...]
    current_epoch_index: int = 0
    window_seconds: int = FOUR_HOUR_WINDOW_SECONDS
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False
    chain_transfer_enabled: bool = False

    def __post_init__(self) -> None:
        if any(not isinstance(epoch, LaunchRewardEpoch) for epoch in self.epochs):
            raise TypeError("epochs_must_contain_launch_reward_epoch_entries")
        epochs = tuple(self.epochs)
        if not epochs:
            raise ValueError("epochs_must_not_be_empty")
        _validate_non_negative_int(self.current_epoch_index, field_name="current_epoch_index")
        _validate_positive_int(self.window_seconds, field_name="window_seconds")
        if self.window_seconds != FOUR_HOUR_WINDOW_SECONDS:
            raise ValueError("window_seconds_must_equal_four_hours")
        _validate_unique_epoch_indexes(epochs)
        if _find_epoch(epochs, self.current_epoch_index) is None:
            raise ValueError("current_epoch_index_must_exist_in_epochs")
        _validate_disabled_flags(
            live_reward_enabled=self.live_reward_enabled,
            payout_executor_enabled=self.payout_executor_enabled,
            chain_transfer_enabled=self.chain_transfer_enabled,
        )
        object.__setattr__(self, "epochs", epochs)


@dataclass(frozen=True, slots=True)
class FourHourMintCap:
    current_epoch_index: int
    current_epoch_seconds: int
    current_epoch_halving_weight: Decimal
    total_halving_weight: Decimal
    current_epoch_mint_cap: Decimal
    window_seconds: int
    max_mint_for_window: Decimal
    calculation_only: bool = True
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False
    chain_transfer_enabled: bool = False

    def __post_init__(self) -> None:
        _validate_bool("calculation_only", self.calculation_only)
        if not self.calculation_only:
            raise ValueError("four_hour_mint_cap_calculation_only_must_remain_true")
        _validate_disabled_flags(
            live_reward_enabled=self.live_reward_enabled,
            payout_executor_enabled=self.payout_executor_enabled,
            chain_transfer_enabled=self.chain_transfer_enabled,
        )


@dataclass(frozen=True, slots=True)
class DuplicatePayoutClaim:
    payout_id: str
    recipient_address: str
    amount: DecimalInput

    def __post_init__(self) -> None:
        _validate_identifier(self.payout_id, field_name="payout_id")
        _validate_public_address(self.recipient_address, field_name="recipient_address")
        amount = _coerce_decimal(self.amount, field_name="amount")
        _validate_positive_decimal(amount, field_name="amount")
        object.__setattr__(self, "amount", amount)


@dataclass(frozen=True, slots=True)
class DuplicatePayoutGuardState:
    completed_payout_ids: tuple[str, ...] = ()
    pending_payout_ids: tuple[str, ...] = ()
    contract_only: bool = True
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False
    chain_transfer_enabled: bool = False

    def __post_init__(self) -> None:
        completed_payout_ids = tuple(self.completed_payout_ids)
        pending_payout_ids = tuple(self.pending_payout_ids)
        for payout_id in completed_payout_ids:
            _validate_identifier(payout_id, field_name="completed_payout_id")
        for payout_id in pending_payout_ids:
            _validate_identifier(payout_id, field_name="pending_payout_id")
        all_payout_ids = completed_payout_ids + pending_payout_ids
        if len(set(all_payout_ids)) != len(all_payout_ids):
            raise ValueError("duplicate_payout_guard_state_contains_duplicate_ids")
        _validate_bool("contract_only", self.contract_only)
        if not self.contract_only:
            raise ValueError("duplicate_payout_guard_must_remain_contract_only")
        _validate_disabled_flags(
            live_reward_enabled=self.live_reward_enabled,
            payout_executor_enabled=self.payout_executor_enabled,
            chain_transfer_enabled=self.chain_transfer_enabled,
        )
        object.__setattr__(self, "completed_payout_ids", completed_payout_ids)
        object.__setattr__(self, "pending_payout_ids", pending_payout_ids)


@dataclass(frozen=True, slots=True)
class DuplicatePayoutGuardDecision:
    payout_id: str
    duplicate_payout: bool
    seen_in_completed: bool
    seen_in_pending: bool
    contract_only: bool = True
    transfer_created: bool = False
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False
    chain_transfer_enabled: bool = False

    def __post_init__(self) -> None:
        _validate_bool("duplicate_payout", self.duplicate_payout)
        _validate_bool("seen_in_completed", self.seen_in_completed)
        _validate_bool("seen_in_pending", self.seen_in_pending)
        _validate_bool("contract_only", self.contract_only)
        _validate_bool("transfer_created", self.transfer_created)
        _validate_disabled_flags(
            live_reward_enabled=self.live_reward_enabled,
            payout_executor_enabled=self.payout_executor_enabled,
            chain_transfer_enabled=self.chain_transfer_enabled,
        )
        if not self.contract_only:
            raise ValueError("duplicate_payout_guard_decision_must_remain_contract_only")
        if self.transfer_created:
            raise ValueError("duplicate_payout_guard_must_not_create_transfer")


def compute_launch_reward_budget(budget: LaunchRewardBudget) -> LaunchRewardBudgetPlan:
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        reserve_20_percent = (
            budget.total_reward_pool
            * Decimal(MINIMUM_RESERVE_BPS)
            / Decimal(BPS_DENOMINATOR)
        )
        reserve_amount = (
            budget.total_reward_pool * Decimal(budget.reserve_bps) / Decimal(BPS_DENOMINATOR)
        )
        remaining_after_paid = budget.total_reward_pool - budget.already_paid_rewards
        remaining_distributable = remaining_after_paid - reserve_amount
    if remaining_distributable < ZERO_DECIMAL:
        raise ValueError("paid_rewards_plus_reserve_must_not_exceed_total_reward_pool")
    return LaunchRewardBudgetPlan(
        total_reward_pool=budget.total_reward_pool,
        already_paid_rewards=budget.already_paid_rewards,
        reserve_bps=budget.reserve_bps,
        reserve_recipient_address=budget.reserve_recipient_address,
        reserve_20_percent=reserve_20_percent,
        reserve_amount=reserve_amount,
        remaining_after_paid_rewards=remaining_after_paid,
        remaining_distributable_pool=remaining_distributable,
    )


def compute_four_hour_mint_cap(
    budget_plan: LaunchRewardBudgetPlan,
    schedule: LaunchRewardSchedule,
) -> FourHourMintCap:
    current_epoch = _find_epoch(schedule.epochs, schedule.current_epoch_index)
    if current_epoch is None:
        raise ValueError("current_epoch_index_must_exist_in_epochs")
    total_weight = sum((epoch.halving_weight for epoch in schedule.epochs), ZERO_DECIMAL)
    _validate_positive_decimal(total_weight, field_name="total_halving_weight")
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        current_epoch_mint_cap = (
            budget_plan.remaining_distributable_pool
            * current_epoch.halving_weight
            / total_weight
        )
        max_mint_for_window = (
            current_epoch_mint_cap
            * Decimal(schedule.window_seconds)
            / Decimal(current_epoch.epoch_seconds)
        )
    return FourHourMintCap(
        current_epoch_index=current_epoch.epoch_index,
        current_epoch_seconds=current_epoch.epoch_seconds,
        current_epoch_halving_weight=current_epoch.halving_weight,
        total_halving_weight=total_weight,
        current_epoch_mint_cap=current_epoch_mint_cap,
        window_seconds=schedule.window_seconds,
        max_mint_for_window=max_mint_for_window,
    )


def evaluate_duplicate_payout_guard(
    state: DuplicatePayoutGuardState,
    claim: DuplicatePayoutClaim,
) -> DuplicatePayoutGuardDecision:
    seen_in_completed = claim.payout_id in state.completed_payout_ids
    seen_in_pending = claim.payout_id in state.pending_payout_ids
    return DuplicatePayoutGuardDecision(
        payout_id=claim.payout_id,
        duplicate_payout=seen_in_completed or seen_in_pending,
        seen_in_completed=seen_in_completed,
        seen_in_pending=seen_in_pending,
    )


def _coerce_decimal(value: object, *, field_name: str) -> Decimal:
    if isinstance(value, bool):
        raise TypeError(f"{field_name}_must_use_decimal_or_int_not_bool")
    if isinstance(value, float):
        raise TypeError(f"{field_name}_must_use_decimal_or_int_not_float")
    if isinstance(value, Decimal):
        if not value.is_finite():
            raise ValueError(f"{field_name}_must_be_finite")
        return value
    if isinstance(value, int):
        return Decimal(value)
    raise TypeError(f"{field_name}_must_use_decimal_or_int")


def _validate_non_negative_decimal(value: Decimal, *, field_name: str) -> None:
    if value < ZERO_DECIMAL:
        raise ValueError(f"{field_name}_must_be_non_negative")


def _validate_positive_decimal(value: Decimal, *, field_name: str) -> None:
    if value <= ZERO_DECIMAL:
        raise ValueError(f"{field_name}_must_be_positive")


def _validate_non_negative_int(value: object, *, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name}_must_be_int")
    if value < 0:
        raise ValueError(f"{field_name}_must_be_non_negative")


def _validate_positive_int(value: object, *, field_name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name}_must_be_int")
    if value <= 0:
        raise ValueError(f"{field_name}_must_be_positive")


def _validate_reserve_bps(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("reserve_bps_must_be_int")
    if not 0 <= value <= BPS_DENOMINATOR:
        raise ValueError("reserve_bps_must_be_between_0_and_10000")
    if value < MINIMUM_RESERVE_BPS:
        raise ValueError("reserve_bps_must_be_at_least_2000")


def _validate_reserve_recipient_address(value: str) -> None:
    _validate_public_address(value, field_name="reserve_recipient_address")
    if value != RESERVE_RECIPIENT_ADDRESS:
        raise ValueError("reserve_recipient_address_must_equal_launch_reserve_recipient")


def _validate_public_address(value: str, *, field_name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name}_must_be_str")
    if not value.strip():
        raise ValueError(f"{field_name}_required")
    if value != value.strip():
        raise ValueError(f"{field_name}_must_not_have_surrounding_whitespace")
    if any(character.isspace() for character in value):
        raise ValueError(f"{field_name}_must_not_contain_whitespace")
    ensure_no_raw_secret(value, field_name=field_name)


def _validate_identifier(value: str, *, field_name: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name}_must_be_str")
    if not value.strip():
        raise ValueError(f"{field_name}_required")
    if value != value.strip():
        raise ValueError(f"{field_name}_must_not_have_surrounding_whitespace")
    if any(character.isspace() for character in value):
        raise ValueError(f"{field_name}_must_not_contain_whitespace")
    ensure_no_raw_secret(value, field_name=field_name)


def _validate_unique_epoch_indexes(epochs: tuple[LaunchRewardEpoch, ...]) -> None:
    epoch_indexes = [epoch.epoch_index for epoch in epochs]
    if len(set(epoch_indexes)) != len(epoch_indexes):
        raise ValueError("epoch_indexes_must_be_unique")


def _find_epoch(
    epochs: tuple[LaunchRewardEpoch, ...],
    current_epoch_index: int,
) -> LaunchRewardEpoch | None:
    for epoch in epochs:
        if epoch.epoch_index == current_epoch_index:
            return epoch
    return None


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
        raise ValueError("launch_reward_live_reward_forbidden")
    if payout_executor_enabled:
        raise ValueError("launch_reward_payout_executor_forbidden")
    if chain_transfer_enabled:
        raise ValueError("launch_reward_chain_transfer_forbidden")

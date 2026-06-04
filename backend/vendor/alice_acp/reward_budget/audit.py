from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, localcontext

from alice_acp.evidence.types import ensure_no_raw_secret

OWNER_RESERVE_PUBLIC_ADDRESS = "a2u85Mwf4TRgoGkPBCw8438qLVQMYctpJAFiGtVnM21RMK3rt"

BPS_DENOMINATOR = Decimal("10000")
DECIMAL_PRECISION = 80
DEFAULT_RESERVE_PERCENT = Decimal("20")
DEFAULT_WINDOW_HOURS = 4
SECONDS_PER_HOUR = 60 * 60
ZERO_DECIMAL = Decimal("0")

Q35_REWARD_BUDGET_VALID = "q35_reward_budget_valid"
Q35_TOTAL_POOL_CONFIRMATION_REQUIRED = "q35_total_pool_confirmation_required"
Q35_NEGATIVE_REMAINING_AFTER_PAID = "q35_negative_remaining_after_paid"
Q35_NEGATIVE_AVAILABLE_AFTER_RESERVE = "q35_negative_available_after_reserve"
Q35_HALVING_SCHEDULE_REQUIRED = "q35_halving_schedule_required"
Q35_RESERVE_ENFORCEMENT_REQUIRED = "q35_reserve_enforcement_required"
Q35_HALVING_ENFORCEMENT_REQUIRED = "q35_halving_enforcement_required"
Q35_DUPLICATE_PAYOUT_GUARD_REQUIRED = "q35_duplicate_payout_guard_required"
Q35_KILL_SWITCH_REQUIRED = "q35_kill_switch_required"
Q35_LOCAL_CHAIN_STATE_UNPROVEN = "q35_local_chain_state_unproven"
Q35_LIVE_REWARD_FORBIDDEN = "q35_live_reward_forbidden"
Q35_PAYOUT_EXECUTOR_FORBIDDEN = "q35_payout_executor_forbidden"

DecimalInput = Decimal | int | str


@dataclass(frozen=True, slots=True)
class HalvingScheduleEntry:
    epoch_index: int
    epoch_seconds: int
    halving_weight: DecimalInput

    def __post_init__(self) -> None:
        _validate_non_negative_int(self.epoch_index, field_name="epoch_index")
        _validate_positive_int(self.epoch_seconds, field_name="epoch_seconds")
        halving_weight = _coerce_decimal(self.halving_weight, field_name="halving_weight")
        if halving_weight <= ZERO_DECIMAL:
            raise ValueError("halving_weight_must_be_positive")
        object.__setattr__(self, "halving_weight", halving_weight)


@dataclass(frozen=True, slots=True)
class ChainUpgradeEvidence:
    local_chain_state_proven: bool = False
    reserve_enforced: bool = False
    halving_enforced: bool = False
    duplicate_payout_guard_enforced: bool = False
    kill_switch_enforced: bool = False

    def __post_init__(self) -> None:
        for field_name, value in (
            ("local_chain_state_proven", self.local_chain_state_proven),
            ("reserve_enforced", self.reserve_enforced),
            ("halving_enforced", self.halving_enforced),
            ("duplicate_payout_guard_enforced", self.duplicate_payout_guard_enforced),
            ("kill_switch_enforced", self.kill_switch_enforced),
        ):
            _validate_bool(field_name, value)

    @property
    def chain_upgrade_required(self) -> bool:
        return not all(
            (
                self.local_chain_state_proven,
                self.reserve_enforced,
                self.halving_enforced,
                self.duplicate_payout_guard_enforced,
                self.kill_switch_enforced,
            )
        )


@dataclass(frozen=True, slots=True)
class RewardBudgetAuditRequest:
    total_reward_pool: DecimalInput
    already_paid_rewards: DecimalInput
    halving_schedule: tuple[HalvingScheduleEntry, ...]
    approved_unpaid_rewards: DecimalInput = ZERO_DECIMAL
    reserve_percent: DecimalInput = DEFAULT_RESERVE_PERCENT
    window_hours: int = DEFAULT_WINDOW_HOURS
    current_epoch_index: int = 0
    owner_reserve_address: str = OWNER_RESERVE_PUBLIC_ADDRESS
    total_reward_pool_confirmed: bool = False
    chain_evidence: ChainUpgradeEvidence = field(default_factory=ChainUpgradeEvidence)
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False

    def __post_init__(self) -> None:
        total_reward_pool = _coerce_decimal(
            self.total_reward_pool,
            field_name="total_reward_pool",
        )
        already_paid_rewards = _coerce_decimal(
            self.already_paid_rewards,
            field_name="already_paid_rewards",
        )
        approved_unpaid_rewards = _coerce_decimal(
            self.approved_unpaid_rewards,
            field_name="approved_unpaid_rewards",
        )
        reserve_percent = _coerce_decimal(self.reserve_percent, field_name="reserve_percent")
        if total_reward_pool < ZERO_DECIMAL:
            raise ValueError("total_reward_pool_must_be_non_negative")
        if already_paid_rewards < ZERO_DECIMAL:
            raise ValueError("already_paid_rewards_must_be_non_negative")
        if approved_unpaid_rewards < ZERO_DECIMAL:
            raise ValueError("approved_unpaid_rewards_must_be_non_negative")
        if not ZERO_DECIMAL <= reserve_percent <= Decimal("100"):
            raise ValueError("reserve_percent_must_be_between_0_and_100")
        if reserve_percent != DEFAULT_RESERVE_PERCENT:
            raise ValueError("reserve_percent_must_equal_20")
        _validate_positive_int(self.window_hours, field_name="window_hours")
        _validate_non_negative_int(self.current_epoch_index, field_name="current_epoch_index")
        _validate_owner_reserve_address(self.owner_reserve_address)
        _validate_bool("total_reward_pool_confirmed", self.total_reward_pool_confirmed)
        _validate_bool("live_reward_enabled", self.live_reward_enabled)
        _validate_bool("payout_executor_enabled", self.payout_executor_enabled)
        if self.live_reward_enabled:
            raise ValueError(Q35_LIVE_REWARD_FORBIDDEN)
        if self.payout_executor_enabled:
            raise ValueError(Q35_PAYOUT_EXECUTOR_FORBIDDEN)
        if any(not isinstance(entry, HalvingScheduleEntry) for entry in self.halving_schedule):
            raise TypeError("halving_schedule_must_contain_halving_schedule_entries")
        object.__setattr__(self, "total_reward_pool", total_reward_pool)
        object.__setattr__(self, "already_paid_rewards", already_paid_rewards)
        object.__setattr__(self, "approved_unpaid_rewards", approved_unpaid_rewards)
        object.__setattr__(self, "reserve_percent", reserve_percent)
        object.__setattr__(self, "halving_schedule", tuple(self.halving_schedule))


@dataclass(frozen=True, slots=True)
class RewardBudgetAuditDecision:
    contract_valid: bool
    reason_codes: tuple[str, ...]
    total_reward_pool: Decimal
    already_paid_rewards: Decimal
    approved_unpaid_rewards: Decimal
    already_distributed_rewards: Decimal
    remaining_after_paid: Decimal
    reserve_20_percent: Decimal
    owner_reserve_percent: Decimal
    owner_reserve_amount: Decimal
    owner_reserve_address: str
    available_after_distributed_and_reserve: Decimal
    emittable_after_reserve: Decimal
    current_epoch_index: int
    current_epoch_mint_cap: Decimal
    current_window_hours: int
    current_window_mint_cap: Decimal
    chain_upgrade_required: bool
    reserve_enforced: bool
    halving_enforced: bool
    duplicate_payout_guard_enforced: bool
    kill_switch_enforced: bool
    local_chain_state_proven: bool
    can_start_4h_live_reward_window: bool = False
    can_start_live_reward: bool = False
    can_start_payout_executor: bool = False
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False
    paid_acu: Decimal = ZERO_DECIMAL
    calculation_only: bool = True


def evaluate_reward_budget_audit(
    request: RewardBudgetAuditRequest,
) -> RewardBudgetAuditDecision:
    reason_codes: list[str] = []
    remaining_after_paid = (
        request.total_reward_pool - request.already_paid_rewards - request.approved_unpaid_rewards
    )

    if not request.total_reward_pool_confirmed:
        reason_codes.append(Q35_TOTAL_POOL_CONFIRMATION_REQUIRED)
    if remaining_after_paid < ZERO_DECIMAL:
        reason_codes.append(Q35_NEGATIVE_REMAINING_AFTER_PAID)
    if not request.halving_schedule:
        reason_codes.append(Q35_HALVING_SCHEDULE_REQUIRED)
    if not request.chain_evidence.reserve_enforced:
        reason_codes.append(Q35_RESERVE_ENFORCEMENT_REQUIRED)
    if not request.chain_evidence.halving_enforced:
        reason_codes.append(Q35_HALVING_ENFORCEMENT_REQUIRED)
    if not request.chain_evidence.duplicate_payout_guard_enforced:
        reason_codes.append(Q35_DUPLICATE_PAYOUT_GUARD_REQUIRED)
    if not request.chain_evidence.kill_switch_enforced:
        reason_codes.append(Q35_KILL_SWITCH_REQUIRED)
    if not request.chain_evidence.local_chain_state_proven:
        reason_codes.append(Q35_LOCAL_CHAIN_STATE_UNPROVEN)

    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        owner_reserve_amount = max(remaining_after_paid, ZERO_DECIMAL) * (
            request.reserve_percent / Decimal("100")
        )
        available_after_distributed_and_reserve = remaining_after_paid - owner_reserve_amount
        emittable_after_reserve = max(available_after_distributed_and_reserve, ZERO_DECIMAL)
        current_epoch_mint_cap = _current_epoch_mint_cap(
            schedule=request.halving_schedule,
            current_epoch_index=request.current_epoch_index,
            emittable_after_reserve=emittable_after_reserve,
        )
        current_window_mint_cap = _current_window_mint_cap(
            schedule=request.halving_schedule,
            current_epoch_index=request.current_epoch_index,
            current_epoch_mint_cap=current_epoch_mint_cap,
            window_hours=request.window_hours,
        )

    if available_after_distributed_and_reserve < ZERO_DECIMAL:
        reason_codes.append(Q35_NEGATIVE_AVAILABLE_AFTER_RESERVE)

    contract_valid = not reason_codes

    return RewardBudgetAuditDecision(
        contract_valid=contract_valid,
        reason_codes=tuple(reason_codes) if reason_codes else (Q35_REWARD_BUDGET_VALID,),
        total_reward_pool=request.total_reward_pool,
        already_paid_rewards=request.already_paid_rewards,
        approved_unpaid_rewards=request.approved_unpaid_rewards,
        already_distributed_rewards=request.already_paid_rewards,
        remaining_after_paid=remaining_after_paid,
        reserve_20_percent=owner_reserve_amount,
        owner_reserve_percent=request.reserve_percent,
        owner_reserve_amount=owner_reserve_amount,
        owner_reserve_address=request.owner_reserve_address,
        available_after_distributed_and_reserve=available_after_distributed_and_reserve,
        emittable_after_reserve=emittable_after_reserve,
        current_epoch_index=request.current_epoch_index,
        current_epoch_mint_cap=current_epoch_mint_cap,
        current_window_hours=request.window_hours,
        current_window_mint_cap=current_window_mint_cap,
        chain_upgrade_required=request.chain_evidence.chain_upgrade_required,
        reserve_enforced=request.chain_evidence.reserve_enforced,
        halving_enforced=request.chain_evidence.halving_enforced,
        duplicate_payout_guard_enforced=request.chain_evidence.duplicate_payout_guard_enforced,
        kill_switch_enforced=request.chain_evidence.kill_switch_enforced,
        local_chain_state_proven=request.chain_evidence.local_chain_state_proven,
    )


def _current_epoch_mint_cap(
    *,
    schedule: tuple[HalvingScheduleEntry, ...],
    current_epoch_index: int,
    emittable_after_reserve: Decimal,
) -> Decimal:
    if not schedule or emittable_after_reserve <= ZERO_DECIMAL:
        return ZERO_DECIMAL
    total_weight = sum((entry.halving_weight for entry in schedule), ZERO_DECIMAL)
    current_entry = _schedule_entry(schedule, current_epoch_index=current_epoch_index)
    if current_entry is None:
        return ZERO_DECIMAL
    return emittable_after_reserve * current_entry.halving_weight / total_weight


def _current_window_mint_cap(
    *,
    schedule: tuple[HalvingScheduleEntry, ...],
    current_epoch_index: int,
    current_epoch_mint_cap: Decimal,
    window_hours: int,
) -> Decimal:
    current_entry = _schedule_entry(schedule, current_epoch_index=current_epoch_index)
    if current_entry is None or current_epoch_mint_cap <= ZERO_DECIMAL:
        return ZERO_DECIMAL
    window_seconds = Decimal(window_hours * SECONDS_PER_HOUR)
    return current_epoch_mint_cap * window_seconds / Decimal(current_entry.epoch_seconds)


def _schedule_entry(
    schedule: tuple[HalvingScheduleEntry, ...],
    *,
    current_epoch_index: int,
) -> HalvingScheduleEntry | None:
    for entry in schedule:
        if entry.epoch_index == current_epoch_index:
            return entry
    return None


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


def _validate_bool(field_name: str, value: bool) -> None:
    if not isinstance(value, bool):
        raise TypeError(f"{field_name}_must_be_bool")


def _validate_owner_reserve_address(owner_reserve_address: str) -> None:
    if not isinstance(owner_reserve_address, str):
        raise TypeError("owner_reserve_address_must_be_str")
    if not owner_reserve_address.strip():
        raise ValueError("owner_reserve_address_required")
    if owner_reserve_address != owner_reserve_address.strip():
        raise ValueError("owner_reserve_address_must_not_have_surrounding_whitespace")
    if any(character.isspace() for character in owner_reserve_address):
        raise ValueError("owner_reserve_address_must_not_contain_whitespace")
    ensure_no_raw_secret(owner_reserve_address, field_name="owner_reserve_address")


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

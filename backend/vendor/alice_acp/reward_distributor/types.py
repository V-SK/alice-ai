from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from alice_acp.reward_schedule import (
    DEFAULT_OWNER_RESERVE_BPS,
    RewardEmissionPlan,
    WindowEmissionPlan,
)

ZERO_DECIMAL = Decimal("0")
Q16_REQUIRED_OWNER_RESERVE_BPS = DEFAULT_OWNER_RESERVE_BPS

REWARD_DISTRIBUTOR_WINDOW_SECONDS_INVALID = "REWARD_DISTRIBUTOR_WINDOW_SECONDS_INVALID"
REWARD_DISTRIBUTOR_OWNER_RESERVE_REQUIRED = "REWARD_DISTRIBUTOR_OWNER_RESERVE_REQUIRED"
REWARD_DISTRIBUTOR_OWNER_RESERVE_ADDRESS_MISMATCH = (
    "REWARD_DISTRIBUTOR_OWNER_RESERVE_ADDRESS_MISMATCH"
)
REWARD_DISTRIBUTOR_DUPLICATE_WINDOW = "REWARD_DISTRIBUTOR_DUPLICATE_WINDOW"
REWARD_DISTRIBUTOR_KILL_SWITCH_ACTIVE = "REWARD_DISTRIBUTOR_KILL_SWITCH_ACTIVE"
REWARD_DISTRIBUTOR_MINER_CAP_EXCEEDED = "REWARD_DISTRIBUTOR_MINER_CAP_EXCEEDED"
REWARD_DISTRIBUTOR_WINDOW_CAP_EXCEEDS_SCHEDULE = (
    "REWARD_DISTRIBUTOR_WINDOW_CAP_EXCEEDS_SCHEDULE"
)
REWARD_DISTRIBUTOR_LIVE_REWARD_FORBIDDEN = "REWARD_DISTRIBUTOR_LIVE_REWARD_FORBIDDEN"
REWARD_DISTRIBUTOR_PAYOUT_EXECUTOR_FORBIDDEN = (
    "REWARD_DISTRIBUTOR_PAYOUT_EXECUTOR_FORBIDDEN"
)
REWARD_DISTRIBUTOR_CHAIN_TRANSFER_FORBIDDEN = "REWARD_DISTRIBUTOR_CHAIN_TRANSFER_FORBIDDEN"
REWARD_DISTRIBUTOR_PAID_ACU_TRANSFER_FORBIDDEN = (
    "REWARD_DISTRIBUTOR_PAID_ACU_TRANSFER_FORBIDDEN"
)

DecimalInput = Decimal | int | str


@dataclass(frozen=True, slots=True)
class MinerRewardCredit:
    miner_id: str
    reward_credit: DecimalInput

    def __post_init__(self) -> None:
        _validate_identifier("miner_id", self.miner_id)
        reward_credit = _coerce_decimal(self.reward_credit, field_name="reward_credit")
        _validate_non_negative(reward_credit, field_name="reward_credit")
        object.__setattr__(self, "reward_credit", reward_credit)


@dataclass(frozen=True, slots=True)
class ChainUpgradeControls:
    reward_distributor_contract_present: bool = False
    four_hour_window_enforced: bool = False
    owner_reserve_credit_enforced: bool = False
    miner_emission_cap_enforced: bool = False
    already_paid_replay_offset_enforced: bool = False
    duplicate_window_guard_enforced: bool = False
    kill_switch_gate_enforced: bool = False

    def __post_init__(self) -> None:
        for field_name, value in (
            ("reward_distributor_contract_present", self.reward_distributor_contract_present),
            ("four_hour_window_enforced", self.four_hour_window_enforced),
            ("owner_reserve_credit_enforced", self.owner_reserve_credit_enforced),
            ("miner_emission_cap_enforced", self.miner_emission_cap_enforced),
            (
                "already_paid_replay_offset_enforced",
                self.already_paid_replay_offset_enforced,
            ),
            ("duplicate_window_guard_enforced", self.duplicate_window_guard_enforced),
            ("kill_switch_gate_enforced", self.kill_switch_gate_enforced),
        ):
            _validate_bool(field_name, value)

    @property
    def chain_upgrade_required(self) -> bool:
        return not all(
            (
                self.reward_distributor_contract_present,
                self.four_hour_window_enforced,
                self.owner_reserve_credit_enforced,
                self.miner_emission_cap_enforced,
                self.already_paid_replay_offset_enforced,
                self.duplicate_window_guard_enforced,
                self.kill_switch_gate_enforced,
            )
        )


@dataclass(frozen=True, slots=True)
class RewardDistributorWindowRequest:
    window_id: str
    emission_plan: RewardEmissionPlan
    window_plan: WindowEmissionPlan
    miner_credits: Iterable[MinerRewardCredit] = ()
    processed_window_ids: Iterable[str] = field(default_factory=tuple)
    chain_controls: ChainUpgradeControls = field(default_factory=ChainUpgradeControls)
    kill_switch_active: bool = False
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False
    chain_transfer_enabled: bool = False
    paid_acu_live_transfer_allowed: bool = False

    def __post_init__(self) -> None:
        _validate_identifier("window_id", self.window_id)
        for field_name, value in (
            ("kill_switch_active", self.kill_switch_active),
            ("live_reward_enabled", self.live_reward_enabled),
            ("payout_executor_enabled", self.payout_executor_enabled),
            ("chain_transfer_enabled", self.chain_transfer_enabled),
            ("paid_acu_live_transfer_allowed", self.paid_acu_live_transfer_allowed),
        ):
            _validate_bool(field_name, value)
        if self.live_reward_enabled:
            raise ValueError(REWARD_DISTRIBUTOR_LIVE_REWARD_FORBIDDEN)
        if self.payout_executor_enabled:
            raise ValueError(REWARD_DISTRIBUTOR_PAYOUT_EXECUTOR_FORBIDDEN)
        if self.chain_transfer_enabled:
            raise ValueError(REWARD_DISTRIBUTOR_CHAIN_TRANSFER_FORBIDDEN)
        if self.paid_acu_live_transfer_allowed:
            raise ValueError(REWARD_DISTRIBUTOR_PAID_ACU_TRANSFER_FORBIDDEN)

        miner_credits = tuple(self.miner_credits)
        if any(not isinstance(credit, MinerRewardCredit) for credit in miner_credits):
            raise TypeError("miner_credits_must_be_miner_reward_credit_entries")
        object.__setattr__(self, "miner_credits", miner_credits)

        processed_window_ids = _coerce_window_id_set(self.processed_window_ids)
        object.__setattr__(self, "processed_window_ids", processed_window_ids)


@dataclass(frozen=True, slots=True)
class RewardDistributorWindowDecision:
    window_id: str
    contract_valid: bool
    reason_codes: tuple[str, ...]
    window_seconds: int
    owner_reserve_address: str
    owner_reserve_bps: int
    owner_reserve_credit: Decimal
    miner_emission_cap: Decimal
    miner_allocated_credit: Decimal
    already_paid_replay_offset: Decimal
    approved_unpaid_replay_offset: Decimal
    deducted_reward_offset: Decimal
    duplicate_window_guard_passed: bool
    kill_switch_gate_passed: bool
    chain_upgrade_required: bool
    calculation_only: bool = True
    can_start_4h_live_reward_window: bool = False
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False
    chain_transfer_enabled: bool = False
    paid_acu_live_transfer_allowed: bool = False


def _coerce_decimal(value: object, *, field_name: str) -> Decimal:
    if isinstance(value, float):
        raise TypeError(f"{field_name}_must_use_decimal_or_int_not_float")
    if isinstance(value, bool):
        raise TypeError(f"{field_name}_must_use_decimal_or_int_not_bool")
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


def _validate_bool(field_name: str, value: bool) -> None:
    if not isinstance(value, bool):
        raise TypeError(f"{field_name}_must_be_bool")


def _validate_identifier(field_name: str, value: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name}_must_be_str")
    if not value or not value.strip():
        raise ValueError(f"{field_name}_required")
    if value != value.strip():
        raise ValueError(f"{field_name}_must_not_have_surrounding_whitespace")
    if any(character.isspace() for character in value):
        raise ValueError(f"{field_name}_must_not_contain_whitespace")


def _coerce_window_id_set(values: Iterable[str]) -> frozenset[str]:
    if isinstance(values, str):
        raise TypeError("processed_window_ids_must_be_an_iterable_of_window_ids")
    processed_window_ids = frozenset(values)
    for window_id in processed_window_ids:
        _validate_identifier("processed_window_id", window_id)
    return processed_window_ids

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, localcontext

from alice_acp.evidence.types import ensure_no_raw_secret
from alice_acp.reward_launch.contract import (
    BPS_DENOMINATOR,
    DECIMAL_PRECISION,
    MINIMUM_RESERVE_BPS,
    RESERVE_RECIPIENT_ADDRESS,
    ZERO_DECIMAL,
    LaunchRewardBudget,
    compute_launch_reward_budget,
)

DecimalInput = Decimal | int

Q29_REWARD_HISTORY_BUDGET_VALID = "q29_reward_history_budget_valid"
Q29_DUPLICATE_PAYOUT_ID = "q29_duplicate_payout_id"
Q29_NEGATIVE_LEDGER_AMOUNT = "q29_negative_ledger_amount"
Q29_PAID_HISTORY_EXCEEDS_TOTAL_POOL = "q29_paid_history_exceeds_total_pool"
Q29_MISSING_SOURCE_LABEL = "q29_missing_source_label"
Q29_NON_PUBLIC_RESERVE_ADDRESS = "q29_non_public_reserve_address"
Q29_CHAIN_ISSUANCE_MISMATCH = "q29_chain_issuance_mismatch"
Q29_TOTAL_POOL_NEGATIVE = "q29_total_pool_negative"


@dataclass(frozen=True, slots=True)
class RewardLedgerEntry:
    payout_id: str
    amount: DecimalInput
    source_label: str | None

    def __post_init__(self) -> None:
        _validate_identifier(self.payout_id, field_name="payout_id")
        amount = _coerce_decimal(self.amount, field_name="amount")
        _validate_source_label_shape(self.source_label, field_name="source_label")
        object.__setattr__(self, "amount", amount)


@dataclass(frozen=True, slots=True)
class RewardHistorySnapshot:
    total_launch_reward_pool: DecimalInput
    total_pool_source_label: str | None
    ledger_entries: tuple[RewardLedgerEntry, ...]
    reserve_recipient_address: str = RESERVE_RECIPIENT_ADDRESS
    reserve_bps: int = MINIMUM_RESERVE_BPS
    chain_total_issued_rewards: DecimalInput | None = None
    chain_total_issued_rewards_source_label: str | None = None
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False
    chain_transfer_enabled: bool = False

    def __post_init__(self) -> None:
        total_launch_reward_pool = _coerce_decimal(
            self.total_launch_reward_pool,
            field_name="total_launch_reward_pool",
        )
        if any(not isinstance(entry, RewardLedgerEntry) for entry in self.ledger_entries):
            raise TypeError("ledger_entries_must_contain_reward_ledger_entries")
        _validate_source_label_shape(
            self.total_pool_source_label,
            field_name="total_pool_source_label",
        )
        _validate_public_address(
            self.reserve_recipient_address,
            field_name="reserve_recipient_address",
        )
        _validate_reserve_bps(self.reserve_bps)
        chain_total_issued_rewards = None
        if self.chain_total_issued_rewards is not None:
            chain_total_issued_rewards = _coerce_decimal(
                self.chain_total_issued_rewards,
                field_name="chain_total_issued_rewards",
            )
            _validate_source_label_shape(
                self.chain_total_issued_rewards_source_label,
                field_name="chain_total_issued_rewards_source_label",
            )
        elif self.chain_total_issued_rewards_source_label is not None:
            _validate_source_label_shape(
                self.chain_total_issued_rewards_source_label,
                field_name="chain_total_issued_rewards_source_label",
            )
        _validate_disabled_flags(
            live_reward_enabled=self.live_reward_enabled,
            payout_executor_enabled=self.payout_executor_enabled,
            chain_transfer_enabled=self.chain_transfer_enabled,
        )
        object.__setattr__(self, "total_launch_reward_pool", total_launch_reward_pool)
        object.__setattr__(self, "ledger_entries", tuple(self.ledger_entries))
        object.__setattr__(self, "chain_total_issued_rewards", chain_total_issued_rewards)


@dataclass(frozen=True, slots=True)
class RewardBudgetAuditResult:
    contract_valid: bool
    reason_codes: tuple[str, ...]
    total_launch_reward_pool: Decimal
    total_pool_source_label: str | None
    already_issued_rewards: Decimal
    remaining_after_paid_history: Decimal
    reserve_bps: int
    reserve_recipient_address: str
    reserve_20_percent: Decimal
    reserve_amount: Decimal
    distributable_live_budget: Decimal
    duplicate_payout_ids: tuple[str, ...] = ()
    negative_payout_ids: tuple[str, ...] = ()
    missing_source_labels: tuple[str, ...] = ()
    chain_total_issued_rewards: Decimal | None = None
    chain_total_issued_rewards_source_label: str | None = None
    calculation_only: bool = True
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False
    chain_transfer_enabled: bool = False
    paid_acu: Decimal = ZERO_DECIMAL
    can_start_live_rewards: bool = False
    can_start_payout_executor: bool = False

    def __post_init__(self) -> None:
        _validate_bool("contract_valid", self.contract_valid)
        if not self.reason_codes:
            raise ValueError("reason_codes_must_not_be_empty")
        _validate_bool("calculation_only", self.calculation_only)
        if not self.calculation_only:
            raise ValueError("q29_reward_history_budget_must_remain_calculation_only")
        _validate_disabled_flags(
            live_reward_enabled=self.live_reward_enabled,
            payout_executor_enabled=self.payout_executor_enabled,
            chain_transfer_enabled=self.chain_transfer_enabled,
        )
        if self.paid_acu != ZERO_DECIMAL:
            raise ValueError("q29_reward_history_budget_paid_acu_must_remain_zero")
        _validate_bool("can_start_live_rewards", self.can_start_live_rewards)
        if self.can_start_live_rewards:
            raise ValueError("q29_reward_history_budget_live_rewards_gate_must_remain_false")
        _validate_bool("can_start_payout_executor", self.can_start_payout_executor)
        if self.can_start_payout_executor:
            raise ValueError("q29_reward_history_budget_payout_gate_must_remain_false")


def audit_reward_history_budget(snapshot: RewardHistorySnapshot) -> RewardBudgetAuditResult:
    reason_codes: list[str] = []
    duplicate_payout_ids = _duplicate_payout_ids(snapshot.ledger_entries)
    negative_payout_ids = tuple(
        entry.payout_id for entry in snapshot.ledger_entries if entry.amount < ZERO_DECIMAL
    )
    missing_source_labels = _missing_source_labels(snapshot)
    already_issued_rewards = sum(
        (entry.amount for entry in snapshot.ledger_entries),
        ZERO_DECIMAL,
    )

    if snapshot.total_launch_reward_pool < ZERO_DECIMAL:
        reason_codes.append(Q29_TOTAL_POOL_NEGATIVE)
    if duplicate_payout_ids:
        reason_codes.append(Q29_DUPLICATE_PAYOUT_ID)
    if negative_payout_ids:
        reason_codes.append(Q29_NEGATIVE_LEDGER_AMOUNT)
    if already_issued_rewards > snapshot.total_launch_reward_pool:
        reason_codes.append(Q29_PAID_HISTORY_EXCEEDS_TOTAL_POOL)
    if missing_source_labels:
        reason_codes.append(Q29_MISSING_SOURCE_LABEL)
    if snapshot.reserve_recipient_address != RESERVE_RECIPIENT_ADDRESS:
        reason_codes.append(Q29_NON_PUBLIC_RESERVE_ADDRESS)
    if (
        snapshot.chain_total_issued_rewards is not None
        and snapshot.chain_total_issued_rewards != already_issued_rewards
    ):
        reason_codes.append(Q29_CHAIN_ISSUANCE_MISMATCH)

    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        remaining_after_paid_history = (
            snapshot.total_launch_reward_pool - already_issued_rewards
        )

    if (
        Q29_TOTAL_POOL_NEGATIVE in reason_codes
        or Q29_NEGATIVE_LEDGER_AMOUNT in reason_codes
        or Q29_PAID_HISTORY_EXCEEDS_TOTAL_POOL in reason_codes
        or Q29_NON_PUBLIC_RESERVE_ADDRESS in reason_codes
    ):
        reserve_20_percent = ZERO_DECIMAL
        reserve_amount = ZERO_DECIMAL
        distributable_live_budget = ZERO_DECIMAL
    else:
        post_paid_budget = compute_launch_reward_budget(
            LaunchRewardBudget(
                total_reward_pool=remaining_after_paid_history,
                already_paid_rewards=ZERO_DECIMAL,
                reserve_recipient_address=snapshot.reserve_recipient_address,
                reserve_bps=snapshot.reserve_bps,
            )
        )
        reserve_20_percent = post_paid_budget.reserve_20_percent
        reserve_amount = post_paid_budget.reserve_amount
        distributable_live_budget = post_paid_budget.remaining_distributable_pool

    contract_valid = not reason_codes

    return RewardBudgetAuditResult(
        contract_valid=contract_valid,
        reason_codes=tuple(reason_codes) if reason_codes else (Q29_REWARD_HISTORY_BUDGET_VALID,),
        total_launch_reward_pool=snapshot.total_launch_reward_pool,
        total_pool_source_label=snapshot.total_pool_source_label,
        already_issued_rewards=already_issued_rewards,
        remaining_after_paid_history=remaining_after_paid_history,
        reserve_bps=snapshot.reserve_bps,
        reserve_recipient_address=snapshot.reserve_recipient_address,
        reserve_20_percent=reserve_20_percent,
        reserve_amount=reserve_amount,
        distributable_live_budget=distributable_live_budget,
        duplicate_payout_ids=duplicate_payout_ids,
        negative_payout_ids=negative_payout_ids,
        missing_source_labels=missing_source_labels,
        chain_total_issued_rewards=snapshot.chain_total_issued_rewards,
        chain_total_issued_rewards_source_label=(
            snapshot.chain_total_issued_rewards_source_label
        ),
    )


def _duplicate_payout_ids(entries: tuple[RewardLedgerEntry, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    duplicates: list[str] = []
    for entry in entries:
        if entry.payout_id in seen and entry.payout_id not in duplicates:
            duplicates.append(entry.payout_id)
        seen.add(entry.payout_id)
    return tuple(duplicates)


def _missing_source_labels(snapshot: RewardHistorySnapshot) -> tuple[str, ...]:
    missing: list[str] = []
    if not _has_label(snapshot.total_pool_source_label):
        missing.append("total_pool_source_label")
    if (
        snapshot.chain_total_issued_rewards is not None
        and not _has_label(snapshot.chain_total_issued_rewards_source_label)
    ):
        missing.append("chain_total_issued_rewards_source_label")
    for index, entry in enumerate(snapshot.ledger_entries):
        if not _has_label(entry.source_label):
            missing.append(f"ledger_entries[{index}].source_label:{entry.payout_id}")
    return tuple(missing)


def _has_label(value: str | None) -> bool:
    return bool((value or "").strip())


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


def _validate_reserve_bps(value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError("reserve_bps_must_be_int")
    if not 0 <= value <= BPS_DENOMINATOR:
        raise ValueError("reserve_bps_must_be_between_0_and_10000")
    if value != MINIMUM_RESERVE_BPS:
        raise ValueError("reserve_bps_must_equal_2000_for_q29_current_policy")


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


def _validate_source_label_shape(value: str | None, *, field_name: str) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        raise TypeError(f"{field_name}_must_be_str")
    ensure_no_raw_secret(value, field_name=field_name)


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
        raise ValueError("q29_live_reward_forbidden")
    if payout_executor_enabled:
        raise ValueError("q29_payout_executor_forbidden")
    if chain_transfer_enabled:
        raise ValueError("q29_chain_transfer_forbidden")

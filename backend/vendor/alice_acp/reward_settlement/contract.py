from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation, localcontext
from typing import Literal

from alice_acp.reward_budget import RewardBudgetAuditDecision

DECIMAL_PRECISION = 80
ZERO_DECIMAL = Decimal("0")

Q36_SETTLEMENT_DRY_RUN_OK = "q36_settlement_dry_run_ok"
Q36_DUPLICATE_PAYOUT_GUARD_APPLIED = "q36_duplicate_payout_guard_applied"
Q36_KILL_SWITCH_ACTIVE = "q36_kill_switch_active"
Q36_Q35_BUDGET_INVALID = "q36_q35_reward_budget_invalid"
Q36_Q35_RESERVE_NOT_ENFORCED = "q36_q35_reserve_not_enforced"
Q36_WINDOW_CAP_EXCEEDED = "q36_window_cap_exceeded"
Q36_LIVE_REWARD_FORBIDDEN = "q36_live_reward_forbidden"
Q36_PAYOUT_EXECUTOR_FORBIDDEN = "q36_payout_executor_forbidden"
Q36_CHAIN_TRANSFER_FORBIDDEN = "q36_chain_transfer_forbidden"

DecimalInput = Decimal | int | str
CapPolicy = Literal["fail_closed", "clamp"]
SettlementStatus = Literal["dry_run_ok", "rejected", "blocked"]


@dataclass(frozen=True, slots=True)
class RewardableCreditProof:
    passport_id: str
    device_id: str
    lane: str
    proof_id: str
    score: DecimalInput
    credit: DecimalInput

    def __post_init__(self) -> None:
        for field_name, value in (
            ("passport_id", self.passport_id),
            ("device_id", self.device_id),
            ("lane", self.lane),
            ("proof_id", self.proof_id),
        ):
            _validate_text(field_name, value)
        score = _coerce_decimal(self.score, field_name="score")
        credit = _coerce_decimal(self.credit, field_name="credit")
        if score < ZERO_DECIMAL:
            raise ValueError("score_must_be_non_negative")
        if credit < ZERO_DECIMAL:
            raise ValueError("credit_must_be_non_negative")
        object.__setattr__(self, "score", score)
        object.__setattr__(self, "credit", credit)

    def duplicate_guard_key(self, *, settlement_key: str, window_id: str) -> tuple[str, ...]:
        return (
            settlement_key,
            window_id,
            self.passport_id,
            self.device_id,
            self.proof_id,
        )

    @property
    def device_key(self) -> tuple[str, str]:
        return (self.passport_id, self.device_id)


@dataclass(frozen=True, slots=True)
class PayoutPlan:
    plan_id: str
    executable: bool = False
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False
    chain_transfer_enabled: bool = False
    reason_code: str = Q36_PAYOUT_EXECUTOR_FORBIDDEN

    def __post_init__(self) -> None:
        _validate_text("plan_id", self.plan_id)
        _validate_bool("executable", self.executable)
        _validate_bool("live_reward_enabled", self.live_reward_enabled)
        _validate_bool("payout_executor_enabled", self.payout_executor_enabled)
        _validate_bool("chain_transfer_enabled", self.chain_transfer_enabled)
        if self.executable:
            raise ValueError("q36_payout_plan_must_be_non_executable")
        if self.live_reward_enabled:
            raise ValueError(Q36_LIVE_REWARD_FORBIDDEN)
        if self.payout_executor_enabled:
            raise ValueError(Q36_PAYOUT_EXECUTOR_FORBIDDEN)
        if self.chain_transfer_enabled:
            raise ValueError(Q36_CHAIN_TRANSFER_FORBIDDEN)

    def execute(self) -> None:
        raise RuntimeError(Q36_PAYOUT_EXECUTOR_FORBIDDEN)


RewardExecutionPlan = PayoutPlan


@dataclass(frozen=True, slots=True)
class LiveRewardSettlementRequest:
    reward_budget: RewardBudgetAuditDecision
    settlement_key: str
    window_id: str
    credits: tuple[RewardableCreditProof, ...]
    cap_policy: CapPolicy = "fail_closed"
    kill_switch_active: bool = False
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False
    chain_transfer_enabled: bool = False

    def __post_init__(self) -> None:
        if not isinstance(self.reward_budget, RewardBudgetAuditDecision):
            raise TypeError("reward_budget_must_be_reward_budget_audit_decision")
        _validate_text("settlement_key", self.settlement_key)
        _validate_text("window_id", self.window_id)
        if self.cap_policy not in {"fail_closed", "clamp"}:
            raise ValueError("cap_policy_must_be_fail_closed_or_clamp")
        _validate_bool("kill_switch_active", self.kill_switch_active)
        _validate_bool("live_reward_enabled", self.live_reward_enabled)
        _validate_bool("payout_executor_enabled", self.payout_executor_enabled)
        _validate_bool("chain_transfer_enabled", self.chain_transfer_enabled)
        if self.live_reward_enabled:
            raise ValueError(Q36_LIVE_REWARD_FORBIDDEN)
        if self.payout_executor_enabled:
            raise ValueError(Q36_PAYOUT_EXECUTOR_FORBIDDEN)
        if self.chain_transfer_enabled:
            raise ValueError(Q36_CHAIN_TRANSFER_FORBIDDEN)
        if any(not isinstance(credit, RewardableCreditProof) for credit in self.credits):
            raise TypeError("credits_must_contain_rewardable_credit_proofs")
        object.__setattr__(self, "credits", tuple(self.credits))


@dataclass(frozen=True, slots=True)
class SettlementDryRunSummary:
    status: SettlementStatus
    reason_codes: tuple[str, ...]
    settlement_key: str
    window_id: str
    per_device_credit: dict[tuple[str, str], Decimal]
    total_emission_requested: Decimal
    total_emission_approved: Decimal
    q35_current_window_mint_cap: Decimal
    reserve_enforced: bool
    window_cap_enforced: bool
    duplicate_payout_guard_enforced: bool
    kill_switch_checked: bool
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False
    chain_transfer_enabled: bool = False
    calculation_only: bool = True
    duplicate_guard_keys: tuple[tuple[str, ...], ...] = ()
    duplicate_payout_guard_hits: int = 0
    payout_plan: PayoutPlan = field(default_factory=lambda: PayoutPlan(plan_id="q36-default-off"))

    @property
    def accepted(self) -> bool:
        return self.status == "dry_run_ok"


def evaluate_live_reward_settlement(
    request: LiveRewardSettlementRequest,
) -> SettlementDryRunSummary:
    reason_codes: list[str] = []
    unique_device_credit: dict[tuple[str, str], Decimal] = {}
    duplicate_guard_keys: list[tuple[str, ...]] = []
    seen_guard_keys: set[tuple[str, ...]] = set()

    for credit in request.credits:
        guard_key = credit.duplicate_guard_key(
            settlement_key=request.settlement_key,
            window_id=request.window_id,
        )
        if guard_key in seen_guard_keys:
            duplicate_guard_keys.append(guard_key)
            continue
        seen_guard_keys.add(guard_key)
        unique_device_credit[credit.device_key] = (
            unique_device_credit.get(credit.device_key, ZERO_DECIMAL) + credit.credit
        )

    if duplicate_guard_keys:
        reason_codes.append(Q36_DUPLICATE_PAYOUT_GUARD_APPLIED)

    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        total_emission_requested = sum(unique_device_credit.values(), ZERO_DECIMAL)
        total_emission_approved = total_emission_requested

        if (
            request.cap_policy == "clamp"
            and request.reward_budget.current_window_mint_cap < total_emission_requested
        ):
            total_emission_approved = request.reward_budget.current_window_mint_cap
            unique_device_credit = _clamp_device_credits(
                unique_device_credit,
                approved_total=total_emission_approved,
                requested_total=total_emission_requested,
            )

    if not request.reward_budget.contract_valid:
        reason_codes.append(Q36_Q35_BUDGET_INVALID)
    if not request.reward_budget.reserve_enforced:
        reason_codes.append(Q36_Q35_RESERVE_NOT_ENFORCED)
    if Q36_Q35_BUDGET_INVALID in reason_codes or Q36_Q35_RESERVE_NOT_ENFORCED in reason_codes:
        total_emission_approved = ZERO_DECIMAL
    if total_emission_requested > request.reward_budget.current_window_mint_cap:
        reason_codes.append(Q36_WINDOW_CAP_EXCEEDED)
        if request.cap_policy == "fail_closed":
            total_emission_approved = ZERO_DECIMAL
    if request.kill_switch_active:
        reason_codes.append(Q36_KILL_SWITCH_ACTIVE)
        total_emission_approved = ZERO_DECIMAL

    status: SettlementStatus = "dry_run_ok"
    if request.kill_switch_active:
        status = "blocked"
    elif any(
        reason
        in {
            Q36_Q35_BUDGET_INVALID,
            Q36_Q35_RESERVE_NOT_ENFORCED,
            Q36_WINDOW_CAP_EXCEEDED,
        }
        for reason in reason_codes
    ) and request.cap_policy != "clamp":
        status = "rejected"
    elif Q36_Q35_BUDGET_INVALID in reason_codes or Q36_Q35_RESERVE_NOT_ENFORCED in reason_codes:
        status = "rejected"

    if not reason_codes:
        reason_codes.append(Q36_SETTLEMENT_DRY_RUN_OK)

    return SettlementDryRunSummary(
        status=status,
        reason_codes=tuple(reason_codes),
        settlement_key=request.settlement_key,
        window_id=request.window_id,
        per_device_credit=unique_device_credit,
        total_emission_requested=total_emission_requested,
        total_emission_approved=total_emission_approved,
        q35_current_window_mint_cap=request.reward_budget.current_window_mint_cap,
        reserve_enforced=request.reward_budget.reserve_enforced,
        window_cap_enforced=True,
        duplicate_payout_guard_enforced=True,
        kill_switch_checked=True,
        duplicate_guard_keys=tuple(duplicate_guard_keys),
        duplicate_payout_guard_hits=len(duplicate_guard_keys),
        payout_plan=PayoutPlan(plan_id=f"{request.settlement_key}:{request.window_id}:dry-run"),
    )


def _clamp_device_credits(
    device_credits: dict[tuple[str, str], Decimal],
    *,
    approved_total: Decimal,
    requested_total: Decimal,
) -> dict[tuple[str, str], Decimal]:
    if requested_total <= ZERO_DECIMAL or approved_total <= ZERO_DECIMAL:
        return {device_key: ZERO_DECIMAL for device_key in device_credits}
    ratio = approved_total / requested_total
    return {
        device_key: credit * ratio
        for device_key, credit in device_credits.items()
    }


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


def _validate_text(field_name: str, value: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name}_must_be_str")
    if not value.strip():
        raise ValueError(f"{field_name}_required")
    if value != value.strip():
        raise ValueError(f"{field_name}_must_not_have_surrounding_whitespace")

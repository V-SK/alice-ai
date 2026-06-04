from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal, InvalidOperation, localcontext

from alice_acp.evidence.types import ensure_no_raw_secret
from alice_acp.reward_launch.contract import (
    BPS_DENOMINATOR,
    DECIMAL_PRECISION,
    FOUR_HOUR_WINDOW_SECONDS,
    MINIMUM_RESERVE_BPS,
    RESERVE_RECIPIENT_ADDRESS,
)
from alice_acp.shadow_server.types import (
    DEFAULT_WINDOW_DURATION,
    MAIN_POOL_AI,
    MAIN_POOL_GPU_PRL,
    MAIN_POOL_GPU_RVN,
    SCRYPT_POOL,
    XMR_POOL,
    RewardStatement,
    SettlementResult,
)

DecimalInput = Decimal | int | str

Q27_REQUIRED_RUNTIME_SPEC_VERSION = 111
Q27_REQUIRED_RUNTIME_CALLS = frozenset(
    {
        "set_owner_reserve_account",
        "settle_live_reward_window",
    }
)

Q27_MAIN_MINER_LANE_BPS = 7_000
Q27_XMR_MINER_LANE_BPS = 1_500
Q27_SCRYPT_MINER_LANE_BPS = 1_500

Q27_RUNTIME_SPEC_VERSION_TOO_OLD = "q27_runtime_spec_version_too_old"
Q27_RUNTIME_CALLS_MISSING = "q27_runtime_calls_missing"
Q27_OWNER_RESERVE_REQUIRED = "q27_owner_reserve_required"
Q27_OWNER_RESERVE_BPS_REQUIRED = "q27_owner_reserve_bps_required"
Q27_DUPLICATE_WINDOW_ID = "q27_duplicate_window_id"
Q27_PAID_ACU_FORBIDDEN = "q27_paid_acu_forbidden"
Q27_LIVE_REWARD_FORBIDDEN = "q27_live_reward_forbidden"
Q27_PAYOUT_EXECUTOR_FORBIDDEN = "q27_payout_executor_forbidden"
Q27_CHAIN_TRANSFER_FORBIDDEN = "q27_chain_transfer_forbidden"
Q27_SIGNING_FORBIDDEN = "q27_signing_forbidden"
Q27_SUBMISSION_FORBIDDEN = "q27_submission_forbidden"
Q27_WINDOW_MUST_BE_FOUR_HOURS = "q27_shadow_window_must_be_four_hours"

ZERO_DECIMAL = Decimal("0")


@dataclass(frozen=True, slots=True)
class RuntimeMetadataEvidence:
    spec_version: int
    calls: tuple[str, ...]
    metadata_ref: str

    def __post_init__(self) -> None:
        _validate_non_negative_int("spec_version", self.spec_version)
        _validate_identifier("metadata_ref", self.metadata_ref)
        calls = tuple(self.calls)
        for call in calls:
            _validate_runtime_call(call)
        if len(set(calls)) != len(calls):
            raise ValueError("q27_runtime_calls_must_be_unique")
        object.__setattr__(self, "calls", calls)

    @property
    def missing_required_calls(self) -> frozenset[str]:
        return Q27_REQUIRED_RUNTIME_CALLS.difference(self.calls)

    @property
    def can_prepare_live_window(self) -> bool:
        return (
            self.spec_version >= Q27_REQUIRED_RUNTIME_SPEC_VERSION
            and not self.missing_required_calls
        )


@dataclass(frozen=True, slots=True)
class LiveRewardWindowMinerCredit:
    miner_id: str
    passport_id: str
    device_id: str
    lane: str
    dry_run_credit: DecimalInput

    def __post_init__(self) -> None:
        _validate_identifier("miner_id", self.miner_id)
        _validate_identifier("passport_id", self.passport_id)
        _validate_identifier("device_id", self.device_id)
        _validate_lane(self.lane)
        dry_run_credit = _coerce_decimal(self.dry_run_credit, field_name="dry_run_credit")
        _validate_non_negative_decimal(dry_run_credit, field_name="dry_run_credit")
        object.__setattr__(self, "dry_run_credit", dry_run_credit)


@dataclass(frozen=True, slots=True)
class LiveRewardWindowLaneAllocation:
    lane: str
    lane_share_bps: int
    lane_budget: DecimalInput
    miner_credits: tuple[LiveRewardWindowMinerCredit, ...] = ()
    unallocated_credit: DecimalInput = ZERO_DECIMAL

    def __post_init__(self) -> None:
        _validate_lane(self.lane)
        _validate_bps("lane_share_bps", self.lane_share_bps)
        lane_budget = _coerce_decimal(self.lane_budget, field_name="lane_budget")
        unallocated_credit = _coerce_decimal(
            self.unallocated_credit,
            field_name="unallocated_credit",
        )
        _validate_non_negative_decimal(lane_budget, field_name="lane_budget")
        _validate_non_negative_decimal(unallocated_credit, field_name="unallocated_credit")
        miner_credits = tuple(self.miner_credits)
        if any(not isinstance(credit, LiveRewardWindowMinerCredit) for credit in miner_credits):
            raise TypeError("miner_credits_must_contain_live_reward_window_miner_credit")
        if any(credit.lane != self.lane for credit in miner_credits):
            raise ValueError("miner_credit_lane_must_match_lane_allocation")
        total_credit = sum((credit.dry_run_credit for credit in miner_credits), ZERO_DECIMAL)
        if total_credit + unallocated_credit > lane_budget:
            raise ValueError("q27_lane_credit_exceeds_lane_budget")
        object.__setattr__(self, "lane_budget", lane_budget)
        object.__setattr__(self, "miner_credits", miner_credits)
        object.__setattr__(self, "unallocated_credit", unallocated_credit)


@dataclass(frozen=True, slots=True)
class LiveRewardWindowPayload:
    window_id: str
    starts_at_iso: str
    ends_at_iso: str
    runtime_spec_version: int
    runtime_metadata_ref: str
    runtime_calls: tuple[str, ...]
    owner_reserve_address: str
    owner_reserve_bps: int
    owner_reserve_credit: DecimalInput
    miner_after_reserve_emission: DecimalInput
    lane_allocations: tuple[LiveRewardWindowLaneAllocation, ...]
    dry_run: bool = True
    can_prepare_live_window: bool = True
    signed: bool = False
    submitted: bool = False
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False
    chain_transfer_enabled: bool = False
    paid_acu: DecimalInput = ZERO_DECIMAL

    def __post_init__(self) -> None:
        _validate_identifier("window_id", self.window_id)
        _validate_iso_text("starts_at_iso", self.starts_at_iso)
        _validate_iso_text("ends_at_iso", self.ends_at_iso)
        _validate_non_negative_int("runtime_spec_version", self.runtime_spec_version)
        _validate_identifier("runtime_metadata_ref", self.runtime_metadata_ref)
        runtime_calls = tuple(self.runtime_calls)
        for call in runtime_calls:
            _validate_runtime_call(call)
        _validate_owner_reserve(self.owner_reserve_address, self.owner_reserve_bps)
        owner_reserve_credit = _coerce_decimal(
            self.owner_reserve_credit,
            field_name="owner_reserve_credit",
        )
        miner_after_reserve_emission = _coerce_decimal(
            self.miner_after_reserve_emission,
            field_name="miner_after_reserve_emission",
        )
        paid_acu = _coerce_decimal(self.paid_acu, field_name="paid_acu")
        _validate_non_negative_decimal(owner_reserve_credit, field_name="owner_reserve_credit")
        _validate_non_negative_decimal(
            miner_after_reserve_emission,
            field_name="miner_after_reserve_emission",
        )
        _validate_default_off_flags(
            live_reward_enabled=self.live_reward_enabled,
            payout_executor_enabled=self.payout_executor_enabled,
            chain_transfer_enabled=self.chain_transfer_enabled,
        )
        _validate_bool("dry_run", self.dry_run)
        if not self.dry_run:
            raise ValueError("q27_payload_must_remain_dry_run")
        _validate_bool("can_prepare_live_window", self.can_prepare_live_window)
        if not self.can_prepare_live_window:
            raise ValueError("q27_payload_requires_verified_runtime_metadata")
        _validate_bool("signed", self.signed)
        if self.signed:
            raise ValueError(Q27_SIGNING_FORBIDDEN)
        _validate_bool("submitted", self.submitted)
        if self.submitted:
            raise ValueError(Q27_SUBMISSION_FORBIDDEN)
        if paid_acu != ZERO_DECIMAL:
            raise ValueError(Q27_PAID_ACU_FORBIDDEN)
        lane_allocations = tuple(self.lane_allocations)
        if any(
            not isinstance(allocation, LiveRewardWindowLaneAllocation)
            for allocation in lane_allocations
        ):
            raise TypeError("lane_allocations_must_contain_live_reward_window_lane_allocation")
        if tuple(allocation.lane for allocation in lane_allocations) != _q27_lane_order():
            raise ValueError("q27_lane_allocations_must_use_70_15_15_miner_lane_order")
        lane_total = sum((allocation.lane_budget for allocation in lane_allocations), ZERO_DECIMAL)
        if lane_total != miner_after_reserve_emission:
            raise ValueError("q27_lane_budgets_must_sum_to_after_reserve_emission")
        object.__setattr__(self, "runtime_calls", runtime_calls)
        object.__setattr__(self, "owner_reserve_credit", owner_reserve_credit)
        object.__setattr__(self, "miner_after_reserve_emission", miner_after_reserve_emission)
        object.__setattr__(self, "lane_allocations", lane_allocations)
        object.__setattr__(self, "paid_acu", paid_acu)


@dataclass(frozen=True, slots=True)
class LiveRewardServerBridgeRequest:
    runtime_metadata: RuntimeMetadataEvidence
    settlement_result: SettlementResult
    processed_window_ids: tuple[str, ...] = field(default_factory=tuple)
    owner_reserve_address: str = RESERVE_RECIPIENT_ADDRESS
    owner_reserve_bps: int = MINIMUM_RESERVE_BPS
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False
    chain_transfer_enabled: bool = False
    paid_acu: DecimalInput = ZERO_DECIMAL

    def __post_init__(self) -> None:
        if not isinstance(self.runtime_metadata, RuntimeMetadataEvidence):
            raise TypeError("runtime_metadata_must_be_runtime_metadata_evidence")
        if not isinstance(self.settlement_result, SettlementResult):
            raise TypeError("settlement_result_must_be_shadow_settlement_result")
        processed_window_ids = tuple(self.processed_window_ids)
        for window_id in processed_window_ids:
            _validate_identifier("processed_window_id", window_id)
        if len(set(processed_window_ids)) != len(processed_window_ids):
            raise ValueError("q27_processed_window_ids_must_be_unique")
        _validate_owner_reserve(self.owner_reserve_address, self.owner_reserve_bps)
        _validate_default_off_flags(
            live_reward_enabled=self.live_reward_enabled,
            payout_executor_enabled=self.payout_executor_enabled,
            chain_transfer_enabled=self.chain_transfer_enabled,
        )
        paid_acu = _coerce_decimal(self.paid_acu, field_name="paid_acu")
        if paid_acu != ZERO_DECIMAL:
            raise ValueError(Q27_PAID_ACU_FORBIDDEN)
        if self.settlement_result.paid_acu != ZERO_DECIMAL:
            raise ValueError(Q27_PAID_ACU_FORBIDDEN)
        _validate_four_hour_window(self.settlement_result)
        object.__setattr__(self, "processed_window_ids", processed_window_ids)
        object.__setattr__(self, "paid_acu", paid_acu)


def prepare_live_reward_window_payload(
    request: LiveRewardServerBridgeRequest,
) -> LiveRewardWindowPayload:
    if request.settlement_result.window.window_id in request.processed_window_ids:
        raise ValueError(Q27_DUPLICATE_WINDOW_ID)
    _validate_runtime_metadata(request.runtime_metadata)

    window_emission = request.settlement_result.window.total_window_emission
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        owner_reserve_credit = (
            window_emission * Decimal(request.owner_reserve_bps) / Decimal(BPS_DENOMINATOR)
        )
        miner_after_reserve_emission = window_emission - owner_reserve_credit

    lane_allocations = _lane_allocations(
        request.settlement_result.reward_statements,
        miner_after_reserve_emission=miner_after_reserve_emission,
    )
    return LiveRewardWindowPayload(
        window_id=request.settlement_result.window.window_id,
        starts_at_iso=request.settlement_result.window.starts_at.isoformat(),
        ends_at_iso=request.settlement_result.window.ends_at.isoformat(),
        runtime_spec_version=request.runtime_metadata.spec_version,
        runtime_metadata_ref=request.runtime_metadata.metadata_ref,
        runtime_calls=request.runtime_metadata.calls,
        owner_reserve_address=request.owner_reserve_address,
        owner_reserve_bps=request.owner_reserve_bps,
        owner_reserve_credit=owner_reserve_credit,
        miner_after_reserve_emission=miner_after_reserve_emission,
        lane_allocations=lane_allocations,
    )


def _lane_allocations(
    reward_statements: tuple[RewardStatement, ...],
    *,
    miner_after_reserve_emission: Decimal,
) -> tuple[LiveRewardWindowLaneAllocation, ...]:
    credits_by_lane: dict[str, list[LiveRewardWindowMinerCredit]] = {
        MAIN_POOL_GPU_RVN: [],
        XMR_POOL: [],
        SCRYPT_POOL: [],
    }
    lane_budgets = _lane_budgets_after_reserve(miner_after_reserve_emission)
    for statement in reward_statements:
        lane = _normalize_miner_lane(statement.lane)
        if lane not in credits_by_lane:
            continue
        dry_run_credit = _scale_statement_credit_after_reserve(
            statement,
            lane_budget_after_reserve=lane_budgets[lane],
        )
        if dry_run_credit == ZERO_DECIMAL:
            continue
        credits_by_lane[lane].append(
            LiveRewardWindowMinerCredit(
                miner_id=f"{statement.passport_id}:{statement.device_id}",
                passport_id=statement.passport_id,
                device_id=statement.device_id,
                lane=lane,
                dry_run_credit=dry_run_credit,
            )
        )

    allocations: list[LiveRewardWindowLaneAllocation] = []
    for lane, lane_share_bps in _q27_lane_bps():
        miner_credits = tuple(credits_by_lane[lane])
        allocated_credit = sum((credit.dry_run_credit for credit in miner_credits), ZERO_DECIMAL)
        allocations.append(
            LiveRewardWindowLaneAllocation(
                lane=lane,
                lane_share_bps=lane_share_bps,
                lane_budget=lane_budgets[lane],
                miner_credits=miner_credits,
                unallocated_credit=lane_budgets[lane] - allocated_credit,
            )
        )
    return tuple(allocations)


def _lane_budgets_after_reserve(miner_after_reserve_emission: Decimal) -> dict[str, Decimal]:
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        return {
            lane: miner_after_reserve_emission * Decimal(lane_bps) / Decimal(BPS_DENOMINATOR)
            for lane, lane_bps in _q27_lane_bps()
        }


def _scale_statement_credit_after_reserve(
    statement: RewardStatement,
    *,
    lane_budget_after_reserve: Decimal,
) -> Decimal:
    source_budget = statement.lane_budget
    # The GPU-RVN, PRL (GPU-primary), and AI lanes all settle out of the shared
    # POOL_MAIN budget, so scale their dry-run live-reward credit against the whole
    # ``pool_budget`` (not the per-lane sub-budget). PRL is included so it scales
    # exactly as it did when it was labeled ``main_pool_gpu_rvn`` (byte-for-byte).
    if statement.lane in {MAIN_POOL_GPU_RVN, MAIN_POOL_GPU_PRL, MAIN_POOL_AI}:
        source_budget = statement.pool_budget
    if source_budget <= ZERO_DECIMAL:
        return ZERO_DECIMAL
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        return statement.simulated_alice_credit * lane_budget_after_reserve / source_budget


def _validate_runtime_metadata(runtime_metadata: RuntimeMetadataEvidence) -> None:
    if runtime_metadata.spec_version < Q27_REQUIRED_RUNTIME_SPEC_VERSION:
        raise ValueError(Q27_RUNTIME_SPEC_VERSION_TOO_OLD)
    if runtime_metadata.missing_required_calls:
        raise ValueError(Q27_RUNTIME_CALLS_MISSING)


def _validate_four_hour_window(settlement_result: SettlementResult) -> None:
    window = settlement_result.window
    if window.ends_at - window.starts_at != DEFAULT_WINDOW_DURATION:
        raise ValueError(Q27_WINDOW_MUST_BE_FOUR_HOURS)
    if window.ends_at - window.starts_at != timedelta(seconds=FOUR_HOUR_WINDOW_SECONDS):
        raise ValueError(Q27_WINDOW_MUST_BE_FOUR_HOURS)


def _validate_owner_reserve(owner_reserve_address: str, owner_reserve_bps: int) -> None:
    _validate_public_address(owner_reserve_address, field_name="owner_reserve_address")
    if owner_reserve_address != RESERVE_RECIPIENT_ADDRESS:
        raise ValueError(Q27_OWNER_RESERVE_REQUIRED)
    if owner_reserve_bps != MINIMUM_RESERVE_BPS:
        raise ValueError(Q27_OWNER_RESERVE_BPS_REQUIRED)


def _validate_default_off_flags(
    *,
    live_reward_enabled: bool,
    payout_executor_enabled: bool,
    chain_transfer_enabled: bool,
) -> None:
    _validate_bool("live_reward_enabled", live_reward_enabled)
    _validate_bool("payout_executor_enabled", payout_executor_enabled)
    _validate_bool("chain_transfer_enabled", chain_transfer_enabled)
    if live_reward_enabled:
        raise ValueError(Q27_LIVE_REWARD_FORBIDDEN)
    if payout_executor_enabled:
        raise ValueError(Q27_PAYOUT_EXECUTOR_FORBIDDEN)
    if chain_transfer_enabled:
        raise ValueError(Q27_CHAIN_TRANSFER_FORBIDDEN)


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
    if isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation as exc:
            raise ValueError(f"{field_name}_must_be_decimal_text") from exc
    raise TypeError(f"{field_name}_must_use_decimal_or_int")


def _validate_non_negative_decimal(value: Decimal, *, field_name: str) -> None:
    if value < ZERO_DECIMAL:
        raise ValueError(f"{field_name}_must_be_non_negative")


def _validate_bool(field_name: str, value: bool) -> None:
    if not isinstance(value, bool):
        raise TypeError(f"{field_name}_must_be_bool")


def _validate_non_negative_int(field_name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name}_must_be_int")
    if value < 0:
        raise ValueError(f"{field_name}_must_be_non_negative")


def _validate_bps(field_name: str, value: int) -> None:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{field_name}_must_be_int")
    if not 0 <= value <= BPS_DENOMINATOR:
        raise ValueError(f"{field_name}_must_be_between_0_and_10000")


def _validate_identifier(field_name: str, value: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name}_must_be_str")
    if not value.strip():
        raise ValueError(f"{field_name}_required")
    if value != value.strip():
        raise ValueError(f"{field_name}_must_not_have_surrounding_whitespace")
    if any(character.isspace() for character in value):
        raise ValueError(f"{field_name}_must_not_contain_whitespace")
    ensure_no_raw_secret(value, field_name=field_name)


def _validate_runtime_call(value: str) -> None:
    _validate_identifier("runtime_call", value)


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


def _validate_iso_text(field_name: str, value: str) -> None:
    if not isinstance(value, str):
        raise TypeError(f"{field_name}_must_be_str")
    if not value.strip():
        raise ValueError(f"{field_name}_required")


def _validate_lane(lane: str) -> None:
    if lane not in {MAIN_POOL_GPU_RVN, XMR_POOL, SCRYPT_POOL}:
        raise ValueError("q27_unsupported_miner_lane")


def _normalize_miner_lane(lane: str) -> str:
    # The AI lane and the PRL (GPU-primary) lane both fold into the GPU miner lane for
    # the Q27 live-reward allocation: AI settles out of POOL_MAIN, and PRL is the same
    # GPU capacity as RVN (RVN is PRL's fallback route). Folding PRL here keeps the OFF
    # live-reward path byte-for-byte what it was when PRL was labeled ``main_pool_gpu_rvn``
    # (otherwise a ``main_pool_gpu_prl`` statement would be dropped as an unknown lane).
    if lane in {MAIN_POOL_AI, MAIN_POOL_GPU_PRL}:
        return MAIN_POOL_GPU_RVN
    return lane


def _q27_lane_order() -> tuple[str, str, str]:
    return (MAIN_POOL_GPU_RVN, XMR_POOL, SCRYPT_POOL)


def _q27_lane_bps() -> tuple[tuple[str, int], ...]:
    return (
        (MAIN_POOL_GPU_RVN, Q27_MAIN_MINER_LANE_BPS),
        (XMR_POOL, Q27_XMR_MINER_LANE_BPS),
        (SCRYPT_POOL, Q27_SCRYPT_MINER_LANE_BPS),
    )

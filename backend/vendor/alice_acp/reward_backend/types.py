from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Literal

from alice_acp.evidence.types import (
    ensure_no_production_alice_reference,
    ensure_no_raw_secret,
    validate_aware_timestamp,
    validate_sha256,
)

REWARD_BACKEND_CONTRACT_VERSION = "q19-server-reward-backend-contract-v1"

LANE_PRL_GPU = "prl_gpu_canary"
LANE_RVN_KAWPOW = "rvn_kawpow"
LANE_XMR_RANDOMX = "xmr_randomx"
LANE_AI_INFERENCE = "ai_inference"
SUPPORTED_REWARD_LANES = (
    LANE_PRL_GPU,
    LANE_RVN_KAWPOW,
    LANE_XMR_RANDOMX,
    LANE_AI_INFERENCE,
)

SOURCE_PROOF_ACCOUNTING = "proof_accounting"
SOURCE_FOUNDATION_REVENUE = "foundation_revenue"

STATUS_STAGED = "staged"
STATUS_REJECTED = "rejected"
STATUS_UNDER_REVIEW = "under_review"
STATUS_EXCLUDED = "excluded"

AUTHORITY_ACCEPTED = "accepted"
AUTHORITY_REWARDABLE_CANDIDATE = "rewardable_candidate"
AUTHORITY_UNDER_REVIEW = "under_review"
AUTHORITY_REJECTED = "rejected"
AUTHORITY_NONREWARDABLE = "nonrewardable"

ANTI_CHEAT_PASSED = "passed"
ANTI_CHEAT_UNDER_REVIEW = "under_review"
ANTI_CHEAT_REJECTED = "rejected"
ANTI_CHEAT_DUPLICATE = "duplicate"
ANTI_CHEAT_TAMPERED = "tampered"

REASON_STAGED = "reward_backend_staged"
REASON_DUPLICATE_SOURCE = "duplicate_reward_source"
REASON_DUPLICATE_CANONICAL_PROOF = "duplicate_canonical_proof"
REASON_KILL_SWITCH = "reward_backend_kill_switch_enabled"
REASON_LIVE_REWARD_FORBIDDEN = "reward_backend_live_reward_forbidden"
REASON_PAYOUT_EXECUTOR_FORBIDDEN = "reward_backend_payout_executor_forbidden"
REASON_CHAIN_TRANSFER_FORBIDDEN = "reward_backend_chain_transfer_forbidden"
REASON_PUBLIC_SERVICE_FORBIDDEN = "reward_backend_public_service_forbidden"
REASON_WINDOW_UNKNOWN = "reward_window_unknown"
REASON_WINDOW_MISMATCH = "reward_window_mismatch"
REASON_FOUNDATION_REVENUE_EXCLUDED = "foundation_revenue_excluded_from_rewards"
REASON_AUTHORITY_REJECTED = "proof_authority_rejected"
REASON_AUTHORITY_NONREWARDABLE = "proof_authority_nonrewardable"
REASON_AUTHORITY_UNDER_REVIEW = "proof_authority_under_review"
REASON_ANTI_CHEAT_REJECTED = "anti_cheat_rejected"
REASON_ANTI_CHEAT_DUPLICATE = "anti_cheat_duplicate"
REASON_ANTI_CHEAT_TAMPERED = "anti_cheat_tampered"
REASON_ANTI_CHEAT_UNDER_REVIEW = "anti_cheat_under_review"
REASON_NON_POSITIVE_SCORE = "non_positive_rewardable_score"

ZERO_DECIMAL = Decimal("0")
PUBLIC_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,191}$")

RewardLane = Literal[
    "prl_gpu_canary",
    "rvn_kawpow",
    "xmr_randomx",
    "ai_inference",
]
SourceKind = Literal["proof_accounting", "foundation_revenue"]
AuthorityStatus = Literal[
    "accepted",
    "rewardable_candidate",
    "under_review",
    "rejected",
    "nonrewardable",
]
AntiCheatStatus = Literal["passed", "under_review", "rejected", "duplicate", "tampered"]
StageStatus = Literal["staged", "rejected", "under_review", "excluded"]


def utc_now() -> datetime:
    return datetime.now(UTC)


@dataclass(frozen=True, slots=True)
class RewardBackendConfig:
    contract_version: str = REWARD_BACKEND_CONTRACT_VERSION
    local_contract_only: bool = True
    public_service_enabled: bool = False
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False
    chain_transfer_enabled: bool = False

    def __post_init__(self) -> None:
        validate_public_identifier("contract_version", self.contract_version)
        if not self.local_contract_only or self.public_service_enabled:
            raise ValueError(REASON_PUBLIC_SERVICE_FORBIDDEN)
        _validate_disabled_flags(
            live_reward_enabled=self.live_reward_enabled,
            payout_executor_enabled=self.payout_executor_enabled,
            chain_transfer_enabled=self.chain_transfer_enabled,
        )


@dataclass(frozen=True, slots=True)
class RewardWindow:
    window_id: str
    starts_at: datetime
    ends_at: datetime
    miner_window_emission_cap: Decimal
    source_ref: str = "q15_window_emission_contract"
    calculation_only: bool = True
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False
    chain_transfer_enabled: bool = False

    def __post_init__(self) -> None:
        validate_public_identifier("window_id", self.window_id)
        validate_public_identifier("source_ref", self.source_ref)
        validate_aware_timestamp("starts_at", self.starts_at)
        validate_aware_timestamp("ends_at", self.ends_at)
        if self.ends_at <= self.starts_at:
            raise ValueError("reward_window_ends_at_must_be_after_starts_at")
        _validate_decimal(self.miner_window_emission_cap, field_name="miner_window_emission_cap")
        if self.miner_window_emission_cap < ZERO_DECIMAL:
            raise ValueError("miner_window_emission_cap_must_be_non_negative")
        if not self.calculation_only:
            raise ValueError("reward_window_must_remain_calculation_only")
        _validate_disabled_flags(
            live_reward_enabled=self.live_reward_enabled,
            payout_executor_enabled=self.payout_executor_enabled,
            chain_transfer_enabled=self.chain_transfer_enabled,
        )


@dataclass(frozen=True, slots=True)
class RewardAccountingSourceRecord:
    source_id: str
    window_id: str
    session_id: str
    proof_id: str
    passport_id: str
    device_id: str
    lane: RewardLane
    observed_at: datetime
    authority_status: AuthorityStatus
    anti_cheat_status: AntiCheatStatus
    rewardable_score: Decimal
    source_kind: SourceKind = SOURCE_PROOF_ACCOUNTING
    reason_code: str | None = None
    canonical_proof_hash: str | None = None
    foundation_revenue_amount: Decimal = ZERO_DECIMAL
    paid_acu: Decimal = ZERO_DECIMAL
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False
    chain_transfer_enabled: bool = False

    def __post_init__(self) -> None:
        for field_name, value in (
            ("source_id", self.source_id),
            ("window_id", self.window_id),
            ("session_id", self.session_id),
            ("proof_id", self.proof_id),
            ("passport_id", self.passport_id),
            ("device_id", self.device_id),
        ):
            validate_public_identifier(field_name, value)
        if self.reason_code is not None:
            validate_public_identifier("reason_code", self.reason_code)
        if self.lane not in SUPPORTED_REWARD_LANES:
            raise ValueError("reward_lane_is_unsupported")
        if self.source_kind not in (SOURCE_PROOF_ACCOUNTING, SOURCE_FOUNDATION_REVENUE):
            raise ValueError("reward_source_kind_is_unsupported")
        if self.authority_status not in (
            AUTHORITY_ACCEPTED,
            AUTHORITY_REWARDABLE_CANDIDATE,
            AUTHORITY_UNDER_REVIEW,
            AUTHORITY_REJECTED,
            AUTHORITY_NONREWARDABLE,
        ):
            raise ValueError("authority_status_is_unsupported")
        if self.anti_cheat_status not in (
            ANTI_CHEAT_PASSED,
            ANTI_CHEAT_UNDER_REVIEW,
            ANTI_CHEAT_REJECTED,
            ANTI_CHEAT_DUPLICATE,
            ANTI_CHEAT_TAMPERED,
        ):
            raise ValueError("anti_cheat_status_is_unsupported")
        validate_aware_timestamp("observed_at", self.observed_at)
        _validate_decimal(self.rewardable_score, field_name="rewardable_score")
        _validate_decimal(
            self.foundation_revenue_amount,
            field_name="foundation_revenue_amount",
        )
        if self.rewardable_score < ZERO_DECIMAL:
            raise ValueError("rewardable_score_must_be_non_negative")
        if self.foundation_revenue_amount < ZERO_DECIMAL:
            raise ValueError("foundation_revenue_amount_must_be_non_negative")
        if self.source_kind == SOURCE_FOUNDATION_REVENUE and self.rewardable_score != ZERO_DECIMAL:
            raise ValueError("foundation_revenue_source_must_not_carry_rewardable_score")
        if self.source_kind == SOURCE_PROOF_ACCOUNTING:
            if self.foundation_revenue_amount != ZERO_DECIMAL:
                raise ValueError("proof_source_must_not_carry_foundation_revenue")
        if self.canonical_proof_hash is not None:
            validate_sha256(self.canonical_proof_hash, field_name="canonical_proof_hash")
        if self.paid_acu != ZERO_DECIMAL:
            raise ValueError("reward_backend_source_paid_acu_must_remain_zero")
        _validate_disabled_flags(
            live_reward_enabled=self.live_reward_enabled,
            payout_executor_enabled=self.payout_executor_enabled,
            chain_transfer_enabled=self.chain_transfer_enabled,
        )


@dataclass(frozen=True, slots=True)
class StagedRewardRecord:
    source_id: str
    window_id: str
    session_id: str
    proof_id: str
    passport_id: str
    device_id: str
    lane: RewardLane
    rewardable_score: Decimal
    authority_status: AuthorityStatus
    anti_cheat_status: AntiCheatStatus
    recorded_at: datetime
    canonical_proof_hash: str | None = None
    paid_acu: Decimal = ZERO_DECIMAL
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False
    chain_transfer_enabled: bool = False

    def __post_init__(self) -> None:
        for field_name, value in (
            ("source_id", self.source_id),
            ("window_id", self.window_id),
            ("session_id", self.session_id),
            ("proof_id", self.proof_id),
            ("passport_id", self.passport_id),
            ("device_id", self.device_id),
        ):
            validate_public_identifier(field_name, value)
        if self.lane not in SUPPORTED_REWARD_LANES:
            raise ValueError("reward_lane_is_unsupported")
        _validate_decimal(self.rewardable_score, field_name="rewardable_score")
        if self.rewardable_score <= ZERO_DECIMAL:
            raise ValueError("staged_rewardable_score_must_be_positive")
        validate_aware_timestamp("recorded_at", self.recorded_at)
        if self.canonical_proof_hash is not None:
            validate_sha256(self.canonical_proof_hash, field_name="canonical_proof_hash")
        if self.paid_acu != ZERO_DECIMAL:
            raise ValueError("staged_reward_paid_acu_must_remain_zero")
        _validate_disabled_flags(
            live_reward_enabled=self.live_reward_enabled,
            payout_executor_enabled=self.payout_executor_enabled,
            chain_transfer_enabled=self.chain_transfer_enabled,
        )


@dataclass(frozen=True, slots=True)
class FoundationRevenueRecord:
    source_id: str
    window_id: str
    session_id: str
    amount: Decimal
    recorded_at: datetime
    reason_code: str = REASON_FOUNDATION_REVENUE_EXCLUDED
    paid_acu: Decimal = ZERO_DECIMAL
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False
    chain_transfer_enabled: bool = False

    def __post_init__(self) -> None:
        for field_name, value in (
            ("source_id", self.source_id),
            ("window_id", self.window_id),
            ("session_id", self.session_id),
            ("reason_code", self.reason_code),
        ):
            validate_public_identifier(field_name, value)
        _validate_decimal(self.amount, field_name="amount")
        if self.amount < ZERO_DECIMAL:
            raise ValueError("foundation_revenue_amount_must_be_non_negative")
        validate_aware_timestamp("recorded_at", self.recorded_at)
        if self.paid_acu != ZERO_DECIMAL:
            raise ValueError("foundation_revenue_paid_acu_must_remain_zero")
        _validate_disabled_flags(
            live_reward_enabled=self.live_reward_enabled,
            payout_executor_enabled=self.payout_executor_enabled,
            chain_transfer_enabled=self.chain_transfer_enabled,
        )


@dataclass(frozen=True, slots=True)
class RewardStageResult:
    status: StageStatus
    reason_code: str
    source_id: str
    window_id: str
    staged_record: StagedRewardRecord | None = None
    foundation_revenue: FoundationRevenueRecord | None = None
    paid_acu: Decimal = ZERO_DECIMAL
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False
    chain_transfer_enabled: bool = False

    def __post_init__(self) -> None:
        if self.status not in (
            STATUS_STAGED,
            STATUS_REJECTED,
            STATUS_UNDER_REVIEW,
            STATUS_EXCLUDED,
        ):
            raise ValueError("reward_stage_status_is_unsupported")
        validate_public_identifier("reason_code", self.reason_code)
        validate_public_identifier("source_id", self.source_id)
        validate_public_identifier("window_id", self.window_id)
        if self.status == STATUS_STAGED and self.staged_record is None:
            raise ValueError("staged_reward_result_requires_record")
        if self.status != STATUS_STAGED and self.staged_record is not None:
            raise ValueError("non_staged_reward_result_must_not_carry_record")
        if self.status == STATUS_EXCLUDED and self.foundation_revenue is None:
            raise ValueError("excluded_reward_result_requires_foundation_revenue_record")
        if self.status != STATUS_EXCLUDED and self.foundation_revenue is not None:
            raise ValueError("non_excluded_reward_result_must_not_carry_foundation_revenue")
        if self.paid_acu != ZERO_DECIMAL:
            raise ValueError("reward_stage_paid_acu_must_remain_zero")
        _validate_disabled_flags(
            live_reward_enabled=self.live_reward_enabled,
            payout_executor_enabled=self.payout_executor_enabled,
            chain_transfer_enabled=self.chain_transfer_enabled,
        )

    @property
    def accepted(self) -> bool:
        return self.status == STATUS_STAGED

    @property
    def rewardable(self) -> bool:
        return self.staged_record is not None


@dataclass(frozen=True, slots=True)
class RewardBalanceView:
    window_id: str
    passport_id: str
    device_id: str
    staged_score: Decimal
    denominator_score: Decimal
    simulated_credit: Decimal
    lane_scores: dict[RewardLane, Decimal] = field(default_factory=dict)
    staged_record_count: int = 0
    paid_acu: Decimal = ZERO_DECIMAL
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False
    chain_transfer_enabled: bool = False

    def __post_init__(self) -> None:
        for field_name, value in (
            ("window_id", self.window_id),
            ("passport_id", self.passport_id),
            ("device_id", self.device_id),
        ):
            validate_public_identifier(field_name, value)
        for field_name, value in (
            ("staged_score", self.staged_score),
            ("denominator_score", self.denominator_score),
            ("simulated_credit", self.simulated_credit),
        ):
            _validate_decimal(value, field_name=field_name)
            if value < ZERO_DECIMAL:
                raise ValueError(f"{field_name}_must_be_non_negative")
        for lane, score in self.lane_scores.items():
            if lane not in SUPPORTED_REWARD_LANES:
                raise ValueError("reward_lane_is_unsupported")
            _validate_decimal(score, field_name="lane_score")
            if score < ZERO_DECIMAL:
                raise ValueError("lane_score_must_be_non_negative")
        if self.staged_record_count < 0:
            raise ValueError("staged_record_count_must_be_non_negative")
        if self.paid_acu != ZERO_DECIMAL:
            raise ValueError("reward_balance_paid_acu_must_remain_zero")
        _validate_disabled_flags(
            live_reward_enabled=self.live_reward_enabled,
            payout_executor_enabled=self.payout_executor_enabled,
            chain_transfer_enabled=self.chain_transfer_enabled,
        )


def validate_public_identifier(field_name: str, value: str) -> None:
    if not value:
        raise ValueError(f"{field_name}_must_be_non_empty")
    if PUBLIC_ID_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name}_is_malformed")
    ensure_no_raw_secret(value, field_name=field_name)
    ensure_no_production_alice_reference(value, field_name=field_name)


def _validate_decimal(value: object, *, field_name: str) -> None:
    if not isinstance(value, Decimal):
        raise TypeError(f"{field_name}_must_be_decimal")


def _validate_disabled_flags(
    *,
    live_reward_enabled: bool,
    payout_executor_enabled: bool,
    chain_transfer_enabled: bool,
) -> None:
    if live_reward_enabled:
        raise ValueError(REASON_LIVE_REWARD_FORBIDDEN)
    if payout_executor_enabled:
        raise ValueError(REASON_PAYOUT_EXECUTOR_FORBIDDEN)
    if chain_transfer_enabled:
        raise ValueError(REASON_CHAIN_TRANSFER_FORBIDDEN)

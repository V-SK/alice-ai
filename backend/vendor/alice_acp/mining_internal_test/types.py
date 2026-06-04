from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Literal

from alice_acp.evidence.types import (
    ensure_no_production_alice_reference,
    ensure_no_raw_secret,
)
from alice_acp.mining_session.types import (
    ALICE_REWARDED_MINING_MODE,
    RVN_KAWPOW,
    MiningAlgorithm,
    MiningMode,
)

INTERNAL_TEST_READY = "INTERNAL_TEST_READY"
INTERNAL_TEST_DISABLED = "INTERNAL_TEST_DISABLED"
INTERNAL_TEST_REAL_POOL_DISABLED = "INTERNAL_TEST_REAL_POOL_DISABLED"
INTERNAL_TEST_LIVE_REWARD_FORBIDDEN = "INTERNAL_TEST_LIVE_REWARD_FORBIDDEN"
INTERNAL_TEST_PAYOUT_EXECUTOR_FORBIDDEN = "INTERNAL_TEST_PAYOUT_EXECUTOR_FORBIDDEN"
INTERNAL_TEST_ALLOWLIST_REQUIRED = "INTERNAL_TEST_ALLOWLIST_REQUIRED"
INTERNAL_TEST_PASSPORT_NOT_ALLOWED = "INTERNAL_TEST_PASSPORT_NOT_ALLOWED"
INTERNAL_TEST_DEVICE_NOT_ALLOWED = "INTERNAL_TEST_DEVICE_NOT_ALLOWED"
INTERNAL_TEST_POOL_NOT_ALLOWED = "INTERNAL_TEST_POOL_NOT_ALLOWED"
INTERNAL_TEST_KILL_SWITCH_ACTIVE = "INTERNAL_TEST_KILL_SWITCH_ACTIVE"
INTERNAL_TEST_LIMIT_INVALID = "INTERNAL_TEST_LIMIT_INVALID"

COLLECTION_WALLET_READY = "COLLECTION_WALLET_READY"
COLLECTION_WALLET_ADDRESS_REQUIRED = "COLLECTION_WALLET_ADDRESS_REQUIRED"
COLLECTION_WALLET_MINER_OVERRIDE_REJECTED = "COLLECTION_WALLET_MINER_OVERRIDE_REJECTED"
COLLECTION_WALLET_SESSION_ADDRESS_MISMATCH = (
    "COLLECTION_WALLET_SESSION_ADDRESS_MISMATCH"
)
COLLECTION_WALLET_LABEL_REQUIRED = "COLLECTION_WALLET_LABEL_REQUIRED"
COLLECTION_WALLET_PLACEHOLDER = "<set outside repo>"

TEST_POOL_PROFILE_READY = "TEST_POOL_PROFILE_READY"
TEST_POOL_PROFILE_ALGORITHM_REJECTED = "TEST_POOL_PROFILE_ALGORITHM_REJECTED"
TEST_POOL_PROFILE_TEMPLATE_REQUIRED = "TEST_POOL_PROFILE_TEMPLATE_REQUIRED"
TEST_POOL_PROFILE_REAL_NETWORK_REQUIRES_GATE = (
    "TEST_POOL_PROFILE_REAL_NETWORK_REQUIRES_GATE"
)
TEST_POOL_PROFILE_NOT_ALLOWLISTED = "TEST_POOL_PROFILE_NOT_ALLOWLISTED"
TEST_POOL_PROFILE_DIRECT_MODE_REJECTED = "TEST_POOL_PROFILE_DIRECT_MODE_REJECTED"

KILL_SWITCH_ALLOW = "KILL_SWITCH_ALLOW"
KILL_SWITCH_REASON_REQUIRED = "KILL_SWITCH_REASON_REQUIRED"
KILL_SWITCH_GLOBAL_STOP = "KILL_SWITCH_GLOBAL_STOP"
KILL_SWITCH_POOL_DISABLED = "KILL_SWITCH_POOL_DISABLED"
KILL_SWITCH_PASSPORT_DISABLED = "KILL_SWITCH_PASSPORT_DISABLED"
KILL_SWITCH_DEVICE_DISABLED = "KILL_SWITCH_DEVICE_DISABLED"

EvidenceExportMode = Literal["fixture", "manual"]
AddressSource = Literal["placeholder", "out_of_band"]
ReadinessStatus = Literal["ready", "blocked"]


@dataclass(frozen=True, slots=True)
class InternalMiningTestGate:
    internal_test_enabled: bool = False
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False
    real_pool_enabled: bool = False
    allowed_passports: tuple[str, ...] = ()
    allowed_devices: tuple[str, ...] = ()
    allowed_pool_ids: tuple[str, ...] = ()
    max_runtime_minutes: int = 60
    max_accepted_shares: int = 100
    kill_switch_enabled: bool = False

    def __post_init__(self) -> None:
        if self.live_reward_enabled:
            raise ValueError(INTERNAL_TEST_LIVE_REWARD_FORBIDDEN)
        if self.payout_executor_enabled:
            raise ValueError(INTERNAL_TEST_PAYOUT_EXECUTOR_FORBIDDEN)
        if self.max_runtime_minutes <= 0 or self.max_accepted_shares <= 0:
            raise ValueError(INTERNAL_TEST_LIMIT_INVALID)
        for field_name, values in (
            ("allowed_passports", self.allowed_passports),
            ("allowed_devices", self.allowed_devices),
            ("allowed_pool_ids", self.allowed_pool_ids),
        ):
            _validate_tuple(field_name, values)


@dataclass(frozen=True, slots=True)
class GateDecision:
    allowed: bool
    reason_code: str


@dataclass(frozen=True, slots=True)
class CollectionWalletPolicy:
    pool_id: str
    epoch_label: str
    address_label: str
    collection_address: str | None = None
    address_source: AddressSource = "placeholder"
    miner_provided_payout_address: str | None = None

    def __post_init__(self) -> None:
        if not self.pool_id or not self.epoch_label or not self.address_label:
            raise ValueError(COLLECTION_WALLET_LABEL_REQUIRED)
        for field_name, value in (
            ("pool_id", self.pool_id),
            ("epoch_label", self.epoch_label),
            ("address_label", self.address_label),
        ):
            _validate_text(field_name, value)
        if self.collection_address is not None:
            _validate_text("collection_address", self.collection_address)
        if self.miner_provided_payout_address is not None:
            raise ValueError(COLLECTION_WALLET_MINER_OVERRIDE_REJECTED)

    @property
    def has_real_collection_address(self) -> bool:
        return (
            self.collection_address is not None
            and self.collection_address != COLLECTION_WALLET_PLACEHOLDER
            and self.address_source == "out_of_band"
        )


@dataclass(frozen=True, slots=True)
class CollectionWalletDecision:
    ready: bool
    reason_code: str


@dataclass(frozen=True, slots=True)
class TestPoolProfile:
    pool_id: str
    stratum_endpoint_template: str
    evidence_export_mode: EvidenceExportMode = "fixture"
    real_network_allowed: bool = False
    algorithm: MiningAlgorithm = RVN_KAWPOW
    mode: MiningMode = ALICE_REWARDED_MINING_MODE

    def __post_init__(self) -> None:
        if not self.pool_id or not self.stratum_endpoint_template:
            raise ValueError(TEST_POOL_PROFILE_TEMPLATE_REQUIRED)
        if self.algorithm != RVN_KAWPOW:
            raise ValueError(TEST_POOL_PROFILE_ALGORITHM_REJECTED)
        if self.mode != ALICE_REWARDED_MINING_MODE:
            raise ValueError(TEST_POOL_PROFILE_DIRECT_MODE_REJECTED)
        _validate_text("pool_id", self.pool_id)
        _validate_text("stratum_endpoint_template", self.stratum_endpoint_template)


@dataclass(frozen=True, slots=True)
class PoolProfileDecision:
    ready: bool
    reason_code: str


@dataclass(frozen=True, slots=True)
class KillSwitchPolicy:
    global_stop: bool = False
    disabled_pool_ids: tuple[str, ...] = ()
    disabled_passport_ids: tuple[str, ...] = ()
    disabled_device_ids: tuple[str, ...] = ()
    reason_code: str | None = None
    audit_payload: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        blocked = (
            self.global_stop
            or bool(self.disabled_pool_ids)
            or bool(self.disabled_passport_ids)
            or bool(self.disabled_device_ids)
        )
        if blocked and not self.reason_code:
            raise ValueError(KILL_SWITCH_REASON_REQUIRED)
        for field_name, values in (
            ("disabled_pool_ids", self.disabled_pool_ids),
            ("disabled_passport_ids", self.disabled_passport_ids),
            ("disabled_device_ids", self.disabled_device_ids),
        ):
            _validate_tuple(field_name, values)
        if self.reason_code is not None:
            _validate_text("reason_code", self.reason_code)
        for key, value in self.audit_payload.items():
            _validate_text("audit_payload_key", key)
            _validate_text("audit_payload_value", value)


@dataclass(frozen=True, slots=True)
class KillSwitchDecision:
    blocked: bool
    reason_code: str


@dataclass(frozen=True, slots=True)
class InternalTestReadinessReport:
    status: ReadinessStatus
    reason_codes: tuple[str, ...]

    @property
    def can_start_queue_8(self) -> bool:
        return self.status == "ready"


def _validate_tuple(field_name: str, values: tuple[str, ...]) -> None:
    if any(not value for value in values):
        raise ValueError(f"{field_name} values must be non-empty")
    for value in values:
        _validate_text(field_name, value)


def _validate_text(field_name: str, value: str) -> None:
    ensure_no_raw_secret(value, field_name=field_name)
    ensure_no_production_alice_reference(value, field_name=field_name)

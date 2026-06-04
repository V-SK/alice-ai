from __future__ import annotations

from dataclasses import asdict, dataclass, field
from decimal import Decimal
from typing import Any

REASON_WHY_NOT_LIVE = (
    "no_real_payout_executor",
    "no_P1_sanitizer",
    "no_seeded_MTTD",
    "no_verifier_fleet",
    "no_production_HA_drill",
)

PHASE_C_REASON_WHY_NOT_LIVE = (
    "no_real_payout_executor",
    "no_production_P1_sanitizer",
    "no_production_verifier_fleet",
    "no_production_HA_drill",
    "no_live_payment_processor",
)

FAILOVER_SCOPE_NOTE = (
    "A7 only proves local idempotency/failure semantics; it does not prove "
    "production active-passive or consensus HA."
)


@dataclass(frozen=True, slots=True)
class BenchmarkReport:
    commit_sha: str
    environment: str
    database_or_storage_backend: str
    partition_count: int
    test_duration_seconds: float
    total_requests: int
    p50_latency_ms: float
    p95_latency_ms: float
    p99_latency_ms: float
    throughput_per_partition: float
    accepted_count: int
    rejected_count: int
    retryable_count: int
    invariant_violations: int
    duplicate_reservation_count: int
    lost_release_count: int
    failover_results: dict[str, Any] = field(default_factory=dict)
    reason_why_not_live: tuple[str, ...] = REASON_WHY_NOT_LIVE

    def as_dict(self) -> dict[str, Any]:
        return _json_ready(asdict(self))


@dataclass(frozen=True, slots=True)
class FailoverScenarioReport:
    scenario: str
    passed: bool
    duplicate_reservation_count: int
    lost_reservation_count: int
    invariant_violations: int
    details: dict[str, Any]

    def as_dict(self) -> dict[str, Any]:
        return _json_ready(asdict(self))


@dataclass(frozen=True, slots=True)
class FailoverReport:
    scenarios: tuple[FailoverScenarioReport, ...]
    scope_note: str = FAILOVER_SCOPE_NOTE
    reason_why_not_live: tuple[str, ...] = REASON_WHY_NOT_LIVE

    @property
    def passed(self) -> bool:
        return all(scenario.passed for scenario in self.scenarios)

    def as_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["passed"] = self.passed
        return _json_ready(payload)


@dataclass(frozen=True, slots=True)
class ShadowLedgerReport:
    run_id: str
    reservation_id: str
    liability_id: str
    passport_id: str
    total_reserved_acu: Decimal
    total_verified_acu: Decimal
    estimated_base_release_acu: Decimal
    estimated_risk_reserve_acu: Decimal
    source_budget_utilization_acu: Decimal
    mode_budget_utilization_acu: Decimal
    cap_hits: int
    idempotency_replays: int
    under_review_count: int
    accounting_corrections: int
    shadow_acu_per_passport: dict[str, Decimal]
    service_functions: tuple[str, ...]
    reason_why_not_live: tuple[str, ...] = REASON_WHY_NOT_LIVE

    def as_dict(self) -> dict[str, Any]:
        return _json_ready(asdict(self))


@dataclass(frozen=True, slots=True)
class PhaseBDryRunReport:
    run_id: str
    wac_contract_status: str
    reservation_id: str
    liability_id: str
    verifier_signal_id: str
    risk_assessment_max_window_seconds: int
    paid_acu_zero: bool
    estimated_base_release_acu: Decimal
    estimated_risk_reserve_acu: Decimal
    service_functions: tuple[str, ...]
    reason_why_not_live: tuple[str, ...] = REASON_WHY_NOT_LIVE

    def as_dict(self) -> dict[str, Any]:
        return _json_ready(asdict(self))


@dataclass(frozen=True, slots=True)
class PhaseBBenchmarkReadinessReport:
    benchmark: BenchmarkReport
    failover: FailoverReport
    production_ha_ready: bool = False
    live_reward_ready: bool = False
    scope_note: str = FAILOVER_SCOPE_NOTE
    reason_why_not_live: tuple[str, ...] = REASON_WHY_NOT_LIVE

    def as_dict(self) -> dict[str, Any]:
        return _json_ready(asdict(self))


@dataclass(frozen=True, slots=True)
class PhaseCDryRunReport:
    run_id: str
    wac_contract_status: str
    reservation_id: str
    liability_id: str
    settlement_status: str
    tranche_status: str
    verifier_ingestion_status: str
    p1_live_reward_ready: bool
    p1_reason_codes: tuple[str, ...]
    r3_eligible: bool
    r3_cap_treatment: str
    correlation_requires_under_review: bool
    seed_budget_namespace: str
    seed_public_miner_bucket_debits: int
    seed_dual_blind_preserved: bool
    signal_store_event_count: int
    paid_acu_zero: bool
    estimated_base_release_acu: Decimal
    estimated_risk_reserve_acu: Decimal
    production_ha_ready: bool = False
    live_reward_ready: bool = False
    service_functions: tuple[str, ...] = ()
    reason_why_not_live: tuple[str, ...] = PHASE_C_REASON_WHY_NOT_LIVE

    def as_dict(self) -> dict[str, Any]:
        return _json_ready(asdict(self))


@dataclass(frozen=True, slots=True)
class PhaseCBenchmarkReadinessReport:
    benchmark: BenchmarkReport
    phase_c_dry_run: PhaseCDryRunReport
    verifier_completed_sample_count: int
    verifier_min_completed_samples: int
    verifier_p95_sample_status: str
    production_ha_ready: bool = False
    live_reward_ready: bool = False
    scope_note: str = FAILOVER_SCOPE_NOTE
    reason_why_not_live: tuple[str, ...] = PHASE_C_REASON_WHY_NOT_LIVE

    def as_dict(self) -> dict[str, Any]:
        return _json_ready(asdict(self))


def _json_ready(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, tuple):
        return [_json_ready(item) for item in value]
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    return value

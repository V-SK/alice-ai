from __future__ import annotations

from dataclasses import dataclass

from alice_acp.test_harness.reports import BenchmarkReport

ABRS_P50_TARGET_MS = 20.0
ABRS_P95_TARGET_MS = 100.0
ABRS_P99_TARGET_MS = 300.0
ABRS_THROUGHPUT_TARGET_PER_PARTITION = 200.0


@dataclass(frozen=True, slots=True)
class RepresentativeBenchmarkProfile:
    expected_first_live_qps: int
    burst_multiplier: int = 2
    representative_environment: bool = False
    postgres_tuned_for_first_live: bool = False
    partition_count_representative: bool = False

    def __post_init__(self) -> None:
        if self.expected_first_live_qps <= 0:
            raise ValueError("expected_first_live_qps must be positive")
        if self.burst_multiplier < 2:
            raise ValueError("burst_multiplier must be at least 2")


@dataclass(frozen=True, slots=True)
class PhaseDBenchmarkGateReport:
    benchmark: BenchmarkReport
    profile: RepresentativeBenchmarkProfile
    reason_codes: tuple[str, ...]
    representative_benchmark_ready: bool = False
    live_reward_ready: bool = False


def evaluate_representative_abrs_benchmark(
    benchmark: BenchmarkReport,
    profile: RepresentativeBenchmarkProfile,
) -> PhaseDBenchmarkGateReport:
    reason_codes: list[str] = []
    if not profile.representative_environment:
        reason_codes.append("REPRESENTATIVE_ENVIRONMENT_MISSING")
    if not profile.postgres_tuned_for_first_live:
        reason_codes.append("POSTGRES_FIRST_LIVE_TUNING_MISSING")
    if not profile.partition_count_representative:
        reason_codes.append("PARTITION_COUNT_NOT_REPRESENTATIVE")
    if benchmark.retryable_count:
        reason_codes.append("RETRYABLE_RESULTS_PRESENT")
    if benchmark.invariant_violations:
        reason_codes.append("BENCHMARK_INVARIANT_VIOLATION")
    if benchmark.duplicate_reservation_count:
        reason_codes.append("BENCHMARK_DUPLICATE_RESERVATION")
    if benchmark.lost_release_count:
        reason_codes.append("BENCHMARK_LOST_RELEASE")
    if benchmark.p50_latency_ms >= ABRS_P50_TARGET_MS:
        reason_codes.append("P50_TARGET_MISSED")
    if benchmark.p95_latency_ms >= ABRS_P95_TARGET_MS:
        reason_codes.append("P95_TARGET_MISSED")
    if benchmark.p99_latency_ms >= ABRS_P99_TARGET_MS:
        reason_codes.append("P99_TARGET_MISSED")
    if benchmark.throughput_per_partition < ABRS_THROUGHPUT_TARGET_PER_PARTITION:
        reason_codes.append("THROUGHPUT_TARGET_MISSED")
    required_requests = profile.expected_first_live_qps * profile.burst_multiplier
    if benchmark.total_requests < required_requests:
        reason_codes.append("TWO_X_BURST_LOAD_NOT_COVERED")

    return PhaseDBenchmarkGateReport(
        benchmark=benchmark,
        profile=profile,
        reason_codes=tuple(reason_codes),
        representative_benchmark_ready=not reason_codes,
        live_reward_ready=False,
    )

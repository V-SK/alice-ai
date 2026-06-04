from __future__ import annotations

import time
from dataclasses import dataclass
from decimal import Decimal

import sqlalchemy as sa

import alice_acp.abrs.service as abrs_service
from alice_acp.abrs import ReservationRequest
from alice_acp.test_harness.failover import run_failover_drill
from alice_acp.test_harness.reports import BenchmarkReport, PhaseBBenchmarkReadinessReport
from alice_acp.test_harness.shadow_ledger import make_reservation_request, seed_budget_accounts


@dataclass(frozen=True, slots=True)
class BenchmarkConfig:
    run_id: str = "benchmark-a7"
    total_requests: int = 8
    partition_count: int = 1
    max_rewardable_acu: Decimal = Decimal("1")
    environment: str = "local-postgres"
    database_or_storage_backend: str = "postgres"
    commit_sha: str = "unknown"


def run_abrs_benchmark(
    engine: sa.Engine,
    *,
    server_secret: bytes,
    config: BenchmarkConfig | None = None,
) -> BenchmarkReport:
    config = config or BenchmarkConfig()
    if config.total_requests <= 0:
        raise ValueError("total_requests must be positive")
    if config.partition_count <= 0:
        raise ValueError("partition_count must be positive")

    base_request = make_reservation_request(
        config.run_id,
        max_rewardable_acu=config.max_rewardable_acu,
    )
    seed_budget_accounts(
        engine,
        base_request,
        limit_acu=(config.max_rewardable_acu * config.total_requests) + Decimal("100"),
    )
    requests = _benchmark_requests(config)

    latencies_ms: list[float] = []
    results = []
    start = time.perf_counter()
    for request in requests:
        request_start = time.perf_counter()
        result = abrs_service.reserve_reward_budget(
            engine,
            request,
            server_secret=server_secret,
            retry_base_sleep_seconds=0,
            retry_jitter_seconds=0,
        )
        latencies_ms.append((time.perf_counter() - request_start) * 1000)
        results.append(result)
    duration = time.perf_counter() - start

    accepted_count = sum(result.accepted for result in results)
    retryable_count = sum(result.retryable for result in results)
    rejected_count = len(results) - accepted_count - retryable_count
    duplicate_count = _duplicate_reservation_count(engine, base_request)
    lost_release_count = _missing_dimension_entry_count(engine, base_request)
    invariant_violations = (
        _over_limit_rows(engine, base_request) + duplicate_count + lost_release_count
    )
    return BenchmarkReport(
        commit_sha=config.commit_sha,
        environment=config.environment,
        database_or_storage_backend=config.database_or_storage_backend,
        partition_count=config.partition_count,
        test_duration_seconds=duration,
        total_requests=config.total_requests,
        p50_latency_ms=_percentile(latencies_ms, 50),
        p95_latency_ms=_percentile(latencies_ms, 95),
        p99_latency_ms=_percentile(latencies_ms, 99),
        throughput_per_partition=config.total_requests / duration / config.partition_count,
        accepted_count=accepted_count,
        rejected_count=rejected_count,
        retryable_count=retryable_count,
        invariant_violations=invariant_violations,
        duplicate_reservation_count=duplicate_count,
        lost_release_count=lost_release_count,
    )


def run_phase_b_benchmark_readiness(
    engine: sa.Engine,
    *,
    server_secret: bytes,
    config: BenchmarkConfig | None = None,
) -> PhaseBBenchmarkReadinessReport:
    benchmark = run_abrs_benchmark(engine, server_secret=server_secret, config=config)
    failover = run_failover_drill(
        engine,
        server_secret=server_secret,
        run_id="phase-b-local-failover",
    )
    return PhaseBBenchmarkReadinessReport(
        benchmark=benchmark,
        failover=failover,
        production_ha_ready=False,
        live_reward_ready=False,
    )


def _benchmark_requests(config: BenchmarkConfig) -> list[ReservationRequest]:
    return [
        make_reservation_request(
            config.run_id,
            admission_index=index,
            max_rewardable_acu=config.max_rewardable_acu,
        )
        for index in range(1, config.total_requests + 1)
    ]


def _percentile(values: list[float], percentile: int) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, round((percentile / 100) * (len(ordered) - 1))))
    return ordered[index]


def _duplicate_reservation_count(engine: sa.Engine, request: ReservationRequest) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(
                sa.text(
                    """
                    SELECT count(*)
                    FROM (
                        SELECT idempotency_key
                        FROM abrs_reservation
                        WHERE epoch_id = :epoch_id
                        GROUP BY idempotency_key
                        HAVING count(*) > 1
                    ) duplicate_keys
                    """
                ),
                {"epoch_id": request.epoch_id},
            ).scalar_one()
        )


def _missing_dimension_entry_count(engine: sa.Engine, request: ReservationRequest) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(
                sa.text(
                    """
                    SELECT count(*)
                    FROM abrs_reservation reservation
                    WHERE reservation.epoch_id = :epoch_id
                      AND (
                          SELECT count(*)
                          FROM abrs_reservation_dimension_entry entry
                          WHERE entry.reservation_id = reservation.reservation_id
                      ) = 0
                    """
                ),
                {"epoch_id": request.epoch_id},
            ).scalar_one()
        )


def _over_limit_rows(engine: sa.Engine, request: ReservationRequest) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(
                sa.text(
                    """
                    SELECT count(*)
                    FROM budget_dimension_account
                    WHERE epoch_id = :epoch_id
                      AND reserved_acu + consumed_acu + under_review_acu > limit_acu
                    """
                ),
                {"epoch_id": request.epoch_id},
            ).scalar_one()
        )

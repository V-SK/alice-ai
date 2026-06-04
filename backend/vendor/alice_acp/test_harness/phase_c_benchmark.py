from __future__ import annotations

from datetime import UTC, datetime, timedelta

import sqlalchemy as sa

from alice_acp.test_harness.benchmark import BenchmarkConfig, run_abrs_benchmark
from alice_acp.test_harness.phase_c_dry_run import run_phase_c_contract_dry_run
from alice_acp.test_harness.reports import PhaseCBenchmarkReadinessReport
from alice_acp.verifier import VerifierQueueSample, compute_verifier_backlog_metrics

DEFAULT_OBSERVED_AT = datetime(2026, 5, 23, 16, 30, tzinfo=UTC)


def run_phase_c_benchmark_readiness(
    engine: sa.Engine,
    *,
    server_secret: bytes,
    config: BenchmarkConfig | None = None,
    min_mttd_samples: int = 3,
) -> PhaseCBenchmarkReadinessReport:
    benchmark_config = config or BenchmarkConfig(run_id="phase-c-benchmark")
    benchmark = run_abrs_benchmark(
        engine,
        server_secret=server_secret,
        config=benchmark_config,
    )
    dry_run = run_phase_c_contract_dry_run(
        engine,
        server_secret=server_secret,
        run_id=f"{benchmark_config.run_id}-contract",
    )
    metrics = compute_verifier_backlog_metrics(
        (
            VerifierQueueSample(
                reservation_id=dry_run.reservation_id,
                enqueued_at=DEFAULT_OBSERVED_AT - timedelta(seconds=90),
                observed_at=DEFAULT_OBSERVED_AT,
                fraud_class="cached_answer_replay",
                verdict="pass",
                completed_at=DEFAULT_OBSERVED_AT,
            ),
        ),
        min_completed_samples=min_mttd_samples,
    )
    return PhaseCBenchmarkReadinessReport(
        benchmark=benchmark,
        phase_c_dry_run=dry_run,
        verifier_completed_sample_count=metrics.completed_sample_count,
        verifier_min_completed_samples=metrics.min_completed_samples,
        verifier_p95_sample_status=metrics.p95_sample_status,
        production_ha_ready=False,
        live_reward_ready=False,
    )

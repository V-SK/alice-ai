from __future__ import annotations

from alice_acp.verifier.types import VerifierBacklogMetrics, VerifierQueueSample


def compute_verifier_backlog_metrics(
    samples: tuple[VerifierQueueSample, ...],
    *,
    min_completed_samples: int = 2,
) -> VerifierBacklogMetrics:
    if min_completed_samples <= 0:
        raise ValueError("min_completed_samples must be positive")
    pending = [sample for sample in samples if sample.completed_at is None]
    completed_durations = sorted(
        int((sample.completed_at - sample.enqueued_at).total_seconds())
        for sample in samples
        if sample.completed_at is not None
    )
    verdict_counts: dict[str, dict[str, int]] = {}
    delayed_or_disputed = 0
    for sample in samples:
        verdict_counts.setdefault(sample.fraud_class, {})
        verdict_counts[sample.fraud_class][sample.verdict] = (
            verdict_counts[sample.fraud_class].get(sample.verdict, 0) + 1
        )
        if sample.verdict in {"delayed", "disputed"}:
            delayed_or_disputed += 1

    oldest_pending_age = max(
        (int((sample.observed_at - sample.enqueued_at).total_seconds()) for sample in pending),
        default=0,
    )
    return VerifierBacklogMetrics(
        queue_depth=len(pending),
        oldest_pending_age_seconds=oldest_pending_age,
        p50_mttd_seconds=_percentile(completed_durations, 50),
        p95_mttd_seconds=_percentile(completed_durations, 95),
        verdict_counts_by_fraud_class=verdict_counts,
        delayed_or_disputed_count=delayed_or_disputed,
        completed_sample_count=len(completed_durations),
        min_completed_samples=min_completed_samples,
        p95_sample_status=(
            "sufficient_samples"
            if len(completed_durations) >= min_completed_samples
            else "insufficient_samples"
        ),
    )


def _percentile(values: list[int], percentile: int) -> int:
    if not values:
        return 0
    index = round((percentile / 100) * (len(values) - 1))
    return values[max(0, min(len(values) - 1, index))]

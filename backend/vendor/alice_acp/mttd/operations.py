from __future__ import annotations

from dataclasses import dataclass

from alice_acp.mttd.types import FOUNDATION_INTERNAL_BUDGET, PUBLIC_MINER_BUCKET, SeedCase
from alice_acp.verifier.types import VerifierBacklogMetrics


@dataclass(frozen=True, slots=True)
class SeededMTTDOperationalReport:
    seed_count: int
    p95_mttd_seconds: int
    public_miner_bucket_debits: int
    dual_blind_violations: int
    reason_codes: tuple[str, ...]
    cap_reduction_required: bool
    live_enforcement_ready: bool = False


def evaluate_seeded_mttd_operations(
    seeds: tuple[SeedCase, ...],
    metrics: VerifierBacklogMetrics,
    *,
    public_miner_bucket_debits: int,
) -> SeededMTTDOperationalReport:
    if public_miner_bucket_debits < 0:
        raise ValueError("public_miner_bucket_debits must be non-negative")

    reason_codes: list[str] = []
    if not seeds:
        reason_codes.append("NO_SEEDS_SCHEDULED")
    if any(seed.budget_namespace == PUBLIC_MINER_BUCKET for seed in seeds):
        reason_codes.append("SEED_USES_PUBLIC_MINER_BUCKET")
    if any(seed.budget_namespace != FOUNDATION_INTERNAL_BUDGET for seed in seeds):
        reason_codes.append("SEED_NAMESPACE_NOT_FOUNDATION_INTERNAL")
    dual_blind_violations = sum(
        seed.verifier_visible_evidence_ref == seed.ground_truth_ref for seed in seeds
    )
    if dual_blind_violations:
        reason_codes.append("DUAL_BLIND_VIOLATION")
    if public_miner_bucket_debits:
        reason_codes.append("PUBLIC_MINER_BUCKET_DEBITED_BY_SEED")
    if metrics.p95_sample_status != "sufficient_samples":
        reason_codes.append("SEEDED_MTTD_INSUFFICIENT_SAMPLES")

    cap_reduction_required = bool(
        public_miner_bucket_debits
        or dual_blind_violations
        or metrics.p95_sample_status != "sufficient_samples"
    )
    return SeededMTTDOperationalReport(
        seed_count=len(seeds),
        p95_mttd_seconds=metrics.p95_mttd_seconds,
        public_miner_bucket_debits=public_miner_bucket_debits,
        dual_blind_violations=dual_blind_violations,
        reason_codes=tuple(reason_codes),
        cap_reduction_required=cap_reduction_required,
        live_enforcement_ready=False,
    )

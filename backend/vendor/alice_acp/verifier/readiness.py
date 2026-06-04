from __future__ import annotations

from dataclasses import dataclass

from alice_acp.verifier.types import VerifierBacklogMetrics

P95_MTTD_TARGET_SECONDS = 12 * 60 * 60


@dataclass(frozen=True, slots=True)
class VerifierFleetReadinessReport:
    metrics: VerifierBacklogMetrics
    reason_codes: tuple[str, ...]
    verifier_fleet_ready: bool = False
    live_reward_ready: bool = False


def evaluate_verifier_fleet_readiness(
    metrics: VerifierBacklogMetrics,
    *,
    capacity_floor_met: bool,
    no_active_integrity_incident: bool,
) -> VerifierFleetReadinessReport:
    reason_codes: list[str] = []
    if metrics.p95_sample_status != "sufficient_samples":
        reason_codes.append("P95_MTTD_INSUFFICIENT_SAMPLES")
    if metrics.p95_mttd_seconds > P95_MTTD_TARGET_SECONDS:
        reason_codes.append("P95_MTTD_TARGET_MISSED")
    if metrics.oldest_pending_age_seconds > P95_MTTD_TARGET_SECONDS:
        reason_codes.append("VERIFIER_BACKLOG_AGE_EXCEEDED")
    if metrics.delayed_or_disputed_count:
        reason_codes.append("DELAYED_OR_DISPUTED_VERDICTS_PRESENT")
    if not capacity_floor_met:
        reason_codes.append("VERIFIER_CAPACITY_FLOOR_NOT_MET")
    if not no_active_integrity_incident:
        reason_codes.append("ACTIVE_VERIFIER_INTEGRITY_INCIDENT")

    return VerifierFleetReadinessReport(
        metrics=metrics,
        reason_codes=tuple(reason_codes),
        verifier_fleet_ready=not reason_codes,
        live_reward_ready=False,
    )

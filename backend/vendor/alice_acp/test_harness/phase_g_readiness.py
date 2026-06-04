from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from alice_acp.test_harness.phase_g_common import (
    PHASE_G_REASON_WHY_NOT_LIVE,
    dedupe_reason_codes,
    json_ready_dataclass,
)
from alice_acp.test_harness.phase_g_reviews import (
    PhaseGBenchmarkReviewReport,
    PhaseGComponentEvidenceReviewReport,
    PhaseGProductionHAReviewReport,
    PhaseGShadowWindowReviewReport,
)


@dataclass(frozen=True, slots=True)
class PhaseGReadinessReport:
    ha_review: PhaseGProductionHAReviewReport
    benchmark_review: PhaseGBenchmarkReviewReport
    shadow_window_review: PhaseGShadowWindowReviewReport
    component_evidence_review: PhaseGComponentEvidenceReviewReport
    reason_codes: tuple[str, ...]
    reason_why_not_live: tuple[str, ...] = PHASE_G_REASON_WHY_NOT_LIVE
    external_review_ready: bool = False
    can_start_next_phase_planning: bool = False
    can_start_live_rewards: bool = False
    can_start_payout_executor: bool = False
    production_ha_ready: bool = False
    production_benchmark_ready: bool = False
    live_payment_processor_ready: bool = False

    def as_dict(self) -> dict[str, Any]:
        return json_ready_dataclass(self)


def build_phase_g_readiness_report(
    *,
    ha_review: PhaseGProductionHAReviewReport,
    benchmark_review: PhaseGBenchmarkReviewReport,
    shadow_window_review: PhaseGShadowWindowReviewReport,
    component_evidence_review: PhaseGComponentEvidenceReviewReport,
) -> PhaseGReadinessReport:
    reason_codes: list[str] = []

    if not ha_review.external_review_ready:
        reason_codes.append("PHASE_G_HA_REVIEW_GATE_BLOCKED")
        reason_codes.extend(ha_review.reason_codes)
    if ha_review.production_ha_ready:
        reason_codes.append("PHASE_G_PRODUCTION_HA_READY_NOT_ALLOWED")
    if ha_review.live_reward_ready:
        reason_codes.append("PHASE_G_HA_LIVE_REWARD_READY_NOT_ALLOWED")
    if ha_review.payout_executor_ready:
        reason_codes.append("PHASE_G_HA_PAYOUT_READY_NOT_ALLOWED")

    if not benchmark_review.representative_benchmark_review_ready:
        reason_codes.append("PHASE_G_BENCHMARK_REVIEW_GATE_BLOCKED")
        reason_codes.extend(benchmark_review.reason_codes)
    if benchmark_review.production_benchmark_ready:
        reason_codes.append("PHASE_G_PRODUCTION_BENCHMARK_READY_NOT_ALLOWED")
    if benchmark_review.live_reward_ready:
        reason_codes.append("PHASE_G_BENCHMARK_LIVE_REWARD_READY_NOT_ALLOWED")
    if benchmark_review.payout_executor_ready:
        reason_codes.append("PHASE_G_BENCHMARK_PAYOUT_READY_NOT_ALLOWED")

    if not shadow_window_review.shadow_window_review_ready:
        reason_codes.append("PHASE_G_SHADOW_REVIEW_GATE_BLOCKED")
        reason_codes.extend(shadow_window_review.reason_codes)
    if shadow_window_review.live_reward_ready:
        reason_codes.append("PHASE_G_SHADOW_LIVE_REWARD_READY_NOT_ALLOWED")
    if shadow_window_review.payout_executor_ready:
        reason_codes.append("PHASE_G_SHADOW_PAYOUT_READY_NOT_ALLOWED")

    if not component_evidence_review.component_evidence_review_ready:
        reason_codes.append("PHASE_G_COMPONENT_REVIEW_GATE_BLOCKED")
        reason_codes.extend(component_evidence_review.reason_codes)
    if component_evidence_review.live_reward_ready:
        reason_codes.append("PHASE_G_COMPONENT_LIVE_REWARD_READY_NOT_ALLOWED")
    if component_evidence_review.live_payment_processor_ready:
        reason_codes.append("PHASE_G_COMPONENT_PAYMENT_READY_NOT_ALLOWED")
    if component_evidence_review.payout_executor_ready:
        reason_codes.append("PHASE_G_COMPONENT_PAYOUT_READY_NOT_ALLOWED")

    deduped_reason_codes = dedupe_reason_codes(reason_codes)
    return PhaseGReadinessReport(
        ha_review=ha_review,
        benchmark_review=benchmark_review,
        shadow_window_review=shadow_window_review,
        component_evidence_review=component_evidence_review,
        reason_codes=deduped_reason_codes,
        external_review_ready=not deduped_reason_codes,
        can_start_next_phase_planning=not deduped_reason_codes,
        can_start_live_rewards=False,
        can_start_payout_executor=False,
        production_ha_ready=False,
        production_benchmark_ready=False,
        live_payment_processor_ready=False,
    )

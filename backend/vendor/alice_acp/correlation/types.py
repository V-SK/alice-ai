from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from alice_acp.policy_engine import EvidenceRef, RiskSignal

CorrelationThresholdBand = Literal["low", "collusion_applicable", "under_review"]


@dataclass(frozen=True, slots=True)
class CorrelationScoreInput:
    signal_id: str
    admission_id: str
    attempt_id: str
    route_contract_id: str
    policy_version: str
    observed_at: datetime
    prompt_template_similarity: Decimal
    time_of_day_correlation: Decimal
    asn_or_network_overlap: Decimal
    wallet_graph_distance_inverse: Decimal
    account_creation_wave_proximity: Decimal
    routing_pattern_repetition: Decimal
    score: Decimal
    evidence_refs: tuple[EvidenceRef, ...] = ()

    def __post_init__(self) -> None:
        required = (
            self.signal_id,
            self.admission_id,
            self.attempt_id,
            self.route_contract_id,
            self.policy_version,
        )
        if any(not value for value in required):
            raise ValueError("correlation signal fields must be non-empty")
        if self.observed_at.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware")
        for name in (
            "prompt_template_similarity",
            "time_of_day_correlation",
            "asn_or_network_overlap",
            "wallet_graph_distance_inverse",
            "account_creation_wave_proximity",
            "routing_pattern_repetition",
            "score",
        ):
            value = getattr(self, name)
            if value < Decimal("0") or value > Decimal("1"):
                raise ValueError(f"{name} must be between 0 and 1")


def threshold_band_for_score(score: Decimal) -> CorrelationThresholdBand:
    if score > Decimal("0.5"):
        return "under_review"
    if score >= Decimal("0.3"):
        return "collusion_applicable"
    return "low"


def correlation_signal(score_input: CorrelationScoreInput) -> RiskSignal:
    return RiskSignal(
        signal_id=score_input.signal_id,
        signal_type="requester_miner_correlation",
        producer="alice_acp.correlation.contract",
        policy_version=score_input.policy_version,
        observed_at=score_input.observed_at,
        admission_id=score_input.admission_id,
        attempt_id=score_input.attempt_id,
        route_contract_id=score_input.route_contract_id,
        confidence=Decimal("1"),
        severity="high" if score_input.score > Decimal("0.5") else "medium",
        reason_code=f"REQUESTER_MINER_CORRELATION_{threshold_band_for_score(score_input.score)}",
        evidence_refs=score_input.evidence_refs,
        payload={
            "requester_miner_correlation_score": score_input.score,
            "components": {
                "prompt_template_similarity": str(score_input.prompt_template_similarity),
                "time_of_day_correlation": str(score_input.time_of_day_correlation),
                "asn_or_network_overlap": str(score_input.asn_or_network_overlap),
                "wallet_graph_distance_inverse": str(score_input.wallet_graph_distance_inverse),
                "account_creation_wave_proximity": str(
                    score_input.account_creation_wave_proximity
                ),
                "routing_pattern_repetition": str(score_input.routing_pattern_repetition),
            },
        },
    )

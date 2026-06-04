"""Requester-miner correlation contracts for Phase B."""

from alice_acp.correlation.harness import (
    CorrelationPolicyEvaluation,
    evaluate_correlation_for_policy,
)
from alice_acp.correlation.types import (
    CorrelationScoreInput,
    CorrelationThresholdBand,
    correlation_signal,
    threshold_band_for_score,
)

__all__ = (
    "CorrelationPolicyEvaluation",
    "CorrelationScoreInput",
    "CorrelationThresholdBand",
    "correlation_signal",
    "evaluate_correlation_for_policy",
    "threshold_band_for_score",
)

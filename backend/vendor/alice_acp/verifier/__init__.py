"""Verifier signal contracts and local readiness gates."""

from alice_acp.verifier.contract import compute_verifier_backlog_metrics
from alice_acp.verifier.ingestion import ingest_verifier_signal
from alice_acp.verifier.readiness import (
    P95_MTTD_TARGET_SECONDS,
    VerifierFleetReadinessReport,
    evaluate_verifier_fleet_readiness,
)
from alice_acp.verifier.types import (
    MTTDSampleStatus,
    VerifierBacklogMetrics,
    VerifierIngestionResult,
    VerifierQueueSample,
    VerifierSignal,
)

__all__ = (
    "P95_MTTD_TARGET_SECONDS",
    "MTTDSampleStatus",
    "VerifierBacklogMetrics",
    "VerifierFleetReadinessReport",
    "VerifierIngestionResult",
    "VerifierQueueSample",
    "VerifierSignal",
    "compute_verifier_backlog_metrics",
    "evaluate_verifier_fleet_readiness",
    "ingest_verifier_signal",
)

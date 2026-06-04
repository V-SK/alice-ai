from __future__ import annotations

from decimal import Decimal

from alice_acp.policy_engine import RiskSignal
from alice_acp.settlement import VerificationResult
from alice_acp.settlement.types import (
    VERIFIER_VERDICT_FAILED_WITH_VERIFIED_ACU,
    VERIFIER_VERDICT_NOT_FINAL,
)
from alice_acp.verifier.types import VerifierIngestionResult, VerifierSignal


def ingest_verifier_signal(signal: VerifierSignal) -> VerifierIngestionResult:
    if signal.verifier_verdict == "pass" or (
        signal.verifier_verdict == "fail" and signal.verified_acu == Decimal("0")
    ):
        return VerifierIngestionResult(
            status=(
                "verification_ready"
                if signal.verifier_verdict == "pass"
                else "terminal_zero_ready"
            ),
            verifier_signal_id=signal.verifier_signal_id,
            verification_result=VerificationResult(
                reservation_id=signal.reservation_id,
                admission_id=signal.admission_id,
                attempt_id=signal.attempt_id,
                verifier_verdict=signal.verifier_verdict,
                verified_acu=signal.verified_acu,
                verification_completed_at=signal.verification_completed_at,
                applicable_signal_snapshot={"verifier_signal_id": signal.verifier_signal_id},
                evidence_refs=tuple(ref.evidence_ref for ref in signal.evidence_refs),
            ),
            risk_signals=signal.risk_signals,
        )

    reason_code = (
        VERIFIER_VERDICT_FAILED_WITH_VERIFIED_ACU
        if signal.verifier_verdict == "fail"
        else VERIFIER_VERDICT_NOT_FINAL
    )
    return VerifierIngestionResult(
        status="under_review",
        verifier_signal_id=signal.verifier_signal_id,
        risk_signals=(
            RiskSignal(
                signal_id=f"{signal.verifier_signal_id}-under-review",
                signal_type="verifier_verdict",
                producer="alice_acp.verifier.ingestion",
                policy_version=signal.verifier_policy_version,
                observed_at=signal.verification_completed_at,
                admission_id=signal.admission_id,
                attempt_id=signal.attempt_id,
                route_contract_id=signal.route_contract_id,
                reservation_id=signal.reservation_id,
                confidence=Decimal("1"),
                severity="medium",
                reason_code=reason_code,
                evidence_refs=signal.evidence_refs,
                payload={"verifier_verdict": signal.verifier_verdict, "under_review": True},
            ),
        ),
        reason_code=reason_code,
    )

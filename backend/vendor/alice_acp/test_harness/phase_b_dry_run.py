from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import sqlalchemy as sa

import alice_acp.abrs.service as abrs_service
import alice_acp.settlement.service as settlement_service
from alice_acp.policy_engine import (
    R2_R3_P1,
    EvidenceRef,
    RiskSignal,
    assess_settlement_risk,
    risk_signals_to_context,
)
from alice_acp.settlement import VerificationResult
from alice_acp.test_harness.reports import PhaseBDryRunReport
from alice_acp.test_harness.shadow_ledger import seed_budget_accounts
from alice_acp.verifier import VerifierSignal
from alice_acp.wac import AttemptRouteRegistry, WACAdmissionAttempt, to_reservation_request

DEFAULT_EXPIRES_AT = datetime(2026, 5, 23, 16, 0, tzinfo=UTC)
DEFAULT_VERIFIED_AT = datetime(2026, 5, 23, 16, 30, tzinfo=UTC)
DEFAULT_ACTOR = "alice_acp.test_harness.phase_b"


def run_phase_b_contract_dry_run(
    engine: sa.Engine,
    *,
    server_secret: bytes,
    run_id: str = "phase-b-dry-run",
) -> PhaseBDryRunReport:
    attempt = WACAdmissionAttempt(
        admission_id=f"{run_id}-admission-1",
        attempt_id=f"{run_id}-attempt-1",
        route_contract_id=f"{run_id}-route-1",
        epoch_id=f"{run_id}-epoch",
        source_budget_id=f"{run_id}-source-budget",
        mode_budget_id=f"{run_id}-mode-budget",
        passport_id=f"{run_id}-passport",
        wallet_id=f"{run_id}-wallet",
        host_id=f"{run_id}-host",
        accelerator_id=f"{run_id}-accelerator",
        max_rewardable_acu=Decimal("100"),
        formula_version="formula-v1",
        tranche_policy_version="tranche-v1",
        reservation_expires_at=DEFAULT_EXPIRES_AT,
        cluster_status="candidate_cluster_high",
        cluster_id=f"{run_id}-cluster",
    )
    wac_result = AttemptRouteRegistry().validate_and_record(attempt)
    if not wac_result.accepted:
        raise RuntimeError(f"WAC contract dry-run rejected: {wac_result.reason_code}")

    request = to_reservation_request(attempt)
    seed_budget_accounts(engine, request)
    reservation = abrs_service.reserve_reward_budget(
        engine,
        request,
        server_secret=server_secret,
        actor_service=DEFAULT_ACTOR,
        retry_base_sleep_seconds=0,
        retry_jitter_seconds=0,
    )
    if not reservation.accepted or reservation.reservation_id is None:
        raise RuntimeError(f"ABRS dry-run rejected: {reservation.reason_code}")

    evidence = EvidenceRef(
        evidence_ref=f"evidence://phase-b/{run_id}/verifier",
        evidence_type="verifier_result_hash",
        digest="sha256:phase-b-dry-run",
        producer=DEFAULT_ACTOR,
        captured_at=DEFAULT_VERIFIED_AT,
    )
    verifier_signal = VerifierSignal(
        verifier_signal_id=f"{run_id}-verifier-signal-1",
        reservation_id=reservation.reservation_id,
        admission_id=request.admission_id,
        attempt_id=request.attempt_id,
        route_contract_id=request.route_contract_id,
        verifier_verdict="pass",
        verified_acu=Decimal("60"),
        verification_completed_at=DEFAULT_VERIFIED_AT,
        verifier_policy_version="verifier-policy-v1",
        evidence_refs=(evidence,),
        risk_signals=(
            RiskSignal(
                signal_id=f"{run_id}-correlation-signal-1",
                signal_type="requester_miner_correlation",
                producer=DEFAULT_ACTOR,
                policy_version="risk_policy_v1",
                observed_at=DEFAULT_VERIFIED_AT,
                admission_id=request.admission_id,
                attempt_id=request.attempt_id,
                route_contract_id=request.route_contract_id,
                confidence=Decimal("1"),
                severity="low",
                reason_code="DRY_RUN_LOW_CORRELATION",
                evidence_refs=(evidence,),
                payload={"requester_miner_correlation_score": Decimal("0.2")},
            ),
        ),
    )
    context = risk_signals_to_context(
        route_source_class=R2_R3_P1,
        signals=verifier_signal.risk_signals,
    )
    assessment = assess_settlement_risk(context)
    verification = VerificationResult(
        reservation_id=reservation.reservation_id,
        admission_id=request.admission_id,
        attempt_id=request.attempt_id,
        verifier_verdict=verifier_signal.verifier_verdict,
        verified_acu=verifier_signal.verified_acu,
        verification_completed_at=verifier_signal.verification_completed_at,
        applicable_signal_snapshot={"phase_b_contract_dry_run": True},
        evidence_refs=(evidence.evidence_ref,),
    )
    liability = settlement_service.consume_reservation_and_create_liability(
        engine,
        verification,
        server_secret=server_secret,
        actor_service=DEFAULT_ACTOR,
    )
    if not liability.accepted or liability.liability_id is None:
        raise RuntimeError(f"settlement dry-run rejected: {liability.reason_code}")
    split = settlement_service.split_pending_liability(
        engine,
        liability.liability_id,
        assessment,
        tranche_policy_version=request.tranche_policy_version,
        server_secret=server_secret,
        actor_service=DEFAULT_ACTOR,
    )
    if not split.accepted:
        raise RuntimeError(f"tranche dry-run rejected: {split.reason_code}")

    row = _liability_row(engine, liability.liability_id)
    return PhaseBDryRunReport(
        run_id=run_id,
        wac_contract_status=wac_result.status,
        reservation_id=reservation.reservation_id,
        liability_id=liability.liability_id,
        verifier_signal_id=verifier_signal.verifier_signal_id,
        risk_assessment_max_window_seconds=assessment.max_window_seconds,
        paid_acu_zero=row["paid_acu"] == Decimal("0"),
        estimated_base_release_acu=row["base_release_acu"],
        estimated_risk_reserve_acu=row["risk_reserve_acu"],
        service_functions=(
            "alice_acp.abrs.service.reserve_reward_budget",
            "alice_acp.settlement.service.consume_reservation_and_create_liability",
            "alice_acp.settlement.service.split_pending_liability",
        ),
    )


def _liability_row(engine: sa.Engine, liability_id: str) -> sa.RowMapping:
    with engine.connect() as connection:
        return connection.execute(
            sa.text("SELECT * FROM settlement_liability WHERE liability_id = :liability_id"),
            {"liability_id": liability_id},
        ).mappings().one()

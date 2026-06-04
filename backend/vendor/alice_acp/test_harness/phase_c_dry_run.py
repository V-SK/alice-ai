from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import cast

import sqlalchemy as sa

import alice_acp.abrs.service as abrs_service
import alice_acp.settlement.service as settlement_service
from alice_acp.correlation import CorrelationScoreInput, evaluate_correlation_for_policy
from alice_acp.mttd import create_seed_case, run_seeded_mttd_reservation
from alice_acp.p1_sanitizer import evaluate_p1_sanitizer_readiness
from alice_acp.payment_policy import CreditProvenance, execute_payment_finality_policy
from alice_acp.policy_engine import (
    R2_R3_P1,
    EvidenceRef,
    RiskSignal,
    append_risk_signal,
    assess_settlement_risk,
    load_risk_signals_for_attempt,
    risk_signals_to_context,
)
from alice_acp.settlement import VerificationResult
from alice_acp.test_harness.reports import PhaseCDryRunReport
from alice_acp.test_harness.shadow_ledger import seed_budget_accounts
from alice_acp.verifier import VerifierSignal, ingest_verifier_signal
from alice_acp.wac import WACAdmissionAttempt, to_reservation_request
from alice_acp.wac.registry import validate_and_record_attempt_route

DEFAULT_EXPIRES_AT = datetime(2026, 5, 23, 16, 0, tzinfo=UTC)
DEFAULT_OBSERVED_AT = datetime(2026, 5, 23, 16, 30, tzinfo=UTC)
DEFAULT_ACTOR = "alice_acp.test_harness.phase_c"


def run_phase_c_contract_dry_run(
    engine: sa.Engine,
    *,
    server_secret: bytes,
    run_id: str = "phase-c-dry-run",
    correlation_score: Decimal = Decimal("0.2"),
) -> PhaseCDryRunReport:
    attempt = _admission_attempt(run_id)
    wac_result = validate_and_record_attempt_route(
        engine,
        attempt,
        actor_service=DEFAULT_ACTOR,
    )
    if not wac_result.accepted:
        raise RuntimeError(f"WAC durable registry rejected: {wac_result.reason_code}")

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

    evidence = _evidence(run_id)
    verifier_signal = VerifierSignal(
        verifier_signal_id=f"{run_id}-verifier-signal-1",
        reservation_id=reservation.reservation_id,
        admission_id=request.admission_id,
        attempt_id=request.attempt_id,
        route_contract_id=request.route_contract_id,
        verifier_verdict="pass",
        verified_acu=Decimal("60"),
        verification_completed_at=DEFAULT_OBSERVED_AT,
        verifier_policy_version="verifier-policy-v1",
        evidence_refs=(evidence,),
    )
    verifier_ingestion = ingest_verifier_signal(verifier_signal)
    if verifier_ingestion.verification_result is None:
        raise RuntimeError(f"verifier dry-run not payable: {verifier_ingestion.reason_code}")

    p1_readiness = evaluate_p1_sanitizer_readiness(
        request_id=run_id,
        input_available=False,
        output_available=False,
        streaming_policy="buffer_and_sanitize",
    )
    p1_signal = RiskSignal(
        signal_id=f"{run_id}-p1-readiness",
        signal_type="p1_sanitizer",
        producer="alice_acp.p1_sanitizer.readiness",
        policy_version="p1-readiness-v1",
        observed_at=DEFAULT_OBSERVED_AT,
        admission_id=request.admission_id,
        attempt_id=request.attempt_id,
        route_contract_id=request.route_contract_id,
        confidence=Decimal("1"),
        severity="medium",
        reason_code=p1_readiness.reason_codes[0],
        evidence_refs=(evidence,),
        payload={"p1": True, "p1_live_reward_ready": p1_readiness.p1_live_reward_ready},
    )

    payment_execution = execute_payment_finality_policy(
        CreditProvenance(
            provenance_id=f"{run_id}-payment-provenance",
            admission_id=request.admission_id,
            attempt_id=request.attempt_id,
            route_contract_id=request.route_contract_id,
            finality_level="level_2_captured",
            credit_type="paid",
            policy_version="payment-policy-v1",
            observed_at=DEFAULT_OBSERVED_AT,
            evidence_refs=(evidence,),
        ),
        other_risk_gates_pass=True,
    )
    correlation = evaluate_correlation_for_policy(
        CorrelationScoreInput(
            signal_id=f"{run_id}-correlation",
            admission_id=request.admission_id,
            attempt_id=request.attempt_id,
            route_contract_id=request.route_contract_id,
            policy_version="correlation-policy-v1",
            observed_at=DEFAULT_OBSERVED_AT,
            prompt_template_similarity=correlation_score,
            time_of_day_correlation=correlation_score,
            asn_or_network_overlap=Decimal("0"),
            wallet_graph_distance_inverse=Decimal("0"),
            account_creation_wave_proximity=Decimal("0"),
            routing_pattern_repetition=correlation_score,
            score=correlation_score,
            evidence_refs=(evidence,),
        )
    )
    for signal in (p1_signal, payment_execution.risk_signal, correlation.signal):
        append_risk_signal(engine, signal, actor_service=DEFAULT_ACTOR)

    loaded_signals = load_risk_signals_for_attempt(
        engine,
        admission_id=request.admission_id,
        attempt_id=request.attempt_id,
        route_contract_id=request.route_contract_id,
    )
    assessment = assess_settlement_risk(
        risk_signals_to_context(
            route_source_class=R2_R3_P1,
            signals=loaded_signals,
        )
    )
    liability = settlement_service.consume_reservation_and_create_liability(
        engine,
        cast(VerificationResult, verifier_ingestion.verification_result),
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
    if split.status == "rejected":
        raise RuntimeError(f"tranche dry-run rejected: {split.reason_code}")

    seed = create_seed_case(
        seed_id=f"{run_id}-seed",
        seed_class="cached_answer_replay",
        verifier_visible_evidence_ref=f"evidence://phase-c/{run_id}/seed/verifier",
        ground_truth_ref=f"evidence://phase-c/{run_id}/seed/ground-truth",
    )
    seed_report = run_seeded_mttd_reservation(
        engine,
        seed,
        server_secret=server_secret,
    )
    row = _liability_row(engine, liability.liability_id)
    return PhaseCDryRunReport(
        run_id=run_id,
        wac_contract_status=wac_result.status,
        reservation_id=reservation.reservation_id,
        liability_id=liability.liability_id,
        settlement_status=liability.status,
        tranche_status=split.status,
        verifier_ingestion_status=verifier_ingestion.status,
        p1_live_reward_ready=p1_readiness.p1_live_reward_ready,
        p1_reason_codes=p1_readiness.reason_codes,
        r3_eligible=payment_execution.eligibility.eligible,
        r3_cap_treatment=payment_execution.eligibility.cap_treatment,
        correlation_requires_under_review=correlation.assessment.requires_under_review,
        seed_budget_namespace=seed_report.budget_namespace,
        seed_public_miner_bucket_debits=seed_report.public_miner_bucket_debits,
        seed_dual_blind_preserved=seed_report.dual_blind_preserved,
        signal_store_event_count=_risk_signal_count(
            engine,
            request.admission_id,
            request.attempt_id,
        ),
        paid_acu_zero=row["paid_acu"] == Decimal("0"),
        estimated_base_release_acu=row["base_release_acu"],
        estimated_risk_reserve_acu=row["risk_reserve_acu"],
        service_functions=(
            "alice_acp.wac.registry.validate_and_record_attempt_route",
            "alice_acp.abrs.service.reserve_reward_budget",
            "alice_acp.verifier.ingestion.ingest_verifier_signal",
            "alice_acp.policy_engine.signal_store.append_risk_signal",
            "alice_acp.settlement.service.consume_reservation_and_create_liability",
            "alice_acp.settlement.service.split_pending_liability",
            "alice_acp.mttd.execution_harness.run_seeded_mttd_reservation",
        ),
    )


def _admission_attempt(run_id: str) -> WACAdmissionAttempt:
    return WACAdmissionAttempt(
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


def _evidence(run_id: str) -> EvidenceRef:
    return EvidenceRef(
        evidence_ref=f"evidence://phase-c/{run_id}/verifier",
        evidence_type="verifier_result_hash",
        digest="sha256:phase-c-dry-run",
        producer=DEFAULT_ACTOR,
        captured_at=DEFAULT_OBSERVED_AT,
    )


def _liability_row(engine: sa.Engine, liability_id: str) -> sa.RowMapping:
    with engine.connect() as connection:
        return connection.execute(
            sa.text("SELECT * FROM settlement_liability WHERE liability_id = :liability_id"),
            {"liability_id": liability_id},
        ).mappings().one()


def _risk_signal_count(engine: sa.Engine, admission_id: str, attempt_id: str) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(
                sa.text(
                    """
                    SELECT count(*)
                    FROM risk_signal_event
                    WHERE admission_id = :admission_id
                      AND attempt_id = :attempt_id
                    """
                ),
                {"admission_id": admission_id, "attempt_id": attempt_id},
            ).scalar_one()
        )

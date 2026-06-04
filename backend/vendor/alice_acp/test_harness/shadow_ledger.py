from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import sqlalchemy as sa

import alice_acp.abrs.service as abrs_service
import alice_acp.settlement.service as settlement_service
from alice_acp.abrs import ReservationDimension, ReservationRequest, build_required_dimensions
from alice_acp.policy_engine import (
    CACHED_ANSWER_REPLAY,
    SECONDS_24H,
    FraudClassApplicability,
    RiskAssessment,
)
from alice_acp.settlement import VerificationResult
from alice_acp.test_harness.reports import ShadowLedgerReport

DEFAULT_EXPIRES_AT = datetime(2026, 5, 23, 16, 0, tzinfo=UTC)
DEFAULT_VERIFIED_AT = datetime(2026, 5, 23, 16, 30, tzinfo=UTC)
DEFAULT_ACTOR = "alice_acp.test_harness"


def make_reservation_request(
    run_id: str,
    *,
    admission_index: int = 1,
    max_rewardable_acu: Decimal = Decimal("100"),
    passport_id: str | None = None,
) -> ReservationRequest:
    return ReservationRequest(
        admission_id=f"{run_id}-admission-{admission_index}",
        attempt_id=f"{run_id}-attempt-{admission_index}",
        route_contract_id=f"{run_id}-route",
        epoch_id=f"{run_id}-epoch",
        source_budget_id=f"{run_id}-source-budget",
        mode_budget_id=f"{run_id}-mode-budget",
        passport_id=passport_id or f"{run_id}-passport",
        wallet_id=f"{run_id}-wallet",
        host_id=f"{run_id}-host",
        accelerator_id=f"{run_id}-accelerator",
        max_rewardable_acu=max_rewardable_acu,
        formula_version="formula-v1",
        tranche_policy_version="tranche-v1",
        reservation_expires_at=DEFAULT_EXPIRES_AT,
        cluster_status="candidate_cluster_high",
        cluster_id=f"{run_id}-cluster",
    )


def default_tranche_assessment() -> RiskAssessment:
    return RiskAssessment(
        applicable_classes=(
            FraudClassApplicability(
                fraud_class=CACHED_ANSWER_REPLAY,
                window_seconds=SECONDS_24H,
                reason_code="shadow_ledger_default_24h_window",
            ),
        ),
        max_window_seconds=SECONDS_24H,
    )


def seed_budget_accounts(
    engine: sa.Engine,
    request: ReservationRequest,
    *,
    limit_acu: Decimal = Decimal("100000"),
    bucket_namespace: str = "PUBLIC_MINER_BUCKET",
) -> tuple[ReservationDimension, ...]:
    if bucket_namespace not in {"PUBLIC_MINER_BUCKET", "FOUNDATION_INTERNAL_BUDGET"}:
        raise ValueError("unsupported bucket namespace")
    dimensions = build_required_dimensions(request)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                """
                INSERT INTO budget_epoch (
                    epoch_id,
                    bucket_namespace,
                    public_miner_bucket_acu,
                    foundation_internal_budget_acu,
                    starts_at,
                    ends_at,
                    policy_version,
                    status,
                    created_at,
                    updated_at
                )
                VALUES (
                    :epoch_id,
                    :bucket_namespace,
                    :public_miner_bucket_acu,
                    :foundation_internal_budget_acu,
                    now(),
                    now() + interval '1 day',
                    'policy-v1',
                    'active',
                    now(),
                    now()
                )
                """
            ),
            {
                "epoch_id": request.epoch_id,
                "bucket_namespace": bucket_namespace,
                "public_miner_bucket_acu": (
                    limit_acu if bucket_namespace == "PUBLIC_MINER_BUCKET" else Decimal("0")
                ),
                "foundation_internal_budget_acu": (
                    limit_acu
                    if bucket_namespace == "FOUNDATION_INTERNAL_BUDGET"
                    else Decimal("100")
                ),
            },
        )
        for index, dimension in enumerate(dimensions, start=1):
            connection.execute(
                sa.text(
                    """
                    INSERT INTO budget_dimension_account (
                        account_id,
                        epoch_id,
                        dimension_type,
                        dimension_key,
                        limit_acu,
                        reserved_acu,
                        consumed_acu,
                        released_acu,
                        under_review_acu,
                        status,
                        policy_version,
                        created_at,
                        updated_at
                    )
                    VALUES (
                        :account_id,
                        :epoch_id,
                        :dimension_type,
                        :dimension_key,
                        :limit_acu,
                        0,
                        0,
                        0,
                        0,
                        'active',
                        'policy-v1',
                        now(),
                        now()
                    )
                    """
                ),
                {
                    "account_id": f"{request.epoch_id}-account-{index}",
                    "epoch_id": request.epoch_id,
                    "dimension_type": dimension.dimension_type,
                    "dimension_key": dimension.dimension_key,
                    "limit_acu": limit_acu,
                },
            )
    return dimensions


def run_shadow_ledger_flow(
    engine: sa.Engine,
    *,
    server_secret: bytes,
    run_id: str = "shadow-a7",
    verified_acu: Decimal = Decimal("60"),
    request: ReservationRequest | None = None,
    assessment: RiskAssessment | None = None,
) -> ShadowLedgerReport:
    request = request or make_reservation_request(run_id)
    assessment = assessment or default_tranche_assessment()
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
        raise RuntimeError(f"shadow reservation failed: {reservation.reason_code}")

    verification = VerificationResult(
        reservation_id=reservation.reservation_id,
        admission_id=request.admission_id,
        attempt_id=request.attempt_id,
        verifier_verdict="pass",
        verified_acu=verified_acu,
        verification_completed_at=DEFAULT_VERIFIED_AT,
        applicable_signal_snapshot={"shadow_ledger": True},
        evidence_refs=(f"evidence://shadow/{run_id}",),
    )
    liability = settlement_service.consume_reservation_and_create_liability(
        engine,
        verification,
        server_secret=server_secret,
        actor_service=DEFAULT_ACTOR,
    )
    if not liability.accepted or liability.liability_id is None:
        raise RuntimeError(f"shadow liability failed: {liability.reason_code}")

    split = settlement_service.split_pending_liability(
        engine,
        liability.liability_id,
        assessment,
        tranche_policy_version=request.tranche_policy_version,
        server_secret=server_secret,
        actor_service=DEFAULT_ACTOR,
    )
    if not split.accepted:
        raise RuntimeError(f"shadow tranche split failed: {split.reason_code}")

    liability_row = _liability_row(engine, liability.liability_id)
    return ShadowLedgerReport(
        run_id=run_id,
        reservation_id=reservation.reservation_id,
        liability_id=liability.liability_id,
        passport_id=request.passport_id,
        total_reserved_acu=liability_row["total_reserved_acu"],
        total_verified_acu=liability_row["total_verified_acu"],
        estimated_base_release_acu=liability_row["base_release_acu"],
        estimated_risk_reserve_acu=liability_row["risk_reserve_acu"],
        source_budget_utilization_acu=_dimension_consumed_acu(
            engine,
            "source_budget",
            request.source_budget_id,
        ),
        mode_budget_utilization_acu=_dimension_consumed_acu(
            engine,
            "mode_budget",
            request.mode_budget_id,
        ),
        cap_hits=_audit_event_count(engine, "abrs_reservation_rejected_cap"),
        idempotency_replays=_audit_event_count(engine, "abrs_idempotent_replay"),
        under_review_count=_liability_state_count(engine, "under_review"),
        accounting_corrections=_table_count(engine, "settlement_correction_entry"),
        shadow_acu_per_passport={request.passport_id: liability_row["total_verified_acu"]},
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


def _dimension_consumed_acu(
    engine: sa.Engine,
    dimension_type: str,
    dimension_key: str,
) -> Decimal:
    with engine.connect() as connection:
        return connection.execute(
            sa.text(
                """
                SELECT consumed_acu
                FROM budget_dimension_account
                WHERE dimension_type = :dimension_type
                  AND dimension_key = :dimension_key
                """
            ),
            {"dimension_type": dimension_type, "dimension_key": dimension_key},
        ).scalar_one()


def _audit_event_count(engine: sa.Engine, event_type: str) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(
                sa.text(
                    """
                    SELECT count(*)
                    FROM accounting_audit_event
                    WHERE event_type = :event_type
                    """
                ),
                {"event_type": event_type},
            ).scalar_one()
        )


def _liability_state_count(engine: sa.Engine, state: str) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(
                sa.text("SELECT count(*) FROM settlement_liability WHERE state = :state"),
                {"state": state},
            ).scalar_one()
        )


def _table_count(engine: sa.Engine, table_name: str) -> int:
    with engine.connect() as connection:
        return int(connection.execute(sa.text(f"SELECT count(*) FROM {table_name}")).scalar_one())

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import sqlalchemy as sa

import alice_acp.abrs.service as abrs_service
from alice_acp.abrs import ReservationRequest
from alice_acp.mttd.types import (
    FOUNDATION_INTERNAL_BUDGET,
    R1_LOW_RISK_SEED,
    R5_SEEDED_ADVERSARIAL,
    SeedCase,
    SeedExecutionReport,
)
from alice_acp.test_harness.shadow_ledger import seed_budget_accounts

DEFAULT_EXPIRES_AT = datetime(2026, 5, 23, 16, 0, tzinfo=UTC)
DEFAULT_ACTOR = "alice_acp.mttd.execution_harness"


def seed_case_to_reservation_request(seed: SeedCase) -> ReservationRequest:
    if seed.route_source_class not in {R5_SEEDED_ADVERSARIAL, R1_LOW_RISK_SEED}:
        raise ValueError("seed route_source_class must be R5 or explicitly allowed R1")
    return ReservationRequest(
        admission_id=f"{seed.seed_id}-admission",
        attempt_id=f"{seed.seed_id}-attempt",
        route_contract_id=f"{seed.route_source_class}-route",
        epoch_id=f"{seed.seed_id}-foundation-epoch",
        source_budget_id=f"{seed.seed_id}-foundation-source-budget",
        mode_budget_id=f"{seed.seed_id}-seed-mode-budget",
        passport_id=f"{seed.seed_id}-passport",
        wallet_id=f"{seed.seed_id}-wallet",
        host_id=f"{seed.seed_id}-host",
        accelerator_id=f"{seed.seed_id}-accelerator",
        max_rewardable_acu=Decimal("1"),
        formula_version="formula-v1",
        tranche_policy_version="tranche-v1",
        reservation_expires_at=DEFAULT_EXPIRES_AT,
        cluster_status="none",
        reward_bearing=True,
    )


def run_seeded_mttd_reservation(
    engine: sa.Engine,
    seed: SeedCase,
    *,
    server_secret: bytes,
) -> SeedExecutionReport:
    request = seed_case_to_reservation_request(seed)
    seed_budget_accounts(
        engine,
        request,
        limit_acu=Decimal("10"),
        bucket_namespace=FOUNDATION_INTERNAL_BUDGET,
    )
    result = abrs_service.reserve_reward_budget(
        engine,
        request,
        server_secret=server_secret,
        actor_service=DEFAULT_ACTOR,
        retry_base_sleep_seconds=0,
        retry_jitter_seconds=0,
    )
    return SeedExecutionReport(
        seed_id=seed.seed_id,
        seed_class=seed.seed_class,
        reservation_id=result.reservation_id,
        accepted=result.accepted,
        budget_namespace=_budget_namespace(engine, request.epoch_id),
        route_source_class=seed.route_source_class,
        public_miner_bucket_debits=_public_miner_bucket_debits(engine, request.epoch_id),
        dual_blind_preserved=seed.verifier_visible_evidence_ref != seed.ground_truth_ref,
        reason_code=result.reason_code,
    )


def _budget_namespace(engine: sa.Engine, epoch_id: str) -> str:
    with engine.connect() as connection:
        return str(
            connection.execute(
                sa.text("SELECT bucket_namespace FROM budget_epoch WHERE epoch_id = :epoch_id"),
                {"epoch_id": epoch_id},
            ).scalar_one()
        )


def _public_miner_bucket_debits(engine: sa.Engine, epoch_id: str) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(
                sa.text(
                    """
                    SELECT count(*)
                    FROM budget_dimension_account account
                    JOIN budget_epoch epoch
                      ON epoch.epoch_id = account.epoch_id
                    WHERE account.epoch_id = :epoch_id
                      AND epoch.bucket_namespace = 'PUBLIC_MINER_BUCKET'
                      AND account.reserved_acu > 0
                    """
                ),
                {"epoch_id": epoch_id},
            ).scalar_one()
        )

from __future__ import annotations

from decimal import Decimal

import sqlalchemy as sa

import alice_acp.abrs.service as abrs_service
from alice_acp.abrs import ReservationRequest, ReservationResult, build_required_dimensions
from alice_acp.test_harness.reports import FailoverReport, FailoverScenarioReport
from alice_acp.test_harness.shadow_ledger import make_reservation_request, seed_budget_accounts


class SimulatedCrashBeforeCommit(RuntimeError):
    pass


class SimulatedResponseLossAfterCommit(RuntimeError):
    pass


def run_failover_drill(
    engine: sa.Engine,
    *,
    server_secret: bytes,
    run_id: str = "failover-a7",
) -> FailoverReport:
    return FailoverReport(
        scenarios=(
            simulate_crash_before_commit(
                engine,
                server_secret=server_secret,
                run_id=f"{run_id}-before",
            ),
            simulate_response_loss_after_commit(
                engine,
                server_secret=server_secret,
                run_id=f"{run_id}-after",
            ),
        )
    )


def simulate_crash_before_commit(
    engine: sa.Engine,
    *,
    server_secret: bytes,
    run_id: str = "failover-before-commit",
) -> FailoverScenarioReport:
    request = make_reservation_request(run_id, max_rewardable_acu=Decimal("10"))
    seed_budget_accounts(engine, request)

    def crash(_connection: sa.Connection) -> None:
        raise SimulatedCrashBeforeCommit("simulated crash before commit")

    crashed = False
    try:
        abrs_service.reserve_reward_budget(
            engine,
            request,
            server_secret=server_secret,
            retry_base_sleep_seconds=0,
            retry_jitter_seconds=0,
            before_commit_hook=crash,
        )
    except SimulatedCrashBeforeCommit:
        crashed = True

    reservation_count = _reservation_count(engine, request)
    debit_count = _dimension_debit_count(engine, request)
    invariant_violations = reservation_count + debit_count
    return FailoverScenarioReport(
        scenario="crash_before_commit",
        passed=crashed and invariant_violations == 0,
        duplicate_reservation_count=_duplicate_reservation_count(engine, request),
        lost_reservation_count=reservation_count,
        invariant_violations=invariant_violations,
        details={
            "crash_observed": crashed,
            "reservation_count": reservation_count,
            "dimension_debit_count": debit_count,
        },
    )


def simulate_response_loss_after_commit(
    engine: sa.Engine,
    *,
    server_secret: bytes,
    run_id: str = "failover-after-commit",
) -> FailoverScenarioReport:
    request = make_reservation_request(run_id, max_rewardable_acu=Decimal("10"))
    dimensions = seed_budget_accounts(engine, request)

    def lose_response(_result: ReservationResult) -> None:
        raise SimulatedResponseLossAfterCommit("simulated response loss after commit")

    response_lost = False
    try:
        abrs_service.reserve_reward_budget(
            engine,
            request,
            server_secret=server_secret,
            retry_base_sleep_seconds=0,
            retry_jitter_seconds=0,
            after_commit_hook=lose_response,
        )
    except SimulatedResponseLossAfterCommit:
        response_lost = True

    retry = abrs_service.reserve_reward_budget(
        engine,
        request,
        server_secret=server_secret,
        retry_base_sleep_seconds=0,
        retry_jitter_seconds=0,
    )
    reservation_count = _reservation_count(engine, request)
    entry_count = _dimension_entry_count(engine, request)
    duplicate_count = _duplicate_reservation_count(engine, request)
    lost_count = 0 if reservation_count == 1 and entry_count == len(dimensions) else 1
    invariant_violations = duplicate_count + lost_count
    return FailoverScenarioReport(
        scenario="response_loss_after_commit",
        passed=(
            response_lost
            and retry.accepted
            and retry.idempotent_replay
            and invariant_violations == 0
        ),
        duplicate_reservation_count=duplicate_count,
        lost_reservation_count=lost_count,
        invariant_violations=invariant_violations,
        details={
            "response_loss_observed": response_lost,
            "retry_accepted": retry.accepted,
            "retry_idempotent_replay": retry.idempotent_replay,
            "reservation_count": reservation_count,
            "dimension_entry_count": entry_count,
        },
    )


def _reservation_count(engine: sa.Engine, request: ReservationRequest) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(
                sa.text(
                    """
                    SELECT count(*)
                    FROM abrs_reservation
                    WHERE admission_id = :admission_id
                      AND attempt_id = :attempt_id
                      AND route_contract_id = :route_contract_id
                    """
                ),
                {
                    "admission_id": request.admission_id,
                    "attempt_id": request.attempt_id,
                    "route_contract_id": request.route_contract_id,
                },
            ).scalar_one()
        )


def _dimension_debit_count(engine: sa.Engine, request: ReservationRequest) -> int:
    dimensions = build_required_dimensions(request)
    with engine.connect() as connection:
        rows = connection.execute(
            sa.text(
                """
                SELECT dimension_type, dimension_key, reserved_acu
                FROM budget_dimension_account
                WHERE epoch_id = :epoch_id
                """
            ),
            {"epoch_id": request.epoch_id},
        ).mappings()
        reserved_by_dimension = {
            (row["dimension_type"], row["dimension_key"]): row["reserved_acu"] for row in rows
        }
    return sum(
        1
        for dimension in dimensions
        if reserved_by_dimension[(dimension.dimension_type, dimension.dimension_key)] != 0
    )


def _dimension_entry_count(engine: sa.Engine, request: ReservationRequest) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(
                sa.text(
                    """
                    SELECT count(*)
                    FROM abrs_reservation_dimension_entry entry
                    JOIN abrs_reservation reservation
                      ON reservation.reservation_id = entry.reservation_id
                    WHERE reservation.admission_id = :admission_id
                      AND reservation.attempt_id = :attempt_id
                      AND reservation.route_contract_id = :route_contract_id
                    """
                ),
                {
                    "admission_id": request.admission_id,
                    "attempt_id": request.attempt_id,
                    "route_contract_id": request.route_contract_id,
                },
            ).scalar_one()
        )


def _duplicate_reservation_count(engine: sa.Engine, request: ReservationRequest) -> int:
    with engine.connect() as connection:
        return int(
            connection.execute(
                sa.text(
                    """
                    SELECT count(*)
                    FROM (
                        SELECT idempotency_key
                        FROM abrs_reservation
                        WHERE epoch_id = :epoch_id
                        GROUP BY idempotency_key
                        HAVING count(*) > 1
                    ) duplicate_keys
                    """
                ),
                {"epoch_id": request.epoch_id},
            ).scalar_one()
        )

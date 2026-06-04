from __future__ import annotations

import random
import time
import uuid
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, IntegrityError

from alice_acp.abrs.dimensions import build_required_dimensions
from alice_acp.abrs.idempotency import reservation_idempotency_key
from alice_acp.abrs.types import (
    DIMENSION_CAP_EXCEEDED,
    IDEMPOTENCY_PAYLOAD_MISMATCH,
    INVALID_CLUSTER_DIMENSION,
    MISSING_DIMENSION_ACCOUNT,
    SERIALIZATION_RETRY_EXHAUSTED,
    ABRSReject,
    ReservationDimension,
    ReservationRequest,
    ReservationResult,
)
from alice_acp.audit import insert_audit_event, payload_digest

_RETRYABLE_SQLSTATES = frozenset({"40001", "40P01"})
_IDEMPOTENCY_RACE_CONSTRAINTS = frozenset(
    {
        "uq_abrs_reservation_idempotency_key",
        "uq_abrs_reservation_admission_attempt_route",
    }
)


def reserve_reward_budget(
    engine: sa.Engine,
    request: ReservationRequest,
    *,
    server_secret: bytes,
    actor_service: str = "alice_acp.abrs",
    max_serialization_retries: int = 5,
    retry_base_sleep_seconds: float = 0.005,
    retry_jitter_seconds: float = 0.005,
    before_commit_hook: Callable[[sa.Connection], None] | None = None,
    after_commit_hook: Callable[[ReservationResult], None] | None = None,
) -> ReservationResult:
    if max_serialization_retries < 0:
        raise ValueError("max_serialization_retries must be non-negative")

    attempts = 0
    while True:
        try:
            result = _reserve_reward_budget_once(
                engine,
                request,
                server_secret=server_secret,
                actor_service=actor_service,
                before_commit_hook=before_commit_hook,
            )
        except DBAPIError as exc:
            if not _is_retryable_concurrency_error(exc):
                raise
            if attempts >= max_serialization_retries:
                return ReservationResult(
                    status="rejected",
                    reason_code=SERIALIZATION_RETRY_EXHAUSTED,
                    retryable=True,
                    retry_attempts=attempts,
                )
            attempts += 1
            _sleep_before_retry(
                attempts,
                base_seconds=retry_base_sleep_seconds,
                jitter_seconds=retry_jitter_seconds,
            )
            continue

        result = ReservationResult(
            status=result.status,
            reservation_id=result.reservation_id,
            reason_code=result.reason_code,
            idempotent_replay=result.idempotent_replay,
            failed_dimension=result.failed_dimension,
            retryable=result.retryable,
            retry_attempts=attempts,
        )
        if after_commit_hook is not None:
            after_commit_hook(result)
        return result


def _reserve_reward_budget_once(
    engine: sa.Engine,
    request: ReservationRequest,
    *,
    server_secret: bytes,
    actor_service: str = "alice_acp.abrs",
    before_commit_hook: Callable[[sa.Connection], None] | None = None,
) -> ReservationResult:
    idempotency_key = reservation_idempotency_key(server_secret, request)
    idempotency_key_hash = payload_digest(idempotency_key)

    try:
        dimensions = build_required_dimensions(request)
    except ABRSReject as exc:
        return _audit_validation_rejection(
            engine,
            request,
            server_secret=server_secret,
            actor_service=actor_service,
            idempotency_key_hash=idempotency_key_hash,
            reason_code=exc.reason_code,
            failed_dimension=exc.failed_dimension,
        )

    with engine.connect().execution_options(isolation_level="SERIALIZABLE") as connection:
        with connection.begin():
            existing = _get_existing_reservation(connection, idempotency_key)
            if existing is not None:
                if _payload_matches_existing(connection, existing, request, dimensions):
                    insert_audit_event(
                        connection,
                        event_type="abrs_idempotent_replay",
                        entity_type="abrs_reservation",
                        entity_id=str(existing["reservation_id"]),
                        payload={
                            "reservation_id": existing["reservation_id"],
                            "admission_id": request.admission_id,
                            "attempt_id": request.attempt_id,
                            "route_contract_id": request.route_contract_id,
                            "idempotency_key_hash": idempotency_key_hash,
                        },
                        secret=server_secret,
                        actor_service=actor_service,
                    )
                    return ReservationResult(
                        status="active_reserved",
                        reservation_id=str(existing["reservation_id"]),
                        idempotent_replay=True,
                    )

                insert_audit_event(
                    connection,
                    event_type="abrs_idempotency_payload_mismatch",
                    entity_type="abrs_reservation",
                    entity_id=str(existing["reservation_id"]),
                    payload={
                        "admission_id": request.admission_id,
                        "attempt_id": request.attempt_id,
                        "route_contract_id": request.route_contract_id,
                        "idempotency_key_hash": idempotency_key_hash,
                        "reason_code": IDEMPOTENCY_PAYLOAD_MISMATCH,
                    },
                    secret=server_secret,
                    actor_service=actor_service,
                )
                return ReservationResult(
                    status="rejected",
                    reason_code=IDEMPOTENCY_PAYLOAD_MISMATCH,
                )

            accounts_by_dimension = _load_accounts_for_update(connection, request, dimensions)
            missing_dimension = _first_missing_dimension(dimensions, accounts_by_dimension)
            if missing_dimension is not None:
                _audit_failure(
                    connection,
                    request,
                    event_type="abrs_reservation_rejected_missing_dimension",
                    reason_code=MISSING_DIMENSION_ACCOUNT,
                    failed_dimension=missing_dimension,
                    idempotency_key_hash=idempotency_key_hash,
                    secret=server_secret,
                    actor_service=actor_service,
                )
                return ReservationResult(
                    status="rejected",
                    reason_code=MISSING_DIMENSION_ACCOUNT,
                    failed_dimension=missing_dimension,
                )

            for dimension in dimensions:
                account = accounts_by_dimension[dimension]
                projected = (
                    account["reserved_acu"]
                    + account["consumed_acu"]
                    + account["under_review_acu"]
                    + request.max_rewardable_acu
                )
                if projected > account["limit_acu"]:
                    _audit_failure(
                        connection,
                        request,
                        event_type="abrs_reservation_rejected_cap",
                        reason_code=DIMENSION_CAP_EXCEEDED,
                        failed_dimension=dimension,
                        account_id=str(account["account_id"]),
                        idempotency_key_hash=idempotency_key_hash,
                        secret=server_secret,
                        actor_service=actor_service,
                    )
                    return ReservationResult(
                        status="rejected",
                        reason_code=DIMENSION_CAP_EXCEEDED,
                        failed_dimension=dimension,
                    )

            reservation_id = f"resv_{uuid.uuid4().hex}"
            _insert_reservation(connection, request, reservation_id, idempotency_key)
            for dimension in dimensions:
                account = accounts_by_dimension[dimension]
                _insert_dimension_entry(connection, request, reservation_id, account)
                _increment_reserved_acu(
                    connection,
                    account_id=str(account["account_id"]),
                    amount=request.max_rewardable_acu,
                )

            insert_audit_event(
                connection,
                event_type="abrs_reservation_created",
                entity_type="abrs_reservation",
                entity_id=reservation_id,
                payload={
                    "reservation_id": reservation_id,
                    "admission_id": request.admission_id,
                    "attempt_id": request.attempt_id,
                    "route_contract_id": request.route_contract_id,
                    "idempotency_key_hash": idempotency_key_hash,
                    "reserved_acu": request.max_rewardable_acu,
                    "dimensions": [_dimension_payload(dimension) for dimension in dimensions],
                },
                secret=server_secret,
                actor_service=actor_service,
            )

            if before_commit_hook is not None:
                before_commit_hook(connection)

            return ReservationResult(status="active_reserved", reservation_id=reservation_id)


def _is_retryable_concurrency_error(error: DBAPIError) -> bool:
    sqlstate = getattr(error.orig, "sqlstate", None)
    if sqlstate in _RETRYABLE_SQLSTATES:
        return True

    if not isinstance(error, IntegrityError):
        return False

    diagnostics = getattr(error.orig, "diag", None)
    constraint_name = getattr(diagnostics, "constraint_name", None)
    return constraint_name in _IDEMPOTENCY_RACE_CONSTRAINTS


def _sleep_before_retry(
    attempt: int,
    *,
    base_seconds: float,
    jitter_seconds: float,
) -> None:
    if base_seconds < 0 or jitter_seconds < 0:
        raise ValueError("retry sleep values must be non-negative")
    delay = base_seconds * attempt
    if jitter_seconds:
        delay += random.uniform(0, jitter_seconds)
    if delay:
        time.sleep(delay)


def _audit_validation_rejection(
    engine: sa.Engine,
    request: ReservationRequest,
    *,
    server_secret: bytes,
    actor_service: str,
    idempotency_key_hash: str,
    reason_code: str,
    failed_dimension: ReservationDimension | None,
) -> ReservationResult:
    with engine.connect().execution_options(isolation_level="SERIALIZABLE") as connection:
        with connection.begin():
            _audit_failure(
                connection,
                request,
                event_type="abrs_reservation_rejected_validation",
                reason_code=reason_code,
                failed_dimension=failed_dimension,
                idempotency_key_hash=idempotency_key_hash,
                secret=server_secret,
                actor_service=actor_service,
            )
    return ReservationResult(
        status="rejected",
        reason_code=reason_code or INVALID_CLUSTER_DIMENSION,
        failed_dimension=failed_dimension,
    )


def _get_existing_reservation(
    connection: sa.Connection,
    idempotency_key: str,
) -> sa.RowMapping | None:
    return connection.execute(
        sa.text(
            """
            SELECT *
            FROM abrs_reservation
            WHERE idempotency_key = :idempotency_key
            FOR UPDATE
            """
        ),
        {"idempotency_key": idempotency_key},
    ).mappings().one_or_none()


def _payload_matches_existing(
    connection: sa.Connection,
    existing: sa.RowMapping,
    request: ReservationRequest,
    dimensions: tuple[ReservationDimension, ...],
) -> bool:
    scalar_fields_match = (
        existing["admission_id"] == request.admission_id
        and existing["attempt_id"] == request.attempt_id
        and existing["route_contract_id"] == request.route_contract_id
        and existing["formula_version"] == request.formula_version
        and existing["tranche_policy_version"] == request.tranche_policy_version
        and existing["epoch_id"] == request.epoch_id
        and existing["reward_bearing"] == request.reward_bearing
        and existing["max_rewardable_acu"] == request.max_rewardable_acu
        and existing["reserved_acu"] == request.max_rewardable_acu
        and existing["expires_at"] == request.reservation_expires_at
    )
    if not scalar_fields_match:
        return False

    rows = connection.execute(
        sa.text(
            """
            SELECT dimension_type, dimension_key, reserved_acu
            FROM abrs_reservation_dimension_entry
            WHERE reservation_id = :reservation_id
            ORDER BY dimension_type, dimension_key
            """
        ),
        {"reservation_id": existing["reservation_id"]},
    ).mappings()
    actual = {
        (row["dimension_type"], row["dimension_key"], row["reserved_acu"])
        for row in rows
    }
    expected = {
        (dimension.dimension_type, dimension.dimension_key, request.max_rewardable_acu)
        for dimension in dimensions
    }
    return actual == expected


def _load_accounts_for_update(
    connection: sa.Connection,
    request: ReservationRequest,
    dimensions: tuple[ReservationDimension, ...],
) -> dict[ReservationDimension, sa.RowMapping]:
    accounts: dict[ReservationDimension, sa.RowMapping] = {}
    for dimension in sorted(dimensions, key=lambda item: (item.dimension_type, item.dimension_key)):
        row = connection.execute(
            sa.text(
                """
                SELECT *
                FROM budget_dimension_account
                WHERE epoch_id = :epoch_id
                  AND dimension_type = :dimension_type
                  AND dimension_key = :dimension_key
                FOR UPDATE
                """
            ),
            {
                "epoch_id": request.epoch_id,
                "dimension_type": dimension.dimension_type,
                "dimension_key": dimension.dimension_key,
            },
        ).mappings().one_or_none()
        if row is not None:
            accounts[dimension] = row
    return accounts


def _first_missing_dimension(
    dimensions: tuple[ReservationDimension, ...],
    accounts_by_dimension: dict[ReservationDimension, sa.RowMapping],
) -> ReservationDimension | None:
    for dimension in dimensions:
        if dimension not in accounts_by_dimension:
            return dimension
    return None


def _insert_reservation(
    connection: sa.Connection,
    request: ReservationRequest,
    reservation_id: str,
    idempotency_key: str,
) -> None:
    connection.execute(
        sa.text(
            """
            INSERT INTO abrs_reservation (
                reservation_id,
                idempotency_key,
                admission_id,
                attempt_id,
                route_contract_id,
                formula_version,
                tranche_policy_version,
                epoch_id,
                reward_bearing,
                max_rewardable_acu,
                reserved_acu,
                consumed_acu,
                unused_reserved_acu,
                status,
                expires_at,
                created_at,
                updated_at
            )
            VALUES (
                :reservation_id,
                :idempotency_key,
                :admission_id,
                :attempt_id,
                :route_contract_id,
                :formula_version,
                :tranche_policy_version,
                :epoch_id,
                :reward_bearing,
                :max_rewardable_acu,
                :reserved_acu,
                0,
                0,
                'active_reserved',
                :expires_at,
                now(),
                now()
            )
            """
        ),
        {
            "reservation_id": reservation_id,
            "idempotency_key": idempotency_key,
            "admission_id": request.admission_id,
            "attempt_id": request.attempt_id,
            "route_contract_id": request.route_contract_id,
            "formula_version": request.formula_version,
            "tranche_policy_version": request.tranche_policy_version,
            "epoch_id": request.epoch_id,
            "reward_bearing": request.reward_bearing,
            "max_rewardable_acu": request.max_rewardable_acu,
            "reserved_acu": request.max_rewardable_acu,
            "expires_at": request.reservation_expires_at,
        },
    )


def _insert_dimension_entry(
    connection: sa.Connection,
    request: ReservationRequest,
    reservation_id: str,
    account: sa.RowMapping,
) -> None:
    connection.execute(
        sa.text(
            """
            INSERT INTO abrs_reservation_dimension_entry (
                reservation_id,
                account_id,
                dimension_type,
                dimension_key,
                reserved_acu,
                consumed_acu,
                released_acu,
                status,
                created_at,
                updated_at
            )
            VALUES (
                :reservation_id,
                :account_id,
                :dimension_type,
                :dimension_key,
                :reserved_acu,
                0,
                0,
                'active_reserved',
                now(),
                now()
            )
            """
        ),
        {
            "reservation_id": reservation_id,
            "account_id": account["account_id"],
            "dimension_type": account["dimension_type"],
            "dimension_key": account["dimension_key"],
            "reserved_acu": request.max_rewardable_acu,
        },
    )


def _increment_reserved_acu(
    connection: sa.Connection,
    *,
    account_id: str,
    amount: Decimal,
) -> None:
    connection.execute(
        sa.text(
            """
            UPDATE budget_dimension_account
            SET reserved_acu = reserved_acu + :amount,
                updated_at = now()
            WHERE account_id = :account_id
            """
        ),
        {"account_id": account_id, "amount": amount},
    )


def _audit_failure(
    connection: sa.Connection,
    request: ReservationRequest,
    *,
    event_type: str,
    reason_code: str,
    failed_dimension: ReservationDimension | None,
    idempotency_key_hash: str,
    secret: bytes,
    actor_service: str,
    account_id: str | None = None,
) -> None:
    payload: dict[str, Any] = {
        "admission_id": request.admission_id,
        "attempt_id": request.attempt_id,
        "route_contract_id": request.route_contract_id,
        "reason_code": reason_code,
        "idempotency_key_hash": idempotency_key_hash,
    }
    if failed_dimension is not None:
        payload["failed_dimension"] = _dimension_payload(failed_dimension)
    if account_id is not None:
        payload["account_id"] = account_id

    insert_audit_event(
        connection,
        event_type=event_type,
        entity_type="abrs_reservation",
        entity_id=request.admission_id,
        payload=payload,
        secret=secret,
        actor_service=actor_service,
    )


def _dimension_payload(dimension: ReservationDimension) -> dict[str, str]:
    return {
        "dimension_type": dimension.dimension_type,
        "dimension_key": dimension.dimension_key,
    }

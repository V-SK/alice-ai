from __future__ import annotations

import sqlalchemy as sa

from alice_acp.wac.contracts import validate_attempt_route_binding
from alice_acp.wac.types import WACAdmissionAttempt, WACContractResult

DEFAULT_ACTOR = "alice_acp.wac.registry"


def validate_and_record_attempt_route(
    engine: sa.Engine,
    attempt: WACAdmissionAttempt,
    *,
    actor_service: str = DEFAULT_ACTOR,
) -> WACContractResult:
    with engine.connect().execution_options(isolation_level="SERIALIZABLE") as connection:
        with connection.begin():
            connection.execute(
                sa.text(
                    """
                    INSERT INTO wac_attempt_route_registry (
                        admission_id,
                        attempt_id,
                        route_contract_id,
                        actor_service,
                        created_at,
                        updated_at
                    )
                    VALUES (
                        :admission_id,
                        :attempt_id,
                        :route_contract_id,
                        :actor_service,
                        now(),
                        now()
                    )
                    ON CONFLICT (admission_id, attempt_id) DO NOTHING
                    """
                ),
                {
                    "admission_id": attempt.admission_id,
                    "attempt_id": attempt.attempt_id,
                    "route_contract_id": attempt.route_contract_id,
                    "actor_service": actor_service,
                },
            )
            route_contract_id = connection.execute(
                sa.text(
                    """
                    SELECT route_contract_id
                    FROM wac_attempt_route_registry
                    WHERE admission_id = :admission_id
                      AND attempt_id = :attempt_id
                    FOR UPDATE
                    """
                ),
                {
                    "admission_id": attempt.admission_id,
                    "attempt_id": attempt.attempt_id,
                },
            ).scalar_one()
            result = validate_attempt_route_binding(
                attempt,
                existing_route_contract_id=str(route_contract_id),
            )
            if result.accepted:
                connection.execute(
                    sa.text(
                        """
                        UPDATE wac_attempt_route_registry
                        SET updated_at = now(),
                            actor_service = :actor_service
                        WHERE admission_id = :admission_id
                          AND attempt_id = :attempt_id
                        """
                    ),
                    {
                        "admission_id": attempt.admission_id,
                        "attempt_id": attempt.attempt_id,
                        "actor_service": actor_service,
                    },
                )
            return result


def load_attempt_route_binding(
    engine: sa.Engine,
    *,
    admission_id: str,
    attempt_id: str,
) -> str | None:
    with engine.connect() as connection:
        return connection.execute(
            sa.text(
                """
                SELECT route_contract_id
                FROM wac_attempt_route_registry
                WHERE admission_id = :admission_id
                  AND attempt_id = :attempt_id
                """
            ),
            {"admission_id": admission_id, "attempt_id": attempt_id},
        ).scalar_one_or_none()

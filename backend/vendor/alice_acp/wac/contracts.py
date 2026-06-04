from __future__ import annotations

from alice_acp.abrs import ReservationRequest
from alice_acp.wac.types import (
    ROUTE_REASSIGNMENT_REQUIRES_NEW_ATTEMPT,
    WACAdmissionAttempt,
    WACContractResult,
)

AttemptRouteKey = tuple[str, str]


def validate_attempt_route_binding(
    attempt: WACAdmissionAttempt,
    existing_route_contract_id: str | None,
) -> WACContractResult:
    if existing_route_contract_id is not None and (
        existing_route_contract_id != attempt.route_contract_id
    ):
        return WACContractResult(
            status="rejected",
            admission_id=attempt.admission_id,
            attempt_id=attempt.attempt_id,
            route_contract_id=attempt.route_contract_id,
            reason_code=ROUTE_REASSIGNMENT_REQUIRES_NEW_ATTEMPT,
        )
    return WACContractResult(
        status="accepted",
        admission_id=attempt.admission_id,
        attempt_id=attempt.attempt_id,
        route_contract_id=attempt.route_contract_id,
    )


class AttemptRouteRegistry:
    """DB-free helper for local contract tests and harnesses."""

    def __init__(self) -> None:
        self._route_by_attempt: dict[AttemptRouteKey, str] = {}

    def validate_and_record(self, attempt: WACAdmissionAttempt) -> WACContractResult:
        key = (attempt.admission_id, attempt.attempt_id)
        result = validate_attempt_route_binding(
            attempt,
            self._route_by_attempt.get(key),
        )
        if result.accepted:
            self._route_by_attempt[key] = attempt.route_contract_id
        return result


def to_reservation_request(attempt: WACAdmissionAttempt) -> ReservationRequest:
    return ReservationRequest(
        admission_id=attempt.admission_id,
        attempt_id=attempt.attempt_id,
        route_contract_id=attempt.route_contract_id,
        epoch_id=attempt.epoch_id,
        source_budget_id=attempt.source_budget_id,
        mode_budget_id=attempt.mode_budget_id,
        passport_id=attempt.passport_id,
        wallet_id=attempt.wallet_id,
        host_id=attempt.host_id,
        accelerator_id=attempt.accelerator_id,
        max_rewardable_acu=attempt.max_rewardable_acu,
        formula_version=attempt.formula_version,
        tranche_policy_version=attempt.tranche_policy_version,
        reservation_expires_at=attempt.reservation_expires_at,
        cluster_status=attempt.cluster_status,
        cluster_id=attempt.cluster_id,
        passport_velocity_window=attempt.passport_velocity_window,
        reward_bearing=attempt.reward_bearing,
    )

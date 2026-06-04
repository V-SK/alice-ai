from __future__ import annotations

from alice_acp.abrs.types import (
    INVALID_CLUSTER_DIMENSION,
    ABRSReject,
    ReservationDimension,
    ReservationRequest,
)

CLUSTER_DIMENSION_BY_STATUS = {
    "candidate_cluster_high": "candidate_cluster_exposure",
    "confirmed_cluster_no_fraud": "confirmed_cluster_exposure",
    "confirmed_fraud_cluster": "confirmed_cluster_exposure",
}
SENTINEL_CLUSTER_IDS = {"none", "null", "unknown", "sentinel", "__none__", "__null__"}


def build_required_dimensions(request: ReservationRequest) -> tuple[ReservationDimension, ...]:
    dimensions = [
        ReservationDimension("source_budget", request.source_budget_id),
        ReservationDimension("mode_budget", request.mode_budget_id),
        ReservationDimension("passport_pending_exposure", request.passport_id),
        ReservationDimension(
            f"passport_velocity_{request.passport_velocity_window}",
            request.passport_id,
        ),
        ReservationDimension("wallet_exposure", request.wallet_id),
        ReservationDimension("host_exposure", request.host_id),
        ReservationDimension("accelerator_exposure", request.accelerator_id),
    ]

    if request.cluster_status != "none":
        cluster_id = request.cluster_id
        if cluster_id is None or not cluster_id or cluster_id.lower() in SENTINEL_CLUSTER_IDS:
            raise ABRSReject(INVALID_CLUSTER_DIMENSION)
        dimensions.append(
            ReservationDimension(CLUSTER_DIMENSION_BY_STATUS[request.cluster_status], cluster_id)
        )

    if len(dimensions) != len(set(dimensions)):
        raise ABRSReject(INVALID_CLUSTER_DIMENSION)

    return tuple(dimensions)

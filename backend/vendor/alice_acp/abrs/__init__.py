"""Admission Budget Reservation Service namespace for Phase A."""

from alice_acp.abrs.dimensions import build_required_dimensions
from alice_acp.abrs.idempotency import reservation_idempotency_key
from alice_acp.abrs.service import reserve_reward_budget
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

__all__ = (
    "DIMENSION_CAP_EXCEEDED",
    "IDEMPOTENCY_PAYLOAD_MISMATCH",
    "INVALID_CLUSTER_DIMENSION",
    "MISSING_DIMENSION_ACCOUNT",
    "SERIALIZATION_RETRY_EXHAUSTED",
    "ABRSReject",
    "ReservationDimension",
    "ReservationRequest",
    "ReservationResult",
    "build_required_dimensions",
    "reservation_idempotency_key",
    "reserve_reward_budget",
)

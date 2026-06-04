"""WAC integration contracts for Phase B."""

from alice_acp.wac.contracts import (
    AttemptRouteRegistry,
    to_reservation_request,
    validate_attempt_route_binding,
)
from alice_acp.wac.registry import load_attempt_route_binding, validate_and_record_attempt_route
from alice_acp.wac.types import (
    ROUTE_REASSIGNMENT_REQUIRES_NEW_ATTEMPT,
    WACAdmissionAttempt,
    WACContractResult,
)

__all__ = (
    "ROUTE_REASSIGNMENT_REQUIRES_NEW_ATTEMPT",
    "AttemptRouteRegistry",
    "WACAdmissionAttempt",
    "WACContractResult",
    "load_attempt_route_binding",
    "to_reservation_request",
    "validate_and_record_attempt_route",
    "validate_attempt_route_binding",
)

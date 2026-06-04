from __future__ import annotations

import hashlib
import hmac

from alice_acp.abrs.types import ReservationRequest


def reservation_idempotency_key(server_secret: bytes, request: ReservationRequest) -> str:
    if not server_secret:
        raise ValueError("server_secret must be non-empty")
    material = f"{request.admission_id}{request.attempt_id}{request.route_contract_id}"
    return hmac.new(server_secret, material.encode("utf-8"), hashlib.sha256).hexdigest()

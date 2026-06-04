from __future__ import annotations

import hashlib
import hmac
import json
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa


def audit_event_id() -> str:
    return f"audit_{uuid.uuid4().hex}"


def payload_digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def canonical_json(payload: dict[str, Any]) -> str:
    return json.dumps(payload, default=_json_default, separators=(",", ":"), sort_keys=True)


def audit_payload_hmac(secret: bytes, payload: dict[str, Any]) -> str:
    if not secret:
        raise ValueError("audit HMAC secret must be non-empty")
    return hmac.new(secret, canonical_json(payload).encode("utf-8"), hashlib.sha256).hexdigest()


def insert_audit_event(
    connection: sa.Connection,
    *,
    event_type: str,
    entity_type: str,
    entity_id: str,
    payload: dict[str, Any],
    secret: bytes,
    actor_service: str = "alice_acp.abrs",
) -> str:
    event_id = audit_event_id()
    connection.execute(
        sa.text(
            """
            INSERT INTO accounting_audit_event (
                event_id,
                entity_type,
                entity_id,
                event_type,
                payload,
                payload_hmac,
                actor_service,
                created_at
            )
            VALUES (
                :event_id,
                :entity_type,
                :entity_id,
                :event_type,
                CAST(:payload AS jsonb),
                :payload_hmac,
                :actor_service,
                now()
            )
            """
        ),
        {
            "event_id": event_id,
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "payload": canonical_json(payload),
            "payload_hmac": audit_payload_hmac(secret, payload),
            "actor_service": actor_service,
        },
    )
    return event_id


def _json_default(value: Any) -> str:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    raise TypeError(f"cannot serialize {type(value).__name__}")

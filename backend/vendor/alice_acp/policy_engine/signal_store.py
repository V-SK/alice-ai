from __future__ import annotations

import json
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import sqlalchemy as sa

from alice_acp.audit import canonical_json
from alice_acp.policy_engine.risk_signals import EvidenceRef, RiskSignal

DEFAULT_ACTOR = "alice_acp.policy_engine.signal_store"
RISK_SIGNAL_PAYLOAD_MISMATCH = "RISK_SIGNAL_PAYLOAD_MISMATCH"


class RiskSignalPayloadMismatchError(ValueError):
    reason_code = RISK_SIGNAL_PAYLOAD_MISMATCH

    def __init__(self, signal_id: str) -> None:
        super().__init__(f"risk signal {signal_id} already exists with different payload")
        self.signal_id = signal_id


def append_risk_signal(
    engine: sa.Engine,
    signal: RiskSignal,
    *,
    actor_service: str = DEFAULT_ACTOR,
) -> bool:
    with engine.begin() as connection:
        params = _signal_params(signal, actor_service=actor_service)
        inserted = connection.execute(
            sa.text(
                """
                INSERT INTO risk_signal_event (
                    signal_id,
                    signal_type,
                    producer,
                    policy_version,
                    observed_at,
                    admission_id,
                    attempt_id,
                    route_contract_id,
                    reservation_id,
                    liability_id,
                    passport_id,
                    wallet_id,
                    host_id,
                    cluster_id,
                    confidence,
                    severity,
                    reason_code,
                    evidence_refs,
                    payload,
                    actor_service,
                    created_at
                )
                VALUES (
                    :signal_id,
                    :signal_type,
                    :producer,
                    :policy_version,
                    :observed_at,
                    :admission_id,
                    :attempt_id,
                    :route_contract_id,
                    :reservation_id,
                    :liability_id,
                    :passport_id,
                    :wallet_id,
                    :host_id,
                    :cluster_id,
                    :confidence,
                    :severity,
                    :reason_code,
                    CAST(:evidence_refs AS jsonb),
                    CAST(:payload AS jsonb),
                    :actor_service,
                    now()
                )
                ON CONFLICT (signal_id) DO NOTHING
                RETURNING signal_id
                """
            ),
            params,
        ).scalar_one_or_none()
        if inserted is not None:
            return True

        existing = connection.execute(
            sa.text(
                """
                SELECT *
                FROM risk_signal_event
                WHERE signal_id = :signal_id
                """
            ),
            {"signal_id": signal.signal_id},
        ).mappings().one()

        if _signal_fingerprint_from_row(existing) == _signal_fingerprint_from_params(params):
            return False

        raise RiskSignalPayloadMismatchError(signal.signal_id)


def load_risk_signals_for_attempt(
    engine: sa.Engine,
    *,
    admission_id: str,
    attempt_id: str,
    route_contract_id: str,
) -> tuple[RiskSignal, ...]:
    with engine.connect() as connection:
        rows = connection.execute(
            sa.text(
                """
                SELECT *
                FROM risk_signal_event
                WHERE admission_id = :admission_id
                  AND attempt_id = :attempt_id
                  AND route_contract_id = :route_contract_id
                ORDER BY observed_at, signal_id
                """
            ),
            {
                "admission_id": admission_id,
                "attempt_id": attempt_id,
                "route_contract_id": route_contract_id,
            },
        ).mappings()
        return tuple(_signal_from_row(row) for row in rows)


def _signal_params(signal: RiskSignal, *, actor_service: str) -> dict[str, Any]:
    return {
        "signal_id": signal.signal_id,
        "signal_type": signal.signal_type,
        "producer": signal.producer,
        "policy_version": signal.policy_version,
        "observed_at": signal.observed_at,
        "admission_id": signal.admission_id,
        "attempt_id": signal.attempt_id,
        "route_contract_id": signal.route_contract_id,
        "reservation_id": signal.reservation_id,
        "liability_id": signal.liability_id,
        "passport_id": signal.passport_id,
        "wallet_id": signal.wallet_id,
        "host_id": signal.host_id,
        "cluster_id": signal.cluster_id,
        "confidence": signal.confidence,
        "severity": signal.severity,
        "reason_code": signal.reason_code,
        "evidence_refs": canonical_json(
            {"evidence_refs": [asdict(ref) for ref in signal.evidence_refs]}
        ),
        "payload": canonical_json(signal.payload),
        "actor_service": actor_service,
    }


def _signal_from_row(row: sa.RowMapping) -> RiskSignal:
    evidence_payload = _json_value(row["evidence_refs"])
    payload = _json_value(row["payload"])
    return RiskSignal(
        signal_id=str(row["signal_id"]),
        signal_type=row["signal_type"],
        producer=str(row["producer"]),
        policy_version=str(row["policy_version"]),
        observed_at=row["observed_at"],
        admission_id=str(row["admission_id"]),
        attempt_id=str(row["attempt_id"]),
        route_contract_id=str(row["route_contract_id"]),
        reservation_id=row["reservation_id"],
        liability_id=row["liability_id"],
        passport_id=row["passport_id"],
        wallet_id=row["wallet_id"],
        host_id=row["host_id"],
        cluster_id=row["cluster_id"],
        confidence=Decimal(str(row["confidence"])),
        severity=row["severity"],
        reason_code=str(row["reason_code"]),
        evidence_refs=tuple(
            EvidenceRef(
                evidence_ref=str(item["evidence_ref"]),
                evidence_type=str(item["evidence_type"]),
                digest=str(item["digest"]),
                producer=str(item["producer"]),
                captured_at=datetime.fromisoformat(str(item["captured_at"])),
                retention_class=item.get("retention_class"),
                metadata=item.get("metadata", {}),
            )
            for item in evidence_payload.get("evidence_refs", [])
        ),
        payload=payload,
    )


def _signal_fingerprint_from_params(params: dict[str, Any]) -> str:
    return canonical_json(
        {
            "signal_type": params["signal_type"],
            "producer": params["producer"],
            "policy_version": params["policy_version"],
            "observed_at": _timestamp_key(params["observed_at"]),
            "admission_id": params["admission_id"],
            "attempt_id": params["attempt_id"],
            "route_contract_id": params["route_contract_id"],
            "reservation_id": params["reservation_id"],
            "liability_id": params["liability_id"],
            "passport_id": params["passport_id"],
            "wallet_id": params["wallet_id"],
            "host_id": params["host_id"],
            "cluster_id": params["cluster_id"],
            "confidence": _decimal_key(params["confidence"]),
            "severity": params["severity"],
            "reason_code": params["reason_code"],
            "evidence_refs": _json_value(params["evidence_refs"]),
            "payload": _json_value(params["payload"]),
        }
    )


def _signal_fingerprint_from_row(row: sa.RowMapping) -> str:
    return canonical_json(
        {
            "signal_type": row["signal_type"],
            "producer": row["producer"],
            "policy_version": row["policy_version"],
            "observed_at": _timestamp_key(row["observed_at"]),
            "admission_id": row["admission_id"],
            "attempt_id": row["attempt_id"],
            "route_contract_id": row["route_contract_id"],
            "reservation_id": row["reservation_id"],
            "liability_id": row["liability_id"],
            "passport_id": row["passport_id"],
            "wallet_id": row["wallet_id"],
            "host_id": row["host_id"],
            "cluster_id": row["cluster_id"],
            "confidence": _decimal_key(row["confidence"]),
            "severity": row["severity"],
            "reason_code": row["reason_code"],
            "evidence_refs": _json_value(row["evidence_refs"]),
            "payload": _json_value(row["payload"]),
        }
    )


def _decimal_key(value: Any) -> str:
    return str(Decimal(str(value)).normalize())


def _timestamp_key(value: Any) -> str:
    if isinstance(value, datetime):
        return value.astimezone(UTC).isoformat()
    return str(value)


def _json_value(value: Any) -> dict[str, Any]:
    if isinstance(value, dict):
        return value
    return json.loads(str(value))

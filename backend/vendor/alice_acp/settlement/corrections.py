from __future__ import annotations

import hashlib
import uuid
from decimal import Decimal
from typing import Any

import sqlalchemy as sa

from alice_acp.audit import canonical_json, insert_audit_event
from alice_acp.settlement.types import (
    BAD_LIABILITY_STATE,
    CORRECTION_AMOUNT_EXCEEDS_UNPAID_CREDIT,
    CORRECTION_INCIDENT_LOGGED,
    CORRECTION_MAKE_UP_PENDING,
    CORRECTION_REJECTED,
    CORRECTION_UNDER_REVIEW_MOVED,
    OVER_CREDIT_AFTER_PAID,
    OVER_CREDIT_BEFORE_PAID,
    UNDER_CREDIT_AFTER_PAID,
    UNDER_CREDIT_BEFORE_PAID,
    UNDER_REVIEW,
    SettlementCorrectionRequest,
    SettlementCorrectionResult,
    SettlementCorrectionStatus,
)

ACU_QUANT = Decimal("0.000000000001")
ZERO_ACU = Decimal("0")

CORRECTION_EVENT_BY_TYPE = {
    OVER_CREDIT_BEFORE_PAID: "settlement_correction_over_credit_before_paid",
    OVER_CREDIT_AFTER_PAID: "settlement_correction_over_credit_after_paid",
    UNDER_CREDIT_BEFORE_PAID: "settlement_correction_under_credit_before_paid",
    UNDER_CREDIT_AFTER_PAID: "settlement_correction_under_credit_after_paid",
}


def settlement_correction_idempotency_key(request: SettlementCorrectionRequest) -> str:
    payload = {
        "liability_id": request.liability_id,
        "correction_type": request.correction_type,
        "amount_acu": _acu_key_amount(request.amount_acu),
        "reason_code": request.reason_code,
        "evidence_refs_hash": _evidence_refs_hash(request.evidence_refs),
    }
    digest = hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
    return f"corr_idem_{digest}"


def apply_settlement_correction(
    engine: sa.Engine,
    request: SettlementCorrectionRequest,
    *,
    server_secret: bytes,
    actor_service: str = "alice_acp.settlement",
) -> SettlementCorrectionResult:
    idempotency_key = settlement_correction_idempotency_key(request)
    with engine.connect().execution_options(isolation_level="SERIALIZABLE") as connection:
        with connection.begin():
            existing = _load_existing_correction(connection, idempotency_key)
            if existing is not None:
                return _existing_correction_result(existing)

            liability = _load_liability_for_update(connection, request.liability_id)
            if liability is None:
                return SettlementCorrectionResult(
                    status="rejected",
                    idempotency_key=idempotency_key,
                    liability_id=request.liability_id,
                    correction_state=CORRECTION_REJECTED,
                    reason_code=BAD_LIABILITY_STATE,
                )

            if request.correction_type == OVER_CREDIT_BEFORE_PAID:
                return _apply_over_credit_before_paid(
                    connection,
                    request,
                    liability,
                    idempotency_key=idempotency_key,
                    server_secret=server_secret,
                    actor_service=actor_service,
                )
            if request.correction_type == OVER_CREDIT_AFTER_PAID:
                return _record_correction(
                    connection,
                    request,
                    liability,
                    idempotency_key=idempotency_key,
                    correction_state=CORRECTION_INCIDENT_LOGGED,
                    event_type=CORRECTION_EVENT_BY_TYPE[request.correction_type],
                    event_payload={"paid_acu_unchanged": liability["paid_acu"]},
                    server_secret=server_secret,
                    actor_service=actor_service,
                )
            if request.correction_type in {UNDER_CREDIT_BEFORE_PAID, UNDER_CREDIT_AFTER_PAID}:
                return _record_correction(
                    connection,
                    request,
                    liability,
                    idempotency_key=idempotency_key,
                    correction_state=CORRECTION_MAKE_UP_PENDING,
                    event_type=CORRECTION_EVENT_BY_TYPE[request.correction_type],
                    event_payload={
                        "make_up_pending_acu": request.amount_acu,
                        "paid_acu_unchanged": liability["paid_acu"],
                    },
                    server_secret=server_secret,
                    actor_service=actor_service,
                )

    raise AssertionError("unreachable correction type branch")


def _apply_over_credit_before_paid(
    connection: sa.Connection,
    request: SettlementCorrectionRequest,
    liability: sa.RowMapping,
    *,
    idempotency_key: str,
    server_secret: bytes,
    actor_service: str,
) -> SettlementCorrectionResult:
    unpaid_correctable_acu = _unpaid_correctable_acu(liability)
    if request.amount_acu > unpaid_correctable_acu:
        return _record_correction(
            connection,
            request,
            liability,
            idempotency_key=idempotency_key,
            correction_state=CORRECTION_REJECTED,
            event_type="settlement_correction_rejected",
            event_payload={
                "unpaid_correctable_acu": unpaid_correctable_acu,
                "paid_acu_unchanged": liability["paid_acu"],
            },
            server_secret=server_secret,
            actor_service=actor_service,
            reason_code=CORRECTION_AMOUNT_EXCEEDS_UNPAID_CREDIT,
            status="rejected",
        )

    movements = _over_credit_bucket_movements(liability, request.amount_acu)
    connection.execute(
        sa.text(
            """
            UPDATE settlement_liability
            SET base_release_acu = :base_release_acu,
                risk_reserve_acu = :risk_reserve_acu,
                under_review_acu = :under_review_acu,
                vested_payable_acu = :vested_payable_acu,
                state = :state,
                state_reason_code = :state_reason_code,
                updated_at = now()
            WHERE liability_id = :liability_id
            """
        ),
        {
            "base_release_acu": movements["base_release_acu"],
            "risk_reserve_acu": movements["risk_reserve_acu"],
            "under_review_acu": movements["under_review_acu"],
            "vested_payable_acu": movements["vested_payable_acu"],
            "state": UNDER_REVIEW,
            "state_reason_code": request.reason_code,
            "liability_id": request.liability_id,
        },
    )
    return _record_correction(
        connection,
        request,
        liability,
        idempotency_key=idempotency_key,
        correction_state=CORRECTION_UNDER_REVIEW_MOVED,
        event_type=CORRECTION_EVENT_BY_TYPE[request.correction_type],
        event_payload={
            "unpaid_correctable_acu": unpaid_correctable_acu,
            "already_under_review_acu": movements["already_under_review_acu"],
            "moved_to_under_review_acu": movements["moved_to_under_review_acu"],
            "base_release_debit_acu": movements["base_release_debit_acu"],
            "risk_reserve_debit_acu": movements["risk_reserve_debit_acu"],
            "vested_payable_debit_acu": movements["vested_payable_debit_acu"],
            "paid_acu_unchanged": liability["paid_acu"],
        },
        server_secret=server_secret,
        actor_service=actor_service,
    )


def _record_correction(
    connection: sa.Connection,
    request: SettlementCorrectionRequest,
    liability: sa.RowMapping,
    *,
    idempotency_key: str,
    correction_state: str,
    event_type: str,
    event_payload: dict[str, Any],
    server_secret: bytes,
    actor_service: str,
    reason_code: str | None = None,
    status: SettlementCorrectionStatus = "correction_applied",
) -> SettlementCorrectionResult:
    correction_id = f"corr_{uuid.uuid4().hex}"
    effective_reason_code = reason_code or request.reason_code
    _insert_correction_entry(
        connection,
        request,
        correction_id=correction_id,
        idempotency_key=idempotency_key,
        paid_acu_snapshot=liability["paid_acu"],
        state=correction_state,
        reason_code=effective_reason_code,
        actor_service=actor_service,
    )
    _insert_correction_event(
        connection,
        request,
        liability,
        correction_id=correction_id,
        idempotency_key=idempotency_key,
        event_type=event_type,
        correction_state=correction_state,
        reason_code=effective_reason_code,
        event_payload=event_payload,
        actor_service=actor_service,
    )
    _audit_correction(
        connection,
        request,
        liability,
        correction_id=correction_id,
        idempotency_key=idempotency_key,
        event_type=event_type,
        correction_state=correction_state,
        reason_code=effective_reason_code,
        event_payload=event_payload,
        secret=server_secret,
        actor_service=actor_service,
    )
    return SettlementCorrectionResult(
        status=status,
        correction_id=correction_id,
        idempotency_key=idempotency_key,
        liability_id=request.liability_id,
        correction_state=correction_state,
        reason_code=effective_reason_code,
    )


def _insert_correction_entry(
    connection: sa.Connection,
    request: SettlementCorrectionRequest,
    *,
    correction_id: str,
    idempotency_key: str,
    paid_acu_snapshot: Decimal,
    state: str,
    reason_code: str,
    actor_service: str,
) -> None:
    connection.execute(
        sa.text(
            """
            INSERT INTO settlement_correction_entry (
                correction_id,
                idempotency_key,
                liability_id,
                correction_type,
                amount_acu,
                paid_acu_snapshot,
                state,
                reason_code,
                actor_service,
                evidence_refs,
                created_at
            )
            VALUES (
                :correction_id,
                :idempotency_key,
                :liability_id,
                :correction_type,
                :amount_acu,
                :paid_acu_snapshot,
                :state,
                :reason_code,
                :actor_service,
                CAST(:evidence_refs AS jsonb),
                now()
            )
            """
        ),
        {
            "correction_id": correction_id,
            "idempotency_key": idempotency_key,
            "liability_id": request.liability_id,
            "correction_type": request.correction_type,
            "amount_acu": request.amount_acu,
            "paid_acu_snapshot": paid_acu_snapshot,
            "state": state,
            "reason_code": reason_code,
            "actor_service": actor_service,
            "evidence_refs": canonical_json({"evidence_refs": list(request.evidence_refs)}),
        },
    )


def _insert_correction_event(
    connection: sa.Connection,
    request: SettlementCorrectionRequest,
    liability: sa.RowMapping,
    *,
    correction_id: str,
    idempotency_key: str,
    event_type: str,
    correction_state: str,
    reason_code: str,
    event_payload: dict[str, Any],
    actor_service: str,
) -> None:
    connection.execute(
        sa.text(
            """
            INSERT INTO settlement_liability_event (
                event_id,
                liability_id,
                event_type,
                previous_state,
                next_state,
                amount_acu,
                reason_code,
                actor_service,
                evidence_refs,
                created_at
            )
            VALUES (
                :event_id,
                :liability_id,
                :event_type,
                :previous_state,
                :next_state,
                :amount_acu,
                :reason_code,
                :actor_service,
                CAST(:evidence_refs AS jsonb),
                now()
            )
            """
        ),
        {
            "event_id": f"slev_{uuid.uuid4().hex}",
            "liability_id": request.liability_id,
            "event_type": event_type,
            "previous_state": liability["state"],
            "next_state": UNDER_REVIEW
            if correction_state == CORRECTION_UNDER_REVIEW_MOVED
            else liability["state"],
            "amount_acu": request.amount_acu,
            "reason_code": reason_code,
            "actor_service": actor_service,
            "evidence_refs": canonical_json(
                {
                    "correction_id": correction_id,
                    "idempotency_key": idempotency_key,
                    "correction_type": request.correction_type,
                    "correction_state": correction_state,
                    "evidence_refs": list(request.evidence_refs),
                    **event_payload,
                }
            ),
        },
    )


def _audit_correction(
    connection: sa.Connection,
    request: SettlementCorrectionRequest,
    liability: sa.RowMapping,
    *,
    correction_id: str,
    idempotency_key: str,
    event_type: str,
    correction_state: str,
    reason_code: str,
    event_payload: dict[str, Any],
    secret: bytes,
    actor_service: str,
) -> None:
    insert_audit_event(
        connection,
        event_type=event_type,
        entity_type="settlement_correction_entry",
        entity_id=correction_id,
        payload={
            "correction_id": correction_id,
            "idempotency_key": idempotency_key,
            "liability_id": request.liability_id,
            "correction_type": request.correction_type,
            "amount_acu": request.amount_acu,
            "paid_acu_snapshot": liability["paid_acu"],
            "correction_state": correction_state,
            "reason_code": reason_code,
            "evidence_refs": list(request.evidence_refs),
            **event_payload,
        },
        secret=secret,
        actor_service=actor_service,
    )


def _load_existing_correction(
    connection: sa.Connection,
    idempotency_key: str,
) -> sa.RowMapping | None:
    return connection.execute(
        sa.text(
            """
            SELECT *
            FROM settlement_correction_entry
            WHERE idempotency_key = :idempotency_key
            FOR UPDATE
            """
        ),
        {"idempotency_key": idempotency_key},
    ).mappings().one_or_none()


def _load_liability_for_update(
    connection: sa.Connection,
    liability_id: str,
) -> sa.RowMapping | None:
    return connection.execute(
        sa.text(
            """
            SELECT *
            FROM settlement_liability
            WHERE liability_id = :liability_id
            FOR UPDATE
            """
        ),
        {"liability_id": liability_id},
    ).mappings().one_or_none()


def _existing_correction_result(existing: sa.RowMapping) -> SettlementCorrectionResult:
    status: SettlementCorrectionStatus = (
        "rejected" if existing["state"] == CORRECTION_REJECTED else "idempotent_replay"
    )
    return SettlementCorrectionResult(
        status=status,
        correction_id=str(existing["correction_id"]),
        idempotency_key=str(existing["idempotency_key"]),
        liability_id=str(existing["liability_id"]),
        correction_state=str(existing["state"]),
        reason_code=str(existing["reason_code"]),
        idempotent_replay=True,
    )


def _unpaid_correctable_acu(liability: sa.RowMapping) -> Decimal:
    return (
        liability["base_release_acu"]
        + liability["risk_reserve_acu"]
        + liability["under_review_acu"]
        + liability["vested_payable_acu"]
    )


def _over_credit_bucket_movements(
    liability: sa.RowMapping,
    amount_acu: Decimal,
) -> dict[str, Decimal]:
    already_under_review_acu = min(liability["under_review_acu"], amount_acu)
    remaining = amount_acu - already_under_review_acu

    vested_payable_debit_acu = min(liability["vested_payable_acu"], remaining)
    remaining -= vested_payable_debit_acu

    base_release_debit_acu = min(liability["base_release_acu"], remaining)
    remaining -= base_release_debit_acu

    risk_reserve_debit_acu = min(liability["risk_reserve_acu"], remaining)
    moved_to_under_review_acu = (
        vested_payable_debit_acu + base_release_debit_acu + risk_reserve_debit_acu
    )
    return {
        "base_release_acu": liability["base_release_acu"] - base_release_debit_acu,
        "risk_reserve_acu": liability["risk_reserve_acu"] - risk_reserve_debit_acu,
        "under_review_acu": liability["under_review_acu"] + moved_to_under_review_acu,
        "vested_payable_acu": liability["vested_payable_acu"] - vested_payable_debit_acu,
        "already_under_review_acu": already_under_review_acu,
        "moved_to_under_review_acu": moved_to_under_review_acu,
        "base_release_debit_acu": base_release_debit_acu,
        "risk_reserve_debit_acu": risk_reserve_debit_acu,
        "vested_payable_debit_acu": vested_payable_debit_acu,
    }


def _acu_key_amount(amount_acu: Decimal) -> str:
    if amount_acu <= ZERO_ACU:
        raise ValueError("amount_acu must be positive")
    return str(amount_acu.quantize(ACU_QUANT))


def _evidence_refs_hash(evidence_refs: tuple[str, ...]) -> str:
    payload = canonical_json({"evidence_refs": list(evidence_refs)})
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()

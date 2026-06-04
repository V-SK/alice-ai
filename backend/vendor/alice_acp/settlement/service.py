from __future__ import annotations

import hashlib
import hmac
import uuid
from collections.abc import Callable
from datetime import timedelta
from decimal import Decimal
from typing import Any

import sqlalchemy as sa

from alice_acp.audit import canonical_json, insert_audit_event
from alice_acp.policy_engine import RiskAssessment, split_verified_acu
from alice_acp.settlement.types import (
    BAD_LIABILITY_STATE,
    BAD_RESERVATION_STATE,
    LIABILITY_CREATED_PENDING_TRANCHE,
    LIABILITY_ID_COLLISION,
    PENDING_A5_RISK_TRANCHE_SPLIT,
    RELEASE_FAILED,
    RISK_RESERVE_ACTIVE,
    RISK_TRANCHE_SPLIT_APPLIED,
    TERMINAL_ZERO,
    TERMINAL_ZERO_VERIFIER_FAILED,
    TRANCHE_POLICY_VERSION_MISMATCH,
    UNDER_REVIEW,
    VERIFICATION_PAYLOAD_MISMATCH,
    VERIFIED_ACU_EXCEEDS_RESERVED,
    VERIFIER_VERDICT_FAILED_WITH_VERIFIED_ACU,
    VERIFIER_VERDICT_NOT_FINAL,
    SettlementConsumeResult,
    SettlementReleasePreflightError,
    SettlementTrancheSplitResult,
    VerificationResult,
)

ZERO_ACU = Decimal("0")


def settlement_liability_id(server_secret: bytes, admission_id: str, attempt_id: str) -> str:
    if not server_secret:
        raise ValueError("settlement liability secret must be non-empty")
    message = "\x1f".join((admission_id, attempt_id)).encode("utf-8")
    digest = hmac.new(server_secret, message, hashlib.sha256).hexdigest()
    return f"liab_{digest[:32]}"


def consume_reservation_and_create_liability(
    engine: sa.Engine,
    result: VerificationResult,
    *,
    server_secret: bytes,
    actor_service: str = "alice_acp.settlement",
    release_preflight_hook: Callable[[], None] | None = None,
) -> SettlementConsumeResult:
    try:
        return _consume_reservation_once(
            engine,
            result,
            server_secret=server_secret,
            actor_service=actor_service,
            release_preflight_hook=release_preflight_hook,
        )
    except SettlementReleasePreflightError:
        return _mark_under_review_after_release_failure(
            engine,
            result,
            server_secret=server_secret,
            actor_service=actor_service,
        )


def split_pending_liability(
    engine: sa.Engine,
    liability_id: str,
    assessment: RiskAssessment,
    *,
    tranche_policy_version: str,
    server_secret: bytes,
    actor_service: str = "alice_acp.settlement",
) -> SettlementTrancheSplitResult:
    with engine.connect().execution_options(isolation_level="SERIALIZABLE") as connection:
        with connection.begin():
            liability = _load_liability_by_id_for_update(connection, liability_id)
            if liability is None:
                return SettlementTrancheSplitResult(
                    status="rejected",
                    reason_code=BAD_LIABILITY_STATE,
                )
            if liability["state"] != LIABILITY_CREATED_PENDING_TRANCHE:
                _audit_tranche_rejection(
                    connection,
                    liability,
                    assessment,
                    event_type="settlement_tranche_split_rejected",
                    reason_code=BAD_LIABILITY_STATE,
                    secret=server_secret,
                    actor_service=actor_service,
                )
                return SettlementTrancheSplitResult(
                    status="rejected",
                    liability_id=str(liability["liability_id"]),
                    reason_code=BAD_LIABILITY_STATE,
                )
            if liability["tranche_policy_version"] != tranche_policy_version:
                _audit_tranche_rejection(
                    connection,
                    liability,
                    assessment,
                    event_type="settlement_tranche_split_rejected",
                    reason_code=TRANCHE_POLICY_VERSION_MISMATCH,
                    secret=server_secret,
                    actor_service=actor_service,
                )
                return SettlementTrancheSplitResult(
                    status="rejected",
                    liability_id=str(liability["liability_id"]),
                    reason_code=TRANCHE_POLICY_VERSION_MISMATCH,
                )

            if assessment.requires_under_review:
                _mark_liability_under_review_for_tranche(
                    connection,
                    liability,
                    assessment,
                    secret=server_secret,
                    actor_service=actor_service,
                )
                return SettlementTrancheSplitResult(
                    status="under_review",
                    liability_id=str(liability["liability_id"]),
                    reason_code=assessment.reason_code,
                )

            split = split_verified_acu(
                liability["total_verified_acu"],
                assessment.max_window_seconds,
            )
            risk_reserve_started_at = liability["created_at"]
            risk_reserve_expires_at = risk_reserve_started_at + timedelta(
                seconds=assessment.max_window_seconds
            )
            payload = assessment.applicable_payload()
            connection.execute(
                sa.text(
                    """
                    UPDATE settlement_liability
                    SET base_release_acu = :base_release_acu,
                        risk_reserve_acu = :risk_reserve_acu,
                        under_review_acu = 0,
                        state = :state,
                        state_reason_code = :state_reason_code,
                        risk_reserve_started_at = :risk_reserve_started_at,
                        risk_reserve_expires_at = :risk_reserve_expires_at,
                        applicable_fraud_classes = CAST(:applicable_fraud_classes AS jsonb),
                        updated_at = now()
                    WHERE liability_id = :liability_id
                    """
                ),
                {
                    "base_release_acu": split.base_release_acu,
                    "risk_reserve_acu": split.risk_reserve_acu,
                    "state": RISK_RESERVE_ACTIVE,
                    "state_reason_code": RISK_TRANCHE_SPLIT_APPLIED,
                    "risk_reserve_started_at": risk_reserve_started_at,
                    "risk_reserve_expires_at": risk_reserve_expires_at,
                    "applicable_fraud_classes": canonical_json(payload),
                    "liability_id": liability["liability_id"],
                },
            )
            _insert_tranche_event(
                connection,
                liability,
                event_type="settlement_risk_tranche_split",
                previous_state=str(liability["state"]),
                next_state=RISK_RESERVE_ACTIVE,
                amount_acu=liability["total_verified_acu"],
                reason_code=RISK_TRANCHE_SPLIT_APPLIED,
                applicable_fraud_classes=payload,
                base_release_acu=split.base_release_acu,
                risk_reserve_acu=split.risk_reserve_acu,
                actor_service=actor_service,
            )
            _audit_tranche_applied(
                connection,
                liability,
                assessment,
                base_release_acu=split.base_release_acu,
                risk_reserve_acu=split.risk_reserve_acu,
                risk_reserve_started_at=risk_reserve_started_at,
                risk_reserve_expires_at=risk_reserve_expires_at,
                secret=server_secret,
                actor_service=actor_service,
            )
            return SettlementTrancheSplitResult(
                status="split_applied",
                liability_id=str(liability["liability_id"]),
            )


def _consume_reservation_once(
    engine: sa.Engine,
    result: VerificationResult,
    *,
    server_secret: bytes,
    actor_service: str,
    release_preflight_hook: Callable[[], None] | None,
) -> SettlementConsumeResult:
    with engine.connect().execution_options(isolation_level="SERIALIZABLE") as connection:
        with connection.begin():
            reservation = _load_reservation_for_update(connection, result.reservation_id)
            if reservation is None:
                return SettlementConsumeResult(status="rejected", reason_code=BAD_RESERVATION_STATE)

            existing = _load_liability_for_update(connection, result.reservation_id)
            if existing is not None:
                return _handle_duplicate_verification(
                    connection,
                    result,
                    reservation,
                    existing,
                    server_secret=server_secret,
                    actor_service=actor_service,
                )

            if not _verification_matches_reservation(result, reservation):
                _audit_settlement_rejection(
                    connection,
                    result,
                    event_type="settlement_verification_payload_mismatch",
                    reason_code=VERIFICATION_PAYLOAD_MISMATCH,
                    secret=server_secret,
                    actor_service=actor_service,
                )
                return SettlementConsumeResult(
                    status="rejected",
                    reason_code=VERIFICATION_PAYLOAD_MISMATCH,
                )

            if reservation["status"] != "active_reserved":
                _audit_settlement_rejection(
                    connection,
                    result,
                    event_type="settlement_consume_rejected_bad_reservation_state",
                    reason_code=BAD_RESERVATION_STATE,
                    secret=server_secret,
                    actor_service=actor_service,
                )
                return SettlementConsumeResult(status="rejected", reason_code=BAD_RESERVATION_STATE)

            if result.verified_acu > reservation["reserved_acu"]:
                _mark_reservation_under_review(
                    connection,
                    result,
                    reason_code=VERIFIED_ACU_EXCEEDS_RESERVED,
                    secret=server_secret,
                    actor_service=actor_service,
                )
                return SettlementConsumeResult(
                    status="under_review",
                    reason_code=VERIFIED_ACU_EXCEEDS_RESERVED,
                )

            if result.verifier_verdict in {"delayed", "disputed"}:
                _mark_reservation_under_review(
                    connection,
                    result,
                    reason_code=VERIFIER_VERDICT_NOT_FINAL,
                    secret=server_secret,
                    actor_service=actor_service,
                )
                return SettlementConsumeResult(
                    status="under_review",
                    reason_code=VERIFIER_VERDICT_NOT_FINAL,
                )

            if result.verifier_verdict == "fail" and result.verified_acu > ZERO_ACU:
                _mark_reservation_under_review(
                    connection,
                    result,
                    reason_code=VERIFIER_VERDICT_FAILED_WITH_VERIFIED_ACU,
                    secret=server_secret,
                    actor_service=actor_service,
                )
                return SettlementConsumeResult(
                    status="under_review",
                    reason_code=VERIFIER_VERDICT_FAILED_WITH_VERIFIED_ACU,
                )

            liability_id = settlement_liability_id(
                server_secret,
                result.admission_id,
                result.attempt_id,
            )
            _lock_liability_id_for_transaction(connection, liability_id)
            liability_id_match = _load_liability_by_id_for_update(connection, liability_id)
            if liability_id_match is not None:
                _audit_liability_id_collision(
                    connection,
                    result,
                    reservation,
                    liability_id_match,
                    liability_id=liability_id,
                    secret=server_secret,
                    actor_service=actor_service,
                )
                return SettlementConsumeResult(
                    status="rejected",
                    liability_id=liability_id,
                    reason_code=LIABILITY_ID_COLLISION,
                )

            entries = _load_dimension_entries_for_update(connection, result.reservation_id)
            if not entries or not _release_preflight_ok(connection, reservation, entries):
                raise SettlementReleasePreflightError(RELEASE_FAILED)
            if release_preflight_hook is not None:
                release_preflight_hook()

            liability_shape = _liability_shape(result, reservation, entries, liability_id)

            _consume_dimension_entries(connection, reservation, entries, result.verified_acu)
            _insert_settlement_liability(connection, liability_shape)
            _insert_liability_event(
                connection,
                liability_shape,
                result,
                actor_service=actor_service,
            )
            _update_reservation_consumed(connection, reservation, liability_shape)
            _audit_liability_created(
                connection,
                result,
                liability_shape,
                secret=server_secret,
                actor_service=actor_service,
            )

            return SettlementConsumeResult(
                status="liability_created",
                liability_id=liability_id,
            )


def _handle_duplicate_verification(
    connection: sa.Connection,
    result: VerificationResult,
    reservation: sa.RowMapping,
    existing: sa.RowMapping,
    *,
    server_secret: bytes,
    actor_service: str,
) -> SettlementConsumeResult:
    expected = _liability_shape(
        result,
        reservation,
        _load_dimension_entries_for_update(connection, result.reservation_id),
        str(existing["liability_id"]),
    )
    if _existing_liability_matches(expected, existing):
        insert_audit_event(
            connection,
            event_type="settlement_liability_idempotent_replay",
            entity_type="settlement_liability",
            entity_id=str(existing["liability_id"]),
            payload={
                "liability_id": existing["liability_id"],
                "reservation_id": result.reservation_id,
                "admission_id": result.admission_id,
                "attempt_id": result.attempt_id,
                "verification_payload_hash": _verification_payload_hash(result),
            },
            secret=server_secret,
            actor_service=actor_service,
        )
        return SettlementConsumeResult(
            status="idempotent_replay",
            liability_id=str(existing["liability_id"]),
            idempotent_replay=True,
        )

    _audit_settlement_rejection(
        connection,
        result,
        event_type="settlement_verification_payload_mismatch",
        reason_code=VERIFICATION_PAYLOAD_MISMATCH,
        secret=server_secret,
        actor_service=actor_service,
        entity_type="settlement_liability",
        entity_id=str(existing["liability_id"]),
    )
    return SettlementConsumeResult(status="rejected", reason_code=VERIFICATION_PAYLOAD_MISMATCH)


def _mark_under_review_after_release_failure(
    engine: sa.Engine,
    result: VerificationResult,
    *,
    server_secret: bytes,
    actor_service: str,
) -> SettlementConsumeResult:
    with engine.connect().execution_options(isolation_level="SERIALIZABLE") as connection:
        with connection.begin():
            reservation = _load_reservation_for_update(connection, result.reservation_id)
            if reservation is None:
                return SettlementConsumeResult(status="rejected", reason_code=BAD_RESERVATION_STATE)
            _mark_reservation_under_review(
                connection,
                result,
                reason_code=RELEASE_FAILED,
                secret=server_secret,
                actor_service=actor_service,
            )
    return SettlementConsumeResult(status="under_review", reason_code=RELEASE_FAILED)


def _load_reservation_for_update(
    connection: sa.Connection,
    reservation_id: str,
) -> sa.RowMapping | None:
    return connection.execute(
        sa.text(
            """
            SELECT *
            FROM abrs_reservation
            WHERE reservation_id = :reservation_id
            FOR UPDATE
            """
        ),
        {"reservation_id": reservation_id},
    ).mappings().one_or_none()


def _load_liability_for_update(
    connection: sa.Connection,
    reservation_id: str,
) -> sa.RowMapping | None:
    return connection.execute(
        sa.text(
            """
            SELECT *
            FROM settlement_liability
            WHERE reservation_id = :reservation_id
            FOR UPDATE
            """
        ),
        {"reservation_id": reservation_id},
    ).mappings().one_or_none()


def _load_liability_by_id_for_update(
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


def _lock_liability_id_for_transaction(connection: sa.Connection, liability_id: str) -> None:
    connection.execute(
        sa.text("SELECT pg_advisory_xact_lock(hashtext(:liability_id))"),
        {"liability_id": liability_id},
    )


def _load_dimension_entries_for_update(
    connection: sa.Connection,
    reservation_id: str,
) -> tuple[sa.RowMapping, ...]:
    return tuple(
        connection.execute(
            sa.text(
                """
                SELECT *
                FROM abrs_reservation_dimension_entry
                WHERE reservation_id = :reservation_id
                ORDER BY account_id
                FOR UPDATE
                """
            ),
            {"reservation_id": reservation_id},
        ).mappings()
    )


def _verification_matches_reservation(
    result: VerificationResult,
    reservation: sa.RowMapping,
) -> bool:
    return (
        result.reservation_id == reservation["reservation_id"]
        and result.admission_id == reservation["admission_id"]
        and result.attempt_id == reservation["attempt_id"]
    )


def _release_preflight_ok(
    connection: sa.Connection,
    reservation: sa.RowMapping,
    entries: tuple[sa.RowMapping, ...],
) -> bool:
    if any(
        entry["consumed_acu"] != ZERO_ACU or entry["released_acu"] != ZERO_ACU
        for entry in entries
    ):
        return False
    if any(entry["reserved_acu"] != reservation["reserved_acu"] for entry in entries):
        return False

    account_ids = [entry["account_id"] for entry in entries]
    account_rows = connection.execute(
        sa.text(
            """
            SELECT account_id, reserved_acu
            FROM budget_dimension_account
            WHERE account_id = ANY(:account_ids)
            ORDER BY account_id
            FOR UPDATE
            """
        ),
        {"account_ids": account_ids},
    ).mappings()
    reserved_by_account = {row["account_id"]: row["reserved_acu"] for row in account_rows}
    return all(
        reserved_by_account.get(entry["account_id"], ZERO_ACU) >= reservation["reserved_acu"]
        for entry in entries
    )


def _consume_dimension_entries(
    connection: sa.Connection,
    reservation: sa.RowMapping,
    entries: tuple[sa.RowMapping, ...],
    verified_acu: Decimal,
) -> None:
    unused_acu = reservation["reserved_acu"] - verified_acu
    entry_status = "consumed" if verified_acu > ZERO_ACU else "released_unused"
    for entry in entries:
        connection.execute(
            sa.text(
                """
                UPDATE abrs_reservation_dimension_entry
                SET consumed_acu = :verified_acu,
                    released_acu = :unused_acu,
                    status = :status,
                    updated_at = now()
                WHERE reservation_id = :reservation_id
                  AND account_id = :account_id
                """
            ),
            {
                "verified_acu": verified_acu,
                "unused_acu": unused_acu,
                "status": entry_status,
                "reservation_id": reservation["reservation_id"],
                "account_id": entry["account_id"],
            },
        )
        connection.execute(
            sa.text(
                """
                UPDATE budget_dimension_account
                SET reserved_acu = reserved_acu - :reserved_acu,
                    consumed_acu = consumed_acu + :verified_acu,
                    released_acu = released_acu + :unused_acu,
                    updated_at = now()
                WHERE account_id = :account_id
                """
            ),
            {
                "reserved_acu": reservation["reserved_acu"],
                "verified_acu": verified_acu,
                "unused_acu": unused_acu,
                "account_id": entry["account_id"],
            },
        )


def _liability_shape(
    result: VerificationResult,
    reservation: sa.RowMapping,
    entries: tuple[sa.RowMapping, ...],
    liability_id: str,
) -> dict[str, Any]:
    unused_acu = reservation["reserved_acu"] - result.verified_acu
    state = LIABILITY_STATE_BY_VERDICT[result.verifier_verdict]
    state_reason_code = LIABILITY_REASON_BY_VERDICT[result.verifier_verdict]
    under_review_acu = result.verified_acu if result.verifier_verdict == "pass" else ZERO_ACU
    return {
        "liability_id": liability_id,
        "admission_id": reservation["admission_id"],
        "attempt_id": reservation["attempt_id"],
        "reservation_id": reservation["reservation_id"],
        "route_contract_id": reservation["route_contract_id"],
        "formula_version": reservation["formula_version"],
        "tranche_policy_version": reservation["tranche_policy_version"],
        "source_budget_id": _dimension_key(entries, "source_budget"),
        "mode_budget_id": _dimension_key(entries, "mode_budget"),
        "public_bucket_epoch": reservation["epoch_id"],
        "total_reserved_acu": reservation["reserved_acu"],
        "total_verified_acu": result.verified_acu,
        "unused_reserved_acu": unused_acu,
        "base_release_acu": ZERO_ACU,
        "risk_reserve_acu": ZERO_ACU,
        "under_review_acu": under_review_acu,
        "vested_payable_acu": ZERO_ACU,
        "forfeited_acu": ZERO_ACU,
        "paid_acu": ZERO_ACU,
        "reserve_pool_acu": ZERO_ACU,
        "applicable_fraud_classes": [],
        "state": state,
        "state_reason_code": state_reason_code,
    }


LIABILITY_STATE_BY_VERDICT = {
    "pass": LIABILITY_CREATED_PENDING_TRANCHE,
    "fail": TERMINAL_ZERO,
    "delayed": LIABILITY_CREATED_PENDING_TRANCHE,
    "disputed": LIABILITY_CREATED_PENDING_TRANCHE,
}
LIABILITY_REASON_BY_VERDICT = {
    "pass": PENDING_A5_RISK_TRANCHE_SPLIT,
    "fail": TERMINAL_ZERO_VERIFIER_FAILED,
    "delayed": PENDING_A5_RISK_TRANCHE_SPLIT,
    "disputed": PENDING_A5_RISK_TRANCHE_SPLIT,
}


def _dimension_key(entries: tuple[sa.RowMapping, ...], dimension_type: str) -> str:
    for entry in entries:
        if entry["dimension_type"] == dimension_type:
            return str(entry["dimension_key"])
    raise SettlementReleasePreflightError(f"missing {dimension_type} dimension entry")


def _insert_settlement_liability(connection: sa.Connection, liability: dict[str, Any]) -> None:
    connection.execute(
        sa.text(
            """
            INSERT INTO settlement_liability (
                liability_id,
                admission_id,
                attempt_id,
                reservation_id,
                route_contract_id,
                formula_version,
                tranche_policy_version,
                source_budget_id,
                mode_budget_id,
                public_bucket_epoch,
                total_reserved_acu,
                total_verified_acu,
                unused_reserved_acu,
                base_release_acu,
                risk_reserve_acu,
                under_review_acu,
                vested_payable_acu,
                forfeited_acu,
                paid_acu,
                reserve_pool_acu,
                applicable_fraud_classes,
                state,
                state_reason_code,
                created_at,
                updated_at
            )
            VALUES (
                :liability_id,
                :admission_id,
                :attempt_id,
                :reservation_id,
                :route_contract_id,
                :formula_version,
                :tranche_policy_version,
                :source_budget_id,
                :mode_budget_id,
                :public_bucket_epoch,
                :total_reserved_acu,
                :total_verified_acu,
                :unused_reserved_acu,
                :base_release_acu,
                :risk_reserve_acu,
                :under_review_acu,
                :vested_payable_acu,
                :forfeited_acu,
                :paid_acu,
                :reserve_pool_acu,
                CAST(:applicable_fraud_classes AS jsonb),
                :state,
                :state_reason_code,
                now(),
                now()
            )
            """
        ),
        {
            **liability,
            "applicable_fraud_classes": canonical_json(liability["applicable_fraud_classes"]),
        },
    )


def _insert_liability_event(
    connection: sa.Connection,
    liability: dict[str, Any],
    result: VerificationResult,
    *,
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
                NULL,
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
            "liability_id": liability["liability_id"],
            "event_type": _liability_event_type(liability),
            "next_state": liability["state"],
            "amount_acu": liability["total_verified_acu"],
            "reason_code": liability["state_reason_code"],
            "actor_service": actor_service,
            "evidence_refs": canonical_json(
                {
                    "evidence_refs": list(result.evidence_refs),
                    "applicable_signal_snapshot": result.applicable_signal_snapshot,
                    "verification_payload_hash": _verification_payload_hash(result),
                }
            ),
        },
    )


def _liability_event_type(liability: dict[str, Any]) -> str:
    if liability["state"] == TERMINAL_ZERO:
        return "terminal_zero_liability_created"
    return "settlement_liability_created"


def _update_reservation_consumed(
    connection: sa.Connection,
    reservation: sa.RowMapping,
    liability: dict[str, Any],
) -> None:
    status = "consumed" if liability["total_verified_acu"] > ZERO_ACU else "released_unused"
    connection.execute(
        sa.text(
            """
            UPDATE abrs_reservation
            SET consumed_acu = :consumed_acu,
                unused_reserved_acu = :unused_reserved_acu,
                status = :status,
                updated_at = now()
            WHERE reservation_id = :reservation_id
            """
        ),
        {
            "consumed_acu": liability["total_verified_acu"],
            "unused_reserved_acu": liability["unused_reserved_acu"],
            "status": status,
            "reservation_id": reservation["reservation_id"],
        },
    )


def _mark_reservation_under_review(
    connection: sa.Connection,
    result: VerificationResult,
    *,
    reason_code: str,
    secret: bytes,
    actor_service: str,
) -> None:
    connection.execute(
        sa.text(
            """
            UPDATE abrs_reservation
            SET status = 'under_review',
                updated_at = now()
            WHERE reservation_id = :reservation_id
            """
        ),
        {"reservation_id": result.reservation_id},
    )
    connection.execute(
        sa.text(
            """
            UPDATE abrs_reservation_dimension_entry
            SET status = 'under_review',
                updated_at = now()
            WHERE reservation_id = :reservation_id
            """
        ),
        {"reservation_id": result.reservation_id},
    )
    _audit_settlement_rejection(
        connection,
        result,
        event_type="settlement_accounting_under_review",
        reason_code=reason_code,
        secret=secret,
        actor_service=actor_service,
    )


def _audit_liability_created(
    connection: sa.Connection,
    result: VerificationResult,
    liability: dict[str, Any],
    *,
    secret: bytes,
    actor_service: str,
) -> None:
    insert_audit_event(
        connection,
        event_type=_liability_event_type(liability),
        entity_type="settlement_liability",
        entity_id=liability["liability_id"],
        payload={
            "liability_id": liability["liability_id"],
            "reservation_id": result.reservation_id,
            "admission_id": result.admission_id,
            "attempt_id": result.attempt_id,
            "verifier_verdict": result.verifier_verdict,
            "total_reserved_acu": liability["total_reserved_acu"],
            "total_verified_acu": liability["total_verified_acu"],
            "unused_reserved_acu": liability["unused_reserved_acu"],
            "state": liability["state"],
            "state_reason_code": liability["state_reason_code"],
            "verification_payload_hash": _verification_payload_hash(result),
        },
        secret=secret,
        actor_service=actor_service,
    )


def _audit_liability_id_collision(
    connection: sa.Connection,
    result: VerificationResult,
    reservation: sa.RowMapping,
    existing_liability: sa.RowMapping,
    *,
    liability_id: str,
    secret: bytes,
    actor_service: str,
) -> None:
    insert_audit_event(
        connection,
        event_type="settlement_liability_id_collision",
        entity_type="settlement_liability",
        entity_id=liability_id,
        payload={
            "liability_id": liability_id,
            "incoming_reservation_id": result.reservation_id,
            "existing_reservation_id": existing_liability["reservation_id"],
            "admission_id": result.admission_id,
            "attempt_id": result.attempt_id,
            "incoming_route_contract_id": reservation["route_contract_id"],
            "existing_route_contract_id": existing_liability["route_contract_id"],
            "reason_code": LIABILITY_ID_COLLISION,
            "verification_payload_hash": _verification_payload_hash(result),
        },
        secret=secret,
        actor_service=actor_service,
    )


def _mark_liability_under_review_for_tranche(
    connection: sa.Connection,
    liability: sa.RowMapping,
    assessment: RiskAssessment,
    *,
    secret: bytes,
    actor_service: str,
) -> None:
    reason_code = assessment.reason_code or "RISK_TRANCHE_REQUIRES_UNDER_REVIEW"
    applicable_fraud_classes = assessment.applicable_payload()
    connection.execute(
        sa.text(
            """
            UPDATE settlement_liability
            SET state = :state,
                state_reason_code = :state_reason_code,
                base_release_acu = 0,
                risk_reserve_acu = 0,
                under_review_acu = total_verified_acu,
                applicable_fraud_classes = CAST(:applicable_fraud_classes AS jsonb),
                updated_at = now()
            WHERE liability_id = :liability_id
            """
        ),
        {
            "state": UNDER_REVIEW,
            "state_reason_code": reason_code,
            "applicable_fraud_classes": canonical_json(applicable_fraud_classes),
            "liability_id": liability["liability_id"],
        },
    )
    _insert_tranche_event(
        connection,
        liability,
        event_type="settlement_tranche_under_review",
        previous_state=str(liability["state"]),
        next_state=UNDER_REVIEW,
        amount_acu=liability["total_verified_acu"],
        reason_code=reason_code,
        applicable_fraud_classes=applicable_fraud_classes,
        base_release_acu=ZERO_ACU,
        risk_reserve_acu=ZERO_ACU,
        actor_service=actor_service,
    )
    insert_audit_event(
        connection,
        event_type="settlement_tranche_under_review",
        entity_type="settlement_liability",
        entity_id=str(liability["liability_id"]),
        payload={
            "liability_id": liability["liability_id"],
            "reservation_id": liability["reservation_id"],
            "reason_code": reason_code,
            "total_verified_acu": liability["total_verified_acu"],
            "under_review_acu": liability["total_verified_acu"],
            "applicable_fraud_classes": applicable_fraud_classes,
        },
        secret=secret,
        actor_service=actor_service,
    )


def _insert_tranche_event(
    connection: sa.Connection,
    liability: sa.RowMapping,
    *,
    event_type: str,
    previous_state: str,
    next_state: str,
    amount_acu: Decimal,
    reason_code: str,
    applicable_fraud_classes: list[dict[str, Any]],
    base_release_acu: Decimal,
    risk_reserve_acu: Decimal,
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
            "liability_id": liability["liability_id"],
            "event_type": event_type,
            "previous_state": previous_state,
            "next_state": next_state,
            "amount_acu": amount_acu,
            "reason_code": reason_code,
            "actor_service": actor_service,
            "evidence_refs": canonical_json(
                {
                    "base_release_acu": base_release_acu,
                    "risk_reserve_acu": risk_reserve_acu,
                    "applicable_fraud_classes": applicable_fraud_classes,
                }
            ),
        },
    )


def _audit_tranche_applied(
    connection: sa.Connection,
    liability: sa.RowMapping,
    assessment: RiskAssessment,
    *,
    base_release_acu: Decimal,
    risk_reserve_acu: Decimal,
    risk_reserve_started_at: object,
    risk_reserve_expires_at: object,
    secret: bytes,
    actor_service: str,
) -> None:
    insert_audit_event(
        connection,
        event_type="settlement_risk_tranche_split",
        entity_type="settlement_liability",
        entity_id=str(liability["liability_id"]),
        payload={
            "liability_id": liability["liability_id"],
            "reservation_id": liability["reservation_id"],
            "tranche_policy_version": liability["tranche_policy_version"],
            "total_verified_acu": liability["total_verified_acu"],
            "base_release_acu": base_release_acu,
            "risk_reserve_acu": risk_reserve_acu,
            "under_review_acu": ZERO_ACU,
            "paid_acu": liability["paid_acu"],
            "state": RISK_RESERVE_ACTIVE,
            "state_reason_code": RISK_TRANCHE_SPLIT_APPLIED,
            "risk_reserve_started_at": risk_reserve_started_at,
            "risk_reserve_expires_at": risk_reserve_expires_at,
            "max_window_seconds": assessment.max_window_seconds,
            "applicable_fraud_classes": assessment.applicable_payload(),
        },
        secret=secret,
        actor_service=actor_service,
    )


def _audit_tranche_rejection(
    connection: sa.Connection,
    liability: sa.RowMapping,
    assessment: RiskAssessment,
    *,
    event_type: str,
    reason_code: str,
    secret: bytes,
    actor_service: str,
) -> None:
    insert_audit_event(
        connection,
        event_type=event_type,
        entity_type="settlement_liability",
        entity_id=str(liability["liability_id"]),
        payload={
            "liability_id": liability["liability_id"],
            "reservation_id": liability["reservation_id"],
            "current_state": liability["state"],
            "tranche_policy_version": liability["tranche_policy_version"],
            "reason_code": reason_code,
            "max_window_seconds": assessment.max_window_seconds,
            "applicable_fraud_classes": assessment.applicable_payload(),
        },
        secret=secret,
        actor_service=actor_service,
    )


def _audit_settlement_rejection(
    connection: sa.Connection,
    result: VerificationResult,
    *,
    event_type: str,
    reason_code: str,
    secret: bytes,
    actor_service: str,
    entity_type: str = "abrs_reservation",
    entity_id: str | None = None,
) -> None:
    insert_audit_event(
        connection,
        event_type=event_type,
        entity_type=entity_type,
        entity_id=entity_id or result.reservation_id,
        payload={
            "reservation_id": result.reservation_id,
            "admission_id": result.admission_id,
            "attempt_id": result.attempt_id,
            "verifier_verdict": result.verifier_verdict,
            "verified_acu": result.verified_acu,
            "reason_code": reason_code,
            "verification_payload_hash": _verification_payload_hash(result),
        },
        secret=secret,
        actor_service=actor_service,
    )


def _existing_liability_matches(expected: dict[str, Any], existing: sa.RowMapping) -> bool:
    deterministic_fields = (
        "liability_id",
        "admission_id",
        "attempt_id",
        "reservation_id",
        "route_contract_id",
        "formula_version",
        "tranche_policy_version",
        "source_budget_id",
        "mode_budget_id",
        "public_bucket_epoch",
        "total_reserved_acu",
        "total_verified_acu",
        "unused_reserved_acu",
        "base_release_acu",
        "risk_reserve_acu",
        "under_review_acu",
        "vested_payable_acu",
        "forfeited_acu",
        "paid_acu",
        "reserve_pool_acu",
        "state",
        "state_reason_code",
    )
    return all(existing[field] == expected[field] for field in deterministic_fields)


def _verification_payload_hash(result: VerificationResult) -> str:
    payload = {
        "reservation_id": result.reservation_id,
        "admission_id": result.admission_id,
        "attempt_id": result.attempt_id,
        "verifier_verdict": result.verifier_verdict,
        "verified_acu": result.verified_acu,
        "verification_completed_at": result.verification_completed_at,
        "applicable_signal_snapshot": result.applicable_signal_snapshot,
        "evidence_refs": list(result.evidence_refs),
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()

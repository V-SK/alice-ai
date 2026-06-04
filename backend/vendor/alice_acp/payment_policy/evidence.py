from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from alice_acp.evidence import (
    EvidenceRecordNotFoundError,
    LocalEvidenceRegistry,
    verify_record_manifest,
)
from alice_acp.evidence.types import parse_evidence_ref
from alice_acp.payment_policy.processor_contract import (
    PaymentProcessorReadinessInput,
    PaymentProcessorReadinessReport,
    evaluate_payment_processor_readiness,
)

PAYMENT_PROCESSOR_CONTRACT_TYPE = "payment_processor_contract"
PAYMENT_FINALITY_MAPPING_TYPE = "payment_finality_mapping"
PAYMENT_CHARGEBACK_RUNBOOK_TYPE = "payment_chargeback_runbook"
PAYMENT_RECONCILIATION_EVIDENCE_TYPE = "payment_reconciliation_evidence"
PAYMENT_SECRET_REFERENCE_TYPE = "payment_secret_reference"


@dataclass(frozen=True, slots=True)
class PaymentEvidenceBindingReport:
    processor_report: PaymentProcessorReadinessReport
    subject: str
    reason_codes: tuple[str, ...]
    processor_evidence_ready: bool = False
    live_payment_processor_ready: bool = False
    payout_executor_ready: bool = False


def validate_payment_processor_evidence(
    readiness: PaymentProcessorReadinessInput,
    registry: LocalEvidenceRegistry,
    *,
    subject: str,
    now: datetime | None = None,
) -> PaymentEvidenceBindingReport:
    processor_report = evaluate_payment_processor_readiness(readiness)
    reason_codes: list[str] = list(processor_report.reason_codes)
    if not processor_report.processor_integration_ready:
        reason_codes.append("PAYMENT_PROCESSOR_CONTRACT_GATE_BLOCKED")

    _require_ref(
        registry,
        readiness.processor_contract_ref,
        subject=subject,
        artifact_type=PAYMENT_PROCESSOR_CONTRACT_TYPE,
        missing_code="PAYMENT_PROCESSOR_CONTRACT_NOT_REGISTERED",
        invalid_code="PAYMENT_PROCESSOR_CONTRACT_INVALID",
        expected_scheme="evidence",
        now=now,
        reason_codes=reason_codes,
    )
    _require_ref(
        registry,
        readiness.finality_mapping_ref,
        subject=subject,
        artifact_type=PAYMENT_FINALITY_MAPPING_TYPE,
        missing_code="PAYMENT_FINALITY_MAPPING_NOT_REGISTERED",
        invalid_code="PAYMENT_FINALITY_MAPPING_INVALID",
        expected_scheme="evidence",
        now=now,
        reason_codes=reason_codes,
    )
    _require_ref(
        registry,
        readiness.chargeback_runbook_ref,
        subject=subject,
        artifact_type=PAYMENT_CHARGEBACK_RUNBOOK_TYPE,
        missing_code="PAYMENT_CHARGEBACK_RUNBOOK_NOT_REGISTERED",
        invalid_code="PAYMENT_CHARGEBACK_RUNBOOK_INVALID",
        expected_scheme="evidence",
        now=now,
        reason_codes=reason_codes,
    )
    _require_ref(
        registry,
        readiness.reconciliation_evidence_ref,
        subject=subject,
        artifact_type=PAYMENT_RECONCILIATION_EVIDENCE_TYPE,
        missing_code="PAYMENT_RECONCILIATION_EVIDENCE_NOT_REGISTERED",
        invalid_code="PAYMENT_RECONCILIATION_EVIDENCE_INVALID",
        expected_scheme="evidence",
        now=now,
        reason_codes=reason_codes,
    )
    _require_ref(
        registry,
        readiness.secret_ref,
        subject=subject,
        artifact_type=PAYMENT_SECRET_REFERENCE_TYPE,
        missing_code="PAYMENT_SECRET_REF_NOT_REGISTERED",
        invalid_code="PAYMENT_SECRET_REF_INVALID",
        expected_scheme="secret-ref",
        now=now,
        reason_codes=reason_codes,
    )

    return PaymentEvidenceBindingReport(
        processor_report=processor_report,
        subject=subject,
        reason_codes=tuple(dict.fromkeys(reason_codes)),
        processor_evidence_ready=not reason_codes,
        live_payment_processor_ready=False,
        payout_executor_ready=False,
    )


def _require_ref(
    registry: LocalEvidenceRegistry,
    ref: str | None,
    *,
    subject: str,
    artifact_type: str,
    missing_code: str,
    invalid_code: str,
    expected_scheme: str,
    now: datetime | None,
    reason_codes: list[str],
) -> None:
    if ref is None:
        reason_codes.append(missing_code)
        return
    try:
        if parse_evidence_ref(ref).scheme != expected_scheme:
            raise ValueError("payment evidence ref scheme mismatch")
        record = registry.require(ref, subject=subject, artifact_type=artifact_type, now=now)
        verify_record_manifest(record)
    except EvidenceRecordNotFoundError:
        reason_codes.append(missing_code)
    except ValueError:
        reason_codes.append(invalid_code)

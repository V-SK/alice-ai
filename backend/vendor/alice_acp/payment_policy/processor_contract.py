from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

ProcessorKind = Literal["card", "crypto", "foundation_credit"]


@dataclass(frozen=True, slots=True)
class PaymentProcessorReadinessInput:
    processor_kind: ProcessorKind
    processor_contract_defined: bool
    finality_mapping_defined: bool
    chargeback_handling_defined: bool
    reconciliation_evidence_defined: bool
    processor_contract_ref: str | None = None
    finality_mapping_ref: str | None = None
    chargeback_runbook_ref: str | None = None
    reconciliation_evidence_ref: str | None = None
    secret_ref: str | None = None
    raw_secret_present: bool = False

    def __post_init__(self) -> None:
        if self.raw_secret_present:
            raise ValueError("raw payment processor secrets are not allowed")
        if self.secret_ref is not None and not self.secret_ref.startswith("secret-ref://"):
            raise ValueError("secret_ref must be an opaque secret-ref URI")
        _validate_evidence_ref("processor_contract_ref", self.processor_contract_ref)
        _validate_evidence_ref("finality_mapping_ref", self.finality_mapping_ref)
        _validate_evidence_ref("chargeback_runbook_ref", self.chargeback_runbook_ref)
        _validate_evidence_ref("reconciliation_evidence_ref", self.reconciliation_evidence_ref)


@dataclass(frozen=True, slots=True)
class PaymentProcessorReadinessReport:
    processor_kind: ProcessorKind
    reason_codes: tuple[str, ...]
    processor_integration_ready: bool = False
    live_payment_processor_ready: bool = False
    payout_executor_ready: bool = False


def evaluate_payment_processor_readiness(
    readiness: PaymentProcessorReadinessInput,
) -> PaymentProcessorReadinessReport:
    reason_codes: list[str] = []
    if not readiness.processor_contract_defined:
        reason_codes.append("PROCESSOR_CONTRACT_NOT_DEFINED")
    if not readiness.finality_mapping_defined:
        reason_codes.append("FINALITY_MAPPING_NOT_DEFINED")
    if not readiness.chargeback_handling_defined:
        reason_codes.append("CHARGEBACK_HANDLING_NOT_DEFINED")
    if not readiness.reconciliation_evidence_defined:
        reason_codes.append("RECONCILIATION_EVIDENCE_NOT_DEFINED")
    if not readiness.processor_contract_ref:
        reason_codes.append("PROCESSOR_CONTRACT_REF_MISSING")
    if not readiness.finality_mapping_ref:
        reason_codes.append("FINALITY_MAPPING_REF_MISSING")
    if not readiness.chargeback_runbook_ref:
        reason_codes.append("CHARGEBACK_RUNBOOK_REF_MISSING")
    if not readiness.reconciliation_evidence_ref:
        reason_codes.append("RECONCILIATION_EVIDENCE_REF_MISSING")
    if readiness.secret_ref is None:
        reason_codes.append("PROCESSOR_SECRET_REF_MISSING")

    return PaymentProcessorReadinessReport(
        processor_kind=readiness.processor_kind,
        reason_codes=tuple(reason_codes),
        processor_integration_ready=not reason_codes,
        live_payment_processor_ready=False,
        payout_executor_ready=False,
    )


def _validate_evidence_ref(name: str, value: str | None) -> None:
    if value is None:
        return
    if not value.strip() or "://" not in value or any(character.isspace() for character in value):
        raise ValueError(f"{name} must be an opaque evidence URI")

"""R3 payment finality, provenance, and readiness contracts."""

from alice_acp.payment_policy.evidence import (
    PaymentEvidenceBindingReport,
    validate_payment_processor_evidence,
)
from alice_acp.payment_policy.execution import (
    PaymentPolicyExecution,
    execute_payment_finality_policy,
)
from alice_acp.payment_policy.processor_contract import (
    PaymentProcessorReadinessInput,
    PaymentProcessorReadinessReport,
    evaluate_payment_processor_readiness,
)
from alice_acp.payment_policy.r3_gate import evaluate_r3_eligibility, payment_finality_signal
from alice_acp.payment_policy.types import (
    CreditProvenance,
    R3EligibilityResult,
)

__all__ = (
    "CreditProvenance",
    "PaymentEvidenceBindingReport",
    "PaymentPolicyExecution",
    "PaymentProcessorReadinessInput",
    "PaymentProcessorReadinessReport",
    "R3EligibilityResult",
    "evaluate_payment_processor_readiness",
    "evaluate_r3_eligibility",
    "execute_payment_finality_policy",
    "payment_finality_signal",
    "validate_payment_processor_evidence",
)

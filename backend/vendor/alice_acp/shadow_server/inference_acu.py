from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from alice_acp.shadow_server.types import InferenceCompletionRequest

if TYPE_CHECKING:
    from alice_acp.shadow_server.route1_peg import Route1PegResolver

INFERENCE_ACU_QUANT = Decimal("0.000001")
MAX_INFERENCE_ACU = Decimal("1000000")

# Phase F (M1): a generous-but-finite upper bound on the per-token face value the
# server is willing to *record* as simulated foundation revenue. The client used
# to be able to declare an arbitrary ``simulated_api_payment`` and have the full
# unbounded face value land in the revenue ledger. We cap the recorded amount to
# ``(input + output tokens) * MAX_SIMULATED_API_PAYMENT_PER_TOKEN`` so a single
# completion cannot inject an absurd headline number. This is intentionally a
# ceiling (well above any real public per-token price), not a price oracle --
# CREDIT-ONLY: it only bounds a *simulated* number; no payout/chain is touched.
MAX_SIMULATED_API_PAYMENT_PER_TOKEN = Decimal("0.10")


def bounded_simulated_api_payment(
    declared_amount: Decimal,
    *,
    input_tokens: int,
    output_tokens: int,
) -> Decimal:
    """Clamp a client-declared simulated API payment to a sane token-derived cap.

    Phase F (M1): returns ``min(declared, (input+output) * per_token_cap)`` and
    never returns a negative amount. ``calculate_verified_inference_acu`` already
    rejects a negative declared payment, so callers only reach here with a
    non-negative value; the ``max(.., 0)`` is belt-and-suspenders fail-closed.
    """

    if declared_amount <= Decimal("0"):
        return Decimal("0")
    billable_tokens = max(input_tokens, 0) + max(output_tokens, 0)
    cap = Decimal(billable_tokens) * MAX_SIMULATED_API_PAYMENT_PER_TOKEN
    return max(min(declared_amount, cap), Decimal("0"))


@dataclass(frozen=True, slots=True)
class InferenceAcuPolicy:
    model_class: str
    input_token_weight: Decimal
    output_token_weight: Decimal
    latency_ms_weight: Decimal
    model_multiplier: Decimal
    max_context_length: int
    max_latency_ms: Decimal


INFERENCE_ACU_POLICIES: dict[str, InferenceAcuPolicy] = {
    "tier1_local_llm": InferenceAcuPolicy(
        model_class="tier1_local_llm",
        input_token_weight=Decimal("0.25"),
        output_token_weight=Decimal("1.00"),
        latency_ms_weight=Decimal("0.001"),
        model_multiplier=Decimal("1.00"),
        max_context_length=32768,
        max_latency_ms=Decimal("60000"),
    ),
    "tier2_local_llm": InferenceAcuPolicy(
        model_class="tier2_local_llm",
        input_token_weight=Decimal("0.50"),
        output_token_weight=Decimal("1.50"),
        latency_ms_weight=Decimal("0.002"),
        model_multiplier=Decimal("1.25"),
        max_context_length=65536,
        max_latency_ms=Decimal("120000"),
    ),
}


class InferenceAcuValidationError(ValueError):
    pass


def calculate_verified_inference_acu(
    request: InferenceCompletionRequest,
    *,
    route1_resolver: Route1PegResolver | None = None,
) -> Decimal:
    """The provisional inference credit (== ``verified_score``) for a completion.

    Validates usage (rejecting absurd token/latency/context values) then prices
    the credit:

    * **Route 1 (M1, plan §3):** when the request carries the peg inputs
      (``gpu_class`` + ``runtime``) AND the peg resolves (an M_rate for the class
      and a throughput row for (tier, gpu_class, runtime, quant)), credit =
      RECOUNTED total tokens * (M_rate(GPU) / T(model, GPU)). A full-load GPU
      then earns ~= its PRL credit/hour, making the GPU switch fair.
    * **Legacy fallback:** otherwise the abstract token/latency-weighted ACU
      (unchanged) -- so every existing caller / test that does not pass the peg
      inputs is byte-for-byte unaffected.

    Credit-only throughout (the result is a provisional credit; ``paid_acu``
    stays "0").
    """
    policy = _policy_for(request.model_id, request.model_class)
    _validate_usage(request, policy)
    pegged = _route1_credit(request, route1_resolver)
    if pegged is not None:
        return pegged
    token_acu = (
        Decimal(request.input_tokens) * policy.input_token_weight
        + Decimal(request.output_tokens) * policy.output_token_weight
    )
    latency_acu = request.latency_ms * policy.latency_ms_weight
    acu = (token_acu + latency_acu) * policy.model_multiplier
    return acu.quantize(INFERENCE_ACU_QUANT)


def _route1_credit(
    request: InferenceCompletionRequest,
    resolver: Route1PegResolver | None,
) -> Decimal | None:
    """Return the Route-1 pegged credit, or ``None`` to fall back to legacy ACU.

    Imported lazily so this module has no import-time dependency on the catalog /
    pinned-models / throughput stack (keeps the abstract-ACU fallback path cheap
    and avoids any import cycle). Returns ``None`` whenever the peg cannot apply
    (no fine catalog ``model_tier`` + ``runtime`` on the request, or the peg does
    not resolve) so the caller uses the legacy ACU.
    """
    if request.model_tier is None or request.runtime is None:
        return None
    from alice_acp.shadow_server.route1_peg import Route1PegResolver as _Resolver

    active = resolver if resolver is not None else _Resolver()
    peg = active.resolve(
        tier=request.model_tier,  # type: ignore[arg-type]
        runtime=request.runtime,  # type: ignore[arg-type]
        gpu_class=request.gpu_class,  # type: ignore[arg-type]
        # M5: select the SERVING device's measured PRL M_rate (the resolver only
        # uses these when a device-rate reader is wired; otherwise they are inert
        # and the per-class table applies, exactly as in M1).
        passport_id=request.route1_passport_id,
        device_id=request.route1_device_id,
        now=request.observed_at,
    )
    if not peg.applies:
        return None
    # Route-1 bills on the RECOUNTED total tokens (the request's input+output ARE
    # the min(declared, recount) counts the edge passed).
    total_tokens = request.input_tokens + request.output_tokens
    return peg.credit_for_tokens(total_recounted_tokens=total_tokens)


def _policy_for(model_id: str, model_class: str) -> InferenceAcuPolicy:
    if not model_id or not model_id.startswith("alice-"):
        raise InferenceAcuValidationError("invalid_inference_usage")
    try:
        return INFERENCE_ACU_POLICIES[model_class]
    except KeyError as exc:
        raise InferenceAcuValidationError("invalid_inference_usage") from exc


def _validate_usage(request: InferenceCompletionRequest, policy: InferenceAcuPolicy) -> None:
    if request.input_tokens <= 0 or request.output_tokens <= 0:
        raise InferenceAcuValidationError("invalid_inference_usage")
    if request.context_length <= 0 or request.context_length > policy.max_context_length:
        raise InferenceAcuValidationError("invalid_inference_usage")
    if request.input_tokens + request.output_tokens > request.context_length:
        raise InferenceAcuValidationError("invalid_inference_usage")
    try:
        latency_ms = Decimal(request.latency_ms)
    except (InvalidOperation, TypeError) as exc:
        raise InferenceAcuValidationError("invalid_inference_usage") from exc
    if latency_ms <= Decimal("0") or latency_ms > policy.max_latency_ms:
        raise InferenceAcuValidationError("invalid_inference_usage")
    if request.simulated_api_payment < Decimal("0"):
        raise InferenceAcuValidationError("invalid_inference_usage")
    if calculate_upper_bound(request, policy) > MAX_INFERENCE_ACU:
        raise InferenceAcuValidationError("invalid_inference_usage")


def calculate_upper_bound(
    request: InferenceCompletionRequest,
    policy: InferenceAcuPolicy,
) -> Decimal:
    return (
        Decimal(request.context_length)
        * max(policy.input_token_weight, policy.output_token_weight)
        + policy.max_latency_ms * policy.latency_ms_weight
    ) * policy.model_multiplier

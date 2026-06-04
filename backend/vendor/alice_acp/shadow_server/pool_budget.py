from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal

from alice_acp.shadow_server.types import (
    MAIN_POOL_AI,
    MAIN_POOL_GPU_PRL,
    MAIN_POOL_GPU_QUAI,
    MAIN_POOL_GPU_RVN,
    MAIN_POOL_SHARE,
    POOL_MAIN,
    POOL_SCRYPT,
    POOL_XMR,
    SCRYPT_POOL_SHARE,
    XMR_POOL_SHARE,
    ZERO_DECIMAL,
    SettlementWindow,
)

#: Quantum the per-device clamped AI credit is rounded to (matches the Route-1
#: peg / inference-ACU quantum so the reconciliation aggregates cleanly).
AI_CAP_CREDIT_QUANT = Decimal("0.000001")

MAIN_POOL_AI_CAP_SHARE_WHEN_GPU_PRESENT = Decimal("0.50")
MAIN_POOL_AI_GPU_CAP_POLICY = "main_pool_ai_max_50_percent_when_gpu_score_present"
MAIN_POOL_SINGLE_ACTIVE_LANE_POLICY = "main_pool_single_active_lane_can_use_pool_budget"

#: The GPU lanes that draw from the main pool's GPU sub-budget. ALL THREE GPU lanes —
#: RVN + Quai (both KawPoW) and PRL (pearlhash/PoUW, the GPU-PRIMARY route for which RVN
#: is the fallback) — are GPU lanes: they SHARE the single GPU sub-budget pro-rata by
#: their own scores (the AI/GPU cap policy is unchanged; "GPU present" means ANY GPU lane
#: has score). This shared-GPU-budget family is the INTENDED design — PRL and RVN are the
#: SAME GPU capacity (a rig mines PRL primary, RVN as fallback), so a GPU's credit comes
#: from the one GPU sub-budget regardless of which GPU route it took. Credit between the
#: GPU lanes stays distinguishable at the per-lane reward-statement level (each device
#: credits from the shared GPU budget by its own lane's score share, under its own lane).
#: Adding PRL here is byte-for-byte score-neutral vs the prior overload: PRL's score was
#: already summed into the GPU total (it was literally labeled ``main_pool_gpu_rvn``); it
#: now contributes under its OWN lane key but to the SAME ``gpu_score_total``.
GPU_LANES: tuple[str, ...] = (MAIN_POOL_GPU_RVN, MAIN_POOL_GPU_QUAI, MAIN_POOL_GPU_PRL)

#: The key the shared GPU sub-budget is stored under in :attr:`MainPoolLaneBudget.lane_budgets`.
#: Historically the GPU sub-budget was the single RVN lane's budget, so it is keyed by
#: ``MAIN_POOL_GPU_RVN`` (unchanged for backward compatibility); with a SECOND GPU lane
#: (Quai) it is the COMBINED GPU-lane budget the scoring split shares across both GPU lanes
#: pro-rata. Reading it via this lane-agnostic alias keeps the scoring code from pinning the
#: GPU budget to one lane name.
MAIN_POOL_GPU_LANE_BUDGET_KEY: str = MAIN_POOL_GPU_RVN


@dataclass(frozen=True, slots=True)
class PoolBudgetContract:
    main_pool_share: Decimal = MAIN_POOL_SHARE
    xmr_pool_share: Decimal = XMR_POOL_SHARE
    scrypt_pool_share: Decimal = SCRYPT_POOL_SHARE
    main_pool_ai_cap_share_when_gpu_present: Decimal = MAIN_POOL_AI_CAP_SHARE_WHEN_GPU_PRESENT

    def validate(self) -> None:
        total_share = self.main_pool_share + self.xmr_pool_share + self.scrypt_pool_share
        if total_share != Decimal("1.00"):
            raise ValueError("shadow_pool_budget_split_must_sum_to_1")
        for value in (
            self.main_pool_share,
            self.xmr_pool_share,
            self.scrypt_pool_share,
            self.main_pool_ai_cap_share_when_gpu_present,
        ):
            if value < ZERO_DECIMAL:
                raise ValueError("shadow_pool_budget_share_must_be_non_negative")
        if self.main_pool_ai_cap_share_when_gpu_present > Decimal("1.00"):
            raise ValueError("shadow_main_pool_ai_cap_share_must_not_exceed_1")


@dataclass(frozen=True, slots=True)
class MainPoolLaneBudget:
    lane_budgets: dict[str, Decimal]
    cap_policy: str


DEFAULT_POOL_BUDGET_CONTRACT = PoolBudgetContract()


def pool_budgets_for_window(
    window: SettlementWindow,
    *,
    contract: PoolBudgetContract = DEFAULT_POOL_BUDGET_CONTRACT,
) -> dict[str, Decimal]:
    contract.validate()
    return {
        POOL_MAIN: window.total_window_emission * contract.main_pool_share,
        POOL_XMR: window.total_window_emission * contract.xmr_pool_share,
        POOL_SCRYPT: window.total_window_emission * contract.scrypt_pool_share,
    }


def main_pool_lane_budget(
    *,
    main_pool_budget: Decimal,
    gpu_score: Decimal,
    ai_score: Decimal,
    contract: PoolBudgetContract = DEFAULT_POOL_BUDGET_CONTRACT,
) -> MainPoolLaneBudget:
    contract.validate()
    if gpu_score > ZERO_DECIMAL and ai_score > ZERO_DECIMAL:
        ai_budget = main_pool_budget * contract.main_pool_ai_cap_share_when_gpu_present
        return MainPoolLaneBudget(
            lane_budgets={
                MAIN_POOL_GPU_RVN: main_pool_budget - ai_budget,
                MAIN_POOL_AI: ai_budget,
            },
            cap_policy=MAIN_POOL_AI_GPU_CAP_POLICY,
        )
    if ai_score > ZERO_DECIMAL:
        return MainPoolLaneBudget(
            lane_budgets={
                MAIN_POOL_GPU_RVN: ZERO_DECIMAL,
                MAIN_POOL_AI: main_pool_budget,
            },
            cap_policy=MAIN_POOL_SINGLE_ACTIVE_LANE_POLICY,
        )
    return MainPoolLaneBudget(
        lane_budgets={
            MAIN_POOL_GPU_RVN: main_pool_budget,
            MAIN_POOL_AI: ZERO_DECIMAL,
        },
        cap_policy=MAIN_POOL_SINGLE_ACTIVE_LANE_POLICY,
    )


# --------------------------------------------------------------------------- #
# AI-lane cap reconciliation (M1 task #3, dispatch plan §2 + §3).
#
# Route 1 sizes each device's PROVISIONAL AI credit as
# ``recounted_tokens * (M_rate(GPU) / T(model,GPU))`` so a full-load GPU earns
# ~= its PRL rate. But the MAIN_POOL_AI lane is capped at 50% of the main pool
# when any GPU lane is present (the cap above). When the AGGREGATE provisional AI
# credit across all devices in a window EXCEEDS that lane budget, we cannot pay
# every device its full provisional amount without blowing the cap. The honest
# resolution (so the "full-load earns its PRL rate" claim stays true AT THE
# BOUNDARY) is:
#
#   * cap_binds = aggregate_provisional > ai_lane_budget
#   * clamp_factor = min(1, ai_lane_budget / aggregate_provisional)
#   * finalized_i = provisional_i * clamp_factor          (PRO-RATA, fair)
#   * clawback_i  = provisional_i - finalized_i           (RECORDED, not paid)
#
# Below the cap (clamp_factor == 1) every device is paid its full Route-1
# provisional credit -- the claim holds exactly. AT/above the cap every device is
# scaled down by the SAME factor (no device is favoured) and the shortfall is
# recorded so the books reconcile. Credit-only: ``finalized`` / ``clawback`` are
# PROVISIONAL credit numbers; ``paid_acu`` is untouched (stays 0). This is the
# Route-1-faithful AI-lane settlement: unlike the generic pro-rata in
# ``settle_reward_scores`` (which distributes the WHOLE lane budget and would
# scale a low-demand lane UP past its PRL peg), this NEVER pays a device more than
# its Route-1 provisional -- it only ever clamps DOWN at the cap.
# --------------------------------------------------------------------------- #
AI_CAP_RECONCILE_BELOW_CAP_POLICY = "main_pool_ai_below_cap_full_route1_credit"
AI_CAP_RECONCILE_AT_CAP_POLICY = "main_pool_ai_at_cap_clamped_pro_rata_recorded"


@dataclass(frozen=True, slots=True)
class AiDeviceCapResult:
    """Per-device outcome of the AI-lane cap reconciliation.

    ``provisional_credit`` is the device's Route-1 pegged credit (its
    PRL-rate-anchored entitlement); ``finalized_credit`` is what it is actually
    credited after the pro-rata cap clamp; ``clawback_credit`` is the recorded
    shortfall (``provisional - finalized``), zero below the cap. All are
    credit-only (provisional Alice credit; ``paid_acu`` untouched).
    """

    passport_id: str
    device_id: str
    provisional_credit: Decimal
    finalized_credit: Decimal
    clawback_credit: Decimal


@dataclass(frozen=True, slots=True)
class AiCapReconciliation:
    """The AI-lane cap reconciliation for one settlement window.

    ``cap_binds`` is True iff aggregate provisional AI credit exceeded the AI lane
    budget (so the clamp was applied). ``clamp_factor`` is the pro-rata scale
    (1 below the cap). ``total_finalized`` never exceeds ``ai_lane_budget``;
    ``total_clawback`` = aggregate provisional - total finalized (the recorded,
    unpaid shortfall). Per-device results are in ``per_device``.
    """

    ai_lane_budget: Decimal
    aggregate_provisional: Decimal
    cap_binds: bool
    clamp_factor: Decimal
    total_finalized: Decimal
    total_clawback: Decimal
    cap_policy: str
    per_device: tuple[AiDeviceCapResult, ...]

    def to_public_dict(self) -> dict[str, object]:
        return {
            "ai_lane_budget": str(self.ai_lane_budget),
            "aggregate_provisional": str(self.aggregate_provisional),
            "cap_binds": self.cap_binds,
            "clamp_factor": str(self.clamp_factor),
            "total_finalized": str(self.total_finalized),
            "total_clawback": str(self.total_clawback),
            "cap_policy": self.cap_policy,
            "paid_acu": "0",
            "per_device": [
                {
                    "passport_id": r.passport_id,
                    "device_id": r.device_id,
                    "provisional_credit": str(r.provisional_credit),
                    "finalized_credit": str(r.finalized_credit),
                    "clawback_credit": str(r.clawback_credit),
                }
                for r in self.per_device
            ],
        }


def reconcile_ai_credit_against_cap(
    *,
    ai_lane_budget: Decimal,
    provisional_credit_by_device: Mapping[tuple[str, str], Decimal],
) -> AiCapReconciliation:
    """Clamp aggregate Route-1 AI credit to the AI lane cap, pro-rata + recorded.

    ``provisional_credit_by_device`` maps ``(passport_id, device_id)`` to that
    device's Route-1 provisional AI credit for the window. Returns the
    reconciliation: below the cap every device keeps its full provisional credit;
    at/above the cap all devices are scaled down by the same ``clamp_factor`` and
    the shortfall is recorded as ``clawback_credit``. The sum of finalized credit
    never exceeds ``ai_lane_budget``. Credit-only; deterministic ordering.
    """
    if ai_lane_budget < ZERO_DECIMAL:
        raise ValueError("ai_lane_budget must be non-negative")
    for amount in provisional_credit_by_device.values():
        if amount < ZERO_DECIMAL:
            raise ValueError("provisional AI credit must be non-negative")

    aggregate = sum(provisional_credit_by_device.values(), ZERO_DECIMAL)
    cap_binds = aggregate > ai_lane_budget
    if not cap_binds or aggregate == ZERO_DECIMAL:
        clamp_factor = Decimal("1")
        cap_policy = AI_CAP_RECONCILE_BELOW_CAP_POLICY
    else:
        clamp_factor = ai_lane_budget / aggregate
        cap_policy = AI_CAP_RECONCILE_AT_CAP_POLICY

    per_device: list[AiDeviceCapResult] = []
    total_finalized = ZERO_DECIMAL
    for identity in sorted(provisional_credit_by_device):
        provisional = provisional_credit_by_device[identity]
        if cap_binds:
            finalized = (provisional * clamp_factor).quantize(AI_CAP_CREDIT_QUANT)
            # Never round a device's finalized credit ABOVE its provisional (the
            # clamp only ever lowers); guard the quantize edge.
            if finalized > provisional:
                finalized = provisional
        else:
            finalized = provisional
        clawback = provisional - finalized
        per_device.append(
            AiDeviceCapResult(
                passport_id=identity[0],
                device_id=identity[1],
                provisional_credit=provisional,
                finalized_credit=finalized,
                clawback_credit=clawback,
            )
        )
        total_finalized += finalized

    # Fail-closed: the clamped total must never exceed the cap (rounding could in
    # principle nudge it; clamp the reported total so the cap is a hard ceiling).
    if cap_binds and total_finalized > ai_lane_budget:
        total_finalized = ai_lane_budget
    total_clawback = aggregate - total_finalized
    if total_clawback < ZERO_DECIMAL:
        total_clawback = ZERO_DECIMAL
    return AiCapReconciliation(
        ai_lane_budget=ai_lane_budget,
        aggregate_provisional=aggregate,
        cap_binds=cap_binds,
        clamp_factor=clamp_factor,
        total_finalized=total_finalized,
        total_clawback=total_clawback,
        cap_policy=cap_policy,
        per_device=tuple(per_device),
    )

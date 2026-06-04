"""M9: the REWARD-CYCLE / FINALIZATION engine (dispatch plan §2 + §6).

PHASE-J GATED. This engine carries credit through its life cycle:

    PROVISIONAL  --(its verification/clawback WINDOW closes)-->  FINALIZED

The verification window is the SPINE of the anti-cheat (§2): credit accrues
immediately+PROVISIONALLY (the worker sees it for UX), but it is FINALIZED into a
payout ONLY AFTER a per-lane VERIFICATION WINDOW closes. The window IS the
fraud-detection / clawback period — fraud caught inside the window claws the
provisional credit back BEFORE finalization, so no real value is ever lost
("发真代币有延迟 = 追回窗口").

Per-lane windows (§2 table + Appendix, V-approved 2026-06-01):

  * **AI** (dispatched) — a FIXED 8h window. AI verification is slow + probabilistic
    (sampled logprob re-scoring), so it needs a long window to sample enough to
    catch fakes.
  * **mining lanes** — the UPSTREAM payout cadence + 2h. Mining fraud is rejected
    INSTANTLY by the per-share re-hash, so the +2h is purely the revenue-landing +
    reconcile buffer (Alice never pays before the upstream pays IT; the +2h
    reconciles Alice's re-hash credit against the upstream's ACTUAL payment). Cadences
    (§2): LTC daily · XMR ~2h · RVN ~3h · Quai ~2h · PRL event-driven post-maturity.

THE PAYOUT-LAGS-WINDOW SEAL (HARD INVARIANT, §2/§7): finalization/payout must LAG
the verification window. Credit still INSIDE its clawback window can NEVER be
finalized or paid. Every finalize path here first asserts ``now >= window_closes_at``
(via :meth:`CreditWindow.is_closed_at`); a not-yet-closed lot is impossible to
finalize through this engine. This is the structural counterpart to the durable
boundary-scan assertion (§7) — finalize cannot run early.

Two finalization paths (§2):

  * **Mining finalization** = distribute the ACTUAL upstream revenue that landed in
    Alice's account, PRO-RATA by re-hash credit, REVENUE-BOUNDED (never pay more
    than the upstream actually paid Alice), per ``device_key``, aggregating up to the
    Alice address. See :func:`finalize_mining_lane`.
  * **AI finalization** = the Route-1 per-device provisional credit, reconciled at
    the 8h epoch boundary by invoking M1's
    :func:`~alice_acp.shadow_server.pool_budget.reconcile_ai_credit_against_cap`
    (clamp to the 0.50 AI lane budget, record clawback). See
    :func:`finalize_ai_epoch`.

CREDIT-ONLY (HARD INVARIANT): everything here computes FINALIZED CREDIT only,
OFF-CHAIN. ``paid_acu`` stays ``"0"`` on every record/result. The
finalize -> payout step is behind :attr:`RewardCycleConfig.finalize_to_payout_enabled`
which DEFAULTS OFF; even when finalized, nothing flips ``paid_acu`` and no real
payout / reward emission / on-chain code path exists here. Phase-J payout is a
later FLIP of that flag against this built machinery, not a rebuild.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from alice_acp.api_chat.types import validate_public_identifier
from alice_acp.evidence.types import validate_aware_timestamp
from alice_acp.shadow_server.pool_budget import (
    AiCapReconciliation,
    reconcile_ai_credit_against_cap,
)
from alice_acp.shadow_server.types import (
    MAIN_POOL_AI,
    MAIN_POOL_GPU_PRL,
    MAIN_POOL_GPU_QUAI,
    MAIN_POOL_GPU_RVN,
    SCRYPT_POOL,
    XMR_POOL,
    Lane,
)

#: Bump when the window cadences or the finalization math change so an audited
#: finalization can pin exactly which engine version produced it.
REWARD_CYCLE_VERSION = "alice-reward-cycle-v1"

#: Credit is quantised to this resolution (matches the Route-1 peg / inference-ACU
#: quantum so finalized credit aggregates cleanly with the rest of the ledger).
CREDIT_QUANT = Decimal("0.000001")
ZERO = Decimal("0")

# Credit lifecycle states.
STATE_PROVISIONAL = "provisional"
STATE_FINALIZED = "finalized"
STATE_CLAWED_BACK = "clawed_back"

# Finalization status / reason codes (surfaced for audit, never raw text).
REASON_WINDOW_OPEN = "reward_cycle_window_still_open"
REASON_FINALIZED = "reward_cycle_finalized"
REASON_CLAWED_BACK = "reward_cycle_clawed_back"
REASON_ALREADY_FINALIZED = "reward_cycle_already_finalized"
REASON_REVENUE_BOUNDED = "reward_cycle_mining_revenue_bounded_pro_rata"
REASON_AI_CAP_RECONCILED = "reward_cycle_ai_cap_reconciled_at_epoch"

# --------------------------------------------------------------------------- #
# Per-lane verification windows (§2). The AI window is a fixed duration; the
# mining windows are the UPSTREAM payout cadence + a fixed +2h reconcile lag.
# These are the SPINE: a lot's window opens at its observed_at and CLOSES at
# observed_at + window_duration; credit cannot finalize until now >= that close.
# --------------------------------------------------------------------------- #

#: AI verification window — FIXED 8h (§2). Long because AI verification is slow +
#: probabilistic (sampled logprob re-scoring needs enough samples to catch fakes).
AI_VERIFICATION_WINDOW = timedelta(hours=8)

#: The fixed reconcile lag added to a mining lane's upstream cadence (§2): Alice
#: distributes 2h AFTER the upstream payout lands, to reconcile its re-hash credit
#: against the upstream's ACTUAL payment.
MINING_RECONCILE_LAG = timedelta(hours=2)

#: Upstream payout cadences per mining lane (§2 table + Appendix). The lane's full
#: verification window is ``cadence + MINING_RECONCILE_LAG``. PRL is event-driven
#: (≈hourly steady once matured); we model its cadence as ~1h (the work->payout
#: path already includes the ~5.4h maturity upstream, so the WINDOW here is only
#: the post-payout reconcile buffer). RVN ~3h, XMR/Quai ~2h, LTC daily.
MINING_UPSTREAM_CADENCE: dict[Lane, timedelta] = {
    MAIN_POOL_GPU_RVN: timedelta(hours=3),  # ravenminer pays every ~3h
    MAIN_POOL_GPU_QUAI: timedelta(hours=2),  # 2Miners pays ~every 2h
    MAIN_POOL_GPU_PRL: timedelta(hours=1),  # pearlhash event-driven, ~hourly matured
    XMR_POOL: timedelta(hours=2),  # supportxmr pays ~every 2h
    SCRYPT_POOL: timedelta(hours=24),  # LTC (F2Pool) pays daily
}

#: The mining lanes (everything that is NOT the AI lane). Used to route a lane to
#: its window policy + the mining (revenue-pro-rata) finalization path.
MINING_LANES: tuple[Lane, ...] = (
    MAIN_POOL_GPU_RVN,
    MAIN_POOL_GPU_QUAI,
    MAIN_POOL_GPU_PRL,
    XMR_POOL,
    SCRYPT_POOL,
)


def verification_window_for_lane(lane: Lane) -> timedelta:
    """The full verification/clawback window duration for ``lane`` (§2).

    AI -> the fixed 8h window. A mining lane -> its upstream payout cadence +
    :data:`MINING_RECONCILE_LAG` (2h). Raises for an unknown lane (fail-closed: we
    never invent a window — an unrecognised lane has no defined clawback period and
    must not silently finalize early).
    """
    if lane == MAIN_POOL_AI:
        return AI_VERIFICATION_WINDOW
    cadence = MINING_UPSTREAM_CADENCE.get(lane)
    if cadence is None:
        raise ValueError(f"no verification window defined for lane: {lane!r}")
    return cadence + MINING_RECONCILE_LAG


@dataclass(frozen=True, slots=True)
class CreditWindow:
    """The verification/clawback window for one lane lot — the finalize SEAL spine.

    A lot's window OPENS at :attr:`opened_at` (the work's ``observed_at``) and
    CLOSES at ``opened_at + duration``. Credit inside the window is still
    clawback-able (PROVISIONAL); once :meth:`is_closed_at` is True the window has
    closed and the credit may finalize. The PAYOUT-LAGS-WINDOW SEAL is enforced by
    every finalize path asserting :meth:`is_closed_at` first.
    """

    lane: Lane
    opened_at: datetime
    duration: timedelta

    def __post_init__(self) -> None:
        validate_aware_timestamp("opened_at", self.opened_at)
        if self.duration <= timedelta(0):
            raise ValueError("verification window duration must be positive")

    @property
    def closes_at(self) -> datetime:
        """The instant the verification window closes (finalize becomes possible)."""
        return self.opened_at + self.duration

    def is_open_at(self, now: datetime) -> bool:
        """True while the lot is still inside its clawback window (NOT finalizable)."""
        validate_aware_timestamp("now", now)
        return now < self.closes_at

    def is_closed_at(self, now: datetime) -> bool:
        """True once the window has closed — the finalize SEAL's precondition.

        Finalize/payout must LAG the window: this returns True iff ``now`` is at or
        past :attr:`closes_at`. Credit still inside its window (``is_open_at``)
        returns False here and can NEVER be finalized through this engine.
        """
        return not self.is_open_at(now)

    def to_public_dict(self) -> dict[str, object]:
        return {
            "lane": self.lane,
            "opened_at": self.opened_at.isoformat(),
            "duration_seconds": self.duration.total_seconds(),
            "closes_at": self.closes_at.isoformat(),
        }


def window_for(lane: Lane, observed_at: datetime) -> CreditWindow:
    """Build the verification window for a lot of ``lane`` work observed at ``observed_at``."""
    return CreditWindow(
        lane=lane,
        opened_at=observed_at,
        duration=verification_window_for_lane(lane),
    )


@dataclass(frozen=True, slots=True)
class ProvisionalCredit:
    """One unit of PROVISIONAL, per-device credit awaiting its window to close.

    This is the credit-cycle's input: a ``(passport_id, device_id)`` (the M5
    per-device measurement unit) on a ``lane``, with the ``provisional_credit`` the
    Route-1 peg / mining accounting already sized, observed at ``observed_at`` (the
    instant that opens its verification window). ``alice_address`` is the credit /
    payout identity the device's credit AGGREGATES UP TO (§6: address = payout unit,
    device = trust/measurement unit). Credit-only: ``paid_acu`` is ``"0"``.
    """

    credit_id: str
    alice_address: str
    passport_id: str
    device_id: str
    lane: Lane
    provisional_credit: Decimal
    observed_at: datetime
    state: str = STATE_PROVISIONAL
    paid_acu: Decimal = ZERO

    def __post_init__(self) -> None:
        for field_name, value in (
            ("credit_id", self.credit_id),
            ("alice_address", self.alice_address),
            ("passport_id", self.passport_id),
            ("device_id", self.device_id),
        ):
            validate_public_identifier(field_name, value)
        validate_aware_timestamp("observed_at", self.observed_at)
        if not isinstance(self.provisional_credit, Decimal):
            raise TypeError("provisional_credit_must_be_decimal")
        if self.provisional_credit < ZERO:
            raise ValueError("provisional_credit_must_be_non_negative")
        if self.state not in (STATE_PROVISIONAL, STATE_FINALIZED, STATE_CLAWED_BACK):
            raise ValueError(f"unsupported credit state: {self.state!r}")
        # CREDIT-ONLY: a provisional lot is never paid. paid_acu stays 0.
        if self.paid_acu != ZERO:
            raise ValueError("reward_cycle_provisional_paid_acu_must_remain_zero")

    @property
    def device_key(self) -> tuple[str, str]:
        return (self.passport_id, self.device_id)

    def window(self) -> CreditWindow:
        """This lot's verification window (opens at :attr:`observed_at`)."""
        return window_for(self.lane, self.observed_at)

    def is_finalizable_at(self, now: datetime) -> bool:
        """True iff the lot is PROVISIONAL and its window has CLOSED at ``now``.

        The finalize SEAL in one predicate: a clawed-back lot is never finalizable,
        an already-finalized lot is not re-finalizable, and a still-open window
        (inside the clawback period) is NOT finalizable.
        """
        if self.state != STATE_PROVISIONAL:
            return False
        return self.window().is_closed_at(now)

    def to_public_dict(self) -> dict[str, object]:
        return {
            "credit_id": self.credit_id,
            "alice_address": self.alice_address,
            "passport_id": self.passport_id,
            "device_id": self.device_id,
            "lane": self.lane,
            "provisional_credit": str(self.provisional_credit),
            "state": self.state,
            "window": self.window().to_public_dict(),
            "paid_acu": "0",
        }


@dataclass(frozen=True, slots=True)
class RewardCycleConfig:
    """Engine configuration — the PHASE-J gate lives here and DEFAULTS OFF.

    ``finalize_to_payout_enabled`` is the single flag that would later let a
    FINALIZED credit lot flow to a real payout. It DEFAULTS ``False`` and the
    engine REFUSES to construct with it True (fail-closed): this build computes
    finalized credit only, off-chain, and ``paid_acu`` stays ``"0"`` regardless.
    Phase-J is a deliberate flip of this flag against the built machinery — never an
    implicit default and never reachable from this milestone.
    """

    finalize_to_payout_enabled: bool = False
    contract_version: str = REWARD_CYCLE_VERSION

    def __post_init__(self) -> None:
        validate_public_identifier("contract_version", self.contract_version)
        # PHASE-J SEAL: the finalize->payout flag must stay OFF in this build. The
        # engine never enables a real payout; a True here is a configuration error.
        if self.finalize_to_payout_enabled:
            raise ValueError("reward_cycle_finalize_to_payout_must_remain_disabled")


@dataclass(frozen=True, slots=True)
class DeviceFinalization:
    """Per-device finalization outcome that aggregates up to the Alice address (§6).

    ``finalized_credit`` is what the device's credit finalizes to AFTER the
    revenue-bound / cap clamp; ``clawback_credit`` is the recorded (unpaid)
    shortfall (``provisional - finalized``). ``alice_address`` is the address the
    device's credit rolls up to. Credit-only: ``paid_acu`` is ``"0"``.
    """

    alice_address: str
    passport_id: str
    device_id: str
    provisional_credit: Decimal
    finalized_credit: Decimal
    clawback_credit: Decimal

    @property
    def device_key(self) -> tuple[str, str]:
        return (self.passport_id, self.device_id)

    def to_public_dict(self) -> dict[str, object]:
        return {
            "alice_address": self.alice_address,
            "passport_id": self.passport_id,
            "device_id": self.device_id,
            "provisional_credit": str(self.provisional_credit),
            "finalized_credit": str(self.finalized_credit),
            "clawback_credit": str(self.clawback_credit),
            "paid_acu": "0",
        }


@dataclass(frozen=True, slots=True)
class LaneFinalization:
    """The finalization of one lane at one boundary (mining or AI).

    Holds the per-device finalized/clawback outcomes, the per-Alice-address
    aggregate (§6: credit aggregates UP TO the address), and the lane-level totals.
    ``finalize_to_payout_enabled`` is echoed (always ``False`` in this build) and
    ``paid_acu`` is ``"0"`` — the finalize is OFF-CHAIN credit only. ``revenue_bound``
    is the actual upstream revenue the mining distribution was bounded by (``None``
    for the AI lane, which is bounded by the 0.50 cap reconciliation instead).
    """

    lane: Lane
    reason_code: str
    per_device: tuple[DeviceFinalization, ...]
    total_provisional: Decimal
    total_finalized: Decimal
    total_clawback: Decimal
    revenue_bound: Decimal | None = None
    ai_reconciliation: AiCapReconciliation | None = None
    finalize_to_payout_enabled: bool = False
    paid_acu: Decimal = ZERO

    def __post_init__(self) -> None:
        # CREDIT-ONLY + PHASE-J: the finalize is off-chain credit; the payout flag
        # is OFF and nothing is paid, asserted structurally.
        if self.finalize_to_payout_enabled:
            raise ValueError("reward_cycle_finalize_to_payout_must_remain_disabled")
        if self.paid_acu != ZERO:
            raise ValueError("reward_cycle_finalization_paid_acu_must_remain_zero")

    def finalized_by_address(self) -> dict[str, Decimal]:
        """Finalized credit AGGREGATED per Alice address (§6: device -> address)."""
        rollup: dict[str, Decimal] = {}
        for d in self.per_device:
            rollup[d.alice_address] = rollup.get(d.alice_address, ZERO) + d.finalized_credit
        return rollup

    def to_public_dict(self) -> dict[str, object]:
        return {
            "contract_version": REWARD_CYCLE_VERSION,
            "lane": self.lane,
            "reason_code": self.reason_code,
            "total_provisional": str(self.total_provisional),
            "total_finalized": str(self.total_finalized),
            "total_clawback": str(self.total_clawback),
            "revenue_bound": str(self.revenue_bound) if self.revenue_bound is not None else None,
            "finalized_by_address": {
                addr: str(amount) for addr, amount in self.finalized_by_address().items()
            },
            "per_device": [d.to_public_dict() for d in self.per_device],
            # PHASE-J / CREDIT-ONLY: off-chain finalized credit, nothing paid.
            "finalize_to_payout_enabled": False,
            "live_reward_enabled": False,
            "payout_executor_enabled": False,
            "paid_acu": "0",
        }


def _assert_window_closed(credits: Iterable[ProvisionalCredit], now: datetime) -> None:
    """THE PAYOUT-LAGS-WINDOW SEAL: refuse to finalize any lot still inside its window.

    Every finalize path calls this FIRST. If ANY supplied lot's verification window
    has not yet closed at ``now``, we raise loudly rather than finalize early —
    credit inside its clawback window can NEVER be finalized or paid (§2/§7). This
    is fail-closed: a single not-yet-closed lot blocks the whole boundary, forcing
    the caller to select only closed lots (see :func:`select_finalizable`).
    """
    for credit in credits:
        if credit.state != STATE_PROVISIONAL:
            continue
        if credit.window().is_open_at(now):
            raise AssertionError(
                "payout-lags-window seal: cannot finalize credit still inside its "
                f"verification window (credit_id={credit.credit_id}, lane={credit.lane}, "
                f"closes_at={credit.window().closes_at.isoformat()}, now={now.isoformat()})"
            )


def select_finalizable(
    credits: Iterable[ProvisionalCredit],
    *,
    lane: Lane,
    now: datetime,
) -> tuple[ProvisionalCredit, ...]:
    """The PROVISIONAL lots of ``lane`` whose window has CLOSED at ``now``.

    The standard way to feed a finalize path safely: it drops lots still inside
    their clawback window (honouring the seal) and lots not in PROVISIONAL state
    (already finalized / clawed back), keeping only this lane's now-finalizable
    lots. Deterministic order (by ``credit_id``) so finalization is reproducible.
    """
    selected = [
        c
        for c in credits
        if c.lane == lane and c.is_finalizable_at(now)
    ]
    return tuple(sorted(selected, key=lambda c: c.credit_id))


def finalize_mining_lane(
    credits: Iterable[ProvisionalCredit],
    *,
    lane: Lane,
    actual_upstream_revenue: Decimal,
    now: datetime,
    config: RewardCycleConfig | None = None,
) -> LaneFinalization:
    """Finalize a mining lane: distribute ACTUAL upstream revenue pro-rata by credit.

    Mining finalization (§2): once the lane's window has closed (upstream cadence +
    2h), distribute the upstream revenue that ACTUALLY landed in Alice's account
    PRO-RATA by each device's re-hash credit, REVENUE-BOUNDED — Alice never
    finalizes more than the upstream paid it. The shortfall vs each device's
    provisional credit is recorded as clawback (unpaid). Finalized credit
    AGGREGATES UP TO the Alice address (§6).

    Math, per the plan:

      * ``total_provisional = sum(provisional_i)`` over this lane's finalizable lots
      * if ``actual_upstream_revenue >= total_provisional`` -> every device finalizes
        its FULL provisional credit (revenue covers the re-hash credit; no clamp)
      * else -> ``factor = actual_upstream_revenue / total_provisional`` and
        ``finalized_i = provisional_i * factor`` (pro-rata, revenue-bounded);
        ``clawback_i = provisional_i - finalized_i`` (recorded, unpaid).

    Bounds: ``total_finalized <= min(total_provisional, actual_upstream_revenue)``.
    The SEAL is enforced first (raises if any lot's window is still open). The given
    ``credits`` SHOULD already be this lane's finalizable lots (see
    :func:`select_finalizable`); a foreign-lane lot is ignored. Credit-only:
    ``paid_acu`` stays ``"0"`` and ``finalize_to_payout_enabled`` is OFF.
    """
    cfg = config or RewardCycleConfig()
    if lane == MAIN_POOL_AI:
        raise ValueError("finalize_mining_lane is for mining lanes; use finalize_ai_epoch")
    if lane not in MINING_LANES:
        raise ValueError(f"unsupported mining lane: {lane!r}")
    if not isinstance(actual_upstream_revenue, Decimal):
        raise TypeError("actual_upstream_revenue_must_be_decimal")
    if actual_upstream_revenue < ZERO:
        raise ValueError("actual_upstream_revenue_must_be_non_negative")

    lane_credits = tuple(c for c in credits if c.lane == lane)
    # SEAL: nothing inside its window may finalize.
    _assert_window_closed(lane_credits, now)
    finalizable = tuple(
        sorted(
            (c for c in lane_credits if c.is_finalizable_at(now)),
            key=lambda c: c.credit_id,
        )
    )

    total_provisional = sum((c.provisional_credit for c in finalizable), ZERO)
    revenue_bounds = actual_upstream_revenue < total_provisional
    if total_provisional <= ZERO or not revenue_bounds:
        # Revenue covers the credit (or there is no credit): full provisional
        # finalizes, no clawback. (Below the bound the claim "credit == re-hash
        # work" holds exactly; the revenue simply caps it and here it does not bind.)
        factor = Decimal("1")
    else:
        factor = actual_upstream_revenue / total_provisional

    per_device: list[DeviceFinalization] = []
    total_finalized = ZERO
    for credit in finalizable:
        if revenue_bounds and total_provisional > ZERO:
            finalized = (credit.provisional_credit * factor).quantize(CREDIT_QUANT)
            # Never finalize ABOVE the provisional entitlement (guard the quantize edge).
            if finalized > credit.provisional_credit:
                finalized = credit.provisional_credit
        else:
            finalized = credit.provisional_credit
        clawback = credit.provisional_credit - finalized
        per_device.append(
            DeviceFinalization(
                alice_address=credit.alice_address,
                passport_id=credit.passport_id,
                device_id=credit.device_id,
                provisional_credit=credit.provisional_credit,
                finalized_credit=finalized,
                clawback_credit=clawback,
            )
        )
        total_finalized += finalized

    # Fail-closed: the finalized total can never exceed the actual upstream revenue
    # NOR the total provisional (rounding could in principle nudge it; clamp the
    # reported total so REVENUE is a hard ceiling — Alice never pays more than it was paid).
    if revenue_bounds:
        ceiling = min(total_provisional, actual_upstream_revenue)
    else:
        ceiling = total_provisional
    if total_finalized > ceiling:
        total_finalized = ceiling
    total_clawback = total_provisional - total_finalized
    if total_clawback < ZERO:
        total_clawback = ZERO
    return LaneFinalization(
        lane=lane,
        reason_code=REASON_REVENUE_BOUNDED,
        per_device=tuple(per_device),
        total_provisional=total_provisional,
        total_finalized=total_finalized,
        total_clawback=total_clawback,
        revenue_bound=actual_upstream_revenue,
        finalize_to_payout_enabled=cfg.finalize_to_payout_enabled,
    )


def finalize_ai_epoch(
    credits: Iterable[ProvisionalCredit],
    *,
    ai_lane_budget: Decimal,
    now: datetime,
    config: RewardCycleConfig | None = None,
) -> LaneFinalization:
    """Finalize the AI lane at the 8h epoch boundary via the M1 cap reconciliation.

    AI finalization (§2): at the 8h epoch boundary, take each device's Route-1
    PROVISIONAL credit and reconcile the AGGREGATE against the 0.50 AI lane budget
    by invoking M1's
    :func:`~alice_acp.shadow_server.pool_budget.reconcile_ai_credit_against_cap`
    (clamp to the cap pro-rata, record the clawback). Below the cap every device
    finalizes its full Route-1 credit; at/above the cap all are scaled by the same
    factor and the shortfall is recorded. Finalized credit AGGREGATES UP TO the
    Alice address (§6).

    The SEAL is enforced first: only this lane's lots whose 8h window has CLOSED are
    reconciled (a lot still inside its 8h window cannot be finalized — raises if any
    open lot is passed; use :func:`select_finalizable` to pre-filter). Credit-only:
    the M1 reconciliation already keeps ``paid_acu`` ``"0"``; this wrapper carries
    that through and keeps ``finalize_to_payout_enabled`` OFF.
    """
    cfg = config or RewardCycleConfig()
    ai_credits = tuple(c for c in credits if c.lane == MAIN_POOL_AI)
    # SEAL: nothing inside its 8h window may finalize.
    _assert_window_closed(ai_credits, now)
    finalizable = tuple(c for c in ai_credits if c.is_finalizable_at(now))

    # Aggregate this epoch's provisional Route-1 credit per device (the M1
    # reconciliation's input). A device with multiple lots in the epoch sums.
    provisional_by_device: dict[tuple[str, str], Decimal] = {}
    address_by_device: dict[tuple[str, str], str] = {}
    for credit in finalizable:
        key = credit.device_key
        provisional_by_device[key] = (
            provisional_by_device.get(key, ZERO) + credit.provisional_credit
        )
        # The address a device rolls up to (consistent across a device's lots).
        address_by_device[key] = credit.alice_address

    reconciliation = reconcile_ai_credit_against_cap(
        ai_lane_budget=ai_lane_budget,
        provisional_credit_by_device=provisional_by_device,
    )

    per_device: list[DeviceFinalization] = []
    for r in reconciliation.per_device:
        key = (r.passport_id, r.device_id)
        per_device.append(
            DeviceFinalization(
                alice_address=address_by_device[key],
                passport_id=r.passport_id,
                device_id=r.device_id,
                provisional_credit=r.provisional_credit,
                finalized_credit=r.finalized_credit,
                clawback_credit=r.clawback_credit,
            )
        )
    return LaneFinalization(
        lane=MAIN_POOL_AI,
        reason_code=REASON_AI_CAP_RECONCILED,
        per_device=tuple(per_device),
        total_provisional=reconciliation.aggregate_provisional,
        total_finalized=reconciliation.total_finalized,
        total_clawback=reconciliation.total_clawback,
        revenue_bound=None,
        ai_reconciliation=reconciliation,
        finalize_to_payout_enabled=cfg.finalize_to_payout_enabled,
    )


# --------------------------------------------------------------------------- #
# A small in-memory credit-cycle ledger that holds provisional lots, applies a
# clawback WITHIN the window (the fraud path), and drives the per-lane finalize at
# a boundary. The deployed edge would back this with the shadow ledger; the
# in-memory store keeps the cycle deterministically testable and credit-only.
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class RewardCycleLedger:
    """Holds provisional credit lots and drives the provisional->finalized cycle.

    The credit-cycle's small state machine: lots are recorded PROVISIONAL; a
    clawback WITHIN the window flips a lot to ``clawed_back`` (so it drops out of
    every future finalization — exactly mirroring the M8 verification-VPS clawback,
    but here at the credit-lot grain); at a boundary the finalize path consumes only
    lots whose window has CLOSED (the seal) and flips them to ``finalized``.

    CREDIT-ONLY: every lot keeps ``paid_acu == "0"``; a clawback removes only
    provisional credit; finalize computes off-chain finalized credit. The Phase-J
    payout flag (:attr:`RewardCycleConfig.finalize_to_payout_enabled`) stays OFF.
    """

    config: RewardCycleConfig = field(default_factory=RewardCycleConfig)
    _lots: dict[str, ProvisionalCredit] = field(default_factory=dict)

    def record_provisional(self, credit: ProvisionalCredit) -> ProvisionalCredit:
        """Record a PROVISIONAL credit lot (idempotent on ``credit_id``).

        Re-recording the same ``credit_id`` is a no-op that returns the stored lot
        (so a duplicated feed never double-counts). A lot is always stored in
        PROVISIONAL state; ``paid_acu`` is asserted ``"0"`` by the dataclass.
        """
        if credit.state != STATE_PROVISIONAL:
            raise ValueError("only provisional credit may be recorded")
        existing = self._lots.get(credit.credit_id)
        if existing is not None:
            return existing
        self._lots[credit.credit_id] = credit
        return credit

    def clawback(self, credit_id: str, *, now: datetime) -> ProvisionalCredit:
        """Claw back a lot WITHIN its window (the fraud path) — reverses provisional credit.

        Flips the lot to ``clawed_back`` so it is excluded from every future
        finalization. HARD SEAL: a clawback is only valid while the lot is still
        INSIDE its verification window (``now < closes_at``) — once the window has
        closed the lot is finalizable and a "clawback" would be reversing
        (eventually) PAID value, which we never do; we raise instead. Idempotent on
        an already-clawed-back lot. Credit-only: nothing was ever paid
        (``paid_acu == "0"``), so this only removes provisional credit.
        """
        validate_public_identifier("credit_id", credit_id)
        validate_aware_timestamp("now", now)
        lot = self._lots.get(credit_id)
        if lot is None:
            raise KeyError(f"unknown credit_id: {credit_id!r}")
        if lot.state == STATE_CLAWED_BACK:
            return lot
        if lot.state == STATE_FINALIZED:
            raise AssertionError(
                "clawback refused: lot already finalized (clawback must occur INSIDE "
                f"the verification window); credit_id={credit_id}"
            )
        # HARD SEAL: a clawback must be inside the window. Past the window close the
        # lot is finalizable; reversing it then would break the payout-lags-window
        # guarantee. The clawback window IS the open verification window (§2).
        if not lot.window().is_open_at(now):
            raise AssertionError(
                "clawback refused: verification window already closed; a closed-window "
                f"lot finalizes, it is not clawed back. credit_id={credit_id}, "
                f"closes_at={lot.window().closes_at.isoformat()}, now={now.isoformat()}"
            )
        # CREDIT-ONLY: nothing was ever paid; we only remove provisional credit.
        if lot.paid_acu != ZERO:
            raise AssertionError("clawback refused: paid_acu must be 0 (credit-only)")
        clawed = ProvisionalCredit(
            credit_id=lot.credit_id,
            alice_address=lot.alice_address,
            passport_id=lot.passport_id,
            device_id=lot.device_id,
            lane=lot.lane,
            provisional_credit=lot.provisional_credit,
            observed_at=lot.observed_at,
            state=STATE_CLAWED_BACK,
        )
        self._lots[credit_id] = clawed
        return clawed

    def provisional_lots(self, *, lane: Lane | None = None) -> tuple[ProvisionalCredit, ...]:
        """All currently-PROVISIONAL lots (optionally filtered to one lane)."""
        lots = [c for c in self._lots.values() if c.state == STATE_PROVISIONAL]
        if lane is not None:
            lots = [c for c in lots if c.lane == lane]
        return tuple(sorted(lots, key=lambda c: c.credit_id))

    def finalize_mining(
        self,
        *,
        lane: Lane,
        actual_upstream_revenue: Decimal,
        now: datetime,
    ) -> LaneFinalization:
        """Finalize a mining lane at a boundary, flipping its closed-window lots to FINALIZED.

        Selects this lane's now-finalizable lots (window closed — the seal),
        finalizes them revenue-bounded pro-rata via :func:`finalize_mining_lane`,
        and flips each consumed lot's state to ``finalized``. Credit-only.
        """
        finalizable = select_finalizable(self._lots.values(), lane=lane, now=now)
        result = finalize_mining_lane(
            finalizable,
            lane=lane,
            actual_upstream_revenue=actual_upstream_revenue,
            now=now,
            config=self.config,
        )
        self._mark_finalized(finalizable)
        return result

    def finalize_ai(
        self,
        *,
        ai_lane_budget: Decimal,
        now: datetime,
    ) -> LaneFinalization:
        """Finalize the AI lane at the 8h epoch boundary, flipping closed-window lots to FINALIZED.

        Selects the AI lane's now-finalizable lots (8h window closed — the seal),
        reconciles them against the 0.50 cap via :func:`finalize_ai_epoch`, and flips
        each consumed lot's state to ``finalized``. Credit-only.
        """
        finalizable = select_finalizable(self._lots.values(), lane=MAIN_POOL_AI, now=now)
        result = finalize_ai_epoch(
            finalizable,
            ai_lane_budget=ai_lane_budget,
            now=now,
            config=self.config,
        )
        self._mark_finalized(finalizable)
        return result

    def _mark_finalized(self, lots: Iterable[ProvisionalCredit]) -> None:
        for lot in lots:
            current = self._lots.get(lot.credit_id)
            if current is None or current.state != STATE_PROVISIONAL:
                continue
            self._lots[lot.credit_id] = ProvisionalCredit(
                credit_id=current.credit_id,
                alice_address=current.alice_address,
                passport_id=current.passport_id,
                device_id=current.device_id,
                lane=current.lane,
                provisional_credit=current.provisional_credit,
                observed_at=current.observed_at,
                state=STATE_FINALIZED,
            )

    def to_public_dict(self) -> dict[str, object]:
        """Redacted health view: per-state lot COUNTS. Credit-only; paid_acu '0'."""
        states: dict[str, int] = {
            STATE_PROVISIONAL: 0,
            STATE_FINALIZED: 0,
            STATE_CLAWED_BACK: 0,
        }
        for lot in self._lots.values():
            states[lot.state] = states.get(lot.state, 0) + 1
        return {
            "contract_version": REWARD_CYCLE_VERSION,
            "lot_counts": states,
            "finalize_to_payout_enabled": False,
            "live_reward_enabled": False,
            "payout_executor_enabled": False,
            "paid_acu": "0",
        }


def aggregate_finalized_to_address(
    finalizations: Iterable[LaneFinalization],
) -> dict[str, Decimal]:
    """Roll finalized credit across lanes UP TO each Alice address (§6).

    The address is the payout/identity unit; the device is the trust/measurement
    unit (§6). This sums every lane's per-device finalized credit by Alice address
    so a multi-lane, multi-device address sees one finalized total. Credit-only
    (these are off-chain finalized-credit numbers; nothing is paid).
    """
    rollup: dict[str, Decimal] = {}
    for fin in finalizations:
        for address, amount in fin.finalized_by_address().items():
            rollup[address] = rollup.get(address, ZERO) + amount
    return rollup

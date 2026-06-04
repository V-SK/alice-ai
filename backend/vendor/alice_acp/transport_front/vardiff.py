"""Per-connection vardiff — difficulty adjustment with ``d=<diff>`` as the seed/floor.

Stratum pools tune each connection's share difficulty so a rig submits at a
roughly constant *share rate* regardless of its hashrate: too-fast a rate wastes
bandwidth/CPU on tiny shares, too-slow gives coarse, high-variance crediting.
This is the standard pool ``vardiff`` retarget, made PURE + per-connection here
(doc §2.1 / §2 "vardiff"):

* The login password's ``d=<diff>`` token is the SEED + FLOOR (doc §2.1): the
  rig asks to start at (and never drop below) ``<diff>``. A miner that knows its
  rig sets a sensible floor; the front never goes under it.
* On each accepted share the connection records the inter-share interval into a
  bounded WINDOW. A retarget fires only PERIODICALLY — when the window fills (fast
  initial convergence) or a retarget interval has elapsed (steady state) — and
  compares the window-AVERAGE interval against the target. It changes difficulty
  ONLY when the average is OUTSIDE a tolerance band around the target (a rate that
  is "close enough" is left alone), then multiplies toward the target (clamped to a
  per-step factor), bounded by ``[floor, max]``.

  This windowed + banded + periodic shape is what keeps vardiff STABLE. Retargeting
  every share on a single raw interval — as a naive implementation does — *hunts*
  and oscillates, because share arrivals are Poisson-random: consecutive intervals
  vary wildly even at a perfectly-tuned difficulty, and a per-share retarget
  amplifies that noise instead of averaging it out. Between retargets the rig sees
  a stable difficulty, so it stops lagging behind a constantly-moving target.

This is purely a TRANSPORT/UX knob: it sets the ``pool_target_difficulty`` the
front hands DOWN to the rig and into the validator's classification. It is NOT a
credit-authority input — the CREDITED magnitude is always Alice's recomputed
``result_difficulty`` (``share_validator``; SELF-REPORT NEVER COUNTS), and the
vardiff value never becomes a credit. A future asyncio server calls
:meth:`Vardiff.on_share` on each accepted share and pushes
``mining.set_difficulty`` when :meth:`Vardiff.current` changes.

CREDIT-ONLY / fail-closed: vardiff sets no reward/payout/chain symbol; a garbage
``d=`` token is ignored (the connection falls back to the configured default
floor), never an error that drops the connection.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation

#: Default starting/floor difficulty when the password carries no usable ``d=``.
DEFAULT_VARDIFF_FLOOR = Decimal("1")
#: The "no hard minimum" sentinel for :meth:`Vardiff.from_password`'s ``min_floor``.
#: A per-connection vardiff floor is always ``> 0`` (``__post_init__`` enforces it), and
#: a login's ``d=`` seed is always ``> 0`` (``parse_password_difficulty`` rejects ``<=0``),
#: so a ``min_floor`` of ``0`` can NEVER clamp a legitimate seed UP — it is a true no-op.
#: This is the right default for lanes WITHOUT a hard floor (RandomX/KawPoW), and crucially
#: it does NOT clamp legitimate SUB-1 difficulties (the Scrypt-stratum scale uses values
#: like ``d=0.008``) the way a ``min_floor`` of ``1`` would.
NO_VARDIFF_FLOOR_CLAMP = Decimal("0")
#: Default target seconds between accepted shares (the retarget aims for this).
DEFAULT_TARGET_SHARE_INTERVAL = timedelta(seconds=15)
#: Minimum seconds between retargets at steady state. The rig sees a STABLE
#: difficulty for at least this long between changes (so it never lags a
#: constantly-moving target). A full sample window can trigger a retarget sooner
#: (fast initial convergence) — see :data:`DEFAULT_RETARGET_SAMPLES`.
DEFAULT_RETARGET_INTERVAL = timedelta(seconds=90)
#: Tolerance band (fraction of the target interval): if the window-average share
#: interval is within ±this of the target, difficulty is LEFT ALONE. This band is
#: the core anti-hunting feature — without it, ordinary Poisson variance around a
#: perfectly-tuned difficulty would trigger endless back-and-forth retargets.
DEFAULT_VARIANCE_FRACTION = Decimal("0.3")
#: Sample-window size: the retarget averages this many recent inter-share
#: intervals (smoothing Poisson noise), and a full window also forces an early
#: retarget so a fresh/low connection converges quickly instead of waiting a full
#: retarget interval.
DEFAULT_RETARGET_SAMPLES = 8
#: Per-retarget clamp: difficulty may change by at most this factor per step
#: (so one unusual window can never swing difficulty wildly).
DEFAULT_MAX_STEP_FACTOR = Decimal("4")
#: A generous absolute ceiling so a runaway retarget can never overflow.
DEFAULT_VARDIFF_MAX = Decimal("1000000000000")


def parse_password_difficulty(password: str | None) -> Decimal | None:
    """Extract the ``d=<diff>`` seed/floor from a login password (doc §2.1).

    The stratum password convention packs optional ``key=value`` tokens separated
    by ``;`` (or whitespace/``,``). We read the ``d`` token as a positive
    :class:`Decimal`. Returns ``None`` (caller falls back to the default floor)
    when the password is empty, carries no ``d`` token, or the value is
    non-numeric / non-positive — fail-soft, never an error.
    """

    if not password:
        return None
    # Split on the common stratum separators; tolerate "d=1024", "x=1;d=512", etc.
    for raw_token in password.replace(",", ";").replace(" ", ";").split(";"):
        token = raw_token.strip()
        if not token or "=" not in token:
            continue
        key, _, value = token.partition("=")
        if key.strip().lower() != "d":
            continue
        try:
            parsed = Decimal(value.strip())
        except (InvalidOperation, ValueError):
            return None
        if parsed <= 0:
            return None
        return parsed
    return None


@dataclass(slots=True)
class Vardiff:
    """Per-connection vardiff state machine (pure; no I/O).

    Construct one per connection from the login password's ``d=`` seed (the floor)
    plus the front's policy (target interval, retarget cadence, tolerance band,
    bounds). Call :meth:`on_share` on each accepted share with its timestamp; it
    accumulates the interval and retargets periodically, returning the (possibly
    new) difficulty. :meth:`current` is the difficulty to hand the rig + use as the
    validator's ``pool_target_difficulty``.
    """

    floor: Decimal = DEFAULT_VARDIFF_FLOOR
    target_interval: timedelta = DEFAULT_TARGET_SHARE_INTERVAL
    retarget_interval: timedelta = DEFAULT_RETARGET_INTERVAL
    variance_fraction: Decimal = DEFAULT_VARIANCE_FRACTION
    retarget_samples: int = DEFAULT_RETARGET_SAMPLES
    max_step_factor: Decimal = DEFAULT_MAX_STEP_FACTOR
    maximum: Decimal = DEFAULT_VARDIFF_MAX
    _current: Decimal = field(default=Decimal("0"))
    _last_share_at: datetime | None = field(default=None)
    _last_retarget_at: datetime | None = field(default=None)
    _intervals: list[float] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self.floor <= 0:
            raise ValueError("vardiff floor must be positive")
        if self.maximum < self.floor:
            raise ValueError("vardiff maximum must be >= floor")
        if self.max_step_factor <= 1:
            raise ValueError("vardiff max_step_factor must be > 1")
        if self.target_interval.total_seconds() <= 0:
            raise ValueError("vardiff target_interval must be positive")
        if self.retarget_interval.total_seconds() <= 0:
            raise ValueError("vardiff retarget_interval must be positive")
        if not (0 <= self.variance_fraction < 1):
            raise ValueError("vardiff variance_fraction must be in [0, 1)")
        if self.retarget_samples < 1:
            raise ValueError("vardiff retarget_samples must be >= 1")
        # Start AT the floor (the seed): the rig begins at the difficulty it asked
        # for and the front only ever moves it within [floor, maximum].
        if self._current <= 0:
            self._current = self.floor

    @classmethod
    def from_password(
        cls,
        password: str | None,
        *,
        default_floor: Decimal = DEFAULT_VARDIFF_FLOOR,
        min_floor: Decimal = NO_VARDIFF_FLOOR_CLAMP,
        target_interval: timedelta = DEFAULT_TARGET_SHARE_INTERVAL,
        retarget_interval: timedelta = DEFAULT_RETARGET_INTERVAL,
        variance_fraction: Decimal = DEFAULT_VARIANCE_FRACTION,
        retarget_samples: int = DEFAULT_RETARGET_SAMPLES,
        max_step_factor: Decimal = DEFAULT_MAX_STEP_FACTOR,
        maximum: Decimal = DEFAULT_VARDIFF_MAX,
    ) -> Vardiff:
        """Build vardiff from the password (``d=`` is the seed; ``min_floor`` is a hard floor).

        A usable ``d=<diff>`` becomes the seed (and the starting difficulty);
        otherwise ``default_floor`` is used (fail-soft). The resulting floor is then
        CLAMPED UP to ``min_floor`` — a per-lane HARD MINIMUM that a login's ``d=``
        may RAISE but can NEVER lower below. This closes the ``d=1`` flood bypass: on
        a lane with a non-trivial ``min_floor`` (the public LTC/Scrypt lane =
        ``16384``) a hostile ``d=1`` login is clamped to the lane minimum instead of
        starting at ``d=1`` and flooding the re-hash validator with sub-target shares
        before vardiff converges.

        ``min_floor`` defaults to :data:`NO_VARDIFF_FLOOR_CLAMP` (=0), i.e. NO clamp —
        the correct behaviour for CPU/GPU lanes (RandomX/KawPoW) and for any caller that
        passes only ``default_floor``. A ``0`` minimum never clamps a legitimate seed
        (seeds are always ``> 0``) AND, unlike a ``1`` minimum, does not wrongly raise a
        legitimate SUB-1 ``d=`` (the Scrypt-stratum scale uses values like ``d=0.008``).
        The floor is never exceeded downward by any retarget (``__post_init__`` enforces
        ``floor > 0``).
        """

        seed = parse_password_difficulty(password)
        floor = seed if seed is not None else default_floor
        # HARD MINIMUM: a login's d= may raise the floor but never lower it below the
        # per-lane minimum. ``min_floor`` defaults to 0 (NO clamp) for CPU/GPU lanes.
        if floor < min_floor:
            floor = min_floor
        return cls(
            floor=floor,
            target_interval=target_interval,
            retarget_interval=retarget_interval,
            variance_fraction=variance_fraction,
            retarget_samples=retarget_samples,
            max_step_factor=max_step_factor,
            maximum=maximum,
        )

    @property
    def current(self) -> Decimal:
        """The current per-connection difficulty (the rig's pool target)."""

        return self._current

    def on_share(self, *, at: datetime) -> Decimal:
        """Record an accepted share at ``at`` and maybe retarget; return the difficulty.

        The first share only seeds the clocks (no interval yet). Each subsequent
        share appends its inter-share interval to a bounded window. A retarget is
        EVALUATED only when the window is full (fast initial convergence) or the
        retarget interval has elapsed — never on every share. The evaluation
        averages the window: if the average is within the tolerance band around the
        target it leaves difficulty unchanged (no churn); otherwise it multiplies
        difficulty toward the target (clamped to ``max_step_factor``) bounded by
        ``[floor, maximum]``. A non-monotonic / zero interval is ignored so a clock
        glitch never corrupts state.
        """

        previous = self._last_share_at
        self._last_share_at = at
        if previous is None:
            # First share: seed both the share clock and the retarget clock.
            self._last_retarget_at = at
            return self._current
        interval = (at - previous).total_seconds()
        if interval <= 0:
            # Out-of-order / identical timestamps: do not record or retarget.
            return self._current
        self._intervals.append(interval)
        if len(self._intervals) > self.retarget_samples:
            self._intervals.pop(0)
        # Retarget only periodically — a full window OR the retarget interval. Until
        # then the rig keeps a stable difficulty (no per-share churn / hunting).
        anchor = self._last_retarget_at if self._last_retarget_at is not None else previous
        since_retarget = (at - anchor).total_seconds()
        window_full = len(self._intervals) >= self.retarget_samples
        if since_retarget < self.retarget_interval.total_seconds() and not window_full:
            return self._current
        # --- evaluate a retarget over the window average ---
        self._last_retarget_at = at
        average = sum(self._intervals) / len(self._intervals)
        self._intervals.clear()
        target = self.target_interval.total_seconds()
        band = target * float(self.variance_fraction)
        if (target - band) <= average <= (target + band):
            # Within tolerance of the target rate: leave difficulty alone (no churn).
            return self._current
        # ratio > 1 => shares came too FAST (avg interval shorter than target) =>
        # raise difficulty; ratio < 1 => too SLOW => lower it.
        ratio = Decimal(str(target)) / Decimal(str(average))
        ratio = _clamp(ratio, Decimal("1") / self.max_step_factor, self.max_step_factor)
        adjusted = (self._current * ratio).quantize(Decimal("1.000000"))
        self._current = _clamp(adjusted, self.floor, self.maximum)
        return self._current


def _clamp(value: Decimal, low: Decimal, high: Decimal) -> Decimal:
    if value < low:
        return low
    if value > high:
        return high
    return value

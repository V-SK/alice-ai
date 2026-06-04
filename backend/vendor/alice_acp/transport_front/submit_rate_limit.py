"""Per-connection submit-rate / abuse limiter — the PRE-RE-HASH flood gate (doc §2.1).

THE BLOCKER THIS CLOSES. Every ``mining.submit`` drives a FULL synchronous PoW
re-hash (``hashlib.scrypt`` N=1024 ~213us; RandomX / KawPoW ms-scale) BEFORE the
difficulty gate and the dedup gate. The per-connection vardiff floor only sets the
ADVERTISED target and only REACTS to ACCEPTED shares — so a rig submitting well-formed
SUB-TARGET garbage at line rate is NEVER throttled by vardiff: every such submit reaches
the re-hash, pegs a core, and (when the re-hash runs inline) starves the event loop for
EVERY other miner on EVERY lane. This limiter is the cheap, O(1), per-connection gate that
stops a flood of cheap garbage from ever reaching the re-hash.

It is the Alice analog of the standard pool flood defenses (cited in the build report):

* node-stratum-pool / NOMP + Miningcore ``ConsiderBan``: ban a peer after a window of
  shares once its INVALID/low-diff fraction crosses a threshold ("If a worker is
  submitting a high threshold of invalid shares we can temporarily ban their IP to
  reduce system/network load" — NOMP ``banning`` block: ``invalidPercent`` over a
  ``checkThreshold`` of shares, banned for ``time`` seconds).
* ckpool's stratifier: a per-client ``reject`` run-length + ``first_invalid`` timer that
  drops a client "lazily" once it has a run of rejects — penalize the SEQUENCE of bad
  submits, not just the ratio.

We combine both shapes, but evaluate them BEFORE the re-hash (the point the pools make
implicitly by validating shares off the network path; here the limiter is the explicit
front gate):

1. A SLIDING-WINDOW submit-RATE cap: at most ``max_submits_per_window`` submits per
   ``window`` seconds. Sized as a GENEROUS multiple of the expected at-target rate so a
   legit fast rig is never touched (see :func:`for_lane_rate`): a high-hashrate ASIC at a
   sane vardiff submits a few shares/sec, so a cap of tens/sec is orders of magnitude of
   head-room yet still cuts a line-rate flood (thousands/sec) off at the knees.
2. A CONSECUTIVE-below-target / invalid run counter (the ckpool ``reject``-run shape):
   ``max_consecutive_rejects`` well-formed sub-target / invalid submits IN A ROW trips the
   limiter. An ACCEPTED share resets the run (a legit rig clears its own target most of the
   time, so its run never builds; a flood of sub-target garbage is ALL rejects, so it trips
   fast). This is the part vardiff cannot do — vardiff only reacts to ACCEPTS, so a
   pure-reject flood would otherwise never move it.

Over EITHER budget → the limiter returns a :class:`SubmitGateDecision` with ``allowed=False``
and ``disconnect=True`` and a stable, secret-free reason code; the transport NACKs the
submit and DROPS + briefly BANS the connection (the ban is enforced at the transport's
admission layer, keyed by the peer — see the server). A stable reason code, never a secret.

PURE + injectable (like :mod:`alice_acp.transport_front.vardiff`): no I/O, an injected
clock, O(1) per call (the rate window is a count + a window-start stamp, not a per-event
deque — bounded memory regardless of flood volume). CREDIT-ONLY: it sets no
reward/payout/chain symbol; it only gates whether a submit is EVALUATED. A dropped submit
credits nothing (it never reaches the validator), and the limiter never accepts anything —
it can only DENY, so it can never cause a credit.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

#: Stable, secret-free reason codes (carried into the NACK + the transport drop event).
SUBMIT_RATE_EXCEEDED = "stratum_submit_rate_exceeded"
SUBMIT_CONSECUTIVE_REJECTS = "stratum_submit_consecutive_rejects"

#: DEFAULT sliding-window length for the submit-rate cap.
DEFAULT_SUBMIT_RATE_WINDOW = timedelta(seconds=1)

#: DEFAULT max submits per window (per connection). ``0`` = UNLIMITED (the cap is OFF) — so
#: a bare connection / the existing tests are never throttled; the deploy sets a real cap.
#: A SANE rig at vardiff submits ~1 share / target-interval (~15s) — well under 1/s. Even a
#: rig that briefly bursts (a just-applied lower target, a batch of in-flight shares) does a
#: few/sec. A cap in the tens/sec is therefore a LARGE multiple of any legit rate while a
#: line-rate sub-target flood (thousands/sec) is cut immediately. The public deploy picks a
#: generous lane-aware value (see :func:`for_lane_rate`); ``0`` here keeps the library default
#: permissive so nothing legit is harmed out of the box.
DEFAULT_MAX_SUBMITS_PER_WINDOW = 0

#: DEFAULT max CONSECUTIVE below-target / invalid submits before the limiter trips. ``0`` =
#: UNLIMITED (OFF). A legit rig clears its OWN per-connection target most of the time, so its
#: consecutive-reject run essentially never reaches a non-trivial bound; a pure sub-target
#: flood is ALL rejects, so it trips quickly. The deploy sets a generous-but-finite value
#: (e.g. a few hundred) that tolerates a transient retarget-straddle reject burst (the
#: bounded retarget grace already absorbs the common case) yet stops an unbounded reject run.
DEFAULT_MAX_CONSECUTIVE_REJECTS = 0


@dataclass(frozen=True, slots=True)
class SubmitGateDecision:
    """The limiter's verdict on whether a submit may be EVALUATED (re-hashed).

    ``allowed`` is ``True`` when the submit is within budget and may proceed to the
    re-hash. When ``False`` the transport NACKs with ``reason`` (a stable code) and, when
    ``disconnect`` is set, DROPS the connection (and briefly bans the peer at admission).
    The limiter only ever DENIES — it can never cause an accept/credit.
    """

    allowed: bool
    disconnect: bool = False
    reason: str = ""


#: A permanently-permissive decision (the OFF / within-budget case) — shared so the hot
#: path allocates nothing when the limiter is disabled or the submit is within budget.
_ALLOWED = SubmitGateDecision(allowed=True)


@dataclass(slots=True)
class SubmitRateLimiter:
    """Per-connection submit-flood limiter (pure; O(1) per call; no I/O).

    Construct one per connection (the transport binds it on the handler). Call
    :meth:`check` at the TOP of submit handling — BEFORE the expensive re-hash — to decide
    whether the submit may be evaluated; call :meth:`note_outcome` AFTER the validator's
    verdict to feed the consecutive-reject run (an accepted share resets it; a below-target
    / invalid submit extends it). Both budgets default to ``0`` = OFF so a bare limiter
    never throttles anything (the deploy sets real caps lane-aware).

    The rate window is a COUNT + a window-start instant (not a per-event deque), so memory
    is O(1) regardless of how fast a flood arrives — a flood cannot blow up the limiter's
    own footprint. The window slides in fixed steps: when ``now`` passes the window end the
    count resets and a fresh window opens at ``now``.
    """

    #: Sliding-window rate cap. ``max_submits_per_window <= 0`` disables the rate cap.
    window: timedelta = DEFAULT_SUBMIT_RATE_WINDOW
    max_submits_per_window: int = DEFAULT_MAX_SUBMITS_PER_WINDOW
    #: Consecutive below-target / invalid run cap. ``<= 0`` disables the run cap.
    max_consecutive_rejects: int = DEFAULT_MAX_CONSECUTIVE_REJECTS
    _window_start: datetime | None = field(default=None)
    _window_count: int = field(default=0)
    _consecutive_rejects: int = field(default=0)
    #: Latched once a budget is tripped: every subsequent submit on this connection is
    #: denied+disconnected with the SAME reason, so a flood that keeps sending after the
    #: trip never sneaks a submit through to the re-hash before the socket actually closes.
    _tripped_reason: str = field(default="")

    def check(self, *, at: datetime) -> SubmitGateDecision:
        """Decide whether a submit arriving at ``at`` may be EVALUATED (re-hashed).

        Evaluated BEFORE the re-hash. Advances the sliding-window submit count and, when
        the window cap is exceeded (or a budget was already tripped), returns a denying
        ``disconnect`` decision with a stable reason. The consecutive-reject budget is
        enforced in :meth:`note_outcome` (it needs the validator's verdict), but once
        EITHER budget has tripped this method denies every further submit (the latch), so
        the connection cannot keep feeding the re-hash while it is being torn down.
        """

        if self._tripped_reason:
            return SubmitGateDecision(allowed=False, disconnect=True, reason=self._tripped_reason)

        if self.max_submits_per_window > 0:
            start = self._window_start
            if start is None or (at - start) >= self.window:
                # Open a fresh window at ``at`` (first submit, or the prior window elapsed).
                self._window_start = at
                self._window_count = 1
            else:
                self._window_count += 1
                if self._window_count > self.max_submits_per_window:
                    # Over the per-window cap → trip + deny + disconnect (latched).
                    self._tripped_reason = SUBMIT_RATE_EXCEEDED
                    return SubmitGateDecision(
                        allowed=False, disconnect=True, reason=SUBMIT_RATE_EXCEEDED
                    )
        return _ALLOWED

    def note_outcome(self, *, is_share: bool) -> SubmitGateDecision:
        """Feed the validator's verdict into the consecutive-reject run.

        Called AFTER ``validate`` returns. An ACCEPTED share (``is_share=True``) RESETS the
        run (a legit rig clears its own target regularly, so its run never builds). A
        below-target / invalid submit EXTENDS it; once the run reaches
        ``max_consecutive_rejects`` the limiter trips (latched) and returns a denying
        ``disconnect`` decision — the caller drops + briefly bans the connection. When the
        run cap is OFF, or the run is still within budget, returns an allowing decision
        (the submit itself already happened — this only governs the NEXT one + the drop).
        """

        if is_share:
            self._consecutive_rejects = 0
            return _ALLOWED
        if self._tripped_reason:
            return SubmitGateDecision(allowed=False, disconnect=True, reason=self._tripped_reason)
        if self.max_consecutive_rejects <= 0:
            return _ALLOWED
        self._consecutive_rejects += 1
        if self._consecutive_rejects >= self.max_consecutive_rejects:
            self._tripped_reason = SUBMIT_CONSECUTIVE_REJECTS
            return SubmitGateDecision(
                allowed=False, disconnect=True, reason=SUBMIT_CONSECUTIVE_REJECTS
            )
        return _ALLOWED

    @property
    def tripped(self) -> bool:
        """Whether a budget has tripped (the connection is being / should be dropped)."""

        return bool(self._tripped_reason)


def for_lane_rate(
    *,
    target_share_interval: timedelta,
    headroom: Decimal,
    window: timedelta = DEFAULT_SUBMIT_RATE_WINDOW,
    max_consecutive_rejects: int = DEFAULT_MAX_CONSECUTIVE_REJECTS,
) -> SubmitRateLimiter:
    """Build a limiter whose RATE cap is a generous multiple of the at-target share rate.

    The vardiff aims for one ACCEPTED share per ``target_share_interval`` (~15s by
    default), i.e. an at-target rate of ``1 / target_share_interval`` shares/sec. A legit
    rig — even bursting just after a retarget, or replaying a few in-flight shares — stays
    within a small multiple of that. We size the per-window cap at
    ``ceil(headroom * at_target_rate * window_seconds)`` with a generous ``headroom`` (a
    large multiple, e.g. 100x-1000x) so a legit rig is NEVER throttled, while a line-rate
    sub-target flood (orders of magnitude above the at-target rate) is still denied.

    The cap is floored at 1 (a positive window cap is always meaningful) so a very long
    target interval cannot round the cap down to 0 (which would read as "unlimited"). The
    consecutive-reject budget is passed through unchanged (it is rate-independent).
    """

    target_seconds = Decimal(str(target_share_interval.total_seconds()))
    window_seconds = Decimal(str(window.total_seconds()))
    if target_seconds <= 0 or window_seconds <= 0:
        # Degenerate inputs → keep the cap OFF rather than fabricate a tiny throttle.
        cap = 0
    else:
        at_target_per_window = window_seconds / target_seconds  # shares/window at target
        raw = (headroom * at_target_per_window).to_integral_value(rounding="ROUND_CEILING")
        cap = max(1, int(raw))
    return SubmitRateLimiter(
        window=window,
        max_submits_per_window=cap,
        max_consecutive_rejects=max_consecutive_rejects,
    )

"""M6: the 72h mandatory-mining ENTRY GATE (plan §6 anti-sybil + §3 M_rate window).

A NEW device must accumulate **72 hours of server-VERIFIED mining** before it is
eligible for ANY inference job. The ``WorkerPullEdge`` consults this gate on every
PULL and REJECTS an inference lease for a device whose cumulative verified mining
time is below the threshold. This window is BOTH:

* the **M_rate measurement** window (plan §3 -- a device's PRL credit/hour is only
  meaningful after enough verified mining has accrued to peg its inference credit);
* the **anti-sybil entry cost** (plan §6 -- a sybil flood of fresh device-ids each
  has to first serve 72h of REAL, re-hash-verified mining before earning a single
  AI job; a demoted device that re-keys its device-id starts again from zero).

Why this lives server-side and is keyed by the **device_key**
(``{alice_address}.{device_id}``): the device is the unit of measurement/scoring
(M5), and the gate must be un-forgeable by the worker -- the worker cannot
self-report "I've mined 72h". The ONLY thing that advances the counter is mining
the server has VERIFIED (accepted shares re-hashed server-side -- mining fraud is
rejected instantly per the plan, so verified mining time is trustworthy). A worker
that re-keys (new device_id) gets a new device_key -> a fresh zero counter -> must
re-serve the 72h. Credit accrues to the Alice ADDRESS but the gate is PER DEVICE,
so one address cannot "lend" a new device its siblings' served time.

Accounting model (monotone, replay-safe):

* Verified mining is folded in as half-open INTERVALS ``[start, end)`` per
  device_key. Overlapping / out-of-order / duplicate intervals are MERGED (union of
  covered time), so re-ingesting the same verified records is idempotent and never
  double-counts -- the counter is the measure of the UNION of verified-mining
  wall-clock, not a naive sum of possibly-overlapping spans.
* :meth:`MiningEntryGate.verified_seconds` returns that union measure; eligibility
  is ``verified_seconds >= required_seconds`` (default 72h).

CREDIT-ONLY: this gate sizes ELIGIBILITY only. It reads/writes no secret, no
payout, no ``paid_acu`` (there is none here); it never touches a reward/chain path.
Rejecting an ineligible device only declines an inference lease.
"""

from __future__ import annotations

from bisect import insort
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from alice_acp.api_chat.types import validate_public_identifier
from alice_acp.api_chat.validators import validate_aware_timestamp
from alice_acp.api_chat_gateway.worker_reputation import address_of_device_key

MINING_ENTRY_GATE_CONTRACT_VERSION = "api-chat-mining-entry-gate-contract-v1"

#: The 72h mandatory-mining window before a NEW device may serve inference.
DEFAULT_ENTRY_GATE_WINDOW = timedelta(hours=72)

#: Reason codes surfaced when the gate admits / rejects a device.
REASON_ENTRY_GATE_ELIGIBLE = "api_chat_mining_entry_gate_eligible"
REASON_ENTRY_GATE_UNDER_72H = "api_chat_mining_entry_gate_under_72h"


@dataclass(frozen=True, slots=True)
class EntryGateStatusDTO:
    """A device's standing against the 72h entry gate (diagnostic + the pull gate).

    ``verified_seconds`` is the UNION measure of this device's server-verified
    mining wall-clock; ``required_seconds`` is the 72h threshold; ``eligible`` is
    ``verified_seconds >= required_seconds``. ``remaining_seconds`` is how much
    more verified mining the device still owes (0 once eligible). All times are
    integer seconds so the wire form is JSON-clean and exact.
    """

    device_key: str
    alice_address: str
    verified_seconds: int
    required_seconds: int
    eligible: bool
    reason_code: str

    @property
    def remaining_seconds(self) -> int:
        return max(0, self.required_seconds - self.verified_seconds)

    def to_public_dict(self) -> dict[str, object]:
        return {
            "contract_version": MINING_ENTRY_GATE_CONTRACT_VERSION,
            "device_key": self.device_key,
            "alice_address": self.alice_address,
            "verified_seconds": self.verified_seconds,
            "required_seconds": self.required_seconds,
            "remaining_seconds": self.remaining_seconds,
            "eligible": self.eligible,
            "reason_code": self.reason_code,
            # Credit-only: the gate is an eligibility check, not a payout path.
            "paid_acu": "0",
        }


@dataclass(slots=True)
class _DeviceIntervals:
    """The merged set of half-open verified-mining intervals for ONE device_key.

    Stored as a sorted list of non-overlapping, non-adjacent ``(start, end)`` pairs
    (epoch-seconds floats from a tz-aware UTC datetime). Inserting an interval
    unions it in (merging any spans it overlaps/touches), so the total covered
    seconds is always the measure of the UNION -- idempotent under re-ingest and
    safe for out-of-order arrival.
    """

    spans: list[tuple[float, float]] = field(default_factory=list)

    def add(self, start: float, end: float) -> None:
        if end <= start:
            return
        # Insert then coalesce: find the run of existing spans that overlap or
        # touch [start, end), merge them into one, and splice it back.
        insort(self.spans, (start, end))
        merged: list[tuple[float, float]] = []
        for span_start, span_end in self.spans:
            if merged and span_start <= merged[-1][1]:
                # Overlaps/touches the last merged span -> extend it.
                prev_start, prev_end = merged[-1]
                merged[-1] = (prev_start, max(prev_end, span_end))
            else:
                merged.append((span_start, span_end))
        self.spans = merged

    def covered_seconds(self) -> float:
        return sum(end - start for start, end in self.spans)


@dataclass(slots=True)
class MiningEntryGate:
    """Server-side per-device cumulative VERIFIED-mining-time ledger + the 72h gate.

    Feed verified mining via :meth:`record_verified_interval` (a span of mining the
    server has verified for a device) or :meth:`fold_verified_record` (fold a single
    verified PRL accounting record, attributing one verified epoch's worth of mining
    to its device). Query :meth:`is_eligible` / :meth:`status_for` on the pull path.

    The gate is keyed by the ``device_key`` (``{alice_address}.{device_id}``) so a
    re-keyed device starts at zero (the anti-sybil re-serve cost) and an address
    cannot pool its devices' served time. Eligibility rolls UP nowhere -- it is a
    strict per-device check (credit still accrues to the address elsewhere).
    """

    window: timedelta = DEFAULT_ENTRY_GATE_WINDOW
    #: When a verified record carries no explicit span, attribute this much mining
    #: to the device for that single verified observation. Defaults to one PRL epoch
    #: (the upstream's hourly cadence, plan §2) -- a CONSERVATIVE per-proof credit so
    #: a device must show ~72 verified hourly epochs (not one burst) to pass.
    per_record_interval: timedelta = timedelta(hours=1)
    #: M7 residual fix: the HARD CAP on a single folded observation's interval. A
    #: per-record span (caller-supplied or default) is CLAMPED to this before it is
    #: unioned in, so no one verified observation can credit more than the whole
    #: window's worth of mining in a single burst -- a device must still accrue the
    #: 72h across many bounded observations (anti-sybil: the entry cost cannot be
    #: collapsed by reporting one enormous interval). Defaults to the window itself.
    max_record_interval: timedelta = DEFAULT_ENTRY_GATE_WINDOW
    _devices: dict[str, _DeviceIntervals] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        if self.window <= timedelta(0):
            raise ValueError("entry gate window must be positive")
        if self.per_record_interval <= timedelta(0):
            raise ValueError("per_record_interval must be positive")
        if self.max_record_interval <= timedelta(0):
            raise ValueError("max_record_interval must be positive")

    @property
    def required_seconds(self) -> int:
        return int(self.window.total_seconds())

    # ------------------------------------------------------------- ingestion ---
    def record_verified_interval(
        self,
        *,
        device_key: str,
        start: datetime,
        end: datetime,
    ) -> None:
        """Fold a half-open ``[start, end)`` span of VERIFIED mining for a device.

        ``start``/``end`` are tz-aware UTC datetimes (validated fail-closed). An
        empty/inverted span is a no-op. Overlapping / duplicate spans are UNIONED
        in (idempotent), so re-ingesting the same verified window never
        double-counts the device's verified time.
        """
        validate_public_identifier("device_key", device_key)
        validate_aware_timestamp("start", start)
        validate_aware_timestamp("end", end)
        intervals = self._devices.get(device_key)
        if intervals is None:
            intervals = _DeviceIntervals()
            self._devices[device_key] = intervals
        intervals.add(start.timestamp(), end.timestamp())

    def fold_verified_record(
        self,
        *,
        device_key: str,
        observed_at: datetime,
        interval: timedelta | None = None,
    ) -> None:
        """Attribute one VERIFIED mining observation to a device.

        Use this to fold a single verified PRL accounting record (one re-hash-
        verified epoch) into the device's counter when no explicit span is known:
        the record's ``observed_at`` is treated as the END of a span of length
        ``interval`` (default :attr:`per_record_interval`, one PRL epoch). Because
        spans are UNIONED, two records within the same epoch overlap and count once
        -- so the counter measures verified mining wall-clock, not proof count.

        M7 residual fix (bound the interval): an inverted / non-positive ``interval``
        is rejected, and an oversized one is CLAMPED to :attr:`max_record_interval`
        (default = the window) so no SINGLE observation can credit more than the
        whole 72h in one burst -- the anti-sybil entry cost cannot be collapsed by
        reporting one enormous span; a device must still accrue the window across
        many bounded observations. The default one-epoch span is well under the cap,
        so the existing hourly-fold behaviour is byte-for-byte unchanged.
        """
        validate_aware_timestamp("observed_at", observed_at)
        span = interval if interval is not None else self.per_record_interval
        if span <= timedelta(0):
            raise ValueError("verified-record interval must be positive")
        # Bound a single observation so one record cannot inflate the counter past
        # the window. Clamp (not reject) so an over-eager feeder still credits the
        # capped maximum rather than failing the fold.
        if span > self.max_record_interval:
            span = self.max_record_interval
        self.record_verified_interval(
            device_key=device_key,
            start=observed_at - span,
            end=observed_at,
        )

    # -------------------------------------------------------------- queries ---
    def verified_seconds(self, device_key: str) -> int:
        """The device's cumulative VERIFIED-mining wall-clock (union measure), secs.

        Unknown device -> 0 (a never-seen device has served no verified mining).
        Floored to an int so the wire/compare is exact.
        """
        validate_public_identifier("device_key", device_key)
        intervals = self._devices.get(device_key)
        if intervals is None:
            return 0
        return int(intervals.covered_seconds())

    def is_eligible(self, device_key: str) -> bool:
        """``True`` iff the device has served >= the 72h window of verified mining."""
        return self.verified_seconds(device_key) >= self.required_seconds

    def status_for(self, device_key: str) -> EntryGateStatusDTO:
        """The device's full standing against the gate (the pull-path decision)."""
        verified = self.verified_seconds(device_key)
        eligible = verified >= self.required_seconds
        return EntryGateStatusDTO(
            device_key=device_key,
            alice_address=address_of_device_key(device_key),
            verified_seconds=verified,
            required_seconds=self.required_seconds,
            eligible=eligible,
            reason_code=(
                REASON_ENTRY_GATE_ELIGIBLE if eligible else REASON_ENTRY_GATE_UNDER_72H
            ),
        )

    def to_public_dict(self) -> dict[str, object]:
        return {
            "contract_version": MINING_ENTRY_GATE_CONTRACT_VERSION,
            "required_seconds": self.required_seconds,
            "per_record_interval_seconds": int(self.per_record_interval.total_seconds()),
            "max_record_interval_seconds": int(self.max_record_interval.total_seconds()),
            "tracked_devices": len(self._devices),
            "eligible_devices": sum(
                1 for key in self._devices if self.is_eligible(key)
            ),
            "paid_acu": "0",
        }

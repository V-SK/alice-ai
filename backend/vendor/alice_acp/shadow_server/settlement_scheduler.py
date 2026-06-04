"""Server-side settlement scheduler for the shadow reward server (Phase H_d).

Settlement windows — the WAC-ABRS settlement that converts accepted proofs into
*settled* simulated-ALICE CREDIT (``ShadowRewardLedger.settle_window`` /
``ShadowServerHarness.shadow_window``) — were until now driven entirely
externally: a ``GET /shadow/window`` request, or a test calling
``settle_window`` directly. There was no server-side component that advanced
settlement on its own cadence.

This module adds a :class:`SettlementScheduler` that drives settlement on a
configurable cadence (injectable ``clock`` + ``interval``). It is built around a
single pure :meth:`SettlementScheduler.tick` step that the deployed loop and the
tests both call, so the cadence behaviour is deterministically testable without
real sleeps.

FAIL-SAFE (the central guarantee): a settlement error on one tick is caught,
recorded (audit hook + ``SettlementTickResult.error``), and the scheduler simply
RETRIES on the next tick. A single bad settlement never crashes the scheduler or
the server. This is the deliberate counterpart to the fail-CLOSED admission
guards: admission must fail closed (deny), but settlement of already-accepted,
already-verified work must be resilient (retry), because a transient settle error
must not wedge the whole credit pipeline.

CREDIT-ONLY: settlement here computes simulated-ALICE CREDIT only. Every result
carries (and asserts) the disabled reward/payout/chain flags and ``paid_acu``
``"0"``; the scheduler performs NO money movement and NO chain write. It is also
OFF by default (``SettlementSchedulerConfig.enabled`` defaults ``False``):
nothing settles on a timer until an operator explicitly enables it at deploy
(owner input: the settlement cadence). reward/payout/chain stay OFF regardless.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal

from alice_acp.shadow_server.ledger import default_settlement_window
from alice_acp.shadow_server.server import ShadowServerHarness
from alice_acp.shadow_server.types import (
    DEFAULT_TOTAL_WINDOW_EMISSION,
    DEFAULT_WINDOW_DURATION,
    SettlementResult,
    SettlementWindow,
    utc_now,
)

# Default cadence: re-settle the current window once per window duration. Pure
# default — an operator pins the real cadence at deploy (owner input).
DEFAULT_SETTLEMENT_INTERVAL = DEFAULT_WINDOW_DURATION

SETTLEMENT_TICK_OK = "settlement_tick_settled"
SETTLEMENT_TICK_DISABLED = "settlement_scheduler_disabled"
SETTLEMENT_TICK_ERROR = "settlement_tick_error"


@dataclass(frozen=True, slots=True)
class SettlementSchedulerConfig:
    """Configuration for the server-side settlement cadence.

    Fail-safe + explicit-enable: ``enabled`` defaults ``False`` so settlement
    never runs on a timer until an operator turns it on at deploy. ``interval``
    is the cadence between ticks; ``window_duration`` / ``total_window_emission``
    parameterise the settlement window each tick settles. All values are
    injected, none hardcoded into the loop.
    """

    enabled: bool = False
    interval: timedelta = DEFAULT_SETTLEMENT_INTERVAL
    window_duration: timedelta = DEFAULT_WINDOW_DURATION
    total_window_emission: Decimal = DEFAULT_TOTAL_WINDOW_EMISSION

    def __post_init__(self) -> None:
        if self.interval <= timedelta(0):
            raise ValueError("settlement interval must be positive")
        if self.window_duration <= timedelta(0):
            raise ValueError("settlement window_duration must be positive")
        if self.total_window_emission <= Decimal("0"):
            raise ValueError("settlement total_window_emission must be positive")


@dataclass(frozen=True, slots=True)
class SettlementTickResult:
    """The outcome of one scheduler tick (credit-only; reward/payout/chain OFF)."""

    status: str
    settled: bool
    window_id: str | None = None
    observed_at: datetime | None = None
    error: str | None = None
    # CREDIT-ONLY invariant — asserted in __post_init__, never anything else.
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False
    chain_writes_enabled: bool = False
    paid_acu: str = "0"

    def __post_init__(self) -> None:
        # Reward/payout/chain stay OFF on every tick result, settled or not.
        assert self.live_reward_enabled is False
        assert self.payout_executor_enabled is False
        assert self.chain_writes_enabled is False
        assert self.paid_acu == "0"

    def to_public_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "settled": self.settled,
            "window_id": self.window_id,
            "observed_at": self.observed_at.isoformat() if self.observed_at is not None else None,
            "error": self.error,
            "live_reward_enabled": self.live_reward_enabled,
            "payout_executor_enabled": self.payout_executor_enabled,
            "chain_writes_enabled": self.chain_writes_enabled,
            "paid_acu": self.paid_acu,
        }


# Audit/log hook signature: receives each tick result. Defaults to a no-op so the
# scheduler has no hard dependency on a logger; the deployed edge can pass a hook
# that appends to the audit store.
SettlementAuditHook = Callable[[SettlementTickResult], None]


def _noop_audit_hook(result: SettlementTickResult) -> None:
    return None


@dataclass(slots=True)
class SettlementScheduler:
    """Drives ``shadow_window`` settlement on a configurable cadence (fail-safe).

    The deployed server constructs one of these with the harness, a real clock,
    and an interval, then calls :meth:`run_forever` on a daemon thread. Tests
    drive :meth:`tick` directly with a pinned clock to assert the cadence and the
    fail-safe behaviour deterministically.
    """

    harness: ShadowServerHarness
    config: SettlementSchedulerConfig = field(default_factory=SettlementSchedulerConfig)
    # Injectable clock (deterministic in tests; real UTC in production).
    clock: Callable[[], datetime] = utc_now
    # Fail-safe audit/log hook — receives every tick result (settled OR errored).
    audit_hook: SettlementAuditHook = _noop_audit_hook
    _last_settled_at: datetime | None = field(default=None, init=False)
    _stop: threading.Event = field(default_factory=threading.Event, init=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False)

    @property
    def last_settled_at(self) -> datetime | None:
        with self._lock:
            return self._last_settled_at

    def window_for(self, observed_at: datetime) -> SettlementWindow:
        """Build the settlement window this tick settles (current window)."""

        if (
            self.config.window_duration == DEFAULT_WINDOW_DURATION
            and self.config.total_window_emission == DEFAULT_TOTAL_WINDOW_EMISSION
        ):
            return default_settlement_window(observed_at)
        return SettlementWindow(
            window_id=f"window-{observed_at.isoformat()}",
            starts_at=observed_at,
            ends_at=observed_at + self.config.window_duration,
            total_window_emission=self.config.total_window_emission,
        )

    def tick(self, *, now: datetime | None = None) -> SettlementTickResult:
        """Run a single settlement step, fail-safe.

        Returns a :class:`SettlementTickResult`. When the scheduler is disabled
        the tick is a no-op (``SETTLEMENT_TICK_DISABLED``). When settlement
        raises, the error is captured into the result and the audit hook fires —
        the exception is NEVER propagated, so a bad tick cannot crash the loop;
        the next tick retries. reward/payout/chain stay OFF either way.
        """

        if not self.config.enabled:
            result = SettlementTickResult(
                status=SETTLEMENT_TICK_DISABLED,
                settled=False,
            )
            self._emit(result)
            return result

        observed_at = now if now is not None else self.clock()
        window = self.window_for(observed_at)
        try:
            settlement = self.harness.shadow_window(window)
        except Exception as exc:  # noqa: BLE001 — fail-safe: never crash the loop.
            result = SettlementTickResult(
                status=SETTLEMENT_TICK_ERROR,
                settled=False,
                window_id=window.window_id,
                observed_at=observed_at,
                error=f"{type(exc).__name__}: {exc}",
            )
            self._emit(result)
            return result

        # CREDIT-ONLY: settlement settles simulated-ALICE credit only; assert the
        # ledger result is closed (no paid_acu) before we report success.
        _assert_settlement_credit_only(settlement)
        with self._lock:
            self._last_settled_at = observed_at
        result = SettlementTickResult(
            status=SETTLEMENT_TICK_OK,
            settled=True,
            window_id=window.window_id,
            observed_at=observed_at,
        )
        self._emit(result)
        return result

    def run_forever(self, *, sleep: Callable[[float], None] | None = None) -> None:
        """Drive settlement every ``interval`` until :meth:`stop` is called.

        Used by the deployed server on a daemon thread. ``sleep`` is injectable
        for tests; it defaults to a :class:`threading.Event`-based wait so a
        :meth:`stop` interrupts the cadence promptly. Each iteration calls the
        fail-safe :meth:`tick`, so a settlement error logs + retries next tick and
        never breaks the loop.
        """

        interval_seconds = self.config.interval.total_seconds()
        while not self._stop.is_set():
            self.tick()
            if sleep is not None:
                sleep(interval_seconds)
                if self._stop.is_set():
                    break
            else:
                # Interruptible wait: stop() wakes this immediately.
                if self._stop.wait(timeout=interval_seconds):
                    break

    def stop(self) -> None:
        self._stop.set()

    def _emit(self, result: SettlementTickResult) -> None:
        # The audit hook itself must never crash the scheduler (fail-safe).
        try:
            self.audit_hook(result)
        except Exception:  # noqa: BLE001 — audit is best-effort telemetry.
            return None


def _assert_settlement_credit_only(settlement: SettlementResult) -> None:
    if settlement.paid_acu != Decimal("0"):
        raise AssertionError("settlement_paid_acu_must_be_zero")
    for statement in settlement.reward_statements:
        if statement.paid_acu != Decimal("0"):
            raise AssertionError("reward_statement_paid_acu_must_be_zero")

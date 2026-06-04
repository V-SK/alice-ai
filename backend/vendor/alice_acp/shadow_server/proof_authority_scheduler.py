"""Server-side proof-authority poller closing the MINING credit loop (Approach B).

THE BUG THIS FIXES
------------------
``POST /proof/ingest`` (``ShadowHttpApp._proof_ingest``) calls
``ShadowServerHarness.proof_ingest`` -> ``ShadowRewardLedger.ingest_mining_proof``
*without* an ``authority_result`` set, so the ledger authority gate returns
``UNDER_REVIEW`` / ``client_only_authority_required`` and never credits. The
pool-evidence provider + authority evaluator already EXIST (and ``main()`` already
wires a provider into the harness), but nothing invokes the authority evaluation
at runtime: the only path that does is ``proof_ingest_with_authority``, which no
runtime caller reaches. The existing :mod:`settlement_scheduler` only *settles*
already-accepted work; it does not *produce* accepted work.

APPROACH B (this module)
------------------------
A background poller that, per cycle, polls the pool per-worker once and credits
the un-spent accepted-share DELTA through the EXISTING ledger guards. The credit
is the increment the SERVER read from the pool (never a client claim); dedup is by
``(pool, worker, cursor)`` (the provider's cursor store) PLUS the ledger's
``proof_id`` and canonical-share dedup. It is ADDITIVE, ENV-GATED (default OFF),
and REVERSIBLE: with the flag off no thread starts and ``/proof/ingest`` behaviour
is byte-for-byte unchanged.

This module mirrors :mod:`settlement_scheduler` exactly for its operational
scaffolding (fail-safe :meth:`tick`, :meth:`run_forever` daemon loop with an
interruptible :class:`threading.Event` wait, :meth:`stop`, an audit hook, and a
credit-only-assert on every tick result). The actual reconstruct-evaluate-ingest
work lives in :meth:`ShadowServerHarness.credit_attested_shares`; this scheduler
only decides *when* to poll and aggregates the per-target counts fail-safe.

FAIL-SAFE (the central guarantee, identical to the settlement scheduler): a poll
error on ONE target is caught, recorded (audit hook + per-target error) and the
cycle CONTINUES to the next target; a single bad worker never wedges the cycle.
The exception is never propagated, so a transient pool error cannot crash the loop
— the next tick retries. (This is the deliberate counterpart to the fail-CLOSED
admission/evidence guards: the EVIDENCE side still fails closed — no evidence =>
no credit — but advancing the credit cadence over many workers must be resilient.)

CREDIT-ONLY: this module sets NO reward/payout/chain symbol anywhere. Every tick
result carries (and asserts in ``__post_init__``) the disabled reward/payout/chain
flags and ``paid_acu`` ``"0"``; the actual crediting goes exclusively through the
ledger's unchanged accepted-share path (which keeps ``paid_acu`` ``"0"``). It is
OFF by default (``ProofAuthoritySchedulerConfig.enabled`` defaults ``False``).
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from alice_acp.shadow_server.mining_authority_bridge import PoolEvidenceProvider
from alice_acp.shadow_server.server import CreditAttestedSharesSummary, ShadowServerHarness
from alice_acp.shadow_server.types import Lane, ShadowSession, utc_now

# Default cadence: one poll per worker per minute. This matches the pool-evidence
# providers' DEFAULT_POLL_CADENCE so a tick costs ~one upstream request per
# address (the provider caches within the cadence). Pure default — an operator
# pins the real cadence at deploy via env (owner input).
DEFAULT_PROOF_AUTHORITY_POLL_INTERVAL = timedelta(seconds=60)

PROOF_AUTHORITY_TICK_OK = "proof_authority_tick_polled"
PROOF_AUTHORITY_TICK_DISABLED = "proof_authority_scheduler_disabled"


@dataclass(frozen=True, slots=True)
class ProofAuthoritySchedulerConfig:
    """Configuration for the server-side proof-authority poll cadence.

    Fail-safe + explicit-enable: ``enabled`` defaults ``False`` so the poller
    never runs on a timer until an operator turns it on at deploy.
    ``poll_interval`` is the cadence between ticks. All values are injected; none
    is hardcoded into the loop.
    """

    enabled: bool = False
    poll_interval: timedelta = DEFAULT_PROOF_AUTHORITY_POLL_INTERVAL

    def __post_init__(self) -> None:
        if self.poll_interval <= timedelta(0):
            raise ValueError("proof authority poll_interval must be positive")


@dataclass(frozen=True, slots=True)
class ProofAuthorityTarget:
    """One worker to poll-and-credit on each tick.

    Carries the SERVER-TRUSTED facts needed to reconstruct a server-side
    ``SignedMiningSession`` + per-share ``MiningShareProof`` for the
    ``credit_attested_shares`` call: the lane, the pool routing/binding
    (``pool_id`` + ``alice_collection_address``, both server-owned, NEVER a client
    body field), the SERVER-ASSIGNED ``worker_name`` (the H_a/H_b pool-correlation
    key), and the issued ``ShadowSession`` (for its passport/device/session_id +
    issued/expires window). ``main()`` builds these by joining the roster's
    enrolled worker_names with the provider's per-lane pool_id/address and
    ``ledger.sessions``.
    """

    lane: Lane
    pool_id: str
    alice_collection_address: str
    worker_name: str
    session: ShadowSession


# A target source is a zero-arg callable so the scheduler re-reads the live roster
# + ledger.sessions every tick (workers enrolled / sessions issued after start are
# picked up on the next cycle without restarting the scheduler).
WorkerTargetsSource = Callable[[], Iterable[ProofAuthorityTarget]]


@dataclass(frozen=True, slots=True)
class ProofAuthorityTickResult:
    """The outcome of one poll-and-credit tick (credit-only; reward/payout/chain OFF)."""

    status: str
    workers_polled: int = 0
    shares_credited: int = 0
    errored_workers: int = 0
    observed_at: datetime | None = None
    # Per-target errors, fail-safe: "<worker_name>: <ExcType>: <msg>" (audit only;
    # never propagated). One bad worker is recorded here, the cycle continues.
    errors: tuple[str, ...] = ()
    # CREDIT-ONLY invariant — asserted in __post_init__, never anything else.
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False
    chain_writes_enabled: bool = False
    paid_acu: str = "0"

    def __post_init__(self) -> None:
        # Reward/payout/chain stay OFF on every tick result, credited or not.
        assert self.live_reward_enabled is False
        assert self.payout_executor_enabled is False
        assert self.chain_writes_enabled is False
        assert self.paid_acu == "0"

    def to_public_dict(self) -> dict[str, object]:
        return {
            "status": self.status,
            "workers_polled": self.workers_polled,
            "shares_credited": self.shares_credited,
            "errored_workers": self.errored_workers,
            "observed_at": self.observed_at.isoformat() if self.observed_at is not None else None,
            "errors": list(self.errors),
            "live_reward_enabled": self.live_reward_enabled,
            "payout_executor_enabled": self.payout_executor_enabled,
            "chain_writes_enabled": self.chain_writes_enabled,
            "paid_acu": self.paid_acu,
        }


# Audit/log hook signature: receives each tick result. Defaults to a no-op so the
# scheduler has no hard dependency on a logger; the deployed edge passes a hook
# that appends to the audit store.
ProofAuthorityAuditHook = Callable[[ProofAuthorityTickResult], None]


def _noop_audit_hook(result: ProofAuthorityTickResult) -> None:
    return None


def _empty_targets() -> Iterable[ProofAuthorityTarget]:
    return ()


@dataclass(slots=True)
class ProofAuthorityScheduler:
    """Polls each worker once per cadence and credits its accepted-share delta.

    The deployed server constructs one of these with the harness, a real clock, a
    target source (roster ∩ provider lanes ∩ ledger.sessions) and a poll interval,
    then calls :meth:`run_forever` on a daemon thread. Tests drive :meth:`tick`
    directly with a pinned clock + an injected fake-HTTP provider to assert the
    poll/credit/dedup behaviour and the fail-safe behaviour deterministically.
    """

    harness: ShadowServerHarness
    config: ProofAuthoritySchedulerConfig = field(default_factory=ProofAuthoritySchedulerConfig)
    # The live target source, re-read each tick. Defaults to empty (no targets).
    worker_targets: WorkerTargetsSource = _empty_targets
    # Injectable clock (deterministic in tests; real UTC in production).
    clock: Callable[[], datetime] = utc_now
    # OPTIONAL explicit evidence provider for the credit call. ``None`` => the
    # harness uses its own configured provider (the deployed default). Tests inject
    # a fake-HTTP-backed provider here so no network is touched.
    evidence_provider: PoolEvidenceProvider | None = None
    # Fail-safe audit/log hook — receives every tick result (polled OR disabled).
    audit_hook: ProofAuthorityAuditHook = _noop_audit_hook
    _stop: threading.Event = field(default_factory=threading.Event, init=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False)
    _last_polled_at: datetime | None = field(default=None, init=False)

    @property
    def last_polled_at(self) -> datetime | None:
        with self._lock:
            return self._last_polled_at

    def tick(self, *, now: datetime | None = None) -> ProofAuthorityTickResult:
        """Run a single poll-and-credit step, fail-safe.

        When disabled the tick is a no-op (``PROOF_AUTHORITY_TICK_DISABLED``).
        Otherwise it iterates the live targets, calling
        :meth:`ShadowServerHarness.credit_attested_shares` per target and
        aggregating ``workers_polled`` + ``shares_credited``. A per-target
        exception is caught, recorded into ``errors`` + the audit hook, and the
        cycle CONTINUES — one bad worker never wedges the tick, and the exception
        is never propagated. reward/payout/chain stay OFF on every path.
        """

        if not self.config.enabled:
            result = ProofAuthorityTickResult(status=PROOF_AUTHORITY_TICK_DISABLED)
            self._emit(result)
            return result

        observed_at = now if now is not None else self.clock()
        workers_polled = 0
        shares_credited = 0
        errors: list[str] = []
        for target in self.worker_targets():
            workers_polled += 1
            try:
                summary = self.harness.credit_attested_shares(
                    lane=target.lane,
                    pool_id=target.pool_id,
                    alice_collection_address=target.alice_collection_address,
                    worker_name=target.worker_name,
                    session=target.session,
                    observed_at=observed_at,
                    evidence_provider=self.evidence_provider,
                )
            except Exception as exc:  # noqa: BLE001 — fail-safe: one bad worker, continue.
                errors.append(f"{target.worker_name}: {type(exc).__name__}: {exc}")
                continue
            # Defensive credit-only assert on the per-target summary (the harness
            # already asserts paid_acu == ZERO_DECIMAL per produced result).
            assert summary.paid_acu == "0"
            shares_credited += summary.shares_credited

        with self._lock:
            self._last_polled_at = observed_at
        result = ProofAuthorityTickResult(
            status=PROOF_AUTHORITY_TICK_OK,
            workers_polled=workers_polled,
            shares_credited=shares_credited,
            errored_workers=len(errors),
            observed_at=observed_at,
            errors=tuple(errors),
        )
        self._emit(result)
        return result

    def run_forever(self, *, sleep: Callable[[float], None] | None = None) -> None:
        """Drive a poll-and-credit cycle every ``poll_interval`` until :meth:`stop`.

        Used by the deployed server on a daemon thread. ``sleep`` is injectable for
        tests; it defaults to a :class:`threading.Event`-based wait so a
        :meth:`stop` interrupts the cadence promptly. Each iteration calls the
        fail-safe :meth:`tick`, so a poll error logs + retries next tick and never
        breaks the loop. (Copied from :class:`SettlementScheduler.run_forever`.)
        """

        interval_seconds = self.config.poll_interval.total_seconds()
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

    def _emit(self, result: ProofAuthorityTickResult) -> None:
        # The audit hook itself must never crash the scheduler (fail-safe).
        try:
            self.audit_hook(result)
        except Exception:  # noqa: BLE001 — audit is best-effort telemetry.
            return None


# Re-export so callers can build a summary type reference without importing server.
__all__ = [
    "DEFAULT_PROOF_AUTHORITY_POLL_INTERVAL",
    "PROOF_AUTHORITY_TICK_DISABLED",
    "PROOF_AUTHORITY_TICK_OK",
    "CreditAttestedSharesSummary",
    "ProofAuthorityAuditHook",
    "ProofAuthorityScheduler",
    "ProofAuthoritySchedulerConfig",
    "ProofAuthorityTarget",
    "ProofAuthorityTickResult",
    "WorkerTargetsSource",
]

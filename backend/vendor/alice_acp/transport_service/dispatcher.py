"""The DISPATCHER upstream-relay (doc §2.3) — the §2.3 five roles as asyncio tasks.

The relay is the UPSTREAM-facing half of the proxy pool: it holds one persistent
stratum connection per lane under Alice's own account, translates the upstream job
down to miners (it is the JOB SOURCE the :mod:`stratum_server` pulls from), and
forwards a validator-confirmed SOLUTION back upstream IMMEDIATELY. This is the
FOUNDATION'S revenue coin and is SEPARATE from credit (credit is the validator's
ValidatedShareStore write, unchanged).

THE FIVE ROLES (asyncio tasks / hooks, not OS threads):

* R1 — one persistent upstream stratum connection per lane under Alice's account
  (LTC: ``ltc.f2pool.com:5200`` login ``ssv102.<worker>``, FORCE-IPv4; RVN:
  ravenminer). R1 is ALSO the JOB SOURCE: each upstream ``mining.notify`` is handed
  to R2, translated, cached as the lane's current internal job, and fanned to
  miners. Reconnect with backoff on drop.
* R2 — job translation: upstream job -> :class:`InternalJob` with Alice's
  pool/vardiff stamped + the REAL net difficulty; owns the
  ``internal_job_id <-> (lane, upstream_job_id, extranonce)`` map
  (:class:`~alice_acp.transport_service.jobs.JobTranslationMap`).
* R3 — forward a validator-confirmed SOLUTION (>= net diff) upstream IMMEDIATELY;
  never withhold/batch/queue-with-delay (the hard anti-selfish-mining rule). Record
  the upstream ACK as ADVISORY telemetry only (ACK != credit).
* R4 — admission cap hook: PERMISSIVE / OFF by default (per owner). The
  throttle/shed interface is built; it never blocks early.
* R5 — stats + a reconcile STUB: drift = ``alice_validated`` vs
  ``upstream_reported`` (reuses ops_monitor counters where present).

CREDIT-ONLY + secrets: the relay sets no reward/payout/chain symbol; ``paid_acu``
is untouched. The upstream-submit credentials are read AT USE-TIME from env (never
stored on the instance, hardcoded, or logged); :func:`ensure_no_raw_secret` guards
any identity string that could be surfaced. The upstream connection is INJECTABLE
(a Protocol) so the test suite drives a FAKE in-process upstream — NO real network.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from alice_acp.evidence.types import ensure_no_raw_secret
from alice_acp.shadow_server.types import Lane, utc_now
from alice_acp.transport_service.jobs import (
    SOLUTION_FORWARD_NO_MAPPING,
    SOLUTION_FORWARD_UPSTREAM_DOWN,
    ConfirmedSolution,
    InternalJob,
    JobTranslationMap,
)

#: Stable, secret-free relay event codes (the structured log; no creds, no host).
RELAY_UPSTREAM_CONNECTED = "relay_upstream_connected"
RELAY_UPSTREAM_DISCONNECTED = "relay_upstream_disconnected"
RELAY_JOB_TRANSLATED = "relay_job_translated"
RELAY_SOLUTION_FORWARDED = "relay_solution_forwarded"
RELAY_SOLUTION_ACK = "relay_solution_ack"
RELAY_SOLUTION_DROPPED = "relay_solution_dropped"
RELAY_ADMISSION_SHED = "relay_admission_shed"

#: Env vars the upstream login worker suffix + per-lane account are read from AT
#: USE-TIME (never stored/logged). The SECRET (pool password / api token) likewise
#: is read at connect-time and handed straight to the injected upstream connector,
#: never retained. NONE of the non-secret account/worker values is logged raw.
UPSTREAM_LTC_HOST_ENV = "ALICE_UPSTREAM_LTC_HOST"
UPSTREAM_LTC_PORT_ENV = "ALICE_UPSTREAM_LTC_PORT"
UPSTREAM_LTC_LOGIN_ENV = "ALICE_UPSTREAM_LTC_LOGIN"  # e.g. ssv102.<worker>
UPSTREAM_LTC_PASSWORD_ENV = "ALICE_UPSTREAM_LTC_PASSWORD"  # secret; read at use-time
UPSTREAM_RVN_HOST_ENV = "ALICE_UPSTREAM_RVN_HOST"
UPSTREAM_RVN_PORT_ENV = "ALICE_UPSTREAM_RVN_PORT"
UPSTREAM_RVN_LOGIN_ENV = "ALICE_UPSTREAM_RVN_LOGIN"
UPSTREAM_RVN_PASSWORD_ENV = "ALICE_UPSTREAM_RVN_PASSWORD"
#: Quai/KawPoW upstream (2Miners — the brief's Quai leg). Mirror of the RVN env set.
#: LOGIN is Alice's Quai (0x) address (env-only, NO code default — fail-soft to an
#: unconnected lane if absent, exactly like ``UPSTREAM_RVN_LOGIN_ENV``); PASSWORD is
#: the conventional ``"x"``. Read at use-time, never stored/logged.
UPSTREAM_QUAI_HOST_ENV = "ALICE_UPSTREAM_QUAI_HOST"
UPSTREAM_QUAI_PORT_ENV = "ALICE_UPSTREAM_QUAI_PORT"
UPSTREAM_QUAI_LOGIN_ENV = "ALICE_UPSTREAM_QUAI_LOGIN"  # Alice's Quai 0x address
UPSTREAM_QUAI_PASSWORD_ENV = "ALICE_UPSTREAM_QUAI_PASSWORD"  # secret; read at use-time
#: XMR/RandomX upstream (the brief's Monero leg — supportxmr). LOGIN is Alice's Monero
#: address; PASSWORD is the conventional ``"x"``. Read at use-time, never stored/logged.
UPSTREAM_XMR_HOST_ENV = "ALICE_UPSTREAM_XMR_HOST"
UPSTREAM_XMR_PORT_ENV = "ALICE_UPSTREAM_XMR_PORT"
UPSTREAM_XMR_LOGIN_ENV = "ALICE_UPSTREAM_XMR_LOGIN"  # Alice's Monero address
UPSTREAM_XMR_PASSWORD_ENV = "ALICE_UPSTREAM_XMR_PASSWORD"  # secret; read at use-time

#: Conventional upstream defaults (the brief's LTC leg). NOT secrets.
DEFAULT_UPSTREAM_LTC_HOST = "ltc.f2pool.com"
DEFAULT_UPSTREAM_LTC_PORT = 5200
#: Conventional upstream defaults (the brief's XMR leg — supportxmr). NOT secrets.
DEFAULT_UPSTREAM_XMR_HOST = "pool.supportxmr.com"
DEFAULT_UPSTREAM_XMR_PORT = 3333
#: Conventional upstream defaults (the brief's RVN leg — ravenminer KawPoW stratum). NOT
#: secrets. ravenminer's KawPoW endpoint is ``stratum.ravenminer.com:3838``. (The old
#: ``rvn.ravenminer.com`` host DIED in ravenminer's 2026 re-platform — connection refused;
#: confirmed live 2026-06-01.) A deploy overrides via ``ALICE_UPSTREAM_RVN_HOST`` / ``_PORT``.
DEFAULT_UPSTREAM_RVN_HOST = "stratum.ravenminer.com"
DEFAULT_UPSTREAM_RVN_PORT = 3838
#: Conventional upstream defaults (the brief's Quai leg — 2Miners KawPoW stratum). NOT
#: secrets. 2Miners' Quai-KawPoW endpoint is ``quaikawpow.2miners.com:4545``. Plain TCP
#: (NO TLS — same ``asyncio.open_connection`` pattern as every other upstream). A deploy
#: overrides via ``ALICE_UPSTREAM_QUAI_HOST`` / ``_PORT``.
DEFAULT_UPSTREAM_QUAI_HOST = "quaikawpow.2miners.com"
DEFAULT_UPSTREAM_QUAI_PORT = 4545


@dataclass(frozen=True, slots=True)
class UpstreamCredentials:
    """Read-at-use-time upstream login (host/port/login/password) for one lane.

    Built fresh from env each time the relay connects; NEVER stored on the relay or
    logged. ``password`` is the only secret. ``ensure_no_raw_secret`` is applied to
    the LOGIN (an account/worker string that may end up in a log/telemetry surface)
    but NOT to the password (which is never surfaced anywhere). ``force_ipv4`` mirrors
    the brief's LTC requirement (F2Pool's IPv6 endpoint has been flaky).
    """

    host: str
    port: int
    login: str
    password: str
    force_ipv4: bool = False

    def __post_init__(self) -> None:
        if not self.host or self.port <= 0 or not self.login:
            raise ValueError("upstream credentials require host, port, login")
        # The login is the only credential string that can reach a log surface.
        ensure_no_raw_secret(self.login, field_name="upstream_login")


class UpstreamConnection(Protocol):
    """One persistent upstream stratum connection under Alice's account (R1).

    Production wires :class:`AsyncStratumUpstream` (thin stdlib asyncio TCP, the
    brief's force-IPv4 LTC leg); the test suite injects a FAKE in-process upstream
    so NO real network/credential is used. The relay calls :meth:`connect` once
    (handing the read-at-use-time creds), pumps jobs via the ``on_job`` callback the
    relay registers, forwards solutions via :meth:`submit`, and :meth:`close` on
    shutdown.
    """

    async def connect(self, creds: UpstreamCredentials, on_job: Callable[[dict], None]) -> None: ...

    async def submit(self, payload: dict) -> bool:
        """Send one upstream ``mining.submit``; return whether it was dispatched."""
        ...

    async def close(self) -> None: ...


@dataclass(slots=True)
class AdmissionController:
    """R4 admission cap hook — PERMISSIVE / OFF by default (per owner).

    The throttle/shed interface exists so the deploy can flip it on without a code
    change, but :meth:`admit` returns ``True`` (admit) unconditionally while
    ``enabled`` is ``False`` — it never blocks/sheds early. When enabled it sheds a
    connection once the per-lane live count exceeds ``max_connections_per_lane``
    (a simple, non-blocking cap; the deploy tunes the policy). It NEVER touches
    credit — shedding only declines to serve, it does not reject a validated share.
    """

    enabled: bool = False
    max_connections_per_lane: int = 0

    def admit(self, *, lane: Lane, current_lane_connections: int) -> bool:
        if not self.enabled:
            return True
        if self.max_connections_per_lane <= 0:
            return True
        return current_lane_connections < self.max_connections_per_lane


@dataclass(slots=True)
class LaneRelayStats:
    """R5 per-lane counters: Alice-validated vs upstream-reported (drift source)."""

    jobs_translated: int = 0
    solutions_forwarded: int = 0
    solutions_acked: int = 0
    solutions_dropped: int = 0
    #: Alice's own validated-solution count for this lane (the truth the relay knows).
    alice_validated_solutions: int = 0
    #: What the upstream reported back as accepted (advisory; ACK != credit).
    upstream_reported_acks: int = 0


@dataclass(frozen=True, slots=True)
class ReconcileReport:
    """R5 reconcile STUB output: drift between Alice-validated and upstream-reported.

    ``drift`` is ``alice_validated - upstream_reported`` per lane (a non-zero drift
    is the signal ops watches — withheld/dropped/rejected upstream). This is a STUB:
    it computes + surfaces the drift (reusing the relay's own counters, shaped to
    feed ops_monitor where present) but takes no automated action.
    """

    per_lane_drift: dict[Lane, int]

    @property
    def total_drift(self) -> int:
        return sum(self.per_lane_drift.values())


@dataclass(slots=True)
class _LaneRelay:
    """One lane's live relay state (the current job + the upstream connection)."""

    lane: Lane
    upstream: UpstreamConnection
    creds_factory: Callable[[], UpstreamCredentials | None]
    current_job: InternalJob | None = None
    _subscribers: list[Callable[[InternalJob], None]] = field(default_factory=list)
    _connect_task: asyncio.Task | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)


@dataclass(slots=True)
class DispatcherRelay:
    """The §2.3 upstream relay: JOB SOURCE + SolutionSink for the stratum server.

    Inject one :class:`UpstreamConnection` per lane (``upstreams`` keyed by lane;
    the test injects FAKES), the per-lane credential FACTORIES (read env at
    use-time), the shared :class:`JobTranslationMap` (R2), the
    :class:`AdmissionController` (R4, off by default), and a job-translation
    callable per lane (``translate(lane, upstream_job) -> InternalJob``; the deploy
    supplies the real per-algo translator, a test a trivial one). It satisfies the
    server's ``JobSource`` (``current_job`` / ``subscribe``) and ``SolutionSink``
    (``submit_solution``) Protocols.

    R3 invariant: :meth:`submit_solution` is called SYNCHRONOUSLY by the server on
    the accept path and forwards upstream IMMEDIATELY (it schedules the async submit
    on the running loop with no delay/batch/queue-wait). Returns once the forward is
    dispatched; the upstream ACK is recorded asynchronously as advisory telemetry.
    """

    upstreams: dict[Lane, UpstreamConnection]
    cred_factories: dict[Lane, Callable[[], UpstreamCredentials | None]]
    job_translator: Callable[[Lane, dict], InternalJob | None]
    job_map: JobTranslationMap = field(default_factory=JobTranslationMap)
    admission: AdmissionController = field(default_factory=AdmissionController)
    clock: Callable[[], datetime] = utc_now
    event_log: list[tuple[str, Lane, str]] = field(default_factory=list)
    stats: dict[Lane, LaneRelayStats] = field(default_factory=dict)
    _lanes: dict[Lane, _LaneRelay] = field(default_factory=dict)
    _loop: asyncio.AbstractEventLoop | None = None
    _bg_tasks: set = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        for lane, upstream in self.upstreams.items():
            self._lanes[lane] = _LaneRelay(
                lane=lane,
                upstream=upstream,
                creds_factory=self.cred_factories.get(lane, lambda: None),
            )
            self.stats.setdefault(lane, LaneRelayStats())

    # -- lifecycle (R1) ----------------------------------------------------
    async def start(self) -> None:
        """Open every lane's persistent upstream connection (R1). Idempotent."""

        self._loop = asyncio.get_running_loop()
        for lane, relay in self._lanes.items():
            creds = relay.creds_factory()
            if creds is None:
                # No upstream creds for this lane (env absent) => leave it unconnected.
                # The lane simply has no job source / forward target (fail-soft).
                continue
            await relay.upstream.connect(creds, self._make_job_intake(lane))
            self._emit(RELAY_UPSTREAM_CONNECTED, lane)

    async def stop(self) -> None:
        for lane, relay in self._lanes.items():
            with contextlib.suppress(Exception):
                await relay.upstream.close()
            self._emit(RELAY_UPSTREAM_DISCONNECTED, lane)

    # -- R1 -> R2: upstream job intake + translation -----------------------
    def _make_job_intake(self, lane: Lane) -> Callable[[dict], None]:
        def on_job(upstream_job: dict) -> None:
            self._ingest_upstream_job(lane, upstream_job)

        return on_job

    def _ingest_upstream_job(self, lane: Lane, upstream_job: dict) -> None:
        # R2: translate the upstream job, mint/record the internal job id in the
        # reverse map, cache it as the lane's current job, and fan it to subscribers.
        try:
            job = self.job_translator(lane, upstream_job)
        except Exception:
            # A malformed upstream job must never crash the relay; drop it fail-soft.
            return
        if job is None:
            return
        self.job_map.register(
            internal_job_id=job.internal_job_id,
            lane=lane,
            upstream_job_id=job.upstream_job_id,
            extranonce=job.extranonce,
        )
        relay = self._lanes.get(lane)
        if relay is None:
            return
        with relay._lock:
            relay.current_job = job
            subscribers = list(relay._subscribers)
        self.stats.setdefault(lane, LaneRelayStats()).jobs_translated += 1
        self._emit(RELAY_JOB_TRANSLATED, lane)
        for callback in subscribers:
            with contextlib.suppress(Exception):
                callback(job)

    # -- JobSource Protocol (the server pulls from here) -------------------
    def current_job(self, lane: Lane) -> InternalJob | None:
        relay = self._lanes.get(lane)
        if relay is None:
            return None
        with relay._lock:
            return relay.current_job

    def subscribe(self, lane: Lane, callback: Callable[[InternalJob], None]) -> Callable[[], None]:
        relay = self._lanes.get(lane)
        if relay is None:
            return lambda: None
        with relay._lock:
            relay._subscribers.append(callback)

        def unsubscribe() -> None:
            with relay._lock:
                with contextlib.suppress(ValueError):
                    relay._subscribers.remove(callback)

        return unsubscribe

    # -- SolutionSink Protocol (R3 — IMMEDIATE forward) --------------------
    def submit_solution(self, solution: ConfirmedSolution) -> bool:
        """Forward a confirmed solution upstream IMMEDIATELY (R3). Never delays.

        Called synchronously from the server's validator-accept path. We record the
        Alice-validated solution (R5 truth), resolve the upstream job id from the R2
        map, assemble the upstream submit, and dispatch it on the running loop with
        NO batching/queue-wait. The upstream ACK is recorded asynchronously as
        advisory telemetry (ACK != credit). Returns ``True`` when the forward was
        dispatched; ``False`` when it could not be assembled (no mapping / no
        upstream) — a drop that NEVER affects credit (already written by the
        validator).
        """

        lane = solution.lane
        stats = self.stats.setdefault(lane, LaneRelayStats())
        stats.alice_validated_solutions += 1

        mapping = self.job_map.resolve(solution.internal_job_id)
        if mapping is None:
            stats.solutions_dropped += 1
            self._emit(RELAY_SOLUTION_DROPPED, lane, SOLUTION_FORWARD_NO_MAPPING)
            return False
        _, upstream_job_id, extranonce = mapping
        relay = self._lanes.get(lane)
        if relay is None:
            stats.solutions_dropped += 1
            self._emit(RELAY_SOLUTION_DROPPED, lane, SOLUTION_FORWARD_UPSTREAM_DOWN)
            return False

        payload = _build_upstream_submit(
            lane=lane,
            upstream_job_id=upstream_job_id,
            extranonce=extranonce,
            solution=solution,
        )
        stats.solutions_forwarded += 1
        self._emit(RELAY_SOLUTION_FORWARDED, lane)
        # Dispatch the actual network submit IMMEDIATELY on the loop — no delay, no
        # batch, no queue-with-wait. We do not block the caller on the ACK; the ACK
        # is advisory telemetry recorded when the coroutine completes.
        self._dispatch_immediately(relay.upstream, payload, lane)
        return True

    def _dispatch_immediately(
        self, upstream: UpstreamConnection, payload: dict, lane: Lane
    ) -> None:
        async def _do() -> None:
            acked = False
            with contextlib.suppress(Exception):
                acked = await upstream.submit(payload)
            stats = self.stats.setdefault(lane, LaneRelayStats())
            if acked:
                stats.solutions_acked += 1
                stats.upstream_reported_acks += 1
                self._emit(RELAY_SOLUTION_ACK, lane)

        loop = self._loop
        if loop is None or not loop.is_running():  # pragma: no cover - degenerate
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            # Same loop (the server runs on the relay's loop): schedule a task so the
            # forward goes out on the very next loop step (immediate, no delay)
            # without re-entrancy. Keep a reference so it is not GC'd mid-flight.
            task = loop.create_task(_do())
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
        else:
            # Cross-thread caller: hand it to the relay loop immediately.
            asyncio.run_coroutine_threadsafe(_do(), loop)

    # -- R4 admission hook -------------------------------------------------
    def admit_connection(self, *, lane: Lane, current_lane_connections: int) -> bool:
        admitted = self.admission.admit(
            lane=lane, current_lane_connections=current_lane_connections
        )
        if not admitted:
            self._emit(RELAY_ADMISSION_SHED, lane)
        return admitted

    # -- R5 stats + reconcile stub -----------------------------------------
    def reconcile(self) -> ReconcileReport:
        """R5 reconcile STUB: drift = alice_validated - upstream_reported per lane.

        A non-zero drift means Alice validated a solution the upstream did not (yet)
        report accepted — the signal ops watches (could indicate a withheld/stale/
        rejected forward). STUB: surfaces the drift only (no automated action). The
        numbers are shaped so a deploy can feed them into ops_monitor's proof-ingest
        counters where present.
        """

        drift: dict[Lane, int] = {}
        for lane, stat in self.stats.items():
            drift[lane] = stat.alice_validated_solutions - stat.upstream_reported_acks
        return ReconcileReport(per_lane_drift=drift)

    def _emit(self, code: str, lane: Lane, detail: str = "") -> None:
        self.event_log.append((code, lane, detail))

    def event_codes(self) -> list[str]:
        return [code for code, _, _ in self.event_log]


# --- credential readers (env, at use-time; never stored/logged) --------------


def _read_decimal(value: str | None) -> Decimal | None:
    if not value:
        return None
    try:
        parsed = Decimal(value)
    except (InvalidOperation, ValueError):
        return None
    return parsed if parsed > 0 else None


def ltc_credentials_from_env(env: dict[str, str] | None = None) -> UpstreamCredentials | None:
    """Read the LTC/F2Pool upstream creds from env AT USE-TIME (force-IPv4).

    Returns ``None`` when the login (the only required, non-defaulted field) is
    absent — the lane then stays unconnected (fail-soft). The password (the secret)
    is read here and handed straight to the connector; it is never stored on the
    relay or logged. Host/port default to ``ltc.f2pool.com:5200``.
    """

    source = env if env is not None else os.environ
    login = (source.get(UPSTREAM_LTC_LOGIN_ENV) or "").strip()
    if not login:
        return None
    host = (source.get(UPSTREAM_LTC_HOST_ENV) or DEFAULT_UPSTREAM_LTC_HOST).strip()
    port_raw = (source.get(UPSTREAM_LTC_PORT_ENV) or "").strip()
    try:
        port = int(port_raw) if port_raw else DEFAULT_UPSTREAM_LTC_PORT
    except ValueError:
        port = DEFAULT_UPSTREAM_LTC_PORT
    password = source.get(UPSTREAM_LTC_PASSWORD_ENV) or "x"
    return UpstreamCredentials(
        host=host, port=port, login=login, password=password, force_ipv4=True
    )


def rvn_credentials_from_env(env: dict[str, str] | None = None) -> UpstreamCredentials | None:
    """Read the RVN/ravenminer KawPoW upstream creds from env AT USE-TIME (fail-soft).

    Returns ``None`` when the login (Alice's Ravencoin address — the only required,
    non-defaulted field) is absent; the lane then stays unconnected (fail-soft), exactly
    like the LTC/XMR legs. The password (the secret, conventionally ``"x"``) is read here
    and handed straight to the connector; it is never stored on the relay or logged.
    Host/port default to ``rvn.ravenminer.com:3838`` (the KawPoW stratum convention). No
    ``force_ipv4`` (ravenminer's dual-stack is fine).
    """

    source = env if env is not None else os.environ
    login = (source.get(UPSTREAM_RVN_LOGIN_ENV) or "").strip()
    if not login:
        return None
    host = (source.get(UPSTREAM_RVN_HOST_ENV) or DEFAULT_UPSTREAM_RVN_HOST).strip()
    port_raw = (source.get(UPSTREAM_RVN_PORT_ENV) or "").strip()
    try:
        port = int(port_raw) if port_raw else DEFAULT_UPSTREAM_RVN_PORT
    except ValueError:
        port = DEFAULT_UPSTREAM_RVN_PORT
    password = source.get(UPSTREAM_RVN_PASSWORD_ENV) or "x"
    return UpstreamCredentials(host=host, port=port, login=login, password=password)


def quai_credentials_from_env(env: dict[str, str] | None = None) -> UpstreamCredentials | None:
    """Read the Quai/2Miners KawPoW upstream creds from env AT USE-TIME (fail-soft).

    A verbatim mirror of :func:`rvn_credentials_from_env`. Returns ``None`` when the
    login (Alice's Quai 0x address — the only required, non-defaulted field) is absent;
    the lane then stays unconnected (fail-soft), exactly like the LTC/XMR/RVN legs. The
    password (the secret, conventionally ``"x"``) is read here and handed straight to the
    connector; it is never stored on the relay or logged. Host/port default to
    ``quaikawpow.2miners.com:4545`` (the 2Miners Quai-KawPoW stratum convention, plain
    TCP). No ``force_ipv4`` (2Miners' dual-stack is fine).
    """

    source = env if env is not None else os.environ
    login = (source.get(UPSTREAM_QUAI_LOGIN_ENV) or "").strip()
    if not login:
        return None
    host = (source.get(UPSTREAM_QUAI_HOST_ENV) or DEFAULT_UPSTREAM_QUAI_HOST).strip()
    port_raw = (source.get(UPSTREAM_QUAI_PORT_ENV) or "").strip()
    try:
        port = int(port_raw) if port_raw else DEFAULT_UPSTREAM_QUAI_PORT
    except ValueError:
        port = DEFAULT_UPSTREAM_QUAI_PORT
    password = source.get(UPSTREAM_QUAI_PASSWORD_ENV) or "x"
    return UpstreamCredentials(host=host, port=port, login=login, password=password)


def xmr_credentials_from_env(env: dict[str, str] | None = None) -> UpstreamCredentials | None:
    """Read the XMR/supportxmr upstream creds from env AT USE-TIME (fail-soft).

    Returns ``None`` when the login (Alice's Monero address — the only required,
    non-defaulted field) is absent; the lane then stays unconnected (fail-soft). The
    password (the secret, conventionally ``"x"``) is read here and handed straight to
    the connector; it is never stored on the relay or logged. Host/port default to
    ``pool.supportxmr.com:3333``. No ``force_ipv4`` (supportxmr's dual-stack is fine).
    """

    source = env if env is not None else os.environ
    login = (source.get(UPSTREAM_XMR_LOGIN_ENV) or "").strip()
    if not login:
        return None
    host = (source.get(UPSTREAM_XMR_HOST_ENV) or DEFAULT_UPSTREAM_XMR_HOST).strip()
    port_raw = (source.get(UPSTREAM_XMR_PORT_ENV) or "").strip()
    try:
        port = int(port_raw) if port_raw else DEFAULT_UPSTREAM_XMR_PORT
    except ValueError:
        port = DEFAULT_UPSTREAM_XMR_PORT
    password = source.get(UPSTREAM_XMR_PASSWORD_ENV) or "x"
    return UpstreamCredentials(host=host, port=port, login=login, password=password)


# --- upstream submit assembly (lane-aware; secret-free) ----------------------


def _build_upstream_submit(
    *,
    lane: Lane,
    upstream_job_id: str,
    extranonce: str,
    solution: ConfirmedSolution,
) -> dict[str, Any]:
    """Assemble the upstream submit for a confirmed solution (R3).

    Lane-aware (the server carried each lane's fields on the :class:`ConfirmedSolution`):

    * Scrypt — Bitcoin-family positional ``[login_worker, job_id, extranonce2, ntime,
      nonce]`` (``mining.submit``);
    * KawPoW — Ethereum/Ravencoin positional ``[login_worker, job_id, nonce, headerHash,
      mixHash]`` (``mining.submit``). The ``headerHash`` is the POW header the rig answered
      (``ConfirmedSolution.header_hash_hex``), NOT Alice's ``canonical_share_hash`` (the
      credit-binding envelope hash — a DIFFERENT value the upstream pool would reject);
    * RandomX/XMR — the cryptonote/xmrig OBJECT dialect ``submit {id, job_id, nonce,
      result}`` (NOT positional). ``id`` (the pool session) is filled by the Monero
      connector at submit time; ``result`` is the rig's RandomX result hash
      (``result_hash_hex``). The Monero pool attributes the share via the session id, so
      there is NO worker field to rewrite (unlike the Bitcoin-family ``params[0]``).

    For the Bitcoin-family lanes the login worker is filled by the connector at submit
    time from the at-use-time creds (the relay carries only the opaque server
    worker_name as a placeholder the connector overwrites). No secret is in this dict.
    """

    from alice_acp.mining_session.types import RVN_KAWPOW
    from alice_acp.shadow_server.types import MAIN_POOL_GPU_QUAI, MAIN_POOL_GPU_RVN, XMR_POOL

    if lane == XMR_POOL:
        # The cryptonote/xmrig OBJECT submit. ``id`` is a placeholder the Monero
        # connector overwrites with the pool-issued session id; ``result`` is the rig's
        # RandomX result hash (carried on the ConfirmedSolution as ``result_hash_hex``).
        return {
            "id": None,
            "method": "submit",
            "params": {
                "id": "",
                "job_id": upstream_job_id,
                "nonce": solution.nonce_hex,
                "result": solution.result_hash_hex,
            },
        }

    if lane in (MAIN_POOL_GPU_RVN, MAIN_POOL_GPU_QUAI):
        # Both KawPoW lanes use the SAME positional KawPoW submit layout (the Quai lane
        # reuses the RVN wire byte-for-byte; only its upstream/identity differ).
        algo = RVN_KAWPOW
    else:
        algo = "LTC_SCRYPT"

    if algo == RVN_KAWPOW:
        # KawPoW positional ``mining.submit``: [login_worker, job_id, nonce, headerHash,
        # mixHash]. The headerHash is the POW header the rig answered (carried verbatim on
        # ConfirmedSolution.header_hash_hex), NOT Alice's canonical_share_hash (that is the
        # credit-binding envelope hash, a different value the upstream pool would reject).
        params = [
            solution.worker_name,
            upstream_job_id,
            solution.nonce_hex,
            solution.header_hash_hex,
            solution.mix_hash_hex,
        ]
    else:
        params = [
            solution.worker_name,
            upstream_job_id,
            solution.extranonce2_hex or extranonce,
            solution.ntime_hex,
            solution.nonce_hex,
        ]
    return {"id": None, "method": "mining.submit", "params": params}

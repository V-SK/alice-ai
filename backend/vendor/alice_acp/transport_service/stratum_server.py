"""The RUNNABLE miner-facing STRATUM SERVER (doc §2.1) — thin stdlib asyncio.

This is the wire layer the merged :mod:`alice_acp.transport_front` glue was
explicitly written to sit behind ("a future asyncio/stdlib server owns the wire").
It adds NO heavy dependency: just :func:`asyncio.start_server` per lane port,
newline-delimited JSON framing, and a per-connection lifecycle that DRIVES the
merged :class:`~alice_acp.transport_front.connection.StratumConnection`:

  accept TCP  -> read newline JSON frames
              -> mining.subscribe  : reply subscribe envelope (extranonce assign)
              -> login/authorize   : conn.handle_login (resolve_login)
                                     -> on accept: push set_difficulty + first job
              -> mining.submit     : conn.handle_submit (-> ShareValidator.validate)
                                     -> ACK/NACK; on ACCEPT: forward the share to R3 NOW
              -> new upstream job  : push mining.notify to every connected miner

FAIL-CLOSED (the brief's hard rules):
* gate OFF / unresolved login -> the resolver returns ``LoginRejected``; the server
  writes the JSON-RPC error reply AND DROPS the connection (no jobs, no submits).
* a validator error / any non-accept -> NACK the share; credit nothing. A malformed
  frame -> structured error reply, never a crashed loop (each frame is guarded).

THE SHARE-FORWARD SEAM (doc §2.1 -> §2.3 R3): the merged ``connection.py`` classifies
``is_share`` against the per-connection vardiff pool target (drives credit, which the
validator already wrote to the ValidatedShareStore). EVERY accepted share is then
forwarded to the injected :class:`SolutionSink` IMMEDIATELY, synchronously, on the
accept path — because the upstream pool measures Alice's account by the SHARES it
receives at pool difficulty, not by solved blocks (an ASIC essentially never solves a
block, so forwarding only block-level solutions sent the upstream pool nothing and the
account read 0 H/s / 0 workers). The SERVER still classifies whether the share ALSO
cleared the REAL upstream net difficulty on the connection's CURRENT job
(:attr:`InternalJob.net_difficulty`, fed from R1/R2 — replacing the ``transport_front``
placeholder net target); that ``is_solution`` bit is now TELEMETRY ONLY (it rides the
forward event detail) and never gates the forward. Never batched / queued-with-delay /
withheld.

CREDIT-ONLY: the server sets no reward/payout/chain symbol; the credited unit is
the validator's store write (unchanged); the upstream forward is the foundation's
revenue coin (R3), separate from credit. No secret is logged: the structured
``ServerEvent`` log carries only stable codes + opaque ids (already
``ensure_no_raw_secret``-guarded upstream).
"""

from __future__ import annotations

import asyncio
import contextlib
import itertools
import json
import os
import socket
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from alice_acp.shadow_server.types import (
    MAIN_POOL_GPU_QUAI,
    MAIN_POOL_GPU_RVN,
    SCRYPT_POOL,
    XMR_POOL,
    Lane,
    utc_now,
)
from alice_acp.transport_front.connection import StratumConnection, SubmitResult
from alice_acp.transport_front.identity import StratumIdentityResolver, lane_for_port
from alice_acp.transport_front.stratum_messages import (
    STRATUM_METHOD_AUTHORIZE,
    STRATUM_METHOD_LOGIN,
    STRATUM_METHOD_SUBMIT_BITCOINFAMILY,
    STRATUM_METHOD_SUBMIT_RANDOMX,
    STRATUM_METHOD_SUBSCRIBE,
    build_monero_job_object,
    build_set_target_notification,
    build_subscribe_reply,
)
from alice_acp.transport_front.submit_rate_limit import (
    DEFAULT_MAX_CONSECUTIVE_REJECTS,
    DEFAULT_SUBMIT_RATE_WINDOW,
    SubmitRateLimiter,
    for_lane_rate,
)
from alice_acp.transport_front.vardiff import (
    DEFAULT_RETARGET_SAMPLES,
    DEFAULT_TARGET_SHARE_INTERVAL,
    DEFAULT_VARDIFF_FLOOR,
    NO_VARDIFF_FLOOR_CLAMP,
)
from alice_acp.transport_service.jobs import (
    ConfirmedSolution,
    InternalJob,
    JobSource,
    SolutionSink,
)
from alice_acp.transport_service.kawpow_translator import (
    KAWPOW_EPOCH_LENGTH,
    KAWPOW_HEADER_HASH_BYTES,
    difficulty_to_target_256,
)
from alice_acp.transport_service.monero_translator import difficulty_to_compact_target
from alice_acp.transport_service.scrypt_translator import (
    NONCE_LEN,
    assemble_scrypt_header,
    build_coinbase,
    merkle_root_from_coinbase,
)

#: Bound on the per-server recent-job cache (internal_job_id -> InternalJob). A submit
#: re-derives its header from the job it answered, so the server keeps the recent jobs
#: it pushed/broadcast. Generous (covers many lanes × rapid job turnover) yet bounded
#: so a long-running server never grows unboundedly; the oldest job is evicted (a
#: submit for an evicted/stale job simply fails to reconstruct -> fail-closed NACK).
MAX_CACHED_JOBS = 512

#: RandomX/XMR blob handling: Alice does NOT rewrite any byte of the upstream mining
#: blob. The blob is handed DOWN to the rig verbatim and re-hashed (and the rig's result
#: forwarded upstream) against that SAME blob — mutating it (e.g. a per-connection
#: extra-nonce at byte 8) diverges Alice's re-hash from the rig's result AND the upstream
#: pool, which re-validates against its own unmodified blob, so every share would be
#: rejected and nothing would be earned upstream. Two rigs sharing one cached upstream
#: job are de-collided by their independent 2**32 nonce search at offset 39 + the upstream
#: pool's (job_id, nonce) dedup — never by rewriting the shared block-template bytes.
#: The Monero mining-blob nonce offset (bytes [39:43]) — MUST match the verifier's
#: ``nonce_offset`` and the translator's ``MONERO_NONCE_OFFSET``. The server splices the
#: rig's submitted nonce here when reconstructing the blob (byte-exact with what the rig
#: hashed); the verifier independently re-splices the same bytes before re-hashing.
MONERO_NONCE_OFFSET = 39
MONERO_NONCE_LEN = 4

#: Stable, secret-free event codes for the structured server log (no raw payloads,
#: no host detail). Mirrors the transport_front reason-code discipline.
SERVER_CONN_OPENED = "stratum_server_connection_opened"
SERVER_CONN_CLOSED = "stratum_server_connection_closed"
SERVER_LOGIN_ACCEPTED = "stratum_server_login_accepted"
SERVER_LOGIN_REJECTED = "stratum_server_login_rejected"
SERVER_SUBMIT_ACCEPTED = "stratum_server_submit_accepted"
SERVER_SUBMIT_REJECTED = "stratum_server_submit_rejected"
SERVER_SOLUTION_FORWARDED = "stratum_server_solution_forwarded"
SERVER_FRAME_OVERSIZE = "stratum_server_frame_oversize"
SERVER_FRAME_MALFORMED = "stratum_server_frame_malformed"
#: A new connection refused at admission (per-IP cap / global cap / lane shed): the
#: socket is closed immediately, secret-free (no peer host, no payload — only a stable
#: reason code as ``detail``). An unauthenticated peer can therefore neither open
#: unlimited sockets nor exhaust the global slot budget.
SERVER_CONN_REJECTED = "stratum_server_connection_rejected"
#: A connection dropped because it did not subscribe+authorize within the pre-login
#: deadline (slowloris defense): an idle/half-open peer cannot pin a ``readuntil``
#: forever. Secret-free.
SERVER_CONN_LOGIN_TIMEOUT = "stratum_server_connection_login_timeout"
#: A connection DROPPED because its per-connection submit-FLOOD limiter tripped (a
#: line-rate sub-target / invalid submit flood). The socket is closed and the peer is
#: BRIEFLY BANNED at admission so it cannot immediately reconnect and re-flood. The
#: stable limiter reason rides as the event ``detail`` (never a peer host / payload).
SERVER_CONN_SUBMIT_FLOOD = "stratum_server_connection_submit_flood"
#: A NEW connection refused at admission because the peer is inside its brief submit-flood
#: ban window (set when a prior connection from that peer tripped the flood limiter).
#: Secret-free (the stable reason as ``detail`` only).
SERVER_CONN_BANNED = "stratum_server_connection_banned"

#: Secret-free admission rejection reasons (carried as the event ``detail``; never a
#: peer host or payload).
ADMIT_REJECT_PER_IP = "per_ip_cap"
ADMIT_REJECT_GLOBAL = "global_cap"
ADMIT_REJECT_LANE = "lane_shed"
#: A new connection refused because the peer is inside its brief submit-flood ban window.
ADMIT_REJECT_BANNED = "submit_flood_ban"

#: A hard per-frame ceiling so a hostile/buggy rig cannot OOM the reader with one
#: unterminated line. A real stratum frame is well under this; a longer line is
#: dropped fail-closed.
DEFAULT_MAX_FRAME_BYTES = 64 * 1024

#: DEFAULT pre-login deadline (seconds): a connection has this long to complete
#: ``mining.subscribe`` + ``mining.authorize`` (a successful login). A peer that opens a
#: socket and sends nothing (or never finishes the handshake) is dropped at this
#: deadline — closing the slowloris that would otherwise pin a ``readuntil`` forever.
#: Generous enough that a real rig (which subscribes+authorizes in well under a second)
#: is never affected; the existing tests' happy path logs in immediately.
DEFAULT_PRE_LOGIN_TIMEOUT_S = 30.0

#: DEFAULT per-IP and GLOBAL connection caps. ``0`` = UNLIMITED (the default — so the
#: existing tests' happy path, which opens a handful of loopback connections, is never
#: throttled; the deploy sets real caps). When > 0 the server refuses a NEW connection
#: from an IP already at the per-IP cap, or any new connection once the global live
#: count is at the global cap — closing the socket immediately, secret-free.
DEFAULT_MAX_CONNECTIONS_PER_IP = 0
DEFAULT_MAX_CONNECTIONS_GLOBAL = 0

#: DEFAULT TCP keepalive idle/interval (seconds) applied to accepted sockets when
#: keepalive is enabled. Keepalive lets the OS reap a silently-dead peer (a yanked
#: cable / crashed rig) so a half-open socket does not leak a connection slot forever.
DEFAULT_TCP_KEEPALIVE_IDLE_S = 60
DEFAULT_TCP_KEEPALIVE_INTERVAL_S = 30

#: DEFAULT brief ban (seconds) applied to a peer whose connection tripped the
#: per-connection submit-flood limiter. While inside the window a NEW connection from that
#: peer is refused at admission, so a flooder cannot instantly reconnect and re-flood. Kept
#: SHORT (a legit rig is never flood-limited, so it never sees this) — long enough to make a
#: reconnect-flood loop pointless, short enough that a transient mistake self-heals. The ban
#: is keyed by peer IP (the same bucket as the per-IP cap); a peer with no exposed peername
#: (degenerate transport) is not bannable (it was never per-IP-capped either) — the global
#: cap + the per-connection drop still bound it. ``0`` disables the brief ban (the drop
#: still happens; only the reconnect-refusal is off).
DEFAULT_SUBMIT_FLOOD_BAN_S = 30.0

#: DEFAULT cap on the number of submit re-hashes that may run CONCURRENTLY in the verify
#: thread pool (the off-loop bound). ``None`` → sized at runtime to ``os.cpu_count()`` (≈
#: cores) so total re-hash CPU work is bounded even under load while the event loop stays
#: responsive. A positive value pins it explicitly (the deploy may tune it).
DEFAULT_VERIFY_CONCURRENCY: int | None = None

#: Floor/ceiling for the auto-sized verify concurrency (when ``DEFAULT_VERIFY_CONCURRENCY``
#: is ``None``). At least 1 (always make progress); capped so a many-core host does not
#: spin up an unreasonably large pool of (memory-heavy, e.g. RandomX ~256 MiB/seed) verify
#: threads — the deploy raises the ceiling via env if it has the RAM.
MIN_VERIFY_CONCURRENCY = 1
MAX_AUTO_VERIFY_CONCURRENCY = 8


@dataclass(frozen=True, slots=True)
class ServerEvent:
    """One structured, secret-free server log event (for tests/ops; no raw data)."""

    code: str
    lane: Lane | None = None
    detail: str = ""
    at: datetime | None = None


@dataclass(slots=True)
class LaneListenerConfig:
    """One lane's listener wiring (doc §2.1).

    ``lane`` is the SERVER-SIDE lane authority for this listener (a client never
    supplies the lane). ``lane_port`` is the canonical lane port the
    :class:`StratumConnection`'s identity resolution uses for its PORT->lane lookup
    (it MUST map to ``lane`` in the resolver's env). ``bind_port`` is the socket the
    server actually binds; it defaults to ``lane_port`` and is set to ``0`` to bind
    an EPHEMERAL port (tests) while the connection still resolves the lane via
    ``lane_port``. ``host`` defaults to loopback; the deploy binds the real
    interface. ``extranonce2_size`` shapes the ``mining.subscribe`` reply.
    """

    lane: Lane
    lane_port: int
    bind_port: int | None = None
    host: str = "127.0.0.1"
    extranonce2_size: int = 4

    @property
    def effective_bind_port(self) -> int:
        return self.lane_port if self.bind_port is None else self.bind_port


@dataclass(slots=True)
class _MinerConnection:
    """Per-accepted-socket server-side state wrapping the merged StratumConnection."""

    lane: Lane
    handler: StratumConnection
    writer: asyncio.StreamWriter
    extranonce1: str
    connection_id: int
    #: The peer's IP (best-effort, for the per-IP cap release on disconnect; ``""`` when
    #: the platform did not expose a peername). Never logged raw.
    peer_ip: str = ""
    #: The connection's reader (the accept loop reads newline frames from it). Stored so
    #: the two-phase pre-login/post-login read loop can share one frame reader.
    reader: asyncio.StreamReader | None = None
    unsubscribe: Callable[[], None] | None = None
    #: The downstream extranonce1 sent to this rig at subscribe (upstream extranonce1 +
    #: this connection's suffix) and the suffix alone. ``suffix_hex`` is what R3 prepends
    #: to the rig's extranonce2 to reconstruct the FULL upstream extranonce2 on a forward.
    downstream_extranonce1: str = ""
    suffix_hex: str = ""
    #: KawPoW/RVN ONLY: the last ``mining.set_target`` target hex pushed to THIS connection
    #: (the per-connection vardiff target). Tracked so a ``set_target`` is sent ONLY when the
    #: target actually changes — NOT redundantly re-pushed on every (frequent) upstream job.
    #: The new-JOB ``mining.notify`` still fans out every job (T-Rex needs the fresh
    #: headerHash) but carries the job's OWN target (shared, built once), so a job push never
    #: forces the rig to re-evaluate an unchanged per-connection target. ``None`` until the
    #: first push (login / first retarget). Other lanes leave it unused.
    last_kawpow_target: str | None = None
    _write_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


@dataclass(slots=True)
class StratumServer:
    """A runnable multi-lane stratum server over thin stdlib asyncio (doc §2.1).

    Inject the shared :class:`StratumIdentityResolver` + the per-lane
    :class:`~alice_acp.transport_front.connection.StratumConnection` FACTORY
    (``connection_factory(lane, port) -> StratumConnection`` — one fresh handler
    per accepted socket, each bound to the SAME shared ``ShareValidator`` so credit
    accrues to one ValidatedShareStore). ``job_source`` (R1/R2) feeds jobs down;
    ``solution_sink`` (R3) receives confirmed solutions immediately. ``lanes`` lists
    the listeners to bind. ``clock`` is injectable for deterministic tests.

    Lifecycle: :meth:`start` binds every lane's :func:`asyncio.start_server` and
    subscribes the lane's job stream (so a new upstream job fans out to all
    connected miners on that lane); :meth:`serve_forever` awaits them;
    :meth:`stop` closes listeners + every open miner socket.

    DoS HARDENING (the accept-path admission + timeouts). Every accepted socket is
    admitted BEFORE it is served: a per-IP cap and a global cap (server-owned counters,
    released on EVERY exit path) plus the injected R4 ``admit_connection`` lane hook —
    a refusal closes the socket immediately, secret-free. The reads up to a SUCCESSFUL
    login are wrapped in a pre-login deadline (``pre_login_timeout_s``) so a peer that
    opens a socket and never finishes the handshake (slowloris) is dropped; the 64KB
    per-frame cap still bounds a single unterminated line. TCP keepalive on the accepted
    socket lets the OS reap a silently-dead peer. All caps/timeouts default to OFF /
    generous so the happy path (a rig that subscribes+authorizes immediately) is never
    affected; the deploy sets real values from env.
    """

    resolver: StratumIdentityResolver
    connection_factory: Callable[[Lane, int], StratumConnection]
    job_source: JobSource
    solution_sink: SolutionSink
    lanes: tuple[LaneListenerConfig, ...]
    clock: Callable[[], datetime] = utc_now
    max_frame_bytes: int = DEFAULT_MAX_FRAME_BYTES
    #: Pre-login deadline (seconds): a connection must complete subscribe+authorize
    #: within this or it is dropped (slowloris defense). ``<= 0`` disables the deadline.
    pre_login_timeout_s: float = DEFAULT_PRE_LOGIN_TIMEOUT_S
    #: Per-IP / global connection caps (``0`` = unlimited). Enforced at admission.
    max_connections_per_ip: int = DEFAULT_MAX_CONNECTIONS_PER_IP
    max_connections_global: int = DEFAULT_MAX_CONNECTIONS_GLOBAL
    #: The R4 admission hook (``admit_connection(*, lane, current_lane_connections) ->
    #: bool``) — the relay's :class:`AdmissionController` (per-lane cap; PERMISSIVE/OFF by
    #: default). ``None`` skips the lane check (the per-IP/global caps still apply).
    admit_connection: Callable[..., bool] | None = None
    #: Enable TCP keepalive on accepted sockets (default on). The idle/interval seconds
    #: are best-effort (only set where the platform exposes the option).
    tcp_keepalive: bool = True
    tcp_keepalive_idle_s: int = DEFAULT_TCP_KEEPALIVE_IDLE_S
    tcp_keepalive_interval_s: int = DEFAULT_TCP_KEEPALIVE_INTERVAL_S
    #: Brief ban (seconds) for a peer whose connection tripped the submit-flood limiter:
    #: a NEW connection from that peer is refused at admission while inside the window
    #: (a flooder cannot instantly reconnect). ``0`` disables the reconnect-refusal (the
    #: per-connection drop still fires). A legit rig is never flood-limited, so never banned.
    submit_flood_ban_s: float = DEFAULT_SUBMIT_FLOOD_BAN_S
    #: Max submit re-hashes running CONCURRENTLY in the verify thread pool (the off-loop
    #: bound — an ``asyncio.Semaphore`` sized to this). ``None`` → ``os.cpu_count()`` clamped
    #: to ``[MIN_VERIFY_CONCURRENCY, MAX_AUTO_VERIFY_CONCURRENCY]`` (≈ cores) so total
    #: re-hash CPU is bounded while the loop stays responsive. The deploy may pin it via env.
    verify_concurrency: int | None = DEFAULT_VERIFY_CONCURRENCY
    event_log: list[ServerEvent] = field(default_factory=list)
    _servers: list[asyncio.AbstractServer] = field(default_factory=list)
    _bound_ports: dict[Lane, int] = field(default_factory=dict)
    _connections: dict[int, _MinerConnection] = field(default_factory=dict)
    _conn_ids: itertools.count = field(default_factory=lambda: itertools.count(1))
    _extranonce_ids: itertools.count = field(default_factory=lambda: itertools.count(1))
    _lane_connections: dict[Lane, set[int]] = field(default_factory=dict)
    #: Live connection count per peer IP (the per-IP cap counter). Incremented at
    #: admission, decremented on EVERY disconnect path; an IP drops out of the map at 0.
    _connections_by_ip: dict[str, int] = field(default_factory=dict)
    #: Recent jobs by internal job id (the Scrypt submit re-derives its header from the
    #: job it answered). Bounded by :data:`MAX_CACHED_JOBS`; oldest evicted.
    _jobs_by_id: dict[str, InternalJob] = field(default_factory=dict)
    _job_order: list[str] = field(default_factory=list)
    #: Peer-IP -> monotonic deadline (``loop.time()``) until which a brief submit-flood
    #: ban holds. Set when a connection from that peer trips the flood limiter; checked at
    #: admission. Pruned lazily (an expired entry is dropped when next seen). Bounded by the
    #: number of DISTINCT flooding peers in the last ``submit_flood_ban_s`` — tiny in
    #: practice (a flood is a handful of IPs), and a single peer only ever holds ONE entry.
    _banned_ips: dict[str, float] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)
    _env: dict[str, str] | None = None
    _loop: asyncio.AbstractEventLoop | None = None
    _bg_tasks: set = field(default_factory=set)
    #: The bounded thread pool the PoW re-hash runs in (OFF the event loop) + the
    #: concurrency Semaphore gating it (sized ≈ cores). Created in :meth:`start`, shut down
    #: in :meth:`stop`. ``None`` before start / after stop.
    _verify_pool: ThreadPoolExecutor | None = None
    _verify_semaphore: asyncio.Semaphore | None = None

    def __post_init__(self) -> None:
        # The resolver's env is the lane-port authority; reuse it so the server's
        # PORT->lane mapping matches the resolver's exactly.
        self._env = self.resolver.env

    # -- lifecycle ---------------------------------------------------------
    async def start(self) -> None:
        """Bind every lane listener + subscribe its upstream job stream.

        Also stands up the bounded VERIFY THREAD POOL + its concurrency Semaphore: the
        per-submit PoW re-hash runs in this pool (OFF the event loop) so one rig's
        expensive re-hash can never block the loop for the other miners, and the Semaphore
        (sized ≈ cores) bounds how many re-hashes run at once so total re-hash CPU stays
        bounded even under load.
        """

        self._loop = asyncio.get_running_loop()
        concurrency = self._verify_concurrency()
        # ``thread_name_prefix`` aids ops; ``max_workers`` == the Semaphore size so the pool
        # is never the bottleneck the Semaphore already enforces (and never larger).
        self._verify_pool = ThreadPoolExecutor(
            max_workers=concurrency, thread_name_prefix="alice-stratum-verify"
        )
        self._verify_semaphore = asyncio.Semaphore(concurrency)
        for lane_cfg in self.lanes:
            lane = lane_cfg.lane
            # The canonical lane port MUST map to this lane in the resolver's env —
            # the connection handler resolves its lane from that port (server-side
            # authority). A mismatch is a config error; fail closed by refusing it.
            if lane_for_port(lane_cfg.lane_port, env=self._env) != lane:
                raise ValueError(f"lane_port {lane_cfg.lane_port} does not map to lane {lane}")
            server = await asyncio.start_server(
                self._make_client_handler(lane, lane_cfg),
                host=lane_cfg.host,
                port=lane_cfg.effective_bind_port,
            )
            self._servers.append(server)
            # Record the actually-bound socket port (ephemeral when bind_port==0).
            bound = (
                server.sockets[0].getsockname()[1]
                if server.sockets
                else lane_cfg.effective_bind_port
            )
            self._bound_ports[lane] = bound
            # Fan a new upstream job out to every connected miner on this lane.
            self.job_source.subscribe(lane, self._make_job_broadcaster(lane))

    def _verify_concurrency(self) -> int:
        """Resolve the verify-pool concurrency (explicit, or auto-sized to ≈ cores).

        ``verify_concurrency`` pins it when positive. ``None`` auto-sizes to
        ``os.cpu_count()`` clamped to ``[MIN_VERIFY_CONCURRENCY, MAX_AUTO_VERIFY_CONCURRENCY]``
        so a tiny host still makes progress and a many-core host does not spin up an
        unreasonably large pool of memory-heavy verify threads. A non-positive explicit value
        falls back to the auto sizing (a typo can never disable the off-loop bound).
        """

        explicit = self.verify_concurrency
        if explicit is not None and explicit > 0:
            return explicit
        cores = os.cpu_count() or MIN_VERIFY_CONCURRENCY
        return max(MIN_VERIFY_CONCURRENCY, min(cores, MAX_AUTO_VERIFY_CONCURRENCY))

    def bound_port(self, lane: Lane) -> int | None:
        """The port a lane actually bound to (resolves ephemeral port 0)."""

        return self._bound_ports.get(lane)

    async def serve_forever(self) -> None:
        await asyncio.gather(*(s.serve_forever() for s in self._servers))

    async def stop(self) -> None:
        """Close every listener + open miner socket (clean shutdown)."""

        for server in self._servers:
            server.close()
            with contextlib.suppress(Exception):
                await server.wait_closed()
        # Snapshot under the lock; close writers outside it.
        with self._lock:
            conns = list(self._connections.values())
            self._connections.clear()
            self._lane_connections.clear()
            self._connections_by_ip.clear()
        for conn in conns:
            if conn.unsubscribe is not None:
                with contextlib.suppress(Exception):
                    conn.unsubscribe()
            conn.writer.close()
            with contextlib.suppress(Exception):
                await conn.writer.wait_closed()
        self._servers.clear()
        # Shut the verify pool down LAST (after every socket is closed, so no in-flight
        # re-hash is still scheduling). ``cancel_futures`` drops any not-yet-started
        # re-hashes; in-flight ones finish (they hold the validator lock + may have spent a
        # dedup key, so cancelling mid-record could strand state — let them complete). Done
        # without blocking the loop (the close already drained the readers).
        pool = self._verify_pool
        self._verify_pool = None
        self._verify_semaphore = None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)

    # -- per-connection lifecycle -----------------------------------------
    def _make_client_handler(
        self, lane: Lane, lane_cfg: LaneListenerConfig
    ) -> Callable[[asyncio.StreamReader, asyncio.StreamWriter], object]:
        async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
            await self._serve_connection(lane, lane_cfg, reader, writer)

        return handle

    async def _serve_connection(
        self,
        lane: Lane,
        lane_cfg: LaneListenerConfig,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        # TCP keepalive on the accepted socket so the OS can reap a silently-dead peer
        # (a half-open socket otherwise leaks a connection slot forever). Best-effort.
        self._enable_keepalive(writer)
        peer_ip = _peer_ip(writer)
        # ADMISSION (DoS gate): per-IP cap + global cap + the R4 lane hook. A refusal
        # closes the socket IMMEDIATELY, secret-free — the connection never enters the
        # served set (so an unauthenticated peer cannot open unlimited sockets). On
        # admit we reserve the per-IP slot under the SAME lock as the cap check (no
        # race that would let two concurrent admits both pass a near-full cap).
        if not self._admit(lane, peer_ip):
            with contextlib.suppress(Exception):
                writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            return

        connection_id = next(self._conn_ids)
        # The CANONICAL lane port (not the possibly-ephemeral bound socket port) is
        # the lane authority handed to the handler, so resolve_login's PORT->lane
        # lookup resolves the lane server-side.
        handler = self.connection_factory(lane, lane_cfg.lane_port)
        # Wire the Scrypt header reconstructor: the server owns the job cache + the
        # coinbase-fold, so a stock LTC submit ([worker, job_id, extranonce2, ntime,
        # nonce]) re-derives the exact 80-byte header the rig hashed.
        handler.scrypt_header_builder = self._scrypt_header_for
        # Wire the RandomX blob reconstructor: the server owns the job cache, so a stock
        # xmrig submit ({job_id, nonce, result}) re-derives the exact mining blob the rig
        # hashed (the upstream blob VERBATIM + the rig's nonce at [39:43]).
        handler.monero_blob_builder = self._monero_blob_for
        # Wire the KawPoW header+epoch reconstructor: the server owns the job cache, so a
        # stock KawPoW submit ([worker, job_id, nonce, headerHash, mixHash]) re-derives the
        # SERVER-SOURCED (epoch, block_number, headerHash) from the cached job — the epoch
        # is NEVER taken from the client's submit (the novel-epoch DoS defense).
        handler.kawpow_header_builder = self._kawpow_header_for
        # RandomX/XMR: NO per-connection blob rewrite. The upstream blob is forwarded to
        # the rig verbatim and Alice re-hashes (and forwards the rig's result upstream)
        # against that same blob — so byte 8 must NOT be mutated (a rewrite diverges the
        # re-hash and the upstream pool, which re-validates against its own unmodified
        # blob, would reject every share). Two rigs on one cached upstream job are
        # de-collided by their independent 2**32 nonce search + the upstream (job_id,
        # nonce) dedup. Leave nonce_extra_hex empty (the default).
        handler.nonce_extra_hex = ""
        # Wire the RandomX INLINE first-job builder: on an xmrig ``login`` the OK reply
        # must carry the first job INLINE (xmrig reads its first job from result.job; it
        # does not understand a separate mining.notify). The builder pulls the lane's
        # current job and re-keys its target to THIS connection's vardiff difficulty.
        if lane == XMR_POOL:
            handler.xmr_initial_job_builder = self._xmr_inline_job_builder(lane)
        # Hand the server-observed source IP to the handler so OPEN-mode enrollment can
        # apply its per-IP rate limit (server-side authority; never a client field).
        handler.peer_ip = peer_ip
        extranonce1 = f"{next(self._extranonce_ids):08x}"
        conn = _MinerConnection(
            lane=lane,
            handler=handler,
            writer=writer,
            extranonce1=extranonce1,
            connection_id=connection_id,
            peer_ip=peer_ip,
            reader=reader,
        )
        with self._lock:
            self._connections[connection_id] = conn
            self._lane_connections.setdefault(lane, set()).add(connection_id)
        self._emit(SERVER_CONN_OPENED, lane=lane)
        try:
            # PHASE 1 — pre-login, under a deadline (slowloris defense): read+dispatch
            # frames until a SUCCESSFUL login binds the handler's identity (or the peer
            # drops / is dropped). A peer that opens the socket and never finishes the
            # subscribe+authorize handshake hits ``pre_login_timeout_s`` and is dropped.
            timeout = self.pre_login_timeout_s
            if timeout and timeout > 0:
                try:
                    keep_serving = await asyncio.wait_for(
                        self._read_dispatch_loop(lane, lane_cfg, conn, until_login=True),
                        timeout=timeout,
                    )
                except asyncio.TimeoutError:
                    self._emit(SERVER_CONN_LOGIN_TIMEOUT, lane=lane)
                    keep_serving = False
            else:
                keep_serving = await self._read_dispatch_loop(
                    lane, lane_cfg, conn, until_login=True
                )
            # PHASE 2 — post-login, NO deadline (a logged-in rig submits shares at its
            # own cadence). Only entered when phase 1 ended on a successful login.
            if keep_serving and conn.handler.identity is not None:
                await self._read_dispatch_loop(lane, lane_cfg, conn, until_login=False)
        except (asyncio.IncompleteReadError, ConnectionError):
            pass
        finally:
            await self._close_connection(connection_id)

    async def _read_dispatch_loop(
        self,
        lane: Lane,
        lane_cfg: LaneListenerConfig,
        conn: _MinerConnection,
        *,
        until_login: bool,
    ) -> bool:
        """Read + dispatch frames; return whether the caller should KEEP serving.

        Returns ``False`` on EOF or a fail-closed drop (the caller stops). When
        ``until_login`` is set the loop ALSO returns ``True`` the moment a login binds
        the handler's identity — handing control back so phase 2 can serve the rest
        WITHOUT the pre-login deadline. The 64KB per-frame cap (:meth:`_read_frame`)
        bounds a single unterminated line regardless of phase.
        """

        reader = conn.reader
        assert reader is not None  # always set by _serve_connection
        while True:
            line = await self._read_frame(reader)
            if line is None:
                return False  # EOF / clean close
            if not line:
                continue  # oversize frame skipped fail-closed
            drop = await self._dispatch_frame(lane, lane_cfg, conn, line)
            if drop:
                return False  # fail-closed drop (gate off / unresolved login)
            if until_login and conn.handler.identity is not None:
                # Login just succeeded — leave the deadline-wrapped phase.
                return True

    # -- admission (per-IP cap + global cap + R4 lane hook) ----------------
    def _admit(self, lane: Lane, peer_ip: str) -> bool:
        """Decide + RESERVE a slot for a new connection (atomic under the lock).

        Checks the per-IP cap, the global cap, and the R4 ``admit_connection`` lane
        hook. On admit it reserves the per-IP slot UNDER THE SAME LOCK as the checks, so
        two concurrent admits cannot both slip past a near-full cap. A refusal emits a
        secret-free :data:`SERVER_CONN_REJECTED` (the reason code only — no peer host)
        and returns ``False``; the caller closes the socket immediately. The lane hook
        is consulted OUTSIDE the lock (it may emit its own event) but only AFTER the
        local caps pass and BEFORE the reservation is committed.
        """

        with self._lock:
            # BRIEF SUBMIT-FLOOD BAN: a peer whose prior connection tripped the per-connection
            # flood limiter is refused here for ``submit_flood_ban_s`` so it cannot instantly
            # reconnect and re-flood. Checked FIRST (cheapest, and the most hostile case). A
            # legit rig is never flood-limited, so it is never in this map.
            if peer_ip and self._is_banned_locked(peer_ip):
                self._emit(SERVER_CONN_BANNED, lane=lane, detail=ADMIT_REJECT_BANNED)
                return False
            global_count = len(self._connections)
            ip_count = self._connections_by_ip.get(peer_ip, 0)
            if self.max_connections_global > 0 and global_count >= self.max_connections_global:
                self._emit(SERVER_CONN_REJECTED, lane=lane, detail=ADMIT_REJECT_GLOBAL)
                return False
            if (
                self.max_connections_per_ip > 0
                and peer_ip
                and ip_count >= self.max_connections_per_ip
            ):
                self._emit(SERVER_CONN_REJECTED, lane=lane, detail=ADMIT_REJECT_PER_IP)
                return False
        # R4 lane hook (the relay's AdmissionController; PERMISSIVE/OFF by default). It
        # reads the CURRENT lane count; a shed declines to serve (never touches credit).
        if self.admit_connection is not None:
            lane_count = self.connection_count(lane)
            try:
                admitted = self.admit_connection(
                    lane=lane, current_lane_connections=lane_count
                )
            except Exception:  # pragma: no cover - a hook must never crash the accept loop
                admitted = True
            if not admitted:
                self._emit(SERVER_CONN_REJECTED, lane=lane, detail=ADMIT_REJECT_LANE)
                return False
        # Commit the per-IP reservation (so the NEXT admit sees this slot taken before
        # the connection is even registered in _connections).
        with self._lock:
            self._connections_by_ip[peer_ip] = self._connections_by_ip.get(peer_ip, 0) + 1
        return True

    def _release_ip_slot(self, peer_ip: str) -> None:
        """Release one per-IP reservation (caller holds ``self._lock``)."""

        count = self._connections_by_ip.get(peer_ip)
        if count is None:
            return
        if count <= 1:
            self._connections_by_ip.pop(peer_ip, None)
        else:
            self._connections_by_ip[peer_ip] = count - 1

    def _ban_peer(self, peer_ip: str) -> None:
        """Briefly ban ``peer_ip`` after its connection tripped the submit-flood limiter.

        Records a deadline ``now + submit_flood_ban_s`` (monotonic ``loop.time()``); a new
        connection from this peer is refused at admission until then. A peer with no exposed
        peername (``""``) is NOT bannable (it was never per-IP-capped either) — the global
        cap + the per-connection drop still bound it. ``submit_flood_ban_s <= 0`` disables the
        reconnect-refusal (the drop still happens). Runs on the loop thread (from the submit
        handler), so ``loop.time()`` is the same clock the admission check reads.
        """

        if not peer_ip or self.submit_flood_ban_s <= 0:
            return
        loop = self._loop
        now = loop.time() if loop is not None else 0.0
        with self._lock:
            self._banned_ips[peer_ip] = now + self.submit_flood_ban_s

    def _is_banned_locked(self, peer_ip: str) -> bool:
        """Whether ``peer_ip`` is inside its brief flood-ban window (caller holds the lock).

        Prunes the entry lazily once expired (so the map never accumulates stale bans). Uses
        the loop's monotonic clock — the SAME one :meth:`_ban_peer` stamped with.
        """

        deadline = self._banned_ips.get(peer_ip)
        if deadline is None:
            return False
        loop = self._loop
        now = loop.time() if loop is not None else 0.0
        if now >= deadline:
            self._banned_ips.pop(peer_ip, None)  # expired → prune
            return False
        return True

    def _enable_keepalive(self, writer: asyncio.StreamWriter) -> None:
        """Enable TCP keepalive on the accepted socket (best-effort, secret-free).

        Keepalive lets the OS detect + reap a silently-dead peer (a yanked cable /
        crashed rig) so a half-open socket does not pin a connection slot forever. The
        idle/interval tunables are set only where the platform exposes them (Linux's
        ``TCP_KEEPIDLE`` / ``TCP_KEEPINTVL``; macOS's ``TCP_KEEPALIVE``); a platform
        that lacks them still gets SO_KEEPALIVE on. Any failure is swallowed (a keepalive
        tweak must never break the connection).
        """

        if not self.tcp_keepalive:
            return
        sock = writer.get_extra_info("socket")
        if sock is None:  # pragma: no cover - always present for a TCP StreamWriter
            return
        with contextlib.suppress(OSError, AttributeError):
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
        # Per-socket idle/interval where available (purely a refinement of the above).
        idle = self.tcp_keepalive_idle_s
        interval = self.tcp_keepalive_interval_s
        with contextlib.suppress(OSError, AttributeError):
            if hasattr(socket, "TCP_KEEPIDLE"):  # Linux
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPIDLE, idle)
            elif hasattr(socket, "TCP_KEEPALIVE"):  # macOS / some BSDs
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPALIVE, idle)
        with contextlib.suppress(OSError, AttributeError):
            if hasattr(socket, "TCP_KEEPINTVL"):
                sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_KEEPINTVL, interval)

    async def _read_frame(self, reader: asyncio.StreamReader) -> bytes | None:
        """Read one newline-delimited frame; ``None`` on EOF, ``b""`` on oversize.

        An oversize line (no newline within :attr:`max_frame_bytes`) is consumed
        and dropped fail-closed (returns ``b""`` so the caller skips it) — a hostile
        rig cannot OOM the reader with one unterminated line.
        """

        try:
            raw = await reader.readuntil(b"\n")
        except asyncio.LimitOverrunError:
            # Stream buffer exceeded before a newline: drain what is buffered and
            # treat the frame as malformed (skip).
            with contextlib.suppress(Exception):
                await reader.read(self.max_frame_bytes)
            self._emit(SERVER_FRAME_OVERSIZE)
            return b""
        except asyncio.IncompleteReadError as exc:
            # EOF: return any trailing partial as nothing (clean close).
            if exc.partial:
                return exc.partial.strip() or None
            return None
        if len(raw) > self.max_frame_bytes:
            self._emit(SERVER_FRAME_OVERSIZE)
            return b""
        return raw.strip()

    async def _dispatch_frame(
        self,
        lane: Lane,
        lane_cfg: LaneListenerConfig,
        conn: _MinerConnection,
        line: bytes,
    ) -> bool:
        """Route one parsed frame; return ``True`` to fail-closed DROP the connection."""

        try:
            message = json.loads(line.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            # Never crash the loop on a garbage frame; there is no id to answer.
            self._emit(SERVER_FRAME_MALFORMED, lane=lane)
            return False
        if not isinstance(message, dict):
            self._emit(SERVER_FRAME_MALFORMED, lane=lane)
            return False
        method = message.get("method")

        if method == STRATUM_METHOD_SUBSCRIBE:
            await self._handle_subscribe(lane_cfg, conn, message)
            return False
        if method in (STRATUM_METHOD_LOGIN, STRATUM_METHOD_AUTHORIZE):
            return await self._handle_login(lane, conn, message)
        if method in (STRATUM_METHOD_SUBMIT_BITCOINFAMILY, STRATUM_METHOD_SUBMIT_RANDOMX):
            # Returns ``True`` when the submit-flood limiter tripped -> fail-closed DROP.
            return await self._handle_submit(lane, conn, message)
        # Unknown method: let the glue's submit-parser produce the structured error (it
        # returns unknown_method) and reply with it; never drop on it. This path takes the
        # parse-error branch in ``handle_submit`` (no re-hash, no flood-gate), so it is cheap
        # and need not go off-loop.
        result = conn.handler.handle_submit(message)
        await self._send(conn, result.reply)
        return False

    async def _handle_subscribe(
        self, lane_cfg: LaneListenerConfig, conn: _MinerConnection, message: dict
    ) -> None:
        # The transport owns extranonce assignment + the subscribe envelope; the glue's
        # parse_subscribe carries no identity (identity is the authorize). For a PROXY we
        # SPLIT the upstream pool's extranonce space: the rig's downstream extranonce1 =
        # upstream extranonce1 + this connection's suffix, and its extranonce2_size
        # shrinks by the suffix width. This (a) binds the rig's coinbase region to what
        # Alice reconstructs at submit (credit), (b) gives every connection a UNIQUE
        # region (no two rigs collide), and (c) keeps the TOTAL extranonce width equal to
        # the upstream's reserved coinbase space (so a forwarded solution is valid upstream).
        extranonce1, extranonce2_size, suffix_hex = self._downstream_extranonce(lane_cfg, conn)
        conn.downstream_extranonce1 = extranonce1
        conn.suffix_hex = suffix_hex
        conn.handler.downstream_extranonce1 = extranonce1
        reply = build_subscribe_reply(
            message_id=message.get("id"),
            subscription_id=conn.extranonce1,
            extranonce1=extranonce1,
            extranonce2_size=extranonce2_size,
        )
        await self._send(conn, reply)

    def _downstream_extranonce(
        self, lane_cfg: LaneListenerConfig, conn: _MinerConnection
    ) -> tuple[str, int, str]:
        """This connection's ``(downstream_extranonce1, extranonce2_size, suffix_hex)``.

        Splits the UPSTREAM pool's extranonce space (read off the lane's current job:
        ``job.extranonce`` = upstream extranonce1, ``payload["extranonce2_size"]`` =
        upstream size): downstream extranonce1 = upstream extranonce1 + this connection's
        suffix (the LOW bytes of the server-minted per-connection hex, which vary per
        connection so two rigs never share a coinbase region); downstream extranonce2_size
        = upstream size − suffix bytes (leaving the rig ≥1 byte). When no upstream job /
        extranonce is available yet, falls back to a suffix-only extranonce1 + the lane's
        default size — still CREDIT-correct (Alice reconstructs with the same value the
        rig was handed) though not upstream-forwardable until the subscription arrives.
        """

        suffix = conn.extranonce1  # server-minted per-connection hex (4 bytes / 8 hex)
        job = self.job_source.current_job(conn.lane)
        if job is not None and job.extranonce:
            try:
                upstream_size = int(job.payload.get("extranonce2_size", 0))
            except (TypeError, ValueError):
                upstream_size = 0
            if upstream_size > 1:
                suffix_bytes = min(len(suffix) // 2, upstream_size - 1)
                suffix_hex = suffix[-(suffix_bytes * 2) :]  # LOW bytes vary per connection
                return job.extranonce + suffix_hex, upstream_size - suffix_bytes, suffix_hex
        return suffix, lane_cfg.extranonce2_size, suffix

    async def _handle_login(self, lane: Lane, conn: _MinerConnection, message: dict) -> bool:
        # Drive the merged glue's identity step. It runs the full §2.5 chain (gate +
        # registry + roster + session) and returns a wire reply (OK or error).
        reply = conn.handler.handle_login(message)
        await self._send(conn, reply)
        if conn.handler.identity is None:
            # Fail-closed: gate off / unresolved login / malformed -> reject + DROP.
            self._emit(SERVER_LOGIN_REJECTED, lane=lane, detail=_error_reason(reply))
            return True
        self._emit(SERVER_LOGIN_ACCEPTED, lane=lane)
        # Push the starting difficulty + the lane's current job (if R1/R2 have one).
        await self._push_initial_work(lane, conn)
        return False

    async def _push_initial_work(self, lane: Lane, conn: _MinerConnection) -> None:
        from alice_acp.transport_front.stratum_messages import (
            build_set_difficulty_notification,
        )

        job = self.job_source.current_job(lane)
        if lane == XMR_POOL:
            # RandomX/XMR: there is NO mining.set_difficulty (a Monero rig ignores it — its
            # difficulty IS the job target), and the first job was already inlined into the
            # login-OK ``result.job`` by the connection's xmr_initial_job_builder. So push
            # NOTHING extra here; the rig is already mining the inline job. (If the lane had
            # no job at login time the inline job was absent; a subsequent upstream job will
            # fan out via the broadcaster, which sends a proper ``job`` push.)
            if job is not None:
                self._remember_job(job)
            return
        vardiff = conn.handler.vardiff
        if lane in (MAIN_POOL_GPU_RVN, MAIN_POOL_GPU_QUAI):
            # KawPoW (Ethereum dialect — RVN AND Quai): there is NO mining.set_difficulty (a
            # KawPoW rig — T-Rex / kawpowminer — reads mining.set_target, a FULL 32-byte
            # target). Push
            # the starting per-connection target via mining.set_target ONCE (recording it so
            # a later retarget only re-pushes on a real change). The subsequent job is a
            # SHARED mining.notify carrying the job's OWN target — the per-connection target
            # is authoritative via set_target, so a job push (which fans out on EVERY
            # frequent upstream job) never forces the rig to re-evaluate the target. A
            # retarget later pushes set_target (not set_difficulty, not a fresh job).
            if vardiff is not None:
                target = self._kawpow_job_target(vardiff.current)
                conn.last_kawpow_target = target
                await self._send(conn, build_set_target_notification(target))
            if job is not None:
                self._remember_job(job)
                # The notify carries the job's OWN target (target=None -> upstream target),
                # the SAME for every connection — the per-connection vardiff target rides
                # mining.set_target above, not this job push (no per-job target churn).
                await self._send(conn, job.to_notification())
            return
        # Bitcoin-family (LTC): push the starting vardiff via mining.set_difficulty, then
        # the lane's current job as a positional mining.notify.
        if vardiff is not None:
            await self._send(
                conn,
                build_set_difficulty_notification(format(vardiff.current.normalize(), "f")),
            )
        if job is not None:
            self._remember_job(job)
            await self._send(conn, job.to_notification())

    def _xmr_job_target(self, pool_difficulty: Decimal) -> str:
        """This connection's vardiff difficulty -> the xmrig 4-byte compact ``target``.

        Reuses the translator's :func:`difficulty_to_compact_target` (the inverse of
        ``net_difficulty_from_target``) so the rig is handed THIS connection's
        per-connection pool difficulty as its share target — a HIGHER vardiff yields a
        HARDER (smaller) target. A Monero rig has no set_difficulty; the target IS the
        difficulty, so retargeting means pushing a new ``job`` with a new target.
        """

        return difficulty_to_compact_target(pool_difficulty)

    def _kawpow_job_target(self, pool_difficulty: Decimal) -> str:
        """This connection's vardiff difficulty -> the KawPoW FULL 32-byte ``target``.

        Reuses the translator's :func:`difficulty_to_target_256` (the inverse of
        ``target_to_difficulty_256``) so the rig is handed THIS connection's per-connection
        pool difficulty as its share target — a HIGHER vardiff yields a HARDER (smaller)
        target. A KawPoW rig has no set_difficulty; the target (in mining.notify AND
        mining.set_target) IS the difficulty, so retargeting means pushing a new
        mining.set_target (NOT a fresh job, unlike XMR; NOT set_difficulty, unlike Scrypt).
        """

        return difficulty_to_target_256(pool_difficulty)

    def _xmr_inline_job_builder(self, lane: Lane) -> Callable[[Decimal], dict | None]:
        """A ``(pool_difficulty) -> job object | None`` builder for the inline login-OK job.

        Pulls the lane's CURRENT internal job and re-keys it under the internal job id
        with THIS connection's per-connection compact ``target`` (from the vardiff
        difficulty). Returns ``None`` when no job is available yet (the login-OK then
        carries no inline job — fail-soft; a later upstream job fans out via the
        broadcaster). Caches the job so a subsequent submit re-derives its blob.
        """

        def build(pool_difficulty: Decimal) -> dict | None:
            job = self.job_source.current_job(lane)
            if job is None:
                return None
            self._remember_job(job)
            return build_monero_job_object(
                job_id=job.internal_job_id,
                payload=job.payload,
                target=self._xmr_job_target(pool_difficulty),
            )

        return build

    def _xmr_retarget_job(self, lane: Lane, conn: _MinerConnection) -> dict | None:
        """A fresh ``job`` push carrying THIS connection's NEW vardiff target (XMR retarget).

        The Monero equivalent of the Bitcoin-family ``mining.set_difficulty`` push: a rig
        retargets by receiving a new ``job`` with a new ``target``. Re-keys the lane's
        CURRENT job under its internal id with the connection's just-advanced vardiff
        difficulty (as the xmrig compact target). Returns ``None`` (push nothing) when no
        job or no vardiff is available.
        """

        job = self.job_source.current_job(lane)
        vardiff = conn.handler.vardiff
        if job is None or vardiff is None:
            return None
        self._remember_job(job)
        return job.to_notification(target=self._xmr_job_target(vardiff.current))

    def _remember_job(self, job: InternalJob) -> None:
        """Cache a recent job by internal id so a later submit re-derives its header.

        Bounded by :data:`MAX_CACHED_JOBS` (oldest evicted); thread-safe (a broadcast
        fires from the relay's task/thread).
        """

        with self._lock:
            if job.internal_job_id not in self._jobs_by_id:
                self._job_order.append(job.internal_job_id)
            self._jobs_by_id[job.internal_job_id] = job
            while len(self._job_order) > MAX_CACHED_JOBS:
                evicted = self._job_order.pop(0)
                self._jobs_by_id.pop(evicted, None)

    def _scrypt_header_for(
        self,
        job_id: str,
        downstream_extranonce1: str,
        extranonce2_hex: str,
        ntime_hex: str,
        nonce_hex: str,
    ) -> bytes | None:
        """Re-derive the 80-byte Scrypt header for a submit (the validator re-hashes this).

        The proxy core: a stock LTC rig submits ``[worker, job_id, extranonce2, ntime,
        nonce]``, so Alice rebuilds the EXACT header the rig hashed. Look the job up by
        id (its coinbase template), fold ``coinb1 + downstream_extranonce1 + extranonce2
        + coinb2`` into the coinbase, merkle-fold through the job's branches, and
        serialize the header with the job's version/prevhash/nbits + the SUBMITTED ntime
        + nonce (via the translator's canonical :func:`assemble_scrypt_header`, the SAME
        byte rules the rig applied). Returns ``None`` (fail-closed) on a missing/evicted
        job or any malformed field — the connection then NACKs and credits nothing.
        """

        with self._lock:
            job = self._jobs_by_id.get(job_id)
        if job is None:
            return None
        payload = job.payload
        try:
            coinbase = build_coinbase(
                coinb1_hex=payload["coinb1"],
                extranonce1_hex=downstream_extranonce1,
                extranonce2_hex=extranonce2_hex,
                coinb2_hex=payload["coinb2"],
            )
            merkle_root = merkle_root_from_coinbase(
                coinbase, list(payload.get("merkle_branch", []))
            )
            # cpuminer/cgminer submit the nonce big-endian; the header's last 4 bytes are
            # little-endian, so reverse the 4 submitted bytes before assembling the header.
            stripped = nonce_hex[2:] if nonce_hex[:2].lower() == "0x" else nonce_hex
            nonce = bytes.fromhex(stripped)[::-1]
            if len(nonce) != NONCE_LEN:
                return None
            return assemble_scrypt_header(
                version_hex=payload["version"],
                prevhash_hex=payload["prevhash"],
                merkle_root=merkle_root,
                ntime_hex=ntime_hex,
                nbits_hex=payload["nbits"],
                nonce=nonce,
            )
        except (KeyError, ValueError):
            return None

    def _monero_blob_for(
        self,
        job_id: str,
        nonce_extra_hex: str,
        nonce_hex: str,
    ) -> tuple[bytes, bytes] | None:
        """Re-derive the RandomX ``(seed, blob)`` for a submit (the validator re-hashes this).

        The proxy core: a stock xmrig submits ``{job_id, nonce, result}``, so Alice
        rebuilds the EXACT 76-byte blob the rig hashed. Look the job up by id (its cached
        ``blob`` + ``seed_hash``), splice the rig's submitted ``nonce`` at bytes [39:43]
        (``MONERO_NONCE_OFFSET``, little-endian as the rig had them), and return
        ``seed = bytes.fromhex(seed_hash)`` (the per-epoch RandomX VM key — the
        seed_hash propagation job→cache→RawSubmission) + the reconstructed blob. The
        verifier independently re-splices the [39:43] nonce and hashes the blob against
        ``seed``. Returns ``None`` (fail-closed) on a missing/evicted job or any
        malformed field — the connection then NACKs and credits nothing.

        THE BLOB IS RECONSTRUCTED VERBATIM (only the [39:43] nonce is spliced). Alice
        does NOT rewrite blob byte 8: the upstream ``blob`` is handed DOWN to the rig
        verbatim (``build_monero_job_object`` copies ``payload['blob']``), so the rig
        hashes the upstream byte-8 value and Alice MUST re-hash that same value or the
        digest diverges — which previously failed EVERY share LOW_DIFFICULTY (the
        recomputed hash was unrelated to the rig's ``result``) and so forwarded NOTHING
        upstream (supportxmr saw ``hash:0``). Equally decisive: Alice forwards the rig's
        ``result`` to the upstream pool, which re-validates against ITS OWN unmodified
        blob (the cryptonote ``submit`` has no field to return a rewritten byte 8) — so a
        proxy that mutates the blob can never produce an upstream-acceptable share. The
        per-connection ``nonce_extra_hex`` is therefore accepted but IGNORED here (the
        server passes ``""``); two rigs sharing one cached upstream job are de-collided by
        their independent 2**32 nonce search at [39:43] + the upstream pool's (job_id,
        nonce) dedup, NOT by rewriting the shared block-template bytes.
        """

        del nonce_extra_hex  # intentionally unused: the blob is reconstructed verbatim
        with self._lock:
            job = self._jobs_by_id.get(job_id)
        if job is None:
            return None
        payload = job.payload
        blob_hex = payload.get("blob")
        seed_hex = payload.get("seed_hash")
        if not isinstance(blob_hex, str) or not isinstance(seed_hex, str):
            return None
        try:
            blob = bytearray(bytes.fromhex(_strip0x(blob_hex)))
            seed = bytes.fromhex(_strip0x(seed_hex))
            nonce = bytes.fromhex(_strip0x(nonce_hex))
        except ValueError:
            return None
        if len(nonce) != MONERO_NONCE_LEN:
            return None
        # The cached blob MUST be long enough to hold the nonce ([39:43]); a
        # short/evicted/garbled blob fails closed.
        if len(blob) < MONERO_NONCE_OFFSET + MONERO_NONCE_LEN:
            return None
        # Splice ONLY the rig's submitted nonce at [39:43] (the verifier re-splices the
        # same bytes). Every other byte — including byte 8 — is kept exactly as the
        # upstream pool sent it and the rig hashed it, so Alice's re-hash reproduces the
        # rig's ``result`` and the forwarded share is valid at the upstream pool.
        blob[MONERO_NONCE_OFFSET : MONERO_NONCE_OFFSET + MONERO_NONCE_LEN] = nonce
        return seed, bytes(blob)

    def _kawpow_header_for(self, job_id: str) -> tuple[int, int, bytes] | None:
        """Re-derive the KawPoW ``(epoch, block_number, header_hash)`` for a submit.

        The proxy core (the KawPoW analog of ``_monero_blob_for``): a stock KawPoW rig
        (T-Rex) submits ``[worker, job_id, nonce, headerHash, mixHash]``, but the SERVER
        does NOT trust the submitted headerHash/epoch. Alice looks the job up by id (its
        cached ``headerHash`` + ``height``) and returns the SERVER-SOURCED epoch +
        block height + the EXACT 32-byte headerHash the rig was handed:

          * ``epoch = height // KAWPOW_EPOCH_LENGTH`` — the DAG epoch the verifier keys on
            (derived from the CACHED job's height, NEVER the client's submit — the
            novel-epoch DoS defense: a hostile rig cannot make Alice build an
            arbitrary-epoch DAG);
          * ``block_number = height`` — the block number KawPoW mixes into the kernel;
          * ``header_hash`` — ``bytes.fromhex(headerHash)`` (the 32-byte header the rig
            hashed; the verifier re-hashes it + the rig's nonce against the epoch DAG).

        Returns ``None`` (fail-closed) on a missing/evicted job or any malformed field —
        the connection then NACKs and credits nothing.
        """

        with self._lock:
            job = self._jobs_by_id.get(job_id)
        if job is None:
            return None
        payload = job.payload
        header_hex = payload.get("headerHash")
        if not isinstance(header_hex, str):
            return None
        try:
            header_hash = bytes.fromhex(_strip0x(header_hex))
        except ValueError:
            return None
        if len(header_hash) != KAWPOW_HEADER_HASH_BYTES:
            return None
        height_raw = payload.get("height", 0)
        try:
            height = int(height_raw)
        except (TypeError, ValueError):
            return None
        if height < 0:
            return None
        epoch = height // KAWPOW_EPOCH_LENGTH
        return epoch, height, header_hash

    async def _handle_submit(self, lane: Lane, conn: _MinerConnection, message: dict) -> bool:
        # Drive the merged glue's submit path: it builds the RawSubmission with the
        # SERVER-OWNED identity + the vardiff pool target and calls
        # ShareValidator.validate (which writes the ValidatedShareStore on is_share
        # -> the credit seam, unchanged). The reply is the protocol ACK/NACK.
        #
        # OFF THE EVENT LOOP (the BLOCKER fix): ``handle_submit`` runs the FULL synchronous
        # PoW re-hash (hashlib.scrypt ~213us; RandomX/KawPoW ms-scale). Running it INLINE on
        # the loop lets one rig's re-hash block logins/jobs/submits for EVERY other miner on
        # EVERY lane. So we hand it to the bounded VERIFY THREAD POOL via
        # ``run_in_executor`` and gate it with the concurrency Semaphore (≈ cores) — the loop
        # stays responsive (it can serve other connections while this re-hash runs) and total
        # re-hash CPU is bounded under load.
        #
        # CORRECTNESS + ORDERING are PRESERVED (no double-credit, no race):
        #  * Per-connection: this connection's read loop AWAITS this call before reading its
        #    next frame, so two submits from ONE connection never run concurrently; and
        #    ``StratumConnection.handle_submit`` holds the connection's own lock throughout
        #    (vardiff / grace / rate-limiter state stay consistent inside the worker thread).
        #  * Cross-connection: all connections share ONE ShareValidator whose threading.Lock
        #    serializes the ENTIRE classify→dedup→record→counters section. Running validate
        #    from several worker threads is exactly what that lock is for: the dedup claim is
        #    atomic under it, so a replayed nonce across two threads still yields exactly one
        #    first_seen=True (no double-credit), and the accounting invariant holds. The
        #    Semaphore only BOUNDS concurrency; it is the validator lock — not the Semaphore —
        #    that guarantees serialized credit.
        result = await self._run_submit_offloop(conn, message)
        if result is None:
            # The server was shut down (pool gone) before the re-hash could run, or the
            # handler is unbound. Nothing to reply; the connection is being torn down.
            return False
        await self._send(conn, result.reply)
        # SUBMIT-FLOOD DROP: the per-connection limiter tripped (a line-rate sub-target /
        # invalid flood). We already NACKed; now DROP the connection + briefly BAN the peer
        # at admission so it cannot instantly reconnect and re-flood. Returning True tells
        # the read loop to stop serving (fail-closed drop, the same channel the gate-off
        # login drop uses). The re-hash for THIS submit was already gated off-loop (or, for
        # the rate cap, skipped before the re-hash entirely), so the flood never pegged the
        # loop on the way to this drop.
        if result.disconnect:
            self._ban_peer(conn.peer_ip)
            self._emit(SERVER_CONN_SUBMIT_FLOOD, lane=lane, detail=result.disconnect_reason)
            return True
        if result.set_difficulty is not None:
            # Vardiff retargeted on this accepted share.
            if lane == XMR_POOL:
                # RandomX/XMR has NO mining.set_difficulty — a Monero rig retargets by
                # receiving a NEW ``job`` with a new ``target``. Push the lane's current
                # job re-keyed with this connection's NEW vardiff difficulty (the compact
                # target). Drop the Bitcoin-family set_difficulty dict on this lane.
                retarget = self._xmr_retarget_job(lane, conn)
                if retarget is not None:
                    await self._send(conn, retarget)
            elif lane in (MAIN_POOL_GPU_RVN, MAIN_POOL_GPU_QUAI):
                # KawPoW (RVN AND Quai) has NO mining.set_difficulty — a KawPoW rig (T-Rex)
                # retargets by receiving a NEW ``mining.set_target`` (a FULL 32-byte target),
                # NOT a fresh job (unlike XMR) and NOT set_difficulty (unlike Scrypt). Push the
                # connection's just-advanced vardiff difficulty as a 32-byte target — but
                # ONLY when the encoded target actually changed vs the last one sent (the
                # vardiff value moved but two nearby difficulties can encode the same
                # MAX_TARGET//d target; suppress that no-op so the rig is not churned).
                vardiff = conn.handler.vardiff
                if vardiff is not None:
                    target = self._kawpow_job_target(vardiff.current)
                    if target != conn.last_kawpow_target:
                        conn.last_kawpow_target = target
                        await self._send(conn, build_set_target_notification(target))
            else:
                # Bitcoin-family (LTC): push the new difficulty via set_difficulty.
                await self._send(conn, result.set_difficulty)
        decision = result.decision
        if decision is None or not decision.is_share:
            self._emit(SERVER_SUBMIT_REJECTED, lane=lane)
            return False
        self._emit(SERVER_SUBMIT_ACCEPTED, lane=lane)
        # Forward EVERY accepted share upstream to Alice's pool account IMMEDIATELY (R3) —
        # synchronously, never batched/queued/withheld (anti-selfish-mining). The upstream
        # pool measures Alice's account by the shares it receives at pool difficulty, NOT by
        # solved blocks; a share that ALSO clears the real chain target is flagged as a
        # solution in telemetry but is forwarded the same way.
        await self._forward_accepted_share(lane, conn, message, decision)
        return False

    async def _run_submit_offloop(
        self, conn: _MinerConnection, message: dict
    ) -> SubmitResult | None:
        """Run ``conn.handler.handle_submit`` in the verify pool, gated by the Semaphore.

        Returns the :class:`SubmitResult`, or ``None`` if the server has been shut down
        (the pool/Semaphore are gone) — the caller then quietly stops serving. The Semaphore
        bounds how many re-hashes run CONCURRENTLY (≈ cores) so total re-hash CPU is bounded;
        the per-connection read loop awaits this, so this connection's submits stay ordered.
        The heavy work (the PoW re-hash inside ``handle_submit``) runs on a pool thread, so
        the event loop is free to serve every OTHER connection while it runs.
        """

        loop = self._loop
        pool = self._verify_pool
        semaphore = self._verify_semaphore
        if loop is None or pool is None or semaphore is None:  # shutting down
            return None
        async with semaphore:
            # Re-read the pool under the Semaphore: ``stop`` may have nulled it while we
            # waited for a slot. If so, bail (the connection is being torn down).
            pool = self._verify_pool
            if pool is None:
                return None
            return await loop.run_in_executor(pool, conn.handler.handle_submit, message)

    async def _forward_accepted_share(
        self, lane: Lane, conn: _MinerConnection, message: dict, decision
    ) -> None:
        """Forward EVERY accepted share upstream to Alice's pool account (R3).

        This is the foundation's revenue path: the upstream pool (F2Pool LTC, supportxmr,
        etc.) measures Alice's account by the SHARES it receives at the per-connection pool
        (vardiff) difficulty — NOT by solved blocks. So every share the validator accepts
        (``decision.is_share``) is relayed upstream immediately, exactly as a stock stratum
        proxy does. (The earlier revision forwarded ONLY shares that also cleared the real
        chain/block target ``net_difficulty``; an ASIC essentially never solves a block, so
        that path sent the upstream pool nothing and the account showed 0 H/s / 0 workers /
        0 reward despite a live miner.)

        ``is_solution`` (the share ALSO cleared the real chain target) is now a TELEMETRY
        bit only — it rides the forward event detail so ops can still see a block-level
        solution — it no longer gates whether the share is forwarded.

        R3 invariants are unchanged: the forward is SYNCHRONOUS + IMMEDIATE (never batched,
        queued-with-delay, or withheld — the anti-selfish-mining rule), and it is advisory
        for the upstream coin only — credit already landed in the validator's
        ValidatedShareStore (``paid_acu`` untouched); a refusal/exception never affects it.
        """

        job = self.job_source.current_job(lane)
        identity = conn.handler.identity
        if job is None or identity is None:
            return
        # ``is_solution`` = the share ALSO cleared the REAL chain (block) target. TELEMETRY
        # ONLY now — it no longer gates the forward (every accepted share is forwarded so the
        # upstream pool registers Alice's account). On the SCRYPT lane the validated share
        # difficulty is on the scrypt-stratum diff-1 scale (2**240,
        # ``LocalScryptVerifier.LTC_DIFF1_TARGET``) while ``net_difficulty_from_nbits``
        # reports the network scale (Bitcoin diff-1, ≈2**224 — 2**16 smaller); scale net UP
        # by 2**16 to compare on the SAME scale as ``result_difficulty``. RandomX/KawPoW
        # share + net are both on the 2**256 scale, so they are unchanged.
        net_target = job.net_difficulty * (1 << 16) if lane == SCRYPT_POOL else job.net_difficulty
        is_solution = decision.result_difficulty >= net_target
        params = message.get("params")
        is_xmr = lane == XMR_POOL
        # Both KawPoW lanes (RVN AND Quai) use the SAME positional KawPoW submit layout.
        is_rvn = lane in (MAIN_POOL_GPU_RVN, MAIN_POOL_GPU_QUAI)
        if is_rvn:
            # KawPoW positional submit: [worker, job_id, nonce, headerHash, mixHash]. Pull
            # the EXACT positions (nonce=2, headerHash=3, mixHash=4) — NOT the generic
            # _submit_fields layout (which conflates the nonce/mix for the Scrypt order).
            nonce_hex, header_hash_hex, mix_hash_hex = _kawpow_submit_fields(params)
            extranonce2_hex = ""
            ntime_hex = ""
            last_hex = ""
        else:
            nonce_hex, extranonce2_hex, ntime_hex, last_hex = _submit_fields(message, params)
            header_hash_hex = ""
            mix_hash_hex = "" if is_xmr else last_hex
        # PROXY split: the rig's extranonce2 is only the LOW part of the upstream
        # extranonce2; the FULL upstream extranonce2 (so the forwarded coinbase matches
        # the one the rig hashed) is this connection's suffix + the rig's extranonce2.
        # KawPoW has no extranonce2 (mix-hash path), so only prepend for the Scrypt lane.
        if lane == SCRYPT_POOL and extranonce2_hex and conn.suffix_hex:
            extranonce2_hex = conn.suffix_hex + extranonce2_hex
        # Route the lane-specific solution fields so R3's upstream submit carries the right
        # values: XMR -> ``result_hash_hex`` (the RandomX ``result``, rides on the Monero
        # submit); RVN -> ``header_hash_hex`` + ``mix_hash_hex`` (the KawPoW header+mix,
        # ride on the positional submit's 4th/5th fields).
        solution = ConfirmedSolution(
            lane=lane,
            internal_job_id=job.internal_job_id,
            worker_name=identity.worker_name,
            nonce_hex=nonce_hex,
            result_difficulty=decision.result_difficulty,
            extranonce2_hex=extranonce2_hex,
            ntime_hex=ntime_hex,
            mix_hash_hex=mix_hash_hex,
            header_hash_hex=header_hash_hex,
            result_hash_hex=last_hex if is_xmr else "",
            canonical_share_hash=decision.canonical_share_hash,
        )
        # Synchronous, immediate forward (R3). A refusal/exception is advisory only —
        # credit already happened in the validator; never let it crash the loop.
        try:
            self.solution_sink.submit_solution(solution)
        except Exception:
            self._emit(SERVER_SOLUTION_FORWARDED, lane=lane, detail="forward_error")
            return
        # The forward fired. ``is_solution`` rides the detail as telemetry only (a share
        # that ALSO cleared the real chain/block target) — it does NOT gate the forward.
        self._emit(
            SERVER_SOLUTION_FORWARDED, lane=lane, detail="solution" if is_solution else "share"
        )

    # -- job broadcast (R1/R2 -> all miners on a lane) ---------------------
    def _make_job_broadcaster(self, lane: Lane) -> Callable[[InternalJob], None]:
        def broadcast(job: InternalJob) -> None:
            # Called from the relay's task/thread; hop onto each connection's loop
            # by scheduling the writes. We snapshot the lane's connections and push.
            self._remember_job(job)
            with self._lock:
                ids = list(self._lane_connections.get(lane, set()))
                conns = [self._connections[i] for i in ids if i in self._connections]
            is_xmr = lane == XMR_POOL
            # The Scrypt AND KawPoW/RVN notify are IDENTICAL for every connection (the
            # per-connection target rides separately — Scrypt on mining.set_difficulty,
            # RVN on mining.set_target — both pushed only on a real change), so build the
            # notify ONCE. The RVN notify carries the job's OWN target (target=None ->
            # upstream target); a job push therefore never re-pushes a per-connection
            # target (no per-job set_target churn — the authoritative target is the last
            # mining.set_target). ONLY the XMR ``job`` push must embed a per-connection
            # target IN the message, because RandomX has NO set_target/set_difficulty — its
            # sole retarget channel is a fresh ``job`` — so it is rebuilt per connection.
            shared_notification = None if is_xmr else job.to_notification()
            for conn in conns:
                # Only push to miners that have completed login (have an identity).
                if conn.handler.identity is None:
                    continue
                if is_xmr:
                    vardiff = conn.handler.vardiff
                    target = self._xmr_job_target(
                        vardiff.current if vardiff is not None else job.pool_difficulty
                    )
                    notification = job.to_notification(target=target)
                else:
                    notification = shared_notification
                self._schedule_send(conn, notification)

        return broadcast

    def _schedule_send(self, conn: _MinerConnection, message: dict) -> None:
        """Schedule a write onto the SERVER'S loop (thread-safe; broadcast path).

        ``subscribe`` callbacks may fire from the relay's task/thread, so we hop
        onto the server's captured loop. When already on that loop we schedule a
        task (keeping a reference so it is not GC'd mid-flight); from another thread
        we use :func:`asyncio.run_coroutine_threadsafe`.
        """

        loop = self._loop
        coro = self._send(conn, message)
        if loop is None or not loop.is_running():  # pragma: no cover - degenerate
            coro.close()
            return
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is loop:
            task = loop.create_task(coro)
            self._bg_tasks.add(task)
            task.add_done_callback(self._bg_tasks.discard)
        else:
            asyncio.run_coroutine_threadsafe(coro, loop)

    # -- framed write ------------------------------------------------------
    async def _send(self, conn: _MinerConnection, message: dict) -> None:
        data = (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
        async with conn._write_lock:
            try:
                conn.writer.write(data)
                await conn.writer.drain()
            except (ConnectionError, RuntimeError):
                # Peer gone mid-write: drop quietly (the read loop will clean up).
                pass

    async def _close_connection(self, connection_id: int) -> None:
        with self._lock:
            conn = self._connections.pop(connection_id, None)
            if conn is not None:
                self._lane_connections.get(conn.lane, set()).discard(connection_id)
                # Release this peer's per-IP slot (the admission reservation). This runs
                # for EVERY admitted connection (the accept loop's finally always calls
                # here), so a per-IP slot can never leak on a drop/EOF/error path.
                self._release_ip_slot(conn.peer_ip)
        if conn is None:
            return
        if conn.unsubscribe is not None:
            with contextlib.suppress(Exception):
                conn.unsubscribe()
        conn.writer.close()
        with contextlib.suppress(Exception):
            await conn.writer.wait_closed()
        self._emit(SERVER_CONN_CLOSED, lane=conn.lane)

    # -- structured, secret-free log --------------------------------------
    def _emit(self, code: str, *, lane: Lane | None = None, detail: str = "") -> None:
        self.event_log.append(ServerEvent(code=code, lane=lane, detail=detail, at=self.clock()))

    def event_codes(self) -> list[str]:
        return [event.code for event in self.event_log]

    def connection_count(self, lane: Lane | None = None) -> int:
        with self._lock:
            if lane is None:
                return len(self._connections)
            return len(self._lane_connections.get(lane, set()))


def _strip0x(text: str) -> str:
    """Strip a leading ``0x`` from a hex string (tolerant of either case)."""

    return text[2:] if text[:2].lower() == "0x" else text


def _error_reason(reply: dict) -> str:
    error = reply.get("error")
    if isinstance(error, (list, tuple)) and len(error) >= 2 and isinstance(error[1], str):
        return error[1]
    return ""


def _peer_ip(writer: asyncio.StreamWriter) -> str:
    """The peer's IP from the accepted socket (best-effort; ``""`` when unavailable).

    Used ONLY as the per-IP cap bucket key + released on disconnect; it is NEVER logged
    raw (the structured event carries a stable reason code, not the host). The peername
    is ``(host, port[, ...])`` for IPv4/IPv6; we take the host. A missing/odd peername
    (degenerate transport) yields ``""`` — such a connection is then not per-IP-capped
    (the global cap still applies), which is the safe fail-open for an unknown peer.
    """

    peername = writer.get_extra_info("peername")
    if isinstance(peername, tuple) and peername and isinstance(peername[0], str):
        return peername[0]
    return ""


def _submit_fields(message: dict, params: object) -> tuple[str, str, str, str]:
    """Pull the upstream-submit fields off a parsed submit (best-effort, secret-free).

    Bitcoin-family ``mining.submit`` is ``[worker, job_id, extranonce2, ntime,
    nonce]`` (Scrypt) or ``[worker, job_id, nonce, header_hash, mix_hash]`` (KawPoW);
    RandomX submit is an object ``{"nonce", "result"}``. We extract the fields R3
    needs to assemble the upstream submit without guessing the algorithm here (R3
    re-keys by lane). Missing fields default to empty strings (R3 fails closed on a
    forward it cannot assemble).
    """

    if isinstance(params, dict):  # RandomX object payload
        nonce = params.get("nonce")
        result = params.get("result")
        return (
            nonce if isinstance(nonce, str) else "",
            "",
            "",
            result if isinstance(result, str) else "",
        )
    if isinstance(params, list):

        def at(idx: int) -> str:
            return params[idx] if len(params) > idx and isinstance(params[idx], str) else ""

        # Scrypt positional layout: extranonce2=2, ntime=3, nonce=4 (the nonce is the last
        # field). This helper is the SCRYPT path only now; the KawPoW path uses
        # :func:`_kawpow_submit_fields` (its nonce/header/mix positions are distinct and the
        # ``at(4) or at(2)`` fallback below would mis-read the KawPoW nonce as the mix-hash).
        return (at(4) or at(2), at(2), at(3), at(4))
    return ("", "", "", "")


def _kawpow_submit_fields(params: object) -> tuple[str, str, str]:
    """Pull ``(nonce, headerHash, mixHash)`` off a KawPoW ``mining.submit`` (secret-free).

    The KawPoW positional ``mining.submit`` is ``[worker, job_id, nonce, headerHash,
    mixHash]`` — nonce at index 2, headerHash at 3, mixHash at 4 (distinct from the Scrypt
    layout, where the nonce is the LAST field). The KawPoW upstream submit
    (``_build_upstream_submit``) carries all three: the nonce + the rig-submitted
    headerHash (the 4th field the pool re-checks) + the mixHash (the 5th). Missing fields
    default to empty strings (R3 fails closed on a forward it cannot assemble).
    """

    if not isinstance(params, list):
        return ("", "", "")

    def at(idx: int) -> str:
        return params[idx] if len(params) > idx and isinstance(params[idx], str) else ""

    return (at(2), at(3), at(4))


def build_connection_factory(
    *,
    resolver: StratumIdentityResolver,
    validator,
    credit_observed_at: Callable[[], datetime] = utc_now,
    hash_difficulty: Decimal = Decimal("1"),
    vardiff_retarget_samples: int = DEFAULT_RETARGET_SAMPLES,
    vardiff_floors: dict[Lane, Decimal] | None = None,
    submit_rate_headroom: Decimal | None = None,
    submit_rate_window: timedelta = DEFAULT_SUBMIT_RATE_WINDOW,
    submit_target_share_interval: timedelta = DEFAULT_TARGET_SHARE_INTERVAL,
    submit_max_consecutive_rejects: int = DEFAULT_MAX_CONSECUTIVE_REJECTS,
) -> Callable[[Lane, int], StratumConnection]:
    """A :class:`StratumConnection` factory binding the SHARED resolver + validator.

    One fresh handler per accepted socket (so per-connection identity + vardiff +
    submit-rate-limiter are isolated) but all sharing the SAME ``ShareValidator`` -> one
    ValidatedShareStore -> one credit budget. ``hash_difficulty`` defaults to the flat
    reconstruction unit the scheduler credits with (the documented split). The placeholder
    net-target factor on the handler is left at its default; the SERVER classifies
    solutions against the real upstream net difficulty, so the handler's net target
    is never the solution authority.

    ``vardiff_floors`` is a per-lane vardiff floor that is BOTH the no-``d=`` default
    AND a HARD MINIMUM: the mapped value is the floor a login that omits ``d=`` starts
    at, and is ALSO the minimum a login's own ``d=`` may never go below (a ``d=`` ABOVE
    it is honored; a ``d=`` BELOW it — e.g. a hostile ``d=1`` flood — is clamped UP to
    it). The Scrypt/ASIC lane sets an ASIC-scale floor (``16384``) so a rig CANNOT run
    the re-hash validator at ``d=1`` regardless of what it sends; lanes absent from the
    map use ``DEFAULT_VARDIFF_FLOOR`` (=1) for BOTH — no effective clamp, the right
    behaviour for CPU/GPU lanes like RandomX/KawPoW where ``d=1`` is legitimate.

    SUBMIT-FLOOD LIMITER (the BLOCKER fix). Each connection gets its OWN
    :class:`~alice_acp.transport_front.submit_rate_limit.SubmitRateLimiter` checked BEFORE
    the re-hash. ``submit_rate_headroom`` is a GENEROUS multiple of the at-target share
    rate (``1 / submit_target_share_interval``, ~15s by default → a tiny at-target rate) —
    so the per-window rate cap is ``ceil(headroom * at_target_rate * window)``, orders of
    magnitude above any legit rig's submit rate yet far below a line-rate flood. ``None``
    (the default — for a bare factory / the existing tests) leaves the RATE cap OFF.
    ``submit_max_consecutive_rejects`` caps a RUN of below-target / invalid submits (the
    part vardiff cannot throttle — it reacts only to ACCEPTS); ``0`` (the default) leaves
    it OFF. The deploy passes a generous headroom + a finite reject run so a flood is cut
    while a legit rig is never touched.
    """

    floors = vardiff_floors or {}

    def factory(lane: Lane, port: int) -> StratumConnection:
        # For a lane IN the map the mapped value is BOTH the no-d= default AND the hard
        # minimum (a login's d= may RAISE but never lower the floor below it). For a lane
        # ABSENT from the map the no-d= default is DEFAULT_VARDIFF_FLOOR (=1) but the hard
        # minimum is NO_VARDIFF_FLOOR_CLAMP (=0) — no clamp, so a legitimate low/sub-1 d=
        # (RandomX/KawPoW CPU/GPU lanes; the Scrypt scale uses sub-1 values) is honored.
        #
        # A FRESH submit-flood limiter per connection (isolated counters). When a generous
        # headroom is given the RATE cap is sized off the at-target share rate (a large
        # multiple, so a legit rig is never limited); when None the rate cap stays OFF and
        # only the consecutive-reject run (if > 0) applies. Either way it can ONLY deny.
        if submit_rate_headroom is not None:
            limiter = for_lane_rate(
                target_share_interval=submit_target_share_interval,
                headroom=submit_rate_headroom,
                window=submit_rate_window,
                max_consecutive_rejects=submit_max_consecutive_rejects,
            )
        else:
            limiter = SubmitRateLimiter(
                window=submit_rate_window,
                max_submits_per_window=0,  # rate cap OFF
                max_consecutive_rejects=submit_max_consecutive_rejects,
            )
        return StratumConnection(
            port=port,
            resolver=resolver,
            validator=validator,
            credit_observed_at=credit_observed_at,
            hash_difficulty=hash_difficulty,
            vardiff_retarget_samples=vardiff_retarget_samples,
            default_vardiff_floor=floors.get(lane, DEFAULT_VARDIFF_FLOOR),
            vardiff_min_floor=floors.get(lane, NO_VARDIFF_FLOOR_CLAMP),
            submit_rate_limiter=limiter,
        )

    return factory

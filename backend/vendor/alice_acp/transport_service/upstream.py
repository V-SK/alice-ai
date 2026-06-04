"""The production upstream stratum connection (R1) — thin stdlib asyncio TCP.

This is the only component that opens a REAL socket to an upstream pool (F2Pool LTC
``ltc.f2pool.com:5200`` / ravenminer). It is NEVER exercised by the test suite (the
suite injects a FAKE :class:`~alice_acp.transport_service.dispatcher.UpstreamConnection`),
mirroring how the merged ``UrllibPoolHttpClient`` is the untested real-network edge
while tests inject fakes.

It speaks the Bitcoin-family stratum client flow under Alice's own account:

  connect (force-IPv4 for LTC per the brief — F2Pool's IPv6 has been flaky)
    -> mining.subscribe                      -> capture extranonce1 / extranonce2_size
    -> mining.authorize [login, password]    (creds read at use-time, NEVER logged)
    -> read mining.set_difficulty            -> update the lane pool/share floor
    -> read mining.notify frames             -> hand each to the relay's on_job
    -> mining.submit (R3 forwards a solution here, [worker, job_id, extranonce2,
       ntime, nonce] for Scrypt) — worker rewritten to Alice's upstream login
    -> on drop: reconnect with backoff, re-subscribe + re-authorize

No heavy dependency: :func:`asyncio.open_connection` + newline-JSON framing. The
login/password come from the :class:`UpstreamCredentials` the relay built from env
at connect-time; they are held only for the lifetime of this connection object and
are NEVER written to the structured log (only stable codes are logged). The worker
field on an R3 submit is rewritten to Alice's upstream login so the upstream
attributes the solution to Alice's account (the foundation's revenue), SEPARATE
from the internal credit identity.

THE SUBSCRIBE-RESULT HANDOFF (what the real Scrypt R2 translator needs)
-----------------------------------------------------------------------
F2Pool's ``mining.subscribe`` REPLY carries the per-connection ``extranonce1`` +
``extranonce2_size`` the translator splices into the coinbase. This connector parses
that reply (matched by JSON-RPC id) into a
:class:`~alice_acp.transport_service.scrypt_translator.ScryptSubscription` and
exposes it via :meth:`current_subscription`; the relay wires that as the
translator's ``subscription_provider`` so every translated job uses the CURRENT
extranonce1 (a reconnect re-subscribes and the extranonce1 changes). Likewise the
latest ``mining.set_difficulty`` is exposed via :meth:`current_pool_difficulty` as
the translator's pool-floor source. NEITHER is a secret.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation

from alice_acp.transport_service.dispatcher import UpstreamCredentials
from alice_acp.transport_service.scrypt_translator import (
    ScryptSubscription,
    parse_subscribe_result,
)

#: Stable, secret-free codes for the upstream connection's own log.
UPSTREAM_SUBSCRIBE_SENT = "upstream_subscribe_sent"
UPSTREAM_SUBSCRIBED = "upstream_subscribed"  # subscribe RESULT parsed (extranonce1 set)
UPSTREAM_AUTHORIZED = "upstream_authorized"
UPSTREAM_SET_DIFFICULTY = "upstream_set_difficulty"
UPSTREAM_NOTIFY_RECEIVED = "upstream_notify_received"
UPSTREAM_SUBMIT_SENT = "upstream_submit_sent"
UPSTREAM_RECONNECTING = "upstream_reconnecting"
UPSTREAM_DISCONNECTED = "upstream_disconnected"


@dataclass(slots=True)
class AsyncStratumUpstream:
    """A real stdlib-asyncio upstream stratum client for one lane (R1).

    Construct with the (optional) reconnect backoff only; :meth:`connect` receives
    the at-use-time :class:`UpstreamCredentials` (host/port/login/password/force_ipv4)
    and the relay's ``on_job`` callback. The credentials are held on the instance
    ONLY for the connection's lifetime and never logged. ``force_ipv4`` constrains
    ``open_connection`` to ``AF_INET`` (the brief's LTC requirement — F2Pool's IPv6
    has been flaky).

    The connector tracks the LATEST subscribe result (``extranonce1`` /
    ``extranonce2_size``) and the LATEST ``mining.set_difficulty`` so the real Scrypt
    R2 translator can read them at translate time (a reconnect re-subscribes and the
    extranonce1 changes — the translator must always use the current one).
    """

    reconnect_backoff_seconds: float = 5.0
    max_reconnect_backoff_seconds: float = 60.0
    _reader: asyncio.StreamReader | None = field(default=None)
    _writer: asyncio.StreamWriter | None = field(default=None)
    _on_job: Callable[[dict], None] | None = field(default=None)
    _creds: UpstreamCredentials | None = field(default=None)
    _login: str | None = field(default=None)
    _read_task: asyncio.Task | None = field(default=None)
    _closed: bool = field(default=False)
    _msg_id: int = field(default=0)
    #: JSON-RPC id of the in-flight ``mining.subscribe`` (to match its result).
    _subscribe_id: int | None = field(default=None)
    _subscription: ScryptSubscription | None = field(default=None)
    _pool_difficulty: Decimal | None = field(default=None)
    #: Set once the first job arrives after a (re)connect — lets a caller await it.
    _subscribed_event: asyncio.Event = field(default_factory=asyncio.Event)
    event_log: list[str] = field(default_factory=list)

    # -- the relay-facing handoffs (public stratum values; not secrets) --------
    def current_subscription(self) -> ScryptSubscription | None:
        """The latest parsed ``mining.subscribe`` result (the translator reads this)."""

        return self._subscription

    def current_pool_difficulty(self) -> Decimal | None:
        """The latest ``mining.set_difficulty`` value (the translator's pool floor)."""

        return self._pool_difficulty

    # -- lifecycle -------------------------------------------------------------
    async def connect(self, creds: UpstreamCredentials, on_job: Callable[[dict], None]) -> None:
        """Open the upstream socket, subscribe + authorize, and start the read loop.

        Holds the creds for THIS connection's lifetime only (never logged). On the
        initial connect this performs the handshake synchronously enough to start the
        read loop; the read loop owns reconnect-with-backoff thereafter.
        """

        self._on_job = on_job
        self._creds = creds
        # Bind the upstream login for this connection only (NEVER logged); used to
        # attribute R3 submits to Alice's upstream account.
        self._login = creds.login
        await self._open_and_handshake(creds)
        self._read_task = asyncio.ensure_future(self._read_loop())

    async def _open_and_handshake(self, creds: UpstreamCredentials) -> None:
        family = socket.AF_INET if creds.force_ipv4 else socket.AF_UNSPEC
        self._reader, self._writer = await asyncio.open_connection(
            host=creds.host, port=creds.port, family=family
        )
        # Reset per-connection subscribe state (a reconnect re-subscribes; the old
        # extranonce1 is stale the instant the socket dropped).
        self._subscription = None
        self._subscribed_event.clear()
        self._subscribe_id = self._next_id()
        await self._send({"id": self._subscribe_id, "method": "mining.subscribe", "params": []})
        self.event_log.append(UPSTREAM_SUBSCRIBE_SENT)
        # Authorize under Alice's account. The password (secret) is sent on the wire
        # to the pool but never stored beyond this call / logged.
        await self._send(
            {
                "id": self._next_id(),
                "method": "mining.authorize",
                "params": [creds.login, creds.password],
            }
        )
        # Stable code only — NEVER the login/password (sent on the wire, not logged).
        self.event_log.append(UPSTREAM_AUTHORIZED)

    async def submit(self, payload: dict) -> bool:
        if self._writer is None or self._closed:
            return False
        # Rewrite the worker (params[0]) to Alice's upstream login so the upstream
        # credits the foundation's account. The internal worker_name never goes up.
        params = payload.get("params")
        if isinstance(params, list) and params and self._login is not None:
            params = list(params)
            params[0] = self._login
            payload = {**payload, "params": params}
        if payload.get("id") is None:
            payload = {**payload, "id": self._next_id()}
        try:
            await self._send(payload)
        except (ConnectionError, RuntimeError):
            return False
        self.event_log.append(UPSTREAM_SUBMIT_SENT)
        return True

    async def close(self) -> None:
        self._closed = True
        if self._read_task is not None:
            self._read_task.cancel()
            # CancelledError is a BaseException (not Exception) in py3.8+, so suppress
            # it explicitly alongside any teardown error.
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._read_task
        await self._close_socket()

    async def _close_socket(self) -> None:
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(Exception):
                await self._writer.wait_closed()
        self._reader = None
        self._writer = None

    # -- the read loop (owns reconnect-with-backoff) ---------------------------
    async def _read_loop(self) -> None:
        backoff = self.reconnect_backoff_seconds
        while not self._closed:
            assert self._reader is not None
            try:
                line = await self._reader.readuntil(b"\n")
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
                # Socket dropped. Reconnect with backoff unless we are closing.
                if self._closed:
                    break
                self.event_log.append(UPSTREAM_DISCONNECTED)
                reconnected = await self._reconnect(backoff)
                if not reconnected:
                    break
                backoff = min(backoff * 2.0, self.max_reconnect_backoff_seconds)
                continue
            backoff = self.reconnect_backoff_seconds  # a good read resets backoff
            self._handle_line(line)

    async def _reconnect(self, backoff: float) -> bool:
        """Close the dead socket, wait ``backoff``, and re-handshake. Fail-soft."""

        await self._close_socket()
        self.event_log.append(UPSTREAM_RECONNECTING)
        try:
            await asyncio.sleep(backoff)
        except asyncio.CancelledError:
            return False
        if self._closed or self._creds is None:
            return False
        try:
            await self._open_and_handshake(self._creds)
        except (ConnectionError, OSError):
            # Could not reconnect this round; the loop's next readuntil will fail and
            # trigger another backed-off attempt (the reader is None -> handled below).
            return await self._reconnect(min(backoff * 2.0, self.max_reconnect_backoff_seconds))
        return True

    def _handle_line(self, line: bytes) -> None:
        text = line.strip()
        if not text:
            return
        try:
            message = json.loads(text.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(message, dict):
            return

        method = message.get("method")
        # A RESULT to our mining.subscribe (matched by id) carries extranonce1/size.
        if message.get("id") is not None and message.get("id") == self._subscribe_id:
            self._absorb_subscribe_result(message)
            return
        if method == "mining.set_difficulty":
            self._absorb_set_difficulty(message)
            return
        if method == "mining.notify" and self._on_job is not None:
            self.event_log.append(UPSTREAM_NOTIFY_RECEIVED)
            self._subscribed_event.set()
            # Hand the upstream job to the relay (R2 translates it).
            with contextlib.suppress(Exception):
                self._on_job(message)

    def _absorb_subscribe_result(self, message: dict) -> None:
        result = message.get("result")
        try:
            self._subscription = parse_subscribe_result(result)
        except ValueError:
            # A malformed subscribe result leaves us without an extranonce1; the
            # translator will return None (no minable job) until a good one arrives.
            self._subscription = None
            return
        self.event_log.append(UPSTREAM_SUBSCRIBED)

    def _absorb_set_difficulty(self, message: dict) -> None:
        params = message.get("params")
        if not isinstance(params, list) or not params:
            return
        try:
            value = Decimal(str(params[0]))
        except (InvalidOperation, ValueError):
            return
        if value > 0:
            self._pool_difficulty = value
            self.event_log.append(UPSTREAM_SET_DIFFICULTY)

    async def _send(self, message: dict) -> None:
        if self._writer is None:
            raise ConnectionError("upstream writer is not connected")
        data = (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
        self._writer.write(data)
        await self._writer.drain()

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id


#: Stable, secret-free codes for the Monero upstream connection's own log.
MONERO_UPSTREAM_LOGIN_SENT = "monero_upstream_login_sent"
MONERO_UPSTREAM_LOGGED_IN = "monero_upstream_logged_in"  # login RESULT parsed (session id)
MONERO_UPSTREAM_JOB_RECEIVED = "monero_upstream_job_received"
MONERO_UPSTREAM_SUBMIT_SENT = "monero_upstream_submit_sent"
MONERO_UPSTREAM_KEEPALIVE_SENT = "monero_upstream_keepalive_sent"
MONERO_UPSTREAM_RECONNECTING = "monero_upstream_reconnecting"
MONERO_UPSTREAM_DISCONNECTED = "monero_upstream_disconnected"

#: DEFAULT idle interval (seconds) between ``keepalived`` pings. A Monero pool drops an
#: idle connection after ~10 min; pinging well inside that keeps the session warm.
DEFAULT_MONERO_KEEPALIVE_S = 60.0


@dataclass(slots=True)
class AsyncMoneroStratumUpstream:
    """A real stdlib-asyncio upstream stratum client for the RandomX/XMR lane (R1).

    The Monero mirror of :class:`AsyncStratumUpstream`, but the cryptonote/xmrig OBJECT
    dialect (NOT the Bitcoin-family subscribe/authorize two-step):

      connect (login under Alice's XMR address)
        -> ``login {login, pass, agent}``        (creds read at use-time, NEVER logged)
        -> parse the login RESULT ``{id:<session>, job:{...}, status:"OK"}``
           -> bind the session id + hand the inline first ``job`` to ``on_job``
        -> read ``{method:"job", params:{...}}`` pushes -> hand each to ``on_job``
        -> ``submit {id:<session>, job_id, nonce, result}`` (R3 forwards a solution here)
        -> ``keepalived {id:<session>}`` on an idle timer (so the pool keeps the session)
        -> on drop: reconnect with backoff, re-login (a fresh session id)

    Unlike the Scrypt connector there is NO ``mining.subscribe`` / extranonce handoff —
    the Monero pool hands a COMPLETE blob on every job, so the
    :class:`~alice_acp.transport_service.monero_translator.MoneroJobTranslator` needs no
    subscription provider. The login/password come from the
    :class:`UpstreamCredentials` the relay built from env at connect-time; they are held
    ONLY for the connection's lifetime and NEVER written to the log (stable codes only).
    The R3 submit's ``login`` (params is an OBJECT, no worker rewrite needed) is implicit
    — the session id the pool issued already attributes the share to Alice's account.
    """

    reconnect_backoff_seconds: float = 5.0
    max_reconnect_backoff_seconds: float = 60.0
    keepalive_interval_seconds: float = DEFAULT_MONERO_KEEPALIVE_S
    _reader: asyncio.StreamReader | None = field(default=None)
    _writer: asyncio.StreamWriter | None = field(default=None)
    _on_job: Callable[[dict], None] | None = field(default=None)
    _creds: UpstreamCredentials | None = field(default=None)
    _login: str | None = field(default=None)
    _read_task: asyncio.Task | None = field(default=None)
    _keepalive_task: asyncio.Task | None = field(default=None)
    _closed: bool = field(default=False)
    _msg_id: int = field(default=0)
    #: JSON-RPC id of the in-flight ``login`` (to match its result).
    _login_id: int | None = field(default=None)
    #: The pool-issued session id echoed on every ``submit`` / ``keepalived``.
    _session_id: str | None = field(default=None)
    event_log: list[str] = field(default_factory=list)

    # -- relay-facing handoffs (the Monero lane needs no subscription/pool-floor) --
    def current_session_id(self) -> str | None:
        """The pool-issued session id (echoed on R3 submits + keepalives)."""

        return self._session_id

    # -- lifecycle -------------------------------------------------------------
    async def connect(self, creds: UpstreamCredentials, on_job: Callable[[dict], None]) -> None:
        """Open the upstream socket, login, and start the read + keepalive loops.

        Holds the creds for THIS connection's lifetime only (never logged). The read
        loop owns reconnect-with-backoff; the keepalive loop pings ``keepalived`` on the
        idle timer so the pool does not drop the session.
        """

        self._on_job = on_job
        self._creds = creds
        self._login = creds.login
        await self._open_and_login(creds)
        self._read_task = asyncio.ensure_future(self._read_loop())
        if self.keepalive_interval_seconds > 0:
            self._keepalive_task = asyncio.ensure_future(self._keepalive_loop())

    async def _open_and_login(self, creds: UpstreamCredentials) -> None:
        family = socket.AF_INET if creds.force_ipv4 else socket.AF_UNSPEC
        self._reader, self._writer = await asyncio.open_connection(
            host=creds.host, port=creds.port, family=family
        )
        # Reset per-connection session state (a reconnect re-logs-in; the old session id
        # is stale the instant the socket dropped).
        self._session_id = None
        self._login_id = self._next_id()
        # The login (xmrig dialect): an OBJECT params {login, pass, agent}. The password
        # (secret) is sent on the wire to the pool but never stored beyond this / logged.
        await self._send(
            {
                "id": self._login_id,
                "method": "login",
                "params": {"login": creds.login, "pass": creds.password, "agent": "alice-acp/1"},
            }
        )
        self.event_log.append(MONERO_UPSTREAM_LOGIN_SENT)

    async def submit(self, payload: dict) -> bool:
        """Send one upstream Monero ``submit``; return whether it was dispatched.

        Stamps the pool-issued session id (params[``id``]) so the pool attributes the
        share to Alice's account, and fills the JSON-RPC envelope id. Fail-soft: returns
        ``False`` when not connected / no session yet / the write fails.
        """

        if self._writer is None or self._closed or self._session_id is None:
            return False
        params = payload.get("params")
        if isinstance(params, dict):
            params = {**params, "id": self._session_id}
            payload = {**payload, "params": params}
        if payload.get("id") is None:
            payload = {**payload, "id": self._next_id()}
        try:
            await self._send(payload)
        except (ConnectionError, RuntimeError):
            return False
        self.event_log.append(MONERO_UPSTREAM_SUBMIT_SENT)
        return True

    async def close(self) -> None:
        self._closed = True
        for task in (self._read_task, self._keepalive_task):
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        await self._close_socket()

    async def _close_socket(self) -> None:
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(Exception):
                await self._writer.wait_closed()
        self._reader = None
        self._writer = None

    # -- the read loop (owns reconnect-with-backoff) ---------------------------
    async def _read_loop(self) -> None:
        backoff = self.reconnect_backoff_seconds
        while not self._closed:
            assert self._reader is not None
            try:
                line = await self._reader.readuntil(b"\n")
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
                if self._closed:
                    break
                self.event_log.append(MONERO_UPSTREAM_DISCONNECTED)
                reconnected = await self._reconnect(backoff)
                if not reconnected:
                    break
                backoff = min(backoff * 2.0, self.max_reconnect_backoff_seconds)
                continue
            backoff = self.reconnect_backoff_seconds  # a good read resets backoff
            self._handle_line(line)

    async def _reconnect(self, backoff: float) -> bool:
        """Close the dead socket, wait ``backoff``, and re-login. Fail-soft."""

        await self._close_socket()
        self.event_log.append(MONERO_UPSTREAM_RECONNECTING)
        try:
            await asyncio.sleep(backoff)
        except asyncio.CancelledError:
            return False
        if self._closed or self._creds is None:
            return False
        try:
            await self._open_and_login(self._creds)
        except (ConnectionError, OSError):
            return await self._reconnect(min(backoff * 2.0, self.max_reconnect_backoff_seconds))
        return True

    async def _keepalive_loop(self) -> None:
        """Ping ``keepalived {id:session}`` on the idle timer so the pool keeps us."""

        while not self._closed:
            try:
                await asyncio.sleep(self.keepalive_interval_seconds)
            except asyncio.CancelledError:
                return
            if self._closed or self._writer is None or self._session_id is None:
                continue
            with contextlib.suppress(ConnectionError, RuntimeError):
                await self._send(
                    {
                        "id": self._next_id(),
                        "method": "keepalived",
                        "params": {"id": self._session_id},
                    }
                )
                self.event_log.append(MONERO_UPSTREAM_KEEPALIVE_SENT)

    def _handle_line(self, line: bytes) -> None:
        text = line.strip()
        if not text:
            return
        try:
            message = json.loads(text.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(message, dict):
            return

        # A RESULT to our login (matched by id) carries the session id + the first job.
        if message.get("id") is not None and message.get("id") == self._login_id:
            self._absorb_login_result(message)
            return
        # A job push: {"method":"job","params":{...blob, job_id, seed_hash, target...}}.
        if message.get("method") == "job" and self._on_job is not None:
            params = message.get("params")
            if isinstance(params, dict):
                self.event_log.append(MONERO_UPSTREAM_JOB_RECEIVED)
                with contextlib.suppress(Exception):
                    self._on_job(params)

    def _absorb_login_result(self, message: dict) -> None:
        result = message.get("result")
        if not isinstance(result, dict):
            return
        session_id = result.get("id")
        if isinstance(session_id, str) and session_id:
            self._session_id = session_id
            self.event_log.append(MONERO_UPSTREAM_LOGGED_IN)
        # The login result carries the FIRST job inline (xmrig style); hand it to the
        # relay so the lane has a current job immediately (no separate job push needed).
        job = result.get("job")
        if isinstance(job, dict) and self._on_job is not None:
            self.event_log.append(MONERO_UPSTREAM_JOB_RECEIVED)
            with contextlib.suppress(Exception):
                self._on_job(job)

    async def _send(self, message: dict) -> None:
        if self._writer is None:
            raise ConnectionError("monero upstream writer is not connected")
        data = (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
        self._writer.write(data)
        await self._writer.drain()

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id


#: Stable, secret-free codes for the KawPoW/RVN upstream connection's own log.
KAWPOW_UPSTREAM_SUBSCRIBE_SENT = "kawpow_upstream_subscribe_sent"
KAWPOW_UPSTREAM_SUBSCRIBED = "kawpow_upstream_subscribed"  # subscribe RESULT parsed
KAWPOW_UPSTREAM_AUTHORIZED = "kawpow_upstream_authorized"
KAWPOW_UPSTREAM_SET_TARGET = "kawpow_upstream_set_target"
KAWPOW_UPSTREAM_JOB_RECEIVED = "kawpow_upstream_job_received"
KAWPOW_UPSTREAM_SUBMIT_SENT = "kawpow_upstream_submit_sent"
KAWPOW_UPSTREAM_RECONNECTING = "kawpow_upstream_reconnecting"
KAWPOW_UPSTREAM_DISCONNECTED = "kawpow_upstream_disconnected"


@dataclass(slots=True)
class AsyncKawPoWStratumUpstream:
    """A real stdlib-asyncio upstream stratum client for the KawPoW/RVN lane (R1).

    The KawPoW mirror of :class:`AsyncStratumUpstream`, but the Ethereum/Ravencoin
    (T-Rex/kawpowminer) stratum dialect — a ``mining.subscribe`` + ``mining.authorize``
    two-step like the Bitcoin-family connector, with two KawPoW-specific differences:
    the subscribe reply is a simple 2-element ack (NO coinbase extranonce handoff — KawPoW
    has no coinbase fold), and the per-share target arrives via ``mining.set_target``
    (NOT ``mining.set_difficulty`` — KawPoW has none), with the ``mining.notify`` a
    7-element positional array:

      connect (login under Alice's RVN address)
        -> ``mining.subscribe``                  -> a 2-element result ack (no extranonce)
        -> ``mining.authorize [RVN_addr.worker, pass]`` (creds at use-time, NEVER logged)
        -> read ``mining.set_target [target]``   -> the lane's current 32-byte target
        -> read ``mining.notify [job_id, headerHash, seedHash, target, clean, height,
           bits]`` frames -> hand each to the relay's ``on_job`` (R2 translates it)
        -> ``mining.submit [login_worker, job_id, nonce, headerHash, mixHash]`` (R3
           forwards a solution here) — worker rewritten to Alice's upstream login
        -> on drop: reconnect with backoff, re-subscribe + re-authorize

    Unlike the Scrypt connector there is NO ``extranonce1`` / ``extranonce2_size`` handoff
    — a KawPoW pool hands a COMPLETE ``headerHash`` on every job, so the
    :class:`~alice_acp.transport_service.kawpow_translator.KawPoWJobTranslator` needs no
    subscription provider (the per-epoch DAG is keyed on the height in the notify, which
    the SERVER reads from the cached job at submit time). The login/password come from the
    :class:`UpstreamCredentials` the relay built from env at connect-time; they are held
    ONLY for the connection's lifetime and NEVER written to the log (stable codes only).
    The R3 submit's ``params[0]`` (worker) is rewritten to Alice's upstream login so the
    upstream attributes the solution to Alice's account (the foundation's revenue),
    SEPARATE from the internal credit identity.
    """

    reconnect_backoff_seconds: float = 5.0
    max_reconnect_backoff_seconds: float = 60.0
    _reader: asyncio.StreamReader | None = field(default=None)
    _writer: asyncio.StreamWriter | None = field(default=None)
    _on_job: Callable[[dict], None] | None = field(default=None)
    _creds: UpstreamCredentials | None = field(default=None)
    _login: str | None = field(default=None)
    _read_task: asyncio.Task | None = field(default=None)
    _closed: bool = field(default=False)
    _msg_id: int = field(default=0)
    #: JSON-RPC id of the in-flight ``mining.subscribe`` (to match its result).
    _subscribe_id: int | None = field(default=None)
    #: The latest ``mining.set_target`` 32-byte target (the lane's current pool target).
    _pool_target: str | None = field(default=None)
    #: Set once subscribed after a (re)connect — lets a caller await it.
    _subscribed_event: asyncio.Event = field(default_factory=asyncio.Event)
    event_log: list[str] = field(default_factory=list)

    # -- the relay-facing handoff (public stratum value; not a secret) ---------
    def current_pool_target(self) -> str | None:
        """The latest ``mining.set_target`` 32-byte target (advisory; not a secret).

        KawPoW has no ``mining.set_difficulty`` pool-floor; the per-share target arrives as
        a 32-byte ``mining.set_target``. The translator does NOT need it (the job's own
        ``target`` is the net target it reads); it is exposed for telemetry / a deploy that
        wants to clamp the per-connection vardiff floor to the upstream pool target.
        """

        return self._pool_target

    # -- lifecycle -------------------------------------------------------------
    async def connect(self, creds: UpstreamCredentials, on_job: Callable[[dict], None]) -> None:
        """Open the upstream socket, subscribe + authorize, and start the read loop.

        Holds the creds for THIS connection's lifetime only (never logged). The read loop
        owns reconnect-with-backoff thereafter.
        """

        self._on_job = on_job
        self._creds = creds
        # Bind the upstream login for this connection only (NEVER logged); used to
        # attribute R3 submits to Alice's upstream account.
        self._login = creds.login
        await self._open_and_handshake(creds)
        self._read_task = asyncio.ensure_future(self._read_loop())

    async def _open_and_handshake(self, creds: UpstreamCredentials) -> None:
        family = socket.AF_INET if creds.force_ipv4 else socket.AF_UNSPEC
        self._reader, self._writer = await asyncio.open_connection(
            host=creds.host, port=creds.port, family=family
        )
        # Reset per-connection subscribe state (a reconnect re-subscribes).
        self._subscribed_event.clear()
        self._subscribe_id = self._next_id()
        # KawPoW/Ethereum subscribe: an empty/agent params list (no extranonce expected
        # back — the pool replies with a simple ack and then pushes set_target + notify).
        await self._send(
            {"id": self._subscribe_id, "method": "mining.subscribe", "params": ["alice-acp/1"]}
        )
        self.event_log.append(KAWPOW_UPSTREAM_SUBSCRIBE_SENT)
        # Authorize under Alice's account. The password (secret) is sent on the wire to
        # the pool but never stored beyond this call / logged.
        await self._send(
            {
                "id": self._next_id(),
                "method": "mining.authorize",
                "params": [creds.login, creds.password],
            }
        )
        # Stable code only — NEVER the login/password (sent on the wire, not logged).
        self.event_log.append(KAWPOW_UPSTREAM_AUTHORIZED)

    async def submit(self, payload: dict) -> bool:
        """Send one upstream KawPoW ``mining.submit``; return whether it was dispatched.

        Rewrites ``params[0]`` (the worker) to Alice's upstream login so the pool credits
        the foundation's account; the internal worker_name never goes up. Fail-soft:
        returns ``False`` when not connected / the write fails.
        """

        if self._writer is None or self._closed:
            return False
        params = payload.get("params")
        if isinstance(params, list) and params and self._login is not None:
            params = list(params)
            params[0] = self._login
            payload = {**payload, "params": params}
        if payload.get("id") is None:
            payload = {**payload, "id": self._next_id()}
        try:
            await self._send(payload)
        except (ConnectionError, RuntimeError):
            return False
        self.event_log.append(KAWPOW_UPSTREAM_SUBMIT_SENT)
        return True

    async def close(self) -> None:
        self._closed = True
        if self._read_task is not None:
            self._read_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._read_task
        await self._close_socket()

    async def _close_socket(self) -> None:
        if self._writer is not None:
            self._writer.close()
            with contextlib.suppress(Exception):
                await self._writer.wait_closed()
        self._reader = None
        self._writer = None

    # -- the read loop (owns reconnect-with-backoff) ---------------------------
    async def _read_loop(self) -> None:
        backoff = self.reconnect_backoff_seconds
        while not self._closed:
            assert self._reader is not None
            try:
                line = await self._reader.readuntil(b"\n")
            except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
                if self._closed:
                    break
                self.event_log.append(KAWPOW_UPSTREAM_DISCONNECTED)
                reconnected = await self._reconnect(backoff)
                if not reconnected:
                    break
                backoff = min(backoff * 2.0, self.max_reconnect_backoff_seconds)
                continue
            backoff = self.reconnect_backoff_seconds  # a good read resets backoff
            self._handle_line(line)

    async def _reconnect(self, backoff: float) -> bool:
        """Close the dead socket, wait ``backoff``, and re-handshake. Fail-soft."""

        await self._close_socket()
        self.event_log.append(KAWPOW_UPSTREAM_RECONNECTING)
        try:
            await asyncio.sleep(backoff)
        except asyncio.CancelledError:
            return False
        if self._closed or self._creds is None:
            return False
        try:
            await self._open_and_handshake(self._creds)
        except (ConnectionError, OSError):
            return await self._reconnect(min(backoff * 2.0, self.max_reconnect_backoff_seconds))
        return True

    def _handle_line(self, line: bytes) -> None:
        text = line.strip()
        if not text:
            return
        try:
            message = json.loads(text.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return
        if not isinstance(message, dict):
            return

        method = message.get("method")
        # A RESULT to our mining.subscribe (matched by id): a simple ack (no extranonce).
        if message.get("id") is not None and message.get("id") == self._subscribe_id:
            self._subscribed_event.set()
            self.event_log.append(KAWPOW_UPSTREAM_SUBSCRIBED)
            return
        # ``mining.set_target [target]`` — the KawPoW per-share target (NOT set_difficulty).
        if method == "mining.set_target":
            self._absorb_set_target(message)
            return
        # ``mining.notify [job_id, headerHash, seedHash, target, clean, height, bits]`` —
        # the 7-element positional KawPoW job. Hand the WHOLE message to the relay so the
        # KawPoWJobTranslator unwraps the positional params (parse_kawpow_job).
        if method == "mining.notify" and self._on_job is not None:
            self.event_log.append(KAWPOW_UPSTREAM_JOB_RECEIVED)
            with contextlib.suppress(Exception):
                self._on_job(message)

    def _absorb_set_target(self, message: dict) -> None:
        params = message.get("params")
        if not isinstance(params, list) or not params:
            return
        target = params[0]
        if isinstance(target, str) and target:
            self._pool_target = target
            self.event_log.append(KAWPOW_UPSTREAM_SET_TARGET)

    async def _send(self, message: dict) -> None:
        if self._writer is None:
            raise ConnectionError("kawpow upstream writer is not connected")
        data = (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
        self._writer.write(data)
        await self._writer.drain()

    def _next_id(self) -> int:
        self._msg_id += 1
        return self._msg_id

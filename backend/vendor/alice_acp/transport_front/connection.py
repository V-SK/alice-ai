"""The per-connection transport-front handler — TRANSPORT-AGNOSTIC glue (doc §2.1).

This is the Alice-specific logic that sits between a (future) stratum transport
and the merged share-validator + credit spine. It operates ENTIRELY on PARSED
messages + injected callables — there is NO socket, NO asyncio, NO json framing
here. A future asyncio/stdlib server (or an xmrig-proxy pool-protocol adapter)
owns the wire; it:

  1. accepts a TCP connection on a per-algo PORT,
  2. reads + ``json.loads`` each newline-framed JSON-RPC message,
  3. calls :meth:`StratumConnection.handle_login` / :meth:`handle_submit` with the
     parsed message,
  4. ``json.dumps`` + writes the dict replies/notifications this returns.

The handler does the Alice-specific work:

* LOGIN → resolve the identity (``identity.StratumIdentityResolver``: gate-off =>
  reject; parse user; device-registry resolve (fail-closed); roster worker_name;
  PORT→lane; mint a roster-gated session). On success it binds the per-connection
  :class:`StratumConnectionIdentity` + a fresh :class:`Vardiff` (seeded by the
  password ``d=``) and replies OK (RandomX inline / Bitcoin-family authorize-ok).
* SUBMIT → only after a successful login (else fail-closed). Decode the rig's
  hex fields into the bytes a :class:`RawSubmission` needs, build it with the
  SERVER-OWNED identity + the two lane targets (vardiff = the pool target), and
  drive ``ShareValidator.validate(submission, session=…, lane=…,
  credit_observed_at=…, hash_difficulty=…, cursor_index=…)`` with EXACTLY the
  contract the validator documented — so the emitted ``canonical_share_hash``
  matches what the scheduler reconstructs at credit time. On an accepted share it
  advances vardiff + ACKs; on any non-accept it NACKs (fail-closed) and credits
  nothing.

THE CREDIT SEAM (doc §2.4, confirmed against the merged code). The
``ProxyPoolEvidenceProvider`` reads the ``ValidatedShareStore`` DIRECTLY (it
``drain_one``s the store; it never polls an HTTP snapshot). So the front's only
job on the credit side is to make the VALIDATOR WRITE the store — which
``ShareValidator.validate`` does for every ``is_share`` decision. The existing
provider + ``ProofAuthorityScheduler`` + ``credit_attested_shares`` then credit
it unchanged. NO snapshot endpoint is required; the front builds none. (A
read-only display snapshot shaped like ``PoolAddressSnapshot`` could be added for
the miner portal later, but it is NOT on the credit path.)

CREDIT-ONLY: the front sets no reward/payout/chain symbol and never reads a
payout address; ``credit_observed_at`` / ``hash_difficulty`` are passed through so
the credited magnitude stays Alice's recomputed difficulty (carried on the
``SelfValidatedShareAuthority`` override) while the canonical hash reconstructs
with the flat default — the documented split.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from alice_acp.shadow_server.server import RECONSTRUCTED_SHARE_DIFFICULTY
from alice_acp.shadow_server.types import utc_now
from alice_acp.share_validator.types import (
    RawSubmission,
    SubmissionIdentity,
    ValidationDecision,
)
from alice_acp.share_validator.validator import ShareValidator
from alice_acp.transport_front.identity import (
    LoginRejected,
    StratumConnectionIdentity,
    StratumIdentityResolver,
)
from alice_acp.transport_front.stratum_messages import (
    DIALECT_RANDOMX,
    STRATUM_ERR_UNAUTHORIZED,
    StratumLogin,
    StratumParseError,
    StratumSubmit,
    build_authorize_ok_reply,
    build_error_reply,
    build_login_ok_reply,
    build_set_difficulty_notification,
    build_submit_accepted_reply,
    build_submit_rejected_reply,
    parse_login,
    parse_submit,
)
from alice_acp.transport_front.submit_rate_limit import (
    SubmitRateLimiter,
)
from alice_acp.transport_front.vardiff import (
    DEFAULT_RETARGET_SAMPLES,
    DEFAULT_VARDIFF_FLOOR,
    NO_VARDIFF_FLOOR_CLAMP,
    Vardiff,
)

#: Reason codes for connection-level fail-closed rejections.
STRATUM_CONN_NOT_LOGGED_IN = "stratum_submit_before_login"
STRATUM_CONN_BAD_NONCE = "stratum_submit_undecodable_fields"
STRATUM_CONN_LOGIN_ALREADY = "stratum_login_already_completed"

#: Net-target headroom over the pool target. The pool (share) target is the
#: per-connection vardiff value; the network (solution) target is the real chain
#: difficulty supplied by the upstream relay. With no relay wired (M1 + deploy),
#: the front sets net = pool * this factor so a submission is classifiable as a
#: SHARE without being mislabelled a SOLUTION (relay is out of scope here). The
#: deploy passes the REAL net difficulty per lane once the relay lands.
DEFAULT_NET_TARGET_FACTOR = Decimal("1000000")


@dataclass(slots=True)
class SubmitResult:
    """The outcome of handling one submit: the validator decision + the wire reply.

    ``decision`` is ``None`` only when the submit was rejected BEFORE validation
    (not logged in / undecodable fields / OVER A SUBMIT-FLOOD BUDGET). ``reply`` is the
    dict the transport serializes back to the rig. ``set_difficulty`` is a
    ``mining.set_difficulty`` notification to also push when vardiff changed on this
    accepted share (else ``None``).

    ``disconnect`` is set when the per-connection submit-flood limiter tripped (a
    line-rate sub-target / invalid flood): the transport NACKs (``reply``) AND DROPS the
    connection (and briefly bans the peer at admission). ``disconnect_reason`` is the
    stable, secret-free code for the drop event. A normal submit leaves ``disconnect``
    ``False``. CRUCIAL: a tripped submit returns BEFORE the re-hash, so a flood of cheap
    garbage never reaches the expensive validator — and a dropped submit credits nothing.
    """

    reply: dict[str, Any]
    decision: ValidationDecision | None = None
    set_difficulty: dict[str, Any] | None = None
    disconnect: bool = False
    disconnect_reason: str = ""


@dataclass(slots=True)
class StratumConnection:
    """One miner connection's Alice-side handler (transport-agnostic).

    Bind one per accepted TCP connection. ``port`` is the SERVER listener port the
    connection arrived on (the lane authority — never a client field).
    ``resolver`` runs the §2.5 identity chain; ``validator`` is the shared
    :class:`ShareValidator`. ``credit_observed_at`` is a callable returning the
    timestamp the credit poll will reconstruct with (the deploy passes the SAME
    clock to the front and the scheduler so the canonical hash matches);
    ``hash_difficulty`` is the difficulty the scheduler reconstructs the canonical
    hash with (the flat production default). ``net_target_factor`` sets the
    solution target relative to the vardiff pool target until the relay supplies
    a real net difficulty.

    State (the bound identity + vardiff) is set on a successful login and used by
    every subsequent submit; a submit before login fails closed.
    """

    port: int
    resolver: StratumIdentityResolver
    validator: ShareValidator
    credit_observed_at: Callable[[], datetime] = utc_now
    hash_difficulty: Decimal = RECONSTRUCTED_SHARE_DIFFICULTY
    default_vardiff_floor: Decimal = DEFAULT_VARDIFF_FLOOR
    #: Per-lane HARD MINIMUM vardiff floor. A login's own ``d=`` may RAISE the floor
    #: but can NEVER lower it below this minimum (the public LTC/Scrypt lane sets
    #: ``16384`` so a hostile ``d=1`` cannot flood the re-hash validator). Defaults to
    #: :data:`NO_VARDIFF_FLOOR_CLAMP` (=0) — no clamp (the right value for CPU/GPU lanes
    #: like RandomX/KawPoW, and it never wrongly raises a legitimate sub-1 ``d=``).
    #: Plumbed by :func:`build_connection_factory` (= the per-lane vardiff floor).
    vardiff_min_floor: Decimal = NO_VARDIFF_FLOOR_CLAMP
    #: Vardiff sample-window size (how many inter-share intervals the retarget
    #: averages, and a full window forces an early retarget). The production default
    #: smooths Poisson noise; tests use a small window for deterministic retargets.
    vardiff_retarget_samples: int = DEFAULT_RETARGET_SAMPLES
    net_target_factor: Decimal = DEFAULT_NET_TARGET_FACTOR
    session_ttl: timedelta = timedelta(minutes=30)
    #: Per-connection submit-FLOOD limiter (the PRE-RE-HASH gate; doc §2.1 BLOCKER fix).
    #: Checked at the TOP of :meth:`handle_submit` — BEFORE ``_build_submission`` and the
    #: expensive ``validator.validate`` re-hash — so a line-rate flood of well-formed
    #: SUB-TARGET / invalid garbage (which vardiff never throttles, since vardiff reacts
    #: only to ACCEPTED shares) can NEVER reach the re-hash that pegs a core. Over budget →
    #: NACK + drop + brief ban (the transport enforces the drop/ban). Defaults to a limiter
    #: with BOTH budgets OFF (``0``) — so a bare connection / the existing tests are never
    #: throttled; :func:`build_connection_factory` (the deploy) injects a generous,
    #: lane-aware cap. NEVER an accept authority — it can only DENY, never credit.
    submit_rate_limiter: SubmitRateLimiter = field(default_factory=SubmitRateLimiter)
    #: Scrypt-lane header reconstructor (job_id, downstream_extranonce1, extranonce2,
    #: ntime, nonce) -> 80-byte header, or ``None``. Injected by the server (which owns
    #: the job cache + the coinbase-fold). ``None`` on a non-proxy/test path falls back
    #: to ``_decode_work`` (which treats the last positional as a pre-assembled header).
    scrypt_header_builder: Callable[[str, str, str, str, str], bytes | None] | None = None
    #: RandomX/XMR-lane blob reconstructor (job_id, nonce_extra_hex, nonce_hex) ->
    #: ``(seed, blob)`` bytes, or ``None``. Injected by the server (which owns the job
    #: cache): it looks up the cached job by id, splices ONLY the rig's nonce at [39:43]
    #: (the rest of the blob — including byte 8 — is kept VERBATIM, exactly as the upstream
    #: pool sent it and the rig hashed it, so Alice's re-hash reproduces the rig's result
    #: and the forwarded share is valid upstream), and returns ``seed =
    #: bytes.fromhex(seed_hash)`` + the reconstructed blob the verifier re-hashes. The
    #: ``nonce_extra_hex`` argument is accepted for signature stability but IGNORED (the
    #: server passes ``""``). ``None`` on a non-proxy/test path falls back to
    #: ``_decode_work`` (which treats the submit's ``result`` as the header — the legacy
    #: stub, fail-closed without a job).
    monero_blob_builder: Callable[[str, str, str], tuple[bytes, bytes] | None] | None = None
    #: KawPoW/RVN-lane header+epoch reconstructor (job_id) -> ``(epoch, block_number,
    #: header_hash_bytes)``, or ``None``. Injected by the server (which owns the job
    #: cache): it looks the cached job up by id and returns the per-epoch + block-height
    #: inputs the KawPoW verifier needs PLUS the 32-byte headerHash the rig hashed — the
    #: epoch + height are SERVER-sourced from the cached job (``epoch = height //
    #: KAWPOW_EPOCH_LENGTH``), NEVER client-supplied (the novel-epoch DoS defense). The
    #: verifier re-hashes ``header_hash`` + the rig's nonce against the DAG for ``epoch``.
    #: ``None`` on a non-proxy/test path falls back to ``_decode_work`` (which treats the
    #: submit's ``header_hash`` field as the header with NO epoch — fail-closed without a
    #: real job/lib: the verifier cannot validate an unknown-epoch header).
    kawpow_header_builder: Callable[[str], tuple[int, int, bytes] | None] | None = None
    #: RandomX/XMR-lane INLINE first-job builder (``(pool_difficulty: Decimal) -> job
    #: object | None``), injected by the server (which owns the job cache + the
    #: per-connection target encoding). On a RandomX ``login`` the OK reply MUST carry the
    #: first job INLINE in ``result.job`` (xmrig reads its first job from there — it does
    #: NOT understand a separate ``mining.notify``). The server passes the connection's
    #: vardiff difficulty so the inlined job's ``target`` reflects THIS connection's pool
    #: difficulty. ``None`` (a test/non-proxy path, or no job yet) replies OK with no
    #: inline job — fail-soft (the server may still push a ``job`` afterward).
    xmr_initial_job_builder: Callable[[Decimal], dict[str, Any] | None] | None = None
    identity: StratumConnectionIdentity | None = field(default=None)
    vardiff: Vardiff | None = field(default=None)
    #: This connection's downstream extranonce1 (upstream extranonce1 + the server's
    #: per-connection suffix), set by the server at ``mining.subscribe``. The rig built
    #: its coinbase with this exact value; Alice re-derives the share with the SAME one.
    downstream_extranonce1: str = ""
    #: RETAINED for signature stability only — the RandomX blob is reconstructed VERBATIM,
    #: so this is unused and the server leaves it empty. (Alice must NOT rewrite any blob
    #: byte: the blob goes down to the rig verbatim and the rig's result is forwarded
    #: upstream, where the pool re-validates against its own unmodified blob — any rewrite
    #: would reject every share. Multi-rig de-collision is the 2**32 nonce search at
    #: offset 39 + the upstream (job_id, nonce) dedup, not a blob rewrite.)
    nonce_extra_hex: str = ""
    #: The connection's SERVER-observed source IP (set by the transport on accept; never
    #: a client field). Passed to ``resolve_login`` for the OPEN-mode per-IP rate limit;
    #: ignored on the roster path. Empty when the platform did not expose a peername.
    peer_ip: str = ""
    _lock: threading.Lock = field(default_factory=threading.Lock)

    # -- login -------------------------------------------------------------
    def handle_login(self, message: Any) -> dict[str, Any]:
        """Parse + resolve a login; return the wire reply (OK or fail-closed error).

        Accepts a RandomX ``login`` OR a Bitcoin-family ``mining.authorize`` (the
        transport handles ``mining.subscribe`` itself; this is the identity step).
        On success binds the per-connection identity + vardiff and returns the
        dialect-appropriate OK reply. On ANY failure (gate off, malformed user,
        unresolved device, not on roster, session-not-issued) returns a JSON-RPC
        error reply and leaves the connection unbound (no jobs, no submits).
        """

        parsed = parse_login(message)
        if isinstance(parsed, StratumParseError):
            return build_error_reply(parsed)
        assert isinstance(parsed, StratumLogin)

        with self._lock:
            if self.identity is not None:
                # A second login on an already-bound connection is refused
                # (fail-closed; the transport should open a fresh connection).
                return build_error_reply(
                    StratumParseError(STRATUM_CONN_LOGIN_ALREADY, message_id=parsed.message_id)
                )
            outcome = self.resolver.resolve_login(
                username=parsed.username,
                port=self.port,
                observed_at=self.credit_observed_at(),
                peer_ip=self.peer_ip,
            )
            if isinstance(outcome, LoginRejected):
                # Map an identity rejection to an unauthorized JSON-RPC error +
                # drop. Reason is the stable code (never a raw error).
                return build_error_reply(
                    StratumParseError(
                        outcome.reason,
                        error_code=STRATUM_ERR_UNAUTHORIZED,
                        message_id=parsed.message_id,
                    )
                )
            assert isinstance(outcome, StratumConnectionIdentity)
            self.identity = outcome
            self.vardiff = Vardiff.from_password(
                parsed.password,
                default_floor=self.default_vardiff_floor,
                min_floor=self.vardiff_min_floor,
                retarget_samples=self.vardiff_retarget_samples,
            )

            if parsed.dialect == DIALECT_RANDOMX:
                # xmrig reads its FIRST job from the login-OK ``result.job`` INLINE — it
                # does NOT understand a separate mining.notify. Build that inline job from
                # the lane's current job re-keyed with THIS connection's per-connection
                # vardiff target (the server-injected builder owns the job cache + the
                # compact-target encoding). No builder / no job yet -> OK with no inline
                # job (fail-soft; the server may push a ``job`` afterward).
                inline_job: dict[str, Any] | None = None
                if self.xmr_initial_job_builder is not None:
                    inline_job = self.xmr_initial_job_builder(self.vardiff.current)
                return build_login_ok_reply(
                    message_id=parsed.message_id,
                    session_id=outcome.session.session_id,
                    job=inline_job,
                )
            return build_authorize_ok_reply(message_id=parsed.message_id)

    # -- submit ------------------------------------------------------------
    def handle_submit(self, message: Any) -> SubmitResult:
        """Parse a submit, drive the validator, and return the reply + decision.

        Fail-closed before validation: a submit on an unbound connection, a
        malformed submit, or undecodable hex fields all NACK with no validation.
        Otherwise builds a :class:`RawSubmission` with the SERVER-OWNED identity +
        the two lane targets (pool target = the current vardiff value) and calls
        ``validator.validate`` with the documented contract. An accepted (is_share)
        decision advances vardiff (possibly emitting a ``mining.set_difficulty``)
        and ACKs; any other decision NACKs.
        """

        parsed = parse_submit(message)
        if isinstance(parsed, StratumParseError):
            return SubmitResult(reply=build_error_reply(parsed))
        assert isinstance(parsed, StratumSubmit)

        with self._lock:
            identity = self.identity
            vardiff = self.vardiff
            if identity is None or vardiff is None:
                return SubmitResult(
                    reply=build_submit_rejected_reply(
                        message_id=parsed.message_id,
                        reason=STRATUM_CONN_NOT_LOGGED_IN,
                    )
                )

            # ONE clock read for this submit: used for the submit-flood limiter and the
            # validator's credit timestamp — so both see a consistent "now" (the injected
            # clock; never wall-clock).
            now = self.credit_observed_at()

            # SUBMIT-FLOOD GATE (the BLOCKER fix) — evaluated BEFORE the expensive re-hash.
            # The per-connection rate cap is cheap O(1) arithmetic; tripping it here means a
            # line-rate flood of well-formed sub-target / invalid garbage NEVER reaches
            # ``_build_submission`` / ``validator.validate`` (the full PoW re-hash that pegs
            # a core). vardiff cannot do this — it reacts only to ACCEPTED shares, so a
            # pure-reject flood would never move it. Over the rate budget → NACK + DROP +
            # brief ban (the transport enforces the drop). A dropped submit credits nothing.
            gate = self.submit_rate_limiter.check(at=now)
            if not gate.allowed:
                return SubmitResult(
                    reply=build_submit_rejected_reply(
                        message_id=parsed.message_id, reason=gate.reason
                    ),
                    disconnect=gate.disconnect,
                    disconnect_reason=gate.reason,
                )

            try:
                submission = self._build_submission(parsed, identity, vardiff)
            except _UndecodableSubmit:
                # An undecodable submit is a (cheap) reject — feed the consecutive-reject run
                # so a flood of garbage-field submits also trips the run cap + drops.
                drop = self.submit_rate_limiter.note_outcome(is_share=False)
                return SubmitResult(
                    reply=build_submit_rejected_reply(
                        message_id=parsed.message_id,
                        reason=STRATUM_CONN_BAD_NONCE,
                    ),
                    disconnect=drop.disconnect,
                    disconnect_reason=drop.reason,
                )

            decision = self.validator.validate(
                submission,
                session=identity.session,
                lane=identity.lane,
                credit_observed_at=now,
                hash_difficulty=self.hash_difficulty,
                cursor_index=0,
            )

            # Feed the verdict into the consecutive-reject run: an accepted share resets it;
            # a below-target / invalid submit extends it, and a long enough run trips the run
            # cap → drop + brief ban (the ckpool "run of rejects" shape). This is the part
            # vardiff structurally cannot cover (it only ever sees ACCEPTS).
            drop = self.submit_rate_limiter.note_outcome(is_share=decision.is_share)

            if not decision.is_share:
                return SubmitResult(
                    reply=build_submit_rejected_reply(
                        message_id=parsed.message_id, reason=decision.reason
                    ),
                    decision=decision,
                    disconnect=drop.disconnect,
                    disconnect_reason=drop.reason,
                )

            # Accepted credited share: retarget vardiff and ACK. Push a new
            # difficulty only when it actually changed.
            before = vardiff.current
            after = vardiff.on_share(at=self.credit_observed_at())
            set_diff = (
                build_set_difficulty_notification(format(after.normalize(), "f"))
                if after != before
                else None
            )
            return SubmitResult(
                reply=build_submit_accepted_reply(
                    message_id=parsed.message_id, dialect=parsed.dialect
                ),
                decision=decision,
                set_difficulty=set_diff,
            )

    def _build_submission(
        self,
        parsed: StratumSubmit,
        identity: StratumConnectionIdentity,
        vardiff: Vardiff,
    ) -> RawSubmission:
        """Turn a parsed submit + bound identity into a ``RawSubmission``.

        The pool (share) target is the current vardiff value; the net (solution)
        target is ``pool * net_target_factor`` until the relay supplies the real
        net difficulty. The work bytes are decoded from the rig's hex fields per
        dialect. The identity is the SERVER-OWNED one (worker_name / pool_id /
        collection address / session id) — the rig's submitted worker is ignored.
        ``claimed_difficulty`` is left ``None`` (the front does not trust a
        self-reported difficulty; SELF-REPORT NEVER COUNTS).
        """

        pool_target = vardiff.current
        net_target = pool_target * self.net_target_factor
        algorithm = _ALGO_FOR_LANE[identity.lane]
        epoch = 0
        block_number = 0
        if algorithm == LTC_SCRYPT and self.scrypt_header_builder is not None:
            # PROXY CORE: a stock LTC rig submits [worker, job_id, extranonce2, ntime,
            # nonce] — Alice re-derives the 80-byte header from the job + this submit.
            seed, header, nonce, extranonce = self._reconstruct_scrypt(parsed)
        elif algorithm == XMR_RANDOMX and self.monero_blob_builder is not None:
            # PROXY CORE: a stock RandomX rig (xmrig) submits {job_id, nonce, result} —
            # Alice re-derives the EXACT 76-byte blob from the cached job + this submit
            # (nonce at [39:43], this connection's nonce_extra at byte 8) and feeds the
            # per-epoch seed_hash so the verifier builds the right seed-keyed VM.
            seed, header, nonce, extranonce = self._reconstruct_monero(parsed)
        elif algorithm == RVN_KAWPOW and self.kawpow_header_builder is not None:
            # PROXY CORE: a stock KawPoW rig (T-Rex) submits [worker, job_id, nonce,
            # headerHash, mixHash] — Alice re-derives the SERVER-sourced epoch + block
            # height from the CACHED job (NEVER the client's submitted header/epoch; the
            # novel-epoch DoS defense) and re-hashes the cached headerHash + the rig's nonce
            # against the DAG for that epoch.
            seed, header, nonce, extranonce, epoch, block_number = self._reconstruct_kawpow(parsed)
        else:
            seed, header, nonce, extranonce = _decode_work(parsed, algorithm)
        return RawSubmission(
            algorithm=algorithm,
            seed=seed,
            header=header,
            nonce=nonce,
            extranonce=extranonce,
            identity=SubmissionIdentity(
                pool_id=identity.pool_id,
                worker_name=identity.worker_name,
                session_id=identity.session.session_id,
                alice_collection_address=identity.alice_collection_address,
            ),
            pool_target_difficulty=pool_target,
            net_target_difficulty=net_target,
            pool_job_id=parsed.job_id if _safe_job_id(parsed.job_id) else "",
            epoch=epoch,
            block_number=block_number,
        )

    def _reconstruct_scrypt(self, parsed: StratumSubmit) -> tuple[bytes, bytes, bytes, bytes]:
        """Rebuild the 80-byte Scrypt header from the job + this submit (the proxy core).

        A stock Litecoin rig submits ``mining.submit [worker, job_id, extranonce2,
        ntime, nonce]`` — NOT a pre-assembled header. Alice re-derives the EXACT header
        the rig hashed by folding the job's coinbase template (coinb1/coinb2/merkle,
        looked up by job id) with THIS connection's ``downstream_extranonce1`` + the
        submitted extranonce2, then serializing version/prevhash/merkle/ntime/nonce. The
        injected ``scrypt_header_builder`` (wired by the server, which owns the job
        cache + the coinbase-fold) does the assembly; we hand it our per-connection
        extranonce1. A missing job or malformed field yields no header -> fail-closed
        :class:`_UndecodableSubmit` (NACK, credits nothing). The returned ``header`` is
        the full 80-byte pre-image (nonce already spliced) the verifier hashes as-is.
        """

        rp = parsed.raw_params
        if len(rp) < 5:
            raise _UndecodableSubmit("scrypt_submit_too_short")
        extranonce2_hex, ntime_hex, nonce_hex = rp[2], rp[3], rp[4]
        builder = self.scrypt_header_builder
        assert builder is not None  # guarded by the caller (only invoked when set)
        header = builder(
            parsed.job_id, self.downstream_extranonce1, extranonce2_hex, ntime_hex, nonce_hex
        )
        if header is None:
            raise _UndecodableSubmit("scrypt_reconstruct_failed")
        return b"", header, b"", _decode_hex(extranonce2_hex)

    def _reconstruct_monero(self, parsed: StratumSubmit) -> tuple[bytes, bytes, bytes, bytes]:
        """Rebuild the RandomX mining blob from the cached job + this submit (the proxy core).

        A stock RandomX rig (xmrig) submits ``submit {id, job_id, nonce, result}`` — NOT
        a blob. Alice re-derives the EXACT blob the rig hashed by looking the job up by
        id (its cached ``blob`` + ``seed_hash``) and splicing ONLY the rig's submitted
        ``nonce`` at bytes [39:43] (little-endian, as the rig had them) — every other byte
        is kept VERBATIM (the upstream pool sent that blob, the rig hashed it, and Alice
        forwards the rig's result back to that pool, so the blob must match byte-for-byte).
        The injected ``monero_blob_builder`` (wired by the server, which owns the job
        cache) does the nonce splice and returns ``(seed, blob)`` where ``seed =
        bytes.fromhex(seed_hash)`` — the per-epoch RandomX VM key.
        A missing/evicted job or malformed field yields no blob -> fail-closed
        :class:`_UndecodableSubmit` (NACK, credits nothing). The returned ``header`` is
        the reconstructed blob (the verifier re-splices the [39:43] nonce + hashes it
        against ``seed``); the ``nonce`` is carried so the dedup key + the verifier's
        re-splice use the rig's exact bytes.
        """

        builder = self.monero_blob_builder
        assert builder is not None  # guarded by the caller (only invoked when set)
        nonce = _decode_hex(parsed.nonce)
        built = builder(parsed.job_id, self.nonce_extra_hex, parsed.nonce)
        if built is None:
            raise _UndecodableSubmit("monero_reconstruct_failed")
        seed, blob = built
        return seed, blob, nonce, b""

    def _reconstruct_kawpow(
        self, parsed: StratumSubmit
    ) -> tuple[bytes, bytes, bytes, bytes, int, int]:
        """Re-derive the KawPoW header + epoch from the cached job + this submit (proxy core).

        A stock KawPoW rig (T-Rex / kawpowminer) submits ``mining.submit [worker, job_id,
        nonce, headerHash, mixHash]`` — but the SERVER does NOT trust the submitted
        headerHash/epoch. Alice looks the job up by id (its cached ``headerHash`` + the
        ``height``) and returns the SERVER-sourced ``(epoch, block_number, header_hash)``:
        ``epoch = height // KAWPOW_EPOCH_LENGTH``, the block height KawPoW mixes in, and the
        EXACT 32-byte headerHash the rig was handed. This is the novel-epoch DoS defense — a
        hostile rig cannot make Alice build an arbitrary-epoch DAG by lying about the epoch
        in its submit. The injected ``kawpow_header_builder`` (wired by the server, which
        owns the job cache) does the lookup. A missing/evicted job yields no header ->
        fail-closed :class:`_UndecodableSubmit` (NACK, credits nothing). The returned
        ``header`` is the cached headerHash (the verifier re-hashes it + the rig's nonce
        against the epoch DAG); ``seed`` is empty (the verifier keys on the epoch number,
        not the seed bytes); the ``nonce`` is the rig's submitted nonce; ``epoch`` /
        ``block_number`` flow onto the RawSubmission for the verifier.

        THE NONCE POSITION (a subtle but load-bearing detail): the parser's
        ``StratumSubmit.nonce`` is the LAST positional element (a display/dedup default),
        which on a KawPoW submit ``[worker, job_id, nonce, headerHash, mixHash]`` is the
        MIXHASH — NOT the nonce. So we read the nonce from ``raw_params[2]`` (its true
        KawPoW position); using ``parsed.nonce`` would feed the mixHash as the nonce, and
        since the mixHash is constant across a rig's shares for one header it would collapse
        every share to ONE dedup key (a spurious duplicate). The dedup key must vary with
        the actual nonce.
        """

        builder = self.kawpow_header_builder
        assert builder is not None  # guarded by the caller (only invoked when set)
        rp = parsed.raw_params
        if len(rp) < 3:
            raise _UndecodableSubmit("kawpow_submit_too_short")
        nonce = _decode_hex(rp[2])  # the KawPoW nonce is at position 2 (NOT parsed.nonce)
        built = builder(parsed.job_id)
        if built is None:
            raise _UndecodableSubmit("kawpow_reconstruct_failed")
        epoch, block_number, header_hash = built
        return b"", header_hash, nonce, b"", epoch, block_number


class _UndecodableSubmit(Exception):
    """A submit whose hex work fields could not be decoded (fail-closed NACK)."""


# Lane -> algorithm (the same mapping server.py::_algorithm_for_lane uses; the
# validator routes the submission to the verifier keyed by this algorithm).
from alice_acp.mining_session.types import (  # noqa: E402  (kept local to the module)
    LTC_SCRYPT,
    RVN_KAWPOW,
    XMR_RANDOMX,
)
from alice_acp.shadow_server.types import (  # noqa: E402
    MAIN_POOL_GPU_QUAI,
    MAIN_POOL_GPU_RVN,
    SCRYPT_POOL,
    XMR_POOL,
)

_ALGO_FOR_LANE = {
    XMR_POOL: XMR_RANDOMX,
    MAIN_POOL_GPU_RVN: RVN_KAWPOW,
    # The Quai lane is KawPoW too — it routes to the SAME RVN_KAWPOW verifier (the
    # crypto is identical; reuse is keyed by algorithm). No new algorithm constant.
    MAIN_POOL_GPU_QUAI: RVN_KAWPOW,
    SCRYPT_POOL: LTC_SCRYPT,
}


def _decode_hex(value: str) -> bytes:
    """Decode a rig-supplied hex string into bytes (fail-closed on garbage).

    Tolerates a leading ``0x`` and odd-length strings (left-padded) the way
    stratum fields sometimes arrive; raises :class:`_UndecodableSubmit` on
    non-hex so the submit NACKs rather than fabricating bytes.
    """

    text = value[2:] if value.lower().startswith("0x") else value
    if len(text) % 2 == 1:
        text = "0" + text
    try:
        return bytes.fromhex(text)
    except ValueError as exc:
        raise _UndecodableSubmit(str(exc)) from exc


def _decode_work(parsed: StratumSubmit, algorithm: str) -> tuple[bytes, bytes, bytes, bytes]:
    """Decode (seed, header, nonce, extranonce) bytes for the validator, LANE-AWARE.

    The KawPoW and Scrypt on-wire ``mining.submit`` are POSITIONAL with DIFFERENT
    field orders that the message parser cannot disambiguate — so the algorithm
    (derived from the connection's PORT, the server-side lane authority) selects
    the layout here:

    * RandomX (``XMR_RANDOMX``): the self-describing object payload — ``nonce`` +
      the optional ``result``. This is the NON-PROXY FALLBACK only: the real proxy
      path reconstructs the blob from the cached job via the injected
      ``monero_blob_builder`` (``_reconstruct_monero``), feeding the per-epoch seed +
      the byte-8-extranonce/[39:43]-nonce-spliced blob. With ONLY the submit in hand
      (no job cache injected) there is no real pre-image, so the ``header`` carries the
      submitted ``result`` and the ``seed`` is empty — which the RandomX verifier
      treats as a candidate it cannot validate against a real seed/blob (it fails
      closed when ``librandomx`` is absent, and against a real lib a bare result-as-blob
      with an empty seed re-hashes to a mismatch — NEVER a fabricated accept).
    * KawPoW (``RVN_KAWPOW``): ``[worker, job_id, nonce, header_hash, mix_hash]`` —
      ``header`` = the pow header_hash (position 3), ``nonce`` = position 2. This is the
      NON-PROXY FALLBACK only: the real proxy path reconstructs the epoch + block height
      from the CACHED job via the injected ``kawpow_header_builder``
      (``_reconstruct_kawpow``), so the verifier keys its DAG on the SERVER-sourced epoch
      (never the client's). With ONLY the submit in hand (no job cache injected) there is
      no server-sourced epoch, so the ``header`` carries the submitted header_hash with
      ``epoch`` left at 0 — which the KawPoW verifier treats as a candidate it cannot
      validate against a real epoch DAG (it fails closed when no reference lib is present,
      and against a real lib an unknown-epoch header re-hashes to a mismatch — NEVER a
      fabricated accept).
    * Scrypt (``LTC_SCRYPT``): ``[worker, job_id, extranonce2, ntime, nonce]`` —
      the deploy assembles the 80-byte header at job time from the upstream blob +
      ``extranonce2`` + ``ntime``; the last positional element is the assembled
      pre-image (or the nonce for a relay-fed deploy). The local Scrypt verifier
      re-hashes an 80-byte header (``header[:76]+nonce``) or a pre-assembled
      header, so the front passes the last positional element through as the
      header pre-image and the prior positions as extranonce context.

    A field that is present but not valid hex fails closed.
    """

    if parsed.dialect == DIALECT_RANDOMX:
        nonce = _decode_hex(parsed.nonce)
        result = parsed.extra.get("result")
        header = _decode_hex(result) if result else b""
        return b"", header, nonce, b""

    params = parsed.raw_params
    if algorithm == RVN_KAWPOW:
        # [worker, job_id, nonce, header_hash, mix_hash]
        if len(params) < 4:
            raise _UndecodableSubmit("kawpow_submit_too_short")
        nonce = _decode_hex(params[2])
        header = _decode_hex(params[3])
        extranonce = _decode_hex(params[4]) if len(params) > 4 and params[4] else b""
        return b"", header, nonce, extranonce

    # LTC_SCRYPT: [worker, job_id, extranonce2, ntime, nonce-or-preimage]
    if len(params) < 3:
        raise _UndecodableSubmit("scrypt_submit_too_short")
    preimage = _decode_hex(params[-1])
    extranonce = _decode_hex(params[2]) if params[2] else b""
    # The 80-byte header pre-image (deploy-assembled / test-supplied) is hashed
    # as-is by the local Scrypt verifier (header with the nonce already spliced).
    return b"", preimage, b"", extranonce


def _safe_job_id(job_id: str) -> bool:
    """Whether a job id is safe to carry as advisory provenance (no raw secret)."""

    from alice_acp.evidence.types import ensure_no_raw_secret

    try:
        ensure_no_raw_secret(job_id, field_name="pool_job_id")
    except ValueError:
        return False
    return True

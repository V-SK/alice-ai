"""Stratum message handling as PURE parse/emit logic (doc §2.1).

The transport-front is the only Alice component that will eventually speak raw
Stratum/TCP to rigs — but THIS module is deliberately TRANSPORT-AGNOSTIC: it
parses ALREADY-DECODED JSON-RPC message dicts into structured value objects and
builds the response/job structures back out as plain dicts. A future
asyncio/stdlib server (or an ``xmrig-proxy`` pool-protocol adapter) owns the
socket, newline-framing, and ``json.loads``/``json.dumps``; it feeds this module
parsed messages and serializes what this module returns. That keeps every
parse/classify/emit rule fully unit-testable with no socket and no event loop.

Three login dialects, ONE structured ``StratumLogin`` (doc §2.1):

* RandomX (XMR) — a single ``{"method": "login", "params": {"login": "<user>",
  "pass": "<pass>", "agent": ...}}`` (the monero/xmrig stratum dialect).
* KawPoW (RVN) + Scrypt (LTC) — the Bitcoin-family two-step:
  ``mining.subscribe`` then ``mining.authorize`` with
  ``params == ["<user>", "<pass>"]``. The username carries the identity and the
  password carries the optional vardiff seed (``d=<diff>``) — see
  :mod:`alice_acp.transport_front.identity` / ``.vardiff``.

A ``submit`` is parsed into a :class:`StratumSubmit` carrying the rig's claimed
solution (job id, nonce, and the algo-specific extra fields). The
transport-front's connection handler (``connection.py``) turns that into a
:class:`~alice_acp.share_validator.types.RawSubmission` for the validator.

CREDIT-ONLY / fail-closed: parsing NEVER trusts a client-asserted lane, payout
address, or difficulty as authority — the PORT determines the lane server-side
(``identity.py``) and Alice's re-hash determines the credited difficulty
(``share_validator``). A malformed message returns a structured
:class:`StratumParseError` (the handler maps it to a JSON-RPC error reply +
drop), never an exception that could crash the loop. ``ensure_no_raw_secret``
guards the username so a secret pasted into the login is refused, not echoed.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any, Literal

# --- JSON-RPC method names (the on-wire stratum verbs) -----------------------

STRATUM_METHOD_LOGIN = "login"  # RandomX/xmrig single-step login
STRATUM_METHOD_SUBSCRIBE = "mining.subscribe"  # KawPoW/Scrypt step 1
STRATUM_METHOD_AUTHORIZE = "mining.authorize"  # KawPoW/Scrypt step 2
STRATUM_METHOD_SUBMIT_RANDOMX = "submit"  # RandomX submit verb
STRATUM_METHOD_SUBMIT_BITCOINFAMILY = "mining.submit"  # KawPoW/Scrypt submit verb

#: Stable machine reason codes for a rejected/parse-failed message (never a raw
#: library error string — those could leak host detail; doc §2.1 fail-closed).
STRATUM_PARSE_NOT_A_MAPPING = "stratum_message_not_a_mapping"
STRATUM_PARSE_MISSING_METHOD = "stratum_message_missing_method"
STRATUM_PARSE_UNKNOWN_METHOD = "stratum_message_unknown_method"
STRATUM_PARSE_BAD_PARAMS = "stratum_message_bad_params"
STRATUM_PARSE_MISSING_USERNAME = "stratum_login_missing_username"
STRATUM_PARSE_BAD_SUBMIT = "stratum_submit_malformed"

#: JSON-RPC error codes mirrored from the stratum convention (rigs understand
#: these). 20 = "Other/Unknown", 24 = "Unauthorized worker", 23 = "Low difficulty
#: share", 21 = "Job not found / stale". We use 20 for parse/identity failures and
#: 24 for an unauthorized (gate-off / unenrolled) login.
STRATUM_ERR_OTHER = 20
STRATUM_ERR_STALE = 21
STRATUM_ERR_LOW_DIFFICULTY = 23
STRATUM_ERR_UNAUTHORIZED = 24


#: Which login dialect a parsed message belongs to (a ``Literal``, matching the
#: codebase's ``Lane`` / ``EvidenceSourceType`` style rather than an enum).
#: ``randomx`` is the monero/xmrig single ``login``; ``bitcoin_family`` is the
#: KawPoW/Scrypt ``mining.subscribe`` + ``mining.authorize`` two-step. The dialect
#: is derived from the METHOD, never from a client-supplied algo field.
StratumDialect = Literal["randomx", "bitcoin_family"]

DIALECT_RANDOMX: StratumDialect = "randomx"
DIALECT_BITCOIN_FAMILY: StratumDialect = "bitcoin_family"


@dataclass(frozen=True, slots=True)
class StratumParseError:
    """A structured, secret-safe parse/validation failure (NOT an exception).

    The connection handler maps this to a JSON-RPC error reply (``error_code`` /
    ``reason``) and a fail-closed drop. Carrying it as a value (not raising) keeps
    a future asyncio read-loop from ever crashing on a malformed frame.
    """

    reason: str
    error_code: int = STRATUM_ERR_OTHER
    message_id: Any = None

    @property
    def ok(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class StratumLogin:
    """A parsed login request, dialect-normalized (doc §2.1).

    ``username`` is the raw ``<passport_id>.<device_id>[.<worker_label>]`` string
    the rig presented (parsed/resolved by :mod:`identity`); ``password`` carries
    the optional ``d=<diff>`` vardiff seed (parsed by ``vardiff``). ``dialect``
    records which stratum flow produced it. ``subscribe_seen`` is True for a
    Bitcoin-family login that already saw ``mining.subscribe`` (the authorize then
    completes it). NONE of these is authority for the lane or the credit
    difficulty — those are server-owned.
    """

    dialect: StratumDialect
    username: str
    password: str = ""
    message_id: Any = None
    agent: str | None = None
    subscribe_seen: bool = False

    @property
    def ok(self) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class StratumSubmit:
    """A parsed share submission from a rig (doc §2.1).

    Carries the rig's CLAIMED solution: the ``job_id`` it is answering, the
    ``nonce`` it found, and the remaining fields as the rig sent them (hex
    strings). The on-wire Bitcoin-family ``mining.submit`` is POSITIONAL and the
    KawPoW vs Scrypt field order is NOT distinguishable from the message alone
    (both are ``[worker, job_id, x, y, z]``) — so the positional payload is kept
    verbatim in :attr:`raw_params` and the LANE-AWARE connection handler (which
    knows the algorithm from the PORT) maps positions to fields in
    ``connection._decode_work``. The parser does NOT guess the algorithm. The
    RandomX object payload is decomposed into :attr:`extra` (``result``) since it
    is self-describing (named keys). The submitted ``username`` (worker) is
    carried for correlation only — the AUTHORITATIVE worker is the per-connection
    server-assigned identity, never this field.
    """

    dialect: StratumDialect
    job_id: str
    nonce: str
    message_id: Any = None
    username: str = ""
    #: RandomX self-describing extras (``result``). Empty for the Bitcoin-family
    #: (whose fields are positional — see :attr:`raw_params`).
    extra: dict[str, str] = field(default_factory=dict)
    #: The verbatim positional ``params`` list for a Bitcoin-family
    #: ``mining.submit`` (``()`` for RandomX). The lane-aware decoder reads the
    #: algorithm-specific positions from this.
    raw_params: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return True


def _as_mapping(message: Any) -> dict[str, Any] | None:
    return message if isinstance(message, dict) else None


def parse_login(message: Any) -> StratumLogin | StratumParseError:
    """Parse a RandomX ``login`` OR a Bitcoin-family ``mining.authorize``.

    Returns a normalized :class:`StratumLogin` or a :class:`StratumParseError`.
    For the Bitcoin-family two-step the caller parses ``mining.subscribe`` with
    :func:`parse_subscribe` first (it carries no identity) and then this with the
    ``mining.authorize`` (which carries ``[username, password]``). A
    single-step RandomX ``login`` carries ``{"login": ..., "pass": ...}`` in an
    object ``params``. Anything else is a structured, secret-safe error.
    """

    mapping = _as_mapping(message)
    if mapping is None:
        return StratumParseError(STRATUM_PARSE_NOT_A_MAPPING)
    method = mapping.get("method")
    message_id = mapping.get("id")
    if not isinstance(method, str) or not method:
        return StratumParseError(STRATUM_PARSE_MISSING_METHOD, message_id=message_id)
    params = mapping.get("params")

    if method == STRATUM_METHOD_LOGIN:
        # RandomX/xmrig: params is an OBJECT {"login": "<user>", "pass": "<pass>"}.
        if not isinstance(params, dict):
            return StratumParseError(STRATUM_PARSE_BAD_PARAMS, message_id=message_id)
        username = params.get("login")
        password = params.get("pass", "")
        agent = params.get("agent")
        if not isinstance(username, str) or not username:
            return StratumParseError(
                STRATUM_PARSE_MISSING_USERNAME,
                error_code=STRATUM_ERR_UNAUTHORIZED,
                message_id=message_id,
            )
        return StratumLogin(
            dialect=DIALECT_RANDOMX,
            username=username,
            password=password if isinstance(password, str) else "",
            message_id=message_id,
            agent=agent if isinstance(agent, str) else None,
        )

    if method == STRATUM_METHOD_AUTHORIZE:
        # Bitcoin-family (KawPoW/Scrypt): params is a LIST ["<user>", "<pass>"].
        if not isinstance(params, list) or not params:
            return StratumParseError(STRATUM_PARSE_BAD_PARAMS, message_id=message_id)
        username = params[0]
        password = params[1] if len(params) > 1 else ""
        if not isinstance(username, str) or not username:
            return StratumParseError(
                STRATUM_PARSE_MISSING_USERNAME,
                error_code=STRATUM_ERR_UNAUTHORIZED,
                message_id=message_id,
            )
        return StratumLogin(
            dialect=DIALECT_BITCOIN_FAMILY,
            username=username,
            password=password if isinstance(password, str) else "",
            message_id=message_id,
            subscribe_seen=True,
        )

    return StratumParseError(STRATUM_PARSE_UNKNOWN_METHOD, message_id=message_id)


def parse_subscribe(message: Any) -> StratumParseError | None:
    """Validate a Bitcoin-family ``mining.subscribe`` (step 1; carries no identity).

    Returns ``None`` when it is a well-formed ``mining.subscribe`` (the handler
    replies with a subscription/extranonce envelope and awaits ``authorize``), or
    a :class:`StratumParseError` otherwise. It deliberately extracts NO identity —
    identity lives entirely in the subsequent ``mining.authorize``.
    """

    mapping = _as_mapping(message)
    if mapping is None:
        return StratumParseError(STRATUM_PARSE_NOT_A_MAPPING)
    method = mapping.get("method")
    if method != STRATUM_METHOD_SUBSCRIBE:
        return StratumParseError(STRATUM_PARSE_UNKNOWN_METHOD, message_id=mapping.get("id"))
    return None


def parse_submit(message: Any) -> StratumSubmit | StratumParseError:
    """Parse a ``submit`` (RandomX) or ``mining.submit`` (KawPoW/Scrypt).

    RandomX ``submit`` carries an OBJECT ``params`` ``{"id": <session>, "job_id":
    ..., "nonce": ..., "result": ...}``. The Bitcoin-family ``mining.submit``
    carries a LIST ``params`` ``[<worker>, <job_id>, <extranonce2/ntime...>,
    <nonce>, ...]`` (KawPoW: ``[worker, job_id, nonce, header_hash, mix_hash]``).
    Returns a normalized :class:`StratumSubmit` or a structured error. The handler
    decodes the hex fields into bytes for the validator; missing required fields
    fail closed.
    """

    mapping = _as_mapping(message)
    if mapping is None:
        return StratumParseError(STRATUM_PARSE_NOT_A_MAPPING)
    method = mapping.get("method")
    message_id = mapping.get("id")
    params = mapping.get("params")

    if method == STRATUM_METHOD_SUBMIT_RANDOMX:
        if not isinstance(params, dict):
            return StratumParseError(STRATUM_PARSE_BAD_PARAMS, message_id=message_id)
        job_id = params.get("job_id")
        nonce = params.get("nonce")
        result = params.get("result")
        if not _nonempty_str(job_id) or not _nonempty_str(nonce):
            return StratumParseError(STRATUM_PARSE_BAD_SUBMIT, message_id=message_id)
        extra: dict[str, str] = {}
        if _nonempty_str(result):
            extra["result"] = result
        return StratumSubmit(
            dialect=DIALECT_RANDOMX,
            job_id=job_id,
            nonce=nonce,
            message_id=message_id,
            username=params.get("id") if _nonempty_str(params.get("id")) else "",
            extra=extra,
        )

    if method == STRATUM_METHOD_SUBMIT_BITCOINFAMILY:
        # POSITIONAL ``[worker, job_id, ...algo-specific...]``. The KawPoW and
        # Scrypt field orders are NOT distinguishable from the message alone
        # (KawPoW: [worker, job_id, nonce, header_hash, mix_hash]; Scrypt:
        # [worker, job_id, extranonce2, ntime, nonce]) — so we DO NOT guess here.
        # We validate the shared prefix (worker, job_id), carry the verbatim
        # positional payload, and let the LANE-AWARE connection decoder map the
        # remaining positions by algorithm. ``nonce`` is set to the LAST element
        # as a sane default for display/dedup; the decoder reads the precise
        # nonce position for its algorithm.
        if not isinstance(params, list) or len(params) < 3:
            return StratumParseError(STRATUM_PARSE_BAD_PARAMS, message_id=message_id)
        worker = params[0] if _nonempty_str(params[0]) else ""
        job_id = params[1]
        if not _nonempty_str(job_id):
            return StratumParseError(STRATUM_PARSE_BAD_SUBMIT, message_id=message_id)
        positional = tuple(p for p in params if isinstance(p, str))
        if len(positional) != len(params):
            # A non-string positional field is malformed (hex strings only).
            return StratumParseError(STRATUM_PARSE_BAD_SUBMIT, message_id=message_id)
        last = params[-1]
        if not _nonempty_str(last):
            return StratumParseError(STRATUM_PARSE_BAD_SUBMIT, message_id=message_id)
        return StratumSubmit(
            dialect=DIALECT_BITCOIN_FAMILY,
            job_id=job_id,
            nonce=last,
            message_id=message_id,
            username=worker,
            raw_params=positional,
        )

    if not isinstance(method, str) or not method:
        return StratumParseError(STRATUM_PARSE_MISSING_METHOD, message_id=message_id)
    return StratumParseError(STRATUM_PARSE_UNKNOWN_METHOD, message_id=message_id)


def _nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and bool(value)


# --- response / job EMITTERS (plain dicts a serializer turns into JSON) -------


def build_error_reply(error: StratumParseError) -> dict[str, Any]:
    """A JSON-RPC error reply for a parse/identity failure (doc §2.1 fail-closed).

    Shaped as the stratum convention ``{"id", "result": null, "error": [code,
    message, null]}`` so a stock rig understands the rejection. The ``message`` is
    the stable reason code (never a raw error string).
    """

    return {
        "id": error.message_id,
        "result": None,
        "error": [error.error_code, error.reason, None],
    }


def build_login_ok_reply(
    *, message_id: Any, session_id: str, job: dict[str, Any] | None = None
) -> dict[str, Any]:
    """A successful RandomX login reply (carries the first job INLINE, xmrig style).

    ``{"id", "jsonrpc": "2.0", "error": null, "result": {"id": "<session>", "job":
    {...}, "status": "OK"}}``. The ``session_id`` is the per-connection server-issued
    shadow session id (the rig echoes it as ``id`` on submits). The ``job`` MUST be the
    Monero JOB OBJECT (:func:`build_monero_job_object`) — xmrig reads the FIRST job from
    ``result.job`` on the login reply and starts hashing immediately; a login-OK WITHOUT
    an inline job leaves a stock xmrig with no work (it does not understand a separate
    ``mining.notify``/``mining.set_difficulty``), which is exactly the live bug this
    corrects. ``jsonrpc: "2.0"`` is included because the cryptonote/xmrig stratum dialect
    is JSON-RPC 2.0 (the rig tolerates its absence but expects it).
    """

    result: dict[str, Any] = {"id": session_id, "status": "OK"}
    if job is not None:
        result["job"] = job
    return {"id": message_id, "jsonrpc": "2.0", "error": None, "result": result}


def build_authorize_ok_reply(*, message_id: Any) -> dict[str, Any]:
    """A successful Bitcoin-family ``mining.authorize`` reply (``result: true``)."""

    return {"id": message_id, "result": True, "error": None}


def build_subscribe_reply(
    *, message_id: Any, subscription_id: str, extranonce1: str, extranonce2_size: int
) -> dict[str, Any]:
    """A ``mining.subscribe`` reply: subscription details + extranonce assignment.

    The Bitcoin-family convention ``{"id", "result": [[["mining.notify",
    "<sub>"]], "<extranonce1>", <extranonce2_size>], "error": null}``. The
    transport (not this module) owns the extranonce values; this only shapes them.
    """

    return {
        "id": message_id,
        "result": [
            [["mining.set_difficulty", subscription_id], ["mining.notify", subscription_id]],
            extranonce1,
            extranonce2_size,
        ],
        "error": None,
    }


def build_set_difficulty_notification(difficulty: Decimal | int | float | str) -> dict[str, Any]:
    """A ``mining.set_difficulty`` notification (vardiff push; doc §2.1 / §2 vardiff).

    ``{"id": null, "method": "mining.set_difficulty", "params": [<number>]}``.
    ``params[0]`` MUST be a JSON NUMBER: a stock rig (cgminer/cpuminer/ASIC firmware)
    reads it via ``json_number_value`` and SILENTLY IGNORES a string (treating it as
    diff 0 → keeps mining at its own default target → mass low-difficulty rejects). We
    accept a :class:`~decimal.Decimal`/number/numeric-string and emit an ``int`` when
    the value is integral (clean for ASIC-scale diffs) else a ``float`` (fractional
    vardiff, e.g. ``0.0001`` for a small rig). The value is the per-connection vardiff
    (the front computes it; see :mod:`alice_acp.transport_front.vardiff`).
    """

    value_decimal = Decimal(str(difficulty))
    number: int | float = (
        int(value_decimal)
        if value_decimal == value_decimal.to_integral_value()
        else float(value_decimal)
    )
    return {
        "id": None,
        "method": "mining.set_difficulty",
        "params": [number],
    }


def build_job_notification(*, job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """A GENERIC ``mining.notify`` push (object payload) — RETAINED for non-stratum lanes.

    ``{"id": null, "method": "mining.notify", "params": [<job_id>, {...payload...}]}``.
    The payload rides as a SINGLE object param. NOTE: this is NOT used by any live lane.
    The RandomX/XMR lane MUST use :func:`build_xmr_job_notification` (the ``job``-method
    OBJECT push xmrig understands — ``mining.notify`` is a Bitcoin-family verb a Monero
    rig ignores), and the Scrypt/KawPoW lanes MUST use the POSITIONAL
    :func:`build_scrypt_job_notification`; :meth:`InternalJob.to_notification` routes by
    lane. Kept only as a generic object-payload emitter for a hypothetical future lane.
    """

    return {
        "id": None,
        "method": "mining.notify",
        "params": [job_id, payload],
    }


def build_monero_job_object(
    *, job_id: str, payload: dict[str, Any], target: str
) -> dict[str, Any]:
    """The Monero/xmrig JOB OBJECT (named keys) a RandomX rig mines (doc §2.1 / §2.3 R2).

    ``{"blob": "<hex>", "job_id": "<id>", "target": "<compact-LE hex>", "seed_hash":
    "<hex>", "height": <int>, "algo": "rx/0"}``. This is the self-describing object xmrig
    reads BOTH inline in the login-OK ``result.job`` AND in a ``job`` push
    (:func:`build_xmr_job_notification`). It is re-keyed under Alice's internal
    ``job_id``; the ``target`` is THIS CONNECTION's per-connection difficulty encoded as
    the xmrig 4-byte compact target (NOT the upstream's) — a Monero rig has no
    ``set_difficulty``, so the ``target`` IS the share difficulty. ``algo`` is normalized
    to ``rx/0`` (the xmrig RandomX algo id) regardless of the internal ``XMR_RANDOMX``
    payload tag. There is deliberately NO ``clean_jobs`` key (a cryptonote job is not a
    Bitcoin-family notify; a new ``job`` simply supersedes the prior one). ``blob`` /
    ``seed_hash`` pass through verbatim (the bytes the rig hashes + the per-epoch VM key
    Alice re-hashes against). Missing ``blob`` / ``seed_hash`` raise ``KeyError``
    (fail-closed; never emit a half-formed job).
    """

    return {
        "blob": payload["blob"],
        "job_id": job_id,
        "target": target,
        "seed_hash": payload["seed_hash"],
        "height": payload.get("height", 0),
        "algo": "rx/0",
    }


def build_xmr_job_notification(*, job: dict[str, Any]) -> dict[str, Any]:
    """A RandomX/XMR ``job`` push: the JOB OBJECT as OBJECT params (NOT ``mining.notify``).

    ``{"jsonrpc": "2.0", "method": "job", "params": <JOB OBJECT>}`` — the EXACT shape a
    stock xmrig parses for a new job (the cryptonote/Monero stratum dialect). The
    ``params`` is the :func:`build_monero_job_object` OBJECT (named keys), NOT a
    positional array, and the method is ``job`` (NOT ``mining.notify``). This is the
    Monero equivalent of the Bitcoin-family ``mining.notify``; a vardiff retarget on this
    lane pushes a FRESH ``job`` with a new ``target`` (there is NO
    ``mining.set_difficulty`` on the XMR lane — a Monero rig would ignore it). Emitting
    ``mining.notify``/``mining.set_difficulty`` here (the prior bug) left a real xmrig
    with no usable job.
    """

    return {
        "jsonrpc": "2.0",
        "method": "job",
        "params": job,
    }


def build_scrypt_job_notification(
    *, job_id: str, payload: dict[str, Any], clean_jobs: bool
) -> dict[str, Any]:
    """A Bitcoin-family (Scrypt/LTC) ``mining.notify`` as the standard POSITIONAL array.

    ``params == [job_id, prevhash, coinb1, coinb2, merkle_branch[], version, nbits,
    ntime, clean_jobs]`` — the exact 9-element order a stock Litecoin rig
    (cgminer/cpuminer/ASIC firmware) parses by index. The extranonce1/extranonce2_size
    are deliberately ABSENT (a stock rig takes those ONLY from the ``mining.subscribe``
    reply — :func:`build_subscribe_reply`). Hex fields pass through verbatim; the rig
    applies the cgminer byte-order rules and Alice's re-hash applies the SAME rules in
    ``scrypt_translator.assemble_scrypt_header`` (the byte-for-byte contract). Raises
    ``KeyError`` if a required field is missing (the caller's payload is malformed) —
    fail-closed, never emit a half-formed notify.
    """

    return {
        "id": None,
        "method": "mining.notify",
        "params": [
            job_id,
            payload["prevhash"],
            payload["coinb1"],
            payload["coinb2"],
            list(payload.get("merkle_branch", [])),
            payload["version"],
            payload["nbits"],
            payload["ntime"],
            bool(clean_jobs),
        ],
    }


def build_kawpow_job_notification(
    *, job_id: str, payload: dict[str, Any], clean_jobs: bool
) -> dict[str, Any]:
    """A KawPoW/RVN ``mining.notify`` as the Ravencoin/T-Rex POSITIONAL array (doc §2.1).

    ``params == [job_id, headerHash, seedHash, target, clean_jobs, height, bits]`` — the
    exact 7-element order a stock KawPoW rig (T-Rex / kawpowminer / NBMiner; the
    Ethereum/Ravencoin stratum dialect) parses by index. Unlike Scrypt this is NOT a
    coinbase-template notify: KawPoW hands the rig a 32-byte ``headerHash`` directly (the
    rig varies the 64-bit nonce + computes the ``mixHash``), the per-epoch ``seedHash``
    (the DAG key), and the 32-byte ``target`` IN the notify (KawPoW has NO
    ``mining.set_difficulty`` — the per-connection target is pushed via
    :func:`build_set_target_notification`, but the notify also carries the current target
    so the first job is always self-contained). ``height`` (the block number KawPoW mixes
    into the hash) and ``bits`` (the network nbits) ride as the last two elements.

    Hex fields pass through verbatim; the rig re-derives the PoW from headerHash + nonce
    against the seed-derived DAG for ``height``'s epoch, and Alice's re-hash applies the
    SAME inputs in :class:`~alice_acp.share_validator.verifiers.kawpow.LocalKawPowVerifier`
    (the byte-for-byte contract). Raises ``KeyError`` if a required field is missing (the
    caller's payload is malformed) — fail-closed, never emit a half-formed notify.

    The ``target`` is THIS CONNECTION's per-connection target (the server passes the
    vardiff difficulty encoded as a 32-byte target); when the payload alone is used the
    upstream job's ``target`` is carried (see :meth:`InternalJob.to_notification`).
    """

    return {
        "id": None,
        "method": "mining.notify",
        "params": [
            job_id,
            payload["headerHash"],
            payload["seedHash"],
            payload["target"],
            bool(clean_jobs),
            payload["height"],
            payload["bits"],
        ],
    }


def build_set_target_notification(target_hex: str) -> dict[str, Any]:
    """A KawPoW/RVN ``mining.set_target`` notification (the per-connection vardiff push).

    ``{"id": null, "method": "mining.set_target", "params": [<target_hex>]}`` where
    ``target_hex`` is the FULL 32-byte (64-hex) target the rig compares its little-endian
    KawPoW digest against. KawPoW has NO ``mining.set_difficulty`` (a stock KawPoW rig —
    T-Rex / kawpowminer — reads ``mining.set_target`` and ignores ``set_difficulty``); the
    per-connection difficulty is expressed ENTIRELY as this 32-byte target (a HARDER
    vardiff => a SMALLER target). A vardiff retarget on the RVN lane pushes THIS (not a
    fresh job, unlike the XMR ``job`` retarget, and not ``set_difficulty``, unlike the
    Scrypt lane). The value is a public per-connection target — no secret. The transport
    computes it from the vardiff via
    :func:`~alice_acp.transport_service.kawpow_translator.difficulty_to_target_256`.
    """

    return {
        "id": None,
        "method": "mining.set_target",
        "params": [target_hex],
    }


def build_submit_accepted_reply(*, message_id: Any, dialect: StratumDialect) -> dict[str, Any]:
    """A successful submit ACK. RandomX → ``{"status":"OK"}``; BTC-family → ``true``.

    NOTE (doc §2.1 / §2.2): an ACK is NOT credit. The credited unit is the
    Alice-re-hash-confirmed share the validator emits into the
    ``ValidatedShareStore``; this reply is the advisory protocol-level
    acknowledgement only.
    """

    if dialect == DIALECT_RANDOMX:
        return {"id": message_id, "result": {"status": "OK"}, "error": None}
    return {"id": message_id, "result": True, "error": None}


def build_submit_rejected_reply(
    *, message_id: Any, reason: str, error_code: int = STRATUM_ERR_LOW_DIFFICULTY
) -> dict[str, Any]:
    """A submit NACK (low-diff / invalid / duplicate / verifier-unavailable).

    The validator's verdict drives this; the ``reason`` is the validator's stable
    reason code. Fail-closed: anything that is not a confirmed credited share is
    NACKed and credits nothing.
    """

    return {
        "id": message_id,
        "result": None,
        "error": [error_code, reason, None],
    }

"""The REAL per-algo R2 job translator for the Scrypt (Litecoin) lane (doc §2.3 R2).

This is the LTC leg of the deploy-flagged "real per-algo R2 translator": it turns a
F2Pool ``mining.notify`` (+ the ``mining.subscribe`` result that fixes
``extranonce1`` / ``extranonce2_size``) into an :class:`InternalJob` that carries
EVERYTHING needed for the two halves of the proxy pool to agree on one 80-byte
block header:

  (a) the DOWNWARD half — a real ``mining.notify`` payload a stock Litecoin ASIC
      can mine (the upstream notify fields, re-keyed under Alice's internal job id);
  (b) the VALIDATION half — the SAME deterministic recipe the miner uses to fold
      the coinbase through the merkle branches and serialize the 80-byte header, so
      Alice's :class:`~alice_acp.share_validator.verifiers.scrypt.LocalScryptVerifier`
      re-hashes EXACTLY the bytes the ASIC mined.

THE CORRECTNESS CONTRACT (the whole point)
------------------------------------------
The assembled header the ASIC mines MUST equal the header Alice re-hashes, byte for
byte, or shares are mis-validated. The local Scrypt verifier hashes an 80-byte
header WITH the nonce already in its last 4 bytes (``scrypt(header, salt=header)``,
``N=1024, r=1, p=1``; the block hash is read little-endian and ``d = MAX_TARGET / H``).
So this module owns ONE canonical header-assembly function,
:func:`assemble_scrypt_header`, used by BOTH the (test) miner and any re-derivation
path; the verifier then re-hashes the literal bytes the front passed through as the
``RawSubmission`` header pre-image (``connection.py::_decode_work`` carries
``params[-1]`` — the assembled 80-byte header — straight to the verifier for the
Scrypt lane).

LITECOIN / STRATUM-V1 BYTE ORDER (documented at every step — see the functions)
-------------------------------------------------------------------------------
Stratum sends every numeric/hash field as a hex string in a pool-specific order;
the header is serialized little-endian except the merkle root. Concretely:

* ``version``  — 4-byte block version. Notify sends it big-endian hex; the header
  takes it LITTLE-endian (reverse the 4 bytes).
* ``prevhash`` — 32 bytes. Notify sends it as 8 big-endian 32-bit WORDS; the header
  takes each word byte-reversed, word order preserved (the cgminer/sgminer
  ``flip32`` convention the LTC ASIC firmware uses). See :func:`_prevhash_to_header`.
* ``merkle root`` — 32 bytes, computed by double-SHA256-folding the coinbase hash
  through ``merkle_branch`` (see :func:`merkle_root_from_coinbase`). It is placed in
  the header in its NATURAL (internal) byte order — NOT reversed.
* ``ntime`` / ``nbits`` — 4-byte fields. Notify sends them big-endian hex; the header
  takes them LITTLE-endian (reverse the 4 bytes).
* ``nonce`` — 4 bytes, header's last 4 bytes, LITTLE-endian (the stratum convention
  the verifier documents: ``header[:76] + nonce``).

The coinbase tx id is ``dSHA256(coinb1 + extranonce1 + extranonce2 + coinb2)`` (raw
bytes, no reversal); each merkle fold is ``dSHA256(current || branch)`` with both
operands in their raw (internal) byte order — the standard Bitcoin/Litecoin merkle
construction. (Litecoin's PoW is scrypt, but its block/merkle structure is
Bitcoin's, so the merkle math is double-SHA256.)

NET DIFFICULTY FROM nbits
-------------------------
``nbits`` is the compact target. :func:`net_difficulty_from_nbits` decompresses it
to the 256-bit network target and returns ``d = SCRYPT_DIFF1_TARGET / target`` — the
REAL chain (solution) difficulty stamped on :attr:`InternalJob.net_difficulty` so the
server's ``is_solution`` classification means "cleared the real chain target" (the
R3-forwardable share). Litecoin's difficulty-1 target is the same Bitcoin-family
``0x00000000ffff...`` baseline; the per-coin constant is folded here so the verifier
stays coin-agnostic.

CREDIT-ONLY: the translated job carries NO reward/payout/chain symbol; the credited
unit is unchanged (the validator's ValidatedShareStore write). ``ensure_no_raw_secret``
guards every advisory-provenance string (the upstream job id, the internal job id).
No secret is ever read here (the translator only sees public stratum job fields).
"""

from __future__ import annotations

import hashlib
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from alice_acp.shadow_server.types import Lane, utc_now
from alice_acp.transport_service.jobs import InternalJob, JobTranslationMap

#: Litecoin/Bitcoin-family difficulty-1 target: ``0x00000000FFFF0000...0000`` (the
#: 0xffff mantissa at the 0x1d00ffff "bits" exponent). A network/pool target ``T``
#: clears difficulty ``d = DIFF1_TARGET / T``. This is the SAME difficulty-1 baseline
#: stratum pools quote ``mining.set_difficulty`` against; folding it here keeps the
#: verifier's raw ``MAX_TARGET / H`` coin-agnostic while the translator reports the
#: lane's net difficulty on the conventional scale.
SCRYPT_DIFF1_TARGET = 0x00000000FFFF0000000000000000000000000000000000000000000000000000

#: The block header is exactly 80 bytes; the nonce is its last 4 bytes (matches
#: ``LocalScryptVerifier``'s ``_LTC_HEADER_LEN`` / ``_NONCE_LEN``).
LTC_HEADER_LEN = 80
NONCE_LEN = 4

#: Stable, secret-free reason codes for a dropped/rejected translation (advisory
#: telemetry only — a drop NEVER affects credit, which has not happened yet at R2).
SCRYPT_TRANSLATE_BAD_NOTIFY = "scrypt_translate_malformed_notify"
SCRYPT_TRANSLATE_BAD_SUBSCRIBE = "scrypt_translate_missing_subscribe"


# --- low-level byte helpers (every endianness rule lives in ONE place) -------


def _dsha256(data: bytes) -> bytes:
    """Bitcoin/Litecoin double-SHA256 (the merkle + coinbase-id hash, NOT the PoW).

    Litecoin's PoW is scrypt, but its block/merkle STRUCTURE is Bitcoin's, so the
    coinbase tx id and every merkle fold use ``SHA256(SHA256(x))`` over RAW bytes.
    """

    return hashlib.sha256(hashlib.sha256(data).digest()).digest()


def _le32(value_be_hex: str) -> bytes:
    """A 4-byte field from big-endian notify hex -> LITTLE-endian header bytes.

    ``version`` / ``ntime`` / ``nbits`` arrive as big-endian hex in ``mining.notify``
    and are serialized little-endian in the header (reverse the 4 bytes). Fails
    closed on a non-4-byte / non-hex field.
    """

    raw = bytes.fromhex(_strip0x(value_be_hex))
    if len(raw) != 4:
        raise ValueError("scrypt 4-byte field must be exactly 4 bytes")
    return raw[::-1]


def _prevhash_to_header(prevhash_hex: str) -> bytes:
    """``prevhash`` notify hex -> the 32 header bytes (the cgminer ``flip32`` rule).

    Stratum sends ``prevhash`` as 8 big-endian 32-bit WORDS; the header wants each
    word byte-reversed with the WORD ORDER PRESERVED (this is the de-facto
    cgminer/sgminer convention the Litecoin ASIC firmware speaks — NOT a plain full
    32-byte reverse). Concretely: for each 4-byte word ``w[i]`` in the notify
    prevhash, emit ``w[i][::-1]``; concatenate i=0..7 in order.

    Example: notify word ``aabbccdd`` -> header bytes ``ddccbbaa``; the 8 words stay
    in their original positions. Fails closed on a non-32-byte / non-hex field.
    """

    raw = bytes.fromhex(_strip0x(prevhash_hex))
    if len(raw) != 32:
        raise ValueError("scrypt prevhash must be exactly 32 bytes")
    out = bytearray(32)
    for i in range(0, 32, 4):
        word = raw[i : i + 4]
        out[i : i + 4] = word[::-1]
    return bytes(out)


def _strip0x(text: str) -> str:
    return text[2:] if text[:2].lower() == "0x" else text


# --- coinbase + merkle root ---------------------------------------------------


def build_coinbase(
    *, coinb1_hex: str, extranonce1_hex: str, extranonce2_hex: str, coinb2_hex: str
) -> bytes:
    """The full coinbase tx = ``coinb1 + extranonce1 + extranonce2 + coinb2`` (RAW).

    Every part is concatenated in its natural (as-sent) byte order — there is NO
    endianness conversion on the coinbase parts; the extranonce1 (pool-assigned, from
    ``mining.subscribe``) and the miner-chosen extranonce2 are spliced literally
    between the two coinbase halves. Fails closed on non-hex.
    """

    return (
        bytes.fromhex(_strip0x(coinb1_hex))
        + bytes.fromhex(_strip0x(extranonce1_hex))
        + bytes.fromhex(_strip0x(extranonce2_hex))
        + bytes.fromhex(_strip0x(coinb2_hex))
    )


def merkle_root_from_coinbase(coinbase: bytes, merkle_branch: list[str]) -> bytes:
    """Fold the coinbase hash through ``merkle_branch`` -> the 32-byte merkle root.

    The coinbase tx id is ``dSHA256(coinbase)`` (raw bytes). Then for each branch
    ``b`` (a 32-byte hash hex, in its natural internal order), the running hash
    becomes ``dSHA256(running || b)`` — current FIRST, branch SECOND, both raw — the
    standard stratum merkle fold. With an EMPTY branch list (a solo/zero-branch job)
    the merkle root is simply the coinbase tx id. The result is placed in the header
    in this natural order (NOT reversed). Fails closed on a non-32-byte branch.
    """

    running = _dsha256(coinbase)
    for branch_hex in merkle_branch:
        branch = bytes.fromhex(_strip0x(branch_hex))
        if len(branch) != 32:
            raise ValueError("scrypt merkle branch must be 32 bytes")
        running = _dsha256(running + branch)
    return running


# --- the 80-byte header (the load-bearing assembly) --------------------------


def assemble_scrypt_header(
    *,
    version_hex: str,
    prevhash_hex: str,
    merkle_root: bytes,
    ntime_hex: str,
    nbits_hex: str,
    nonce: bytes,
) -> bytes:
    """Serialize the 80-byte Litecoin block header (the bytes scrypt hashes).

    Layout (offsets): ``version[0:4] + prevhash[4:36] + merkle_root[36:68] +
    ntime[68:72] + nbits[72:76] + nonce[76:80]``. Endianness:

    * ``version``      little-endian (notify big-endian hex reversed),
    * ``prevhash``     per-4-byte-word reversed (cgminer ``flip32``),
    * ``merkle_root``  NATURAL internal order (NOT reversed),
    * ``ntime``        little-endian,
    * ``nbits``        little-endian,
    * ``nonce``        little-endian, the header's last 4 bytes.

    The nonce is the rig's chosen 4 bytes; the verifier re-hashes ``header[:76] +
    nonce`` so this assembly is byte-identical to what Alice re-hashes. Returns
    exactly 80 bytes; raises on any malformed field (fail-closed).
    """

    if len(merkle_root) != 32:
        raise ValueError("merkle_root must be 32 bytes")
    if len(nonce) != NONCE_LEN:
        raise ValueError("nonce must be 4 bytes")
    header = (
        _le32(version_hex)
        + _prevhash_to_header(prevhash_hex)
        + merkle_root
        + _le32(ntime_hex)
        + _le32(nbits_hex)
        + nonce
    )
    if len(header) != LTC_HEADER_LEN:
        # Defensive: the field-level checks above guarantee 80, but never emit a
        # header of the wrong length to the verifier.
        raise ValueError("assembled header must be exactly 80 bytes")
    return header


# --- nbits -> network difficulty ---------------------------------------------


def target_from_nbits(nbits_hex: str) -> int:
    """Decompress the compact ``nbits`` to the full 256-bit target integer.

    ``nbits`` is the standard Bitcoin/Litecoin compact form: the high byte is the
    exponent (byte length of the target), the low 3 bytes are the mantissa, both
    big-endian as sent in ``mining.notify``. ``target = mantissa * 256**(exp-3)``.
    Fails closed on a non-4-byte / non-hex field.
    """

    raw = bytes.fromhex(_strip0x(nbits_hex))
    if len(raw) != 4:
        raise ValueError("nbits must be exactly 4 bytes")
    exponent = raw[0]
    mantissa = int.from_bytes(raw[1:], "big")
    if exponent <= 3:
        target = mantissa >> (8 * (3 - exponent))
    else:
        target = mantissa << (8 * (exponent - 3))
    return target


def net_difficulty_from_nbits(nbits_hex: str) -> Decimal:
    """The REAL network (solution) difficulty from ``nbits``: ``DIFF1 / target``.

    Uses the Litecoin/Bitcoin-family difficulty-1 baseline
    (:data:`SCRYPT_DIFF1_TARGET`). A zero/degenerate target clears the maximum
    representable difficulty (never divide by zero). Returned as a ``Decimal`` so it
    composes with the validator's ``Decimal`` target arithmetic.
    """

    target = target_from_nbits(nbits_hex)
    if target <= 0:
        return Decimal(SCRYPT_DIFF1_TARGET)
    return Decimal(SCRYPT_DIFF1_TARGET) / Decimal(target)


# --- the F2Pool subscribe result (extranonce1 / extranonce2_size) ------------


@dataclass(frozen=True, slots=True)
class ScryptSubscription:
    """The ``mining.subscribe`` result the translator needs for the coinbase (R2).

    ``extranonce1`` is the pool-assigned per-connection hex string spliced into the
    coinbase; ``extranonce2_size`` is the byte count the miner fills for its own
    extranonce2. Both come from the upstream subscribe reply
    ``[[...subscriptions...], extranonce1, extranonce2_size]`` (see
    :func:`parse_subscribe_result`). ``ensure_no_raw_secret`` is NOT applied — these
    are public protocol values, never secrets — but they ARE validated as hex / a
    positive size (fail-closed on garbage).
    """

    extranonce1: str
    extranonce2_size: int

    def __post_init__(self) -> None:
        # Validate extranonce1 is hex (it is spliced raw into the coinbase).
        bytes.fromhex(_strip0x(self.extranonce1))
        if self.extranonce2_size <= 0:
            raise ValueError("extranonce2_size must be positive")


def parse_subscribe_result(result: Any) -> ScryptSubscription:
    """Parse a Bitcoin-family ``mining.subscribe`` RESULT into a subscription.

    The stratum subscribe reply ``result`` is
    ``[[["mining.set_difficulty", id], ["mining.notify", id]], <extranonce1>,
    <extranonce2_size>]``. We read positions [1] (extranonce1) and [2]
    (extranonce2_size); the subscription tuples are advisory and not required here.
    Raises ``ValueError`` (fail-closed) on a malformed result.
    """

    if not isinstance(result, list) or len(result) < 3:
        raise ValueError(SCRYPT_TRANSLATE_BAD_SUBSCRIBE)
    extranonce1 = result[1]
    extranonce2_size = result[2]
    if not isinstance(extranonce1, str) or not isinstance(extranonce2_size, int):
        raise ValueError(SCRYPT_TRANSLATE_BAD_SUBSCRIBE)
    return ScryptSubscription(extranonce1=extranonce1, extranonce2_size=extranonce2_size)


# --- the mining.notify -> InternalJob translator (R2) ------------------------

#: The F2Pool ``mining.notify`` positional params (Litecoin stratum v1):
#: ``[job_id, prevhash, coinb1, coinb2, merkle_branch[], version, nbits, ntime,
#: clean_jobs]`` — the standard Bitcoin-family notify order.
_NOTIFY_JOB_ID = 0
_NOTIFY_PREVHASH = 1
_NOTIFY_COINB1 = 2
_NOTIFY_COINB2 = 3
_NOTIFY_MERKLE_BRANCH = 4
_NOTIFY_VERSION = 5
_NOTIFY_NBITS = 6
_NOTIFY_NTIME = 7
_NOTIFY_CLEAN_JOBS = 8


@dataclass(frozen=True, slots=True)
class ScryptNotify:
    """A parsed F2Pool ``mining.notify`` (all fields validated, fail-closed).

    Carries the upstream job fields the miner needs + the translator folds. Hex
    fields are kept as the strings the upstream sent (the header assembly applies the
    byte-order rules); ``merkle_branch`` is the list of 32-byte branch hashes (hex).
    """

    job_id: str
    prevhash: str
    coinb1: str
    coinb2: str
    merkle_branch: list[str]
    version: str
    nbits: str
    ntime: str
    clean_jobs: bool


def parse_notify(message: dict) -> ScryptNotify:
    """Parse a F2Pool ``mining.notify`` message into a validated :class:`ScryptNotify`.

    Validates the method, arity (>= 9 positional params), the hex shape of the fixed
    fields (prevhash 32B, version/nbits/ntime 4B), and that each merkle branch is
    32-byte hex. Raises ``ValueError`` (fail-closed) on anything malformed — the relay
    catches it and drops the job without crashing.
    """

    if not isinstance(message, dict) or message.get("method") != "mining.notify":
        raise ValueError(SCRYPT_TRANSLATE_BAD_NOTIFY)
    params = message.get("params")
    if not isinstance(params, list) or len(params) < 9:
        raise ValueError(SCRYPT_TRANSLATE_BAD_NOTIFY)
    job_id = params[_NOTIFY_JOB_ID]
    prevhash = params[_NOTIFY_PREVHASH]
    coinb1 = params[_NOTIFY_COINB1]
    coinb2 = params[_NOTIFY_COINB2]
    merkle_branch = params[_NOTIFY_MERKLE_BRANCH]
    version = params[_NOTIFY_VERSION]
    nbits = params[_NOTIFY_NBITS]
    ntime = params[_NOTIFY_NTIME]
    clean_jobs = params[_NOTIFY_CLEAN_JOBS]

    if not all(
        isinstance(v, str) and v for v in (job_id, prevhash, coinb1, coinb2, version, nbits, ntime)
    ):
        raise ValueError(SCRYPT_TRANSLATE_BAD_NOTIFY)
    if not isinstance(merkle_branch, list) or not all(isinstance(b, str) for b in merkle_branch):
        raise ValueError(SCRYPT_TRANSLATE_BAD_NOTIFY)
    # Validate the byte-order-sensitive fixed fields up front (fail-closed): these
    # raise if the field is not the right byte length / not hex.
    _prevhash_to_header(prevhash)
    _le32(version)
    _le32(nbits)
    _le32(ntime)
    for branch in merkle_branch:
        if len(bytes.fromhex(_strip0x(branch))) != 32:
            raise ValueError(SCRYPT_TRANSLATE_BAD_NOTIFY)
    return ScryptNotify(
        job_id=job_id,
        prevhash=prevhash,
        coinb1=coinb1,
        coinb2=coinb2,
        merkle_branch=list(merkle_branch),
        version=version,
        nbits=nbits,
        ntime=ntime,
        clean_jobs=bool(clean_jobs),
    )


@dataclass(slots=True)
class ScryptJobTranslator:
    """The REAL R2 Scrypt translator: ``(lane, upstream_notify) -> InternalJob``.

    Construct ONE per relay, sharing the relay's :class:`JobTranslationMap` (so the
    minted internal job ids and the reverse map stay coherent) and a
    ``subscription_provider`` the upstream connector updates when it (re)subscribes —
    the translator reads ``extranonce1`` / ``extranonce2_size`` from the LATEST
    subscription at translate time (a fresh upstream connection re-subscribes and the
    extranonce1 changes; the translator must always use the current one). It is a
    plain callable (``__call__``) so it drops straight into
    :attr:`DispatcherRelay.job_translator`.

    The translated :class:`InternalJob`:
    * ``internal_job_id`` minted from the shared map (opaque; the upstream id never
      leaks downward),
    * ``upstream_job_id`` = the notify ``job_id`` (advisory provenance; R3 maps it
      back for the upstream submit),
    * ``net_difficulty`` = :func:`net_difficulty_from_nbits` (the REAL chain target),
    * ``pool_difficulty`` = the upstream pool/share difficulty floor (from the latest
      ``mining.set_difficulty``, defaulted to 1 until one arrives; the server's
      vardiff owns the per-connection pool target it actually sends),
    * ``extranonce`` = the current ``extranonce1`` (advisory provenance for the map),
    * ``payload`` = a ``mining.notify`` body a Litecoin ASIC can mine (the upstream
      job fields + the assigned extranonce1/size + the algo tag), re-keyed under the
      internal job id by :meth:`InternalJob.to_notification`.

    NET >= POOL is enforced: the upstream pool difficulty can momentarily exceed the
    net difficulty for a freshly-started lane (e.g. before the first
    ``set_difficulty``); the translator clamps ``pool_difficulty`` to at most
    ``net_difficulty`` so the :class:`InternalJob` invariant (net >= pool) holds.

    CREDIT-ONLY: no reward/payout/chain symbol; reads no secret. ``payload`` carries
    only public stratum fields.
    """

    job_map: JobTranslationMap
    subscription_provider: Callable[[], ScryptSubscription | None]
    pool_difficulty_provider: Callable[[], Decimal | None] = lambda: None
    clock: Callable[[], datetime] = utc_now

    def __call__(self, lane: Lane, upstream_job: dict) -> InternalJob | None:
        return self.translate(lane, upstream_job)

    def translate(self, lane: Lane, upstream_job: dict) -> InternalJob | None:
        """Translate one upstream ``mining.notify`` into an :class:`InternalJob`.

        Returns ``None`` (drop, fail-soft) when there is no subscription yet (the
        coinbase cannot be built without ``extranonce1``) — the relay simply has no
        current job until the upstream subscribe completes. Raises nothing the relay
        cannot swallow; a malformed notify surfaces as ``ValueError`` which the
        relay's ``_ingest_upstream_job`` catches and drops.
        """

        subscription = self.subscription_provider()
        if subscription is None:
            # No extranonce1 yet => cannot define the coinbase => no minable job.
            return None
        notify = parse_notify(upstream_job)

        net_difficulty = net_difficulty_from_nbits(notify.nbits)
        pool_floor = self.pool_difficulty_provider() or Decimal("1")
        if pool_floor <= 0:
            pool_floor = Decimal("1")
        # A solution is necessarily a share: clamp pool <= net so the InternalJob
        # invariant holds even if the upstream pool diff transiently exceeds net.
        pool_difficulty = pool_floor if pool_floor <= net_difficulty else net_difficulty

        internal_job_id = self.job_map.mint_internal_id(lane)
        payload = self._build_payload(notify, subscription)
        return InternalJob(
            lane=lane,
            internal_job_id=internal_job_id,
            upstream_job_id=notify.job_id,
            net_difficulty=net_difficulty,
            pool_difficulty=pool_difficulty,
            payload=payload,
            extranonce=subscription.extranonce1,
            clean_jobs=notify.clean_jobs,
            created_at=self.clock(),
        )

    @staticmethod
    def _build_payload(notify: ScryptNotify, subscription: ScryptSubscription) -> dict[str, Any]:
        """The ``mining.notify`` payload body a Litecoin ASIC mines (downward half).

        Carries the upstream job fields verbatim (the ASIC applies the SAME byte-order
        rules :func:`assemble_scrypt_header` documents) plus the pool-assigned
        extranonce1 / extranonce2_size (so the miner can build the coinbase). The
        server re-keys this under Alice's internal job id via
        :meth:`InternalJob.to_notification`; the upstream job id is NOT included.
        """

        return {
            "algo": "LTC_SCRYPT",
            "prevhash": notify.prevhash,
            "coinb1": notify.coinb1,
            "coinb2": notify.coinb2,
            "merkle_branch": list(notify.merkle_branch),
            "version": notify.version,
            "nbits": notify.nbits,
            "ntime": notify.ntime,
            "extranonce1": subscription.extranonce1,
            "extranonce2_size": subscription.extranonce2_size,
        }

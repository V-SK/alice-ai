"""The REAL per-algo R2 job translator for the KawPoW (Ravencoin/RVN) lane (doc §2.3 R2).

This is the RVN leg of the deploy-flagged "real per-algo R2 translator" — the KawPoW
mirror of :mod:`alice_acp.transport_service.monero_translator`. It turns one upstream
Ravencoin ``mining.notify`` (the T-Rex/kawpowminer Ethereum-dialect stratum job) into an
:class:`InternalJob` that carries EVERYTHING needed for the two halves of the proxy pool
to agree on one block to mine:

  (a) the DOWNWARD half — a self-describing job a stock KawPoW rig (T-Rex /
      kawpowminer / NBMiner) can mine (``[job_id, headerHash, seedHash, target,
      clean_jobs, height, bits]``), re-keyed under Alice's internal job id;
  (b) the VALIDATION half — the SAME ``headerHash`` + ``seedHash`` + ``height`` so the
      server can reconstruct, at submit time, the EXACT 32-byte header hash the rig
      hashed (the rig varies only the 64-bit nonce; KawPoW does NOT fold a coinbase) and
      Alice's :class:`~alice_acp.share_validator.verifiers.kawpow.LocalKawPowVerifier`
      re-hashes ``headerHash`` + nonce against the per-epoch DAG (keyed by the epoch
      derived from ``height``).

THE CORRECTNESS CONTRACT (the whole point)
------------------------------------------
KawPoW (like RandomX, unlike Scrypt) is NOT folded from a coinbase template by the
front — the upstream pool hands a COMPLETE 32-byte ``headerHash`` and the rig only
varies the 64-bit nonce. So this module does NO header assembly: it carries the upstream
``headerHash`` + ``seedHash`` + ``height`` verbatim on the :class:`InternalJob` payload,
and the server's ``_kawpow_header_for`` (``stratum_server.py``) looks the cached job up
by id and returns ``(epoch, height, header_hash_bytes)`` for the verifier — the epoch +
height are SERVER-SOURCED from the cached job, NEVER client-supplied (the novel-epoch
DoS defense: a hostile rig cannot ask Alice to build an arbitrary-epoch DAG). The
translator's job is to PROPAGATE ``headerHash`` + ``seedHash`` + ``height`` faithfully.

KAWPOW STRATUM ``mining.notify`` FIELDS (the T-Rex/Ravencoin Ethereum dialect)
------------------------------------------------------------------------------
The upstream ``mining.notify`` is a POSITIONAL array (the Ethereum/Ravencoin dialect,
NOT the cryptonote object dialect):

* ``headerHash`` — the 32-byte hex header hash (the bytes KawPoW hashes with the nonce).
* ``seedHash``   — the per-epoch KawPoW seed (32-byte hex, the DAG key). It changes every
  ``KAWPOW_EPOCH_LENGTH`` (7500) blocks. Advisory here: the verifier takes the epoch
  NUMBER (derived from ``height``), but the seedHash is carried for the rig + receipts.
* ``target``     — the share/network target as a FULL 32-byte (64-hex) BIG-ENDIAN target
  (NOT the xmrig 4-byte compact target). :func:`target_to_difficulty_256` reads it on the
  2**256 scale so it composes with the verifier's ``MAX_TARGET / H`` (also 2**256).
* ``height``     — the block height (the block NUMBER KawPoW mixes into the hash AND the
  source of the epoch: ``epoch = height // KAWPOW_EPOCH_LENGTH``). LOAD-BEARING (unlike
  the Monero ``height`` which is advisory): the verifier's DAG is keyed on the epoch.
* ``bits``       — the network nbits (advisory; carried for the rig + telemetry).

NET DIFFICULTY (2**256 SCALE — coin-agnostic, same as the verifier)
-------------------------------------------------------------------
KawPoW difficulty genuinely IS ``2**256 / H`` (T-Rex/Ethash compare the final hash to the
boundary as a BIG-endian 256-bit number — ``cpp-kawpow ethash::is_less_or_equal``). So
:func:`target_to_difficulty_256` returns ``d = MAX_TARGET / int(target, 16)`` on the FULL
2**256 scale — the SAME scale the verifier's ``difficulty_from_hash(.., byteorder="big")``
(default ``MAX_TARGET``) reports ``result_difficulty`` on. No 2**16 rescale (that was a
Scrypt-only artifact). (This 2**256 "expected-hash" scale is ravenminer's conventional
wire difficulty x 2**32 — MEASURED; internally consistent, so no diff-1 constant needed.)
The server's solution-classification compares ``result_difficulty`` directly against
``net_difficulty`` for this lane.

CREDIT-ONLY: the translated job carries NO reward/payout/chain symbol; the credited unit
is unchanged (the validator's ValidatedShareStore write). ``ensure_no_raw_secret`` guards
the advisory-provenance strings (the upstream job id, the internal job id). No secret is
read here (the translator only sees public stratum job fields).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any

from alice_acp.shadow_server.types import Lane, utc_now
from alice_acp.share_validator.types import MAX_TARGET
from alice_acp.transport_service.jobs import InternalJob, JobTranslationMap

#: KawPoW header-hash length (32 bytes). The notify's ``headerHash`` MUST be exactly this;
#: a shorter/longer value is rejected fail-closed (the verifier hashes a 32-byte header).
KAWPOW_HEADER_HASH_BYTES = 32

#: Ravencoin KawPoW epoch length (blocks per DAG epoch) — MUST match the verifier's
#: ``KAWPOW_EPOCH_LENGTH``. The epoch the verifier keys its DAG on is
#: ``height // KAWPOW_EPOCH_LENGTH``; the server derives it from the cached job's height.
KAWPOW_EPOCH_LENGTH = 7500

#: Stable, secret-free reason code for a dropped/rejected translation (advisory telemetry
#: only — a drop NEVER affects credit, which has not happened yet at R2).
KAWPOW_TRANSLATE_BAD_JOB = "kawpow_translate_malformed_job"


def _strip0x(text: str) -> str:
    return text[2:] if text[:2].lower() == "0x" else text


def target_to_difficulty_256(target_hex: str) -> Decimal:
    """The network/pool difficulty from a KawPoW 32-byte ``target`` on the 2**256 scale.

    KawPoW/T-Rex quotes the share target as a FULL 32-byte (64-hex) BIG-ENDIAN target the
    rig compares its KawPoW final hash against as a big-endian 256-bit number (NOT the
    xmrig 4-byte compact target). ``d = MAX_TARGET / int(target, 16)`` — the SAME 2**256
    baseline the KawPoW verifier reports ``result_difficulty`` on (``difficulty_from_hash(
    .., byteorder="big")`` with the default ``MAX_TARGET``), so the server compares a share's
    ``result_difficulty`` directly against this net difficulty with NO rescale (unlike the
    Scrypt lane's 2**16 shift). Reads BIG-ENDIAN (the stratum target wire order — the
    target is a hex string of the 256-bit number). A degenerate (zero / non-hex) target
    raises ``ValueError`` (fail-closed) — never a divide-by-zero, never a silent "every
    hash is a share". Returned as a ``Decimal`` so it composes with the validator's
    ``Decimal`` arithmetic.
    """

    raw = _strip0x(target_hex)
    if not raw:
        raise ValueError(KAWPOW_TRANSLATE_BAD_JOB)
    try:
        target_int = int(raw, 16)
    except ValueError as exc:
        raise ValueError(KAWPOW_TRANSLATE_BAD_JOB) from exc
    if target_int <= 0:
        raise ValueError(KAWPOW_TRANSLATE_BAD_JOB)
    return Decimal(MAX_TARGET) / Decimal(target_int)


def difficulty_to_target_256(difficulty: Decimal | int | float | str) -> str:
    """The inverse of :func:`target_to_difficulty_256`: a 2**256 difficulty -> the FULL
    32-byte (64-hex) KawPoW ``target`` hex string (doc §2.1 / §2.3 R2).

    A KawPoW rig has NO ``set_difficulty`` verb: its share difficulty IS expressed as the
    ``mining.set_target`` 32-byte target. So to hand a rig THIS connection's per-connection
    vardiff difficulty we encode that difficulty as the full 256-bit target
    ``target = MAX_TARGET // difficulty`` (the same ``MAX_TARGET / d`` relation
    :func:`target_to_difficulty_256` reads back) and emit it as a 64-hex BIG-ENDIAN string
    (NOT a 4-byte compact — KawPoW carries the full target). This round-trips EXACTLY
    through :func:`target_to_difficulty_256` for an integer difficulty. A non-positive /
    sub-1 difficulty clamps to difficulty 1 (the maximal ``ffff…ff`` target) — fail-soft,
    never an exception and never a zero target (which a rig would read as "every hash is a
    share").

    Returns the 64-hex (32-byte) BIG-ENDIAN target the rig compares its KawPoW final hash
    against as a big-endian 256-bit number. CREDIT-ONLY: no secret is read; the target is a
    public per-connection difficulty.
    """

    d = Decimal(str(difficulty))
    # A target encodes a difficulty >= 1 (difficulty 1 == the maximal target). A sub-1 /
    # non-positive vardiff clamps to 1 so the rig never gets a zero/oversized target.
    d_int = int(d) if d >= Decimal("1") else 1
    if d_int < 1:
        d_int = 1
    target_int = MAX_TARGET // d_int
    if target_int < 1:
        target_int = 1
    if target_int > MAX_TARGET:
        target_int = MAX_TARGET
    # The full 32-byte target as a 64-hex BIG-ENDIAN string (the KawPoW wire form).
    return target_int.to_bytes(32, "big").hex()


def epoch_for_height(height: int) -> int:
    """The KawPoW DAG epoch for a block ``height`` (``height // KAWPOW_EPOCH_LENGTH``).

    KawPoW's per-epoch DAG/light-cache changes every :data:`KAWPOW_EPOCH_LENGTH` (7500)
    blocks; the verifier keys its memoized light-cache on this epoch number. The SERVER
    derives the epoch from the CACHED job's height (server-sourced, never client-supplied)
    — the novel-epoch DoS defense. A negative height clamps to epoch 0 (fail-soft).
    """

    if height < 0:
        return 0
    return height // KAWPOW_EPOCH_LENGTH


@dataclass(frozen=True, slots=True)
class KawPoWJob:
    """A parsed KawPoW ``mining.notify`` job (all fields validated, fail-closed).

    Carries the upstream job fields the rig needs + the server folds at submit.
    ``header_hash`` / ``seed_hash`` / ``target`` are kept as the hex strings the upstream
    sent (the server returns ``header_hash`` bytes + the height-derived epoch for the
    verifier). ``height`` is LOAD-BEARING (the epoch source); ``bits`` is advisory.
    """

    job_id: str
    header_hash: str
    seed_hash: str
    target: str
    height: int
    bits: str
    clean_jobs: bool = True


def parse_kawpow_job(message: dict) -> KawPoWJob:
    """Parse a KawPoW ``mining.notify`` into a validated :class:`KawPoWJob` (fail-closed).

    Accepts either the bare job object ``{job_id, headerHash, seedHash, target, height,
    bits}`` OR a ``mining.notify`` envelope ``{"method": "mining.notify", "params":
    [job_id, headerHash, seedHash, target, clean_jobs, height, bits]}`` (the T-Rex/
    Ravencoin positional notify shape). Validates that ``headerHash`` / ``seedHash`` /
    ``target`` are hex, that ``headerHash`` is exactly 32 bytes and ``seedHash`` is the
    32-byte KawPoW seed, and that ``target`` decompresses to a positive 256-bit target.
    Raises ``ValueError`` (fail-closed) on anything malformed — the relay catches it in
    ``_ingest_upstream_job`` and drops the job without crashing.
    """

    if not isinstance(message, dict):
        raise ValueError(KAWPOW_TRANSLATE_BAD_JOB)
    # Unwrap a positional ``mining.notify`` envelope; a bare object is used as-is.
    if message.get("method") == "mining.notify" and isinstance(message.get("params"), list):
        params = message["params"]
        # [job_id, headerHash, seedHash, target, clean_jobs, height, bits]
        if len(params) < 7:
            raise ValueError(KAWPOW_TRANSLATE_BAD_JOB)
        body: dict[str, Any] = {
            "job_id": params[0],
            "headerHash": params[1],
            "seedHash": params[2],
            "target": params[3],
            "clean_jobs": params[4],
            "height": params[5],
            "bits": params[6],
        }
    else:
        body = message

    job_id = body.get("job_id")
    header_hash = body.get("headerHash")
    seed_hash = body.get("seedHash")
    target = body.get("target")
    bits = body.get("bits", "")
    height = body.get("height", 0)
    clean_jobs = body.get("clean_jobs", True)

    if not all(isinstance(v, str) and v for v in (job_id, header_hash, seed_hash, target)):
        raise ValueError(KAWPOW_TRANSLATE_BAD_JOB)
    # Validate the byte-sensitive fields up front (fail-closed): headerHash is exactly 32
    # bytes; seedHash is the 32-byte KawPoW seed; target decompresses to a positive target.
    try:
        header_bytes = bytes.fromhex(_strip0x(header_hash))
        seed_bytes = bytes.fromhex(_strip0x(seed_hash))
    except ValueError as exc:
        raise ValueError(KAWPOW_TRANSLATE_BAD_JOB) from exc
    if len(header_bytes) != KAWPOW_HEADER_HASH_BYTES:
        raise ValueError(KAWPOW_TRANSLATE_BAD_JOB)
    if len(seed_bytes) != 32:
        raise ValueError(KAWPOW_TRANSLATE_BAD_JOB)
    target_to_difficulty_256(target)  # raises on a malformed/zero target
    if not isinstance(height, int):
        try:
            height = int(height)
        except (TypeError, ValueError):
            height = 0
    return KawPoWJob(
        job_id=job_id,
        header_hash=header_hash,
        seed_hash=seed_hash,
        target=target,
        height=height,
        bits=bits if isinstance(bits, str) else "",
        clean_jobs=bool(clean_jobs),
    )


@dataclass(slots=True)
class KawPoWJobTranslator:
    """The REAL R2 KawPoW translator: ``(lane, upstream_job) -> InternalJob``.

    Construct ONE per relay, sharing the relay's :class:`JobTranslationMap` (so the minted
    internal job ids and the reverse map stay coherent). Unlike the Scrypt translator it
    needs NO subscription provider — a KawPoW pool hands a COMPLETE ``headerHash`` (no
    coinbase fold), and the verifier keys the DAG on the epoch the SERVER derives from the
    cached job's height at submit time (it owns the job cache). It is a plain callable
    (``__call__``) so it drops straight into :attr:`DispatcherRelay.job_translator`.

    The translated :class:`InternalJob`:
    * ``internal_job_id`` minted from the shared map (opaque; the upstream id never leaks
      downward),
    * ``upstream_job_id`` = the job's ``job_id`` (advisory provenance; R3 maps it back for
      the upstream submit),
    * ``net_difficulty`` = :func:`target_to_difficulty_256` (the REAL pool/net target on
      the 2**256 scale),
    * ``pool_difficulty`` = the SAME net difficulty (a KawPoW pool quotes ONE target per
      job — the share target IS the job target). The server's vardiff owns the
      per-connection target it actually sends via ``mining.set_target``.
    * ``payload`` = a job body a KawPoW rig can mine + the server reconstructs from (the
      ``headerHash`` / ``seedHash`` / ``target`` / ``height`` / ``bits`` + the algo tag),
      re-keyed under the internal job id by :meth:`InternalJob.to_notification`.

    CREDIT-ONLY: no reward/payout/chain symbol; reads no secret. ``payload`` carries only
    public stratum fields.
    """

    job_map: JobTranslationMap
    clock: Callable[[], datetime] = utc_now

    def __call__(self, lane: Lane, upstream_job: dict) -> InternalJob | None:
        return self.translate(lane, upstream_job)

    def translate(self, lane: Lane, upstream_job: dict) -> InternalJob | None:
        """Translate one upstream KawPoW ``mining.notify`` into an :class:`InternalJob`.

        A malformed job surfaces as ``ValueError`` (which the relay's
        ``_ingest_upstream_job`` catches and drops fail-soft). Returns the translated job
        otherwise; there is no "no subscription yet" drop on this lane (the headerHash is
        self-contained — the first job is immediately minable).
        """

        job = parse_kawpow_job(upstream_job)
        net_difficulty = target_to_difficulty_256(job.target)
        # A KawPoW pool quotes ONE target per job: the share (pool) target IS the job
        # target. So pool == net here (the InternalJob invariant net >= pool holds with
        # equality). The server's vardiff still owns the per-connection target it sends.
        pool_difficulty = net_difficulty

        internal_job_id = self.job_map.mint_internal_id(lane)
        payload = self._build_payload(job)
        return InternalJob(
            lane=lane,
            internal_job_id=internal_job_id,
            upstream_job_id=job.job_id,
            net_difficulty=net_difficulty,
            pool_difficulty=pool_difficulty,
            payload=payload,
            extranonce="",  # KawPoW assigns no per-connection extranonce (mix-hash path)
            clean_jobs=job.clean_jobs,
            created_at=self.clock(),
        )

    @staticmethod
    def _build_payload(job: KawPoWJob) -> dict[str, Any]:
        """The job payload body a KawPoW rig mines (downward half) + the server folds.

        Carries the upstream ``headerHash`` verbatim (the bytes the rig hashes with the
        nonce) plus the ``seedHash`` (the per-epoch DAG key), ``target``, ``height`` (the
        epoch source — LOAD-BEARING), and ``bits``. The server re-keys this under Alice's
        internal job id via :meth:`InternalJob.to_notification` and CACHES it so
        ``_kawpow_header_for`` can reconstruct ``(epoch, height, header_hash)`` at submit;
        the upstream job id is NOT included. The keys are the exact ``mining.notify``
        field names (``headerHash`` / ``seedHash``) the positional builder reads.
        """

        return {
            "algo": "RVN_KAWPOW",
            "headerHash": job.header_hash,
            "seedHash": job.seed_hash,
            "target": job.target,
            "height": job.height,
            "bits": job.bits,
        }

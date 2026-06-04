"""The REAL per-algo R2 job translator for the RandomX (Monero/XMR) lane (doc §2.3 R2).

This is the XMR leg of the deploy-flagged "real per-algo R2 translator" — the RandomX
mirror of :mod:`alice_acp.transport_service.scrypt_translator`. It turns one upstream
Monero ``job`` (the xmrig/cryptonote stratum object dialect) into an
:class:`InternalJob` that carries EVERYTHING needed for the two halves of the proxy
pool to agree on one mining blob:

  (a) the DOWNWARD half — a self-describing ``job`` object a stock RandomX rig
      (xmrig) can mine (``{algo, blob, job_id, target, seed_hash, height}``), re-keyed
      under Alice's internal job id;
  (b) the VALIDATION half — the SAME ``blob`` + ``seed_hash`` so the server can
      reconstruct, at submit time, the EXACT 76-byte blob the rig hashed (with the
      rig's nonce spliced at [39:43] and this connection's per-miner extranonce at
      byte 8) and Alice's
      :class:`~alice_acp.share_validator.verifiers.randomx.LocalRandomXVerifier`
      re-hashes it against ``seed_hash``.

THE CORRECTNESS CONTRACT (the whole point)
------------------------------------------
RandomX (unlike Scrypt) is NOT folded from a coinbase template by the front — the
upstream pool hands a COMPLETE mining ``blob`` and the rig only varies the 4-byte
nonce at offset 39 (and, in a proxy, a per-connection extranonce region the pool
reserves). So this module does NO header assembly: it carries the upstream ``blob``
verbatim on the :class:`InternalJob` payload, and the server's ``_monero_blob_for``
(``stratum_server.py``) splices the rig's submitted nonce + this connection's
extranonce into that cached blob to rebuild the precise pre-image. The verifier then
does the [39:43] nonce-splice + the seed-keyed RandomX hash. The translator's job is
to PROPAGATE ``blob`` + ``seed_hash`` faithfully (the seed_hash propagation
job→cache→RawSubmission is the load-bearing thread — a wrong seed means a wrong VM).

MONERO STRATUM ``job`` FIELDS (the xmrig/cryptonote object dialect)
-------------------------------------------------------------------
The upstream ``job`` (sent inline in the ``login`` result and as a ``job`` push) is an
OBJECT (named keys), NOT a positional array:

* ``blob``      — the hex mining blob (the bytes RandomX hashes; the nonce lives at
  bytes [39:43]). 76 bytes for a Monero v8+ block-template blob, but the translator
  does not pin the length (other RandomX coins differ) — it only validates it is hex
  and long enough to hold the nonce at offset 39.
* ``job_id``    — the upstream job id (opaque; advisory provenance for the R2 map).
* ``target``    — the pool/share target as a hex string. Monero pools send a 4-byte
  (8-hex) LITTLE-ENDIAN truncated target (``0xFFFFFFFF / t`` on the 32-bit scale) or a
  full 8-byte target. :func:`net_difficulty_from_target` decompresses it to the 2**256
  scale so it composes with the verifier's ``MAX_TARGET / H`` (which is also 2**256).
* ``seed_hash`` — the per-epoch RandomX seed (32-byte hex). The VM is keyed on this; it
  changes every ~2048 blocks. The server feeds ``bytes.fromhex(seed_hash)`` as the
  ``RawSubmission.seed`` so the verifier builds/looks-up the right per-seed VM.
* ``height``    — the block height (advisory; carried for the rig + telemetry).

NET DIFFICULTY (2**256 SCALE — coin-agnostic, same as the verifier)
-------------------------------------------------------------------
RandomX/Monero difficulty genuinely IS ``2**256 / H`` (xmrig compares the
little-endian digest against the target). So :func:`net_difficulty_from_target`
returns ``d = MAX_TARGET / target_full`` on the FULL 2**256 scale — the SAME scale the
verifier's ``difficulty_from_hash(..., byteorder="little")`` (default ``MAX_TARGET``)
reports ``result_difficulty`` on. No 2**16 rescale (that was a Scrypt-only artifact);
the server's solution-classification compares ``result_difficulty`` directly against
``net_difficulty`` for this lane.

CREDIT-ONLY: the translated job carries NO reward/payout/chain symbol; the credited
unit is unchanged (the validator's ValidatedShareStore write). ``ensure_no_raw_secret``
guards the advisory-provenance strings (the upstream job id, the internal job id). No
secret is read here (the translator only sees public stratum job fields).
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

#: The Monero mining-blob nonce offset (bytes [39:43]) — MUST match
#: :attr:`LocalRandomXVerifier.nonce_offset`. The blob must be long enough to hold a
#: 4-byte nonce at this offset; a shorter blob is rejected fail-closed (the verifier
#: would otherwise fall back to a header+nonce concat that the rig never hashed).
MONERO_NONCE_OFFSET = 39
NONCE_LEN = 4

#: Stable, secret-free reason codes for a dropped/rejected translation (advisory
#: telemetry only — a drop NEVER affects credit, which has not happened yet at R2).
MONERO_TRANSLATE_BAD_JOB = "monero_translate_malformed_job"


def _strip0x(text: str) -> str:
    return text[2:] if text[:2].lower() == "0x" else text


def target_to_full_256(target_hex: str) -> int:
    """Decompress a Monero pool ``target`` hex string to the full 256-bit target int.

    Monero/cryptonote stratum quotes the share target as a hex string the rig compares
    its little-endian RandomX digest against. Two encodings are seen in the wild and
    both are handled (fail-closed on anything else):

    * a 4-byte (8-hex) LITTLE-ENDIAN truncated target ``t`` — the common xmrig form. The
      conventional decompression is ``target_256 = floor(2**256 / (0xFFFFFFFF / t))``,
      i.e. the full target whose 32-bit difficulty is ``0xFFFFFFFF / t``. We compute the
      32-bit difficulty ``D32 = 0xFFFFFFFF / t`` and return ``MAX_TARGET // D32`` so the
      downstream ``MAX_TARGET / target_256`` lands on the SAME 2**256 scale the verifier
      uses (``D32`` cancels cleanly: a 4-byte target of ``0xFFFFFFFF`` => difficulty 1 =>
      target_256 = ``MAX_TARGET``).
    * an 8-byte (16-hex) LITTLE-ENDIAN target ``t`` — read as the high 64 bits of the
      256-bit target: ``target_256 = t << (256 - 64)``.

    Reads LITTLE-ENDIAN (the cryptonote wire order). Raises ``ValueError`` (fail-closed)
    on a non-hex / wrong-length / zero target.
    """

    raw = bytes.fromhex(_strip0x(target_hex))
    value = int.from_bytes(raw, "little")
    if value <= 0:
        raise ValueError(MONERO_TRANSLATE_BAD_JOB)
    if len(raw) <= 4:
        # 32-bit truncated target: difficulty is 0xFFFFFFFF / t; the full 256-bit
        # target whose 2**256/target equals that difficulty is MAX_TARGET // D32.
        diff32 = 0xFFFFFFFF // value if value <= 0xFFFFFFFF else 1
        if diff32 <= 0:
            diff32 = 1
        return MAX_TARGET // diff32
    if len(raw) <= 8:
        # 64-bit target occupying the high 8 bytes of the 256-bit target word.
        return value << (256 - 64)
    # A full (or near-full) 256-bit target sent as-is (little-endian).
    return value


def net_difficulty_from_target(target_hex: str) -> Decimal:
    """The network/pool difficulty from a Monero ``target`` on the 2**256 scale.

    ``d = MAX_TARGET / target_256`` (:func:`target_to_full_256`). This is the SAME
    2**256 baseline the RandomX verifier reports ``result_difficulty`` on
    (``difficulty_from_hash(.., byteorder="little")`` with the default ``MAX_TARGET``),
    so the server compares a share's ``result_difficulty`` directly against this net
    difficulty with NO rescale (unlike the Scrypt lane's 2**16 shift). A degenerate
    target clears the maximum representable difficulty (never divide by zero). Returned
    as a ``Decimal`` so it composes with the validator's ``Decimal`` arithmetic.
    """

    target_256 = target_to_full_256(target_hex)
    if target_256 <= 0:
        return Decimal(MAX_TARGET)
    return Decimal(MAX_TARGET) / Decimal(target_256)


def difficulty_to_compact_target(difficulty: Decimal | int | float | str) -> str:
    """The inverse of :func:`net_difficulty_from_target`: a 2**256-scale difficulty -> the
    xmrig 4-byte compact ``target`` hex string (doc §2.1 / §2.3 R2).

    A Monero/cryptonote rig has NO ``set_difficulty`` verb: its share difficulty IS the
    job's ``target``. So to hand a rig THIS connection's per-connection vardiff difficulty
    we encode that difficulty as the standard xmrig 4-byte compact target. We take the FULL
    256-bit target ``full_target = MAX_TARGET // difficulty`` (the same ``MAX_TARGET / d``
    relation :func:`net_difficulty_from_target` reads back), then emit the TOP 4 bytes of
    its 32-byte BIG-ENDIAN form, little-endian — i.e. ``struct.pack('<I', floor(...))`` the
    way xmrig encodes a compact target. This round-trips through :func:`target_to_full_256`
    / :func:`net_difficulty_from_target` within the 32-bit compaction tolerance (a target is
    only 4 significant bytes, so a sub-LSB difficulty maps to the nearest representable
    compact value). A non-positive / sub-1 difficulty clamps to difficulty 1 (the maximal
    ``0xFFFFFFFF`` target) — fail-soft, never an exception and never a zero target (which a
    rig would read as "every hash is a share").

    Returns the 8-hex (4-byte) LITTLE-ENDIAN compact target the rig compares its
    little-endian RandomX digest against (the cryptonote wire order). CREDIT-ONLY: no
    secret is read; the target is a public per-connection difficulty.
    """

    d = Decimal(str(difficulty))
    # A target encodes only a difficulty >= 1 (difficulty 1 == the maximal target). A
    # sub-1 / non-positive vardiff clamps to 1 so the rig never gets a zero/oversized
    # target (which would make every hash a "share").
    d_int = int(d) if d >= Decimal("1") else 1
    if d_int < 1:
        d_int = 1
    full_target = MAX_TARGET // d_int
    if full_target < 1:
        full_target = 1
    # The xmrig compact target is the TOP 4 bytes of the 32-byte big-endian full target,
    # sent little-endian (== struct.pack('<I', 0xFFFFFFFF // d) on the 32-bit scale).
    top_four_be = full_target.to_bytes(32, "big")[:4]
    return top_four_be[::-1].hex()


@dataclass(frozen=True, slots=True)
class MoneroJob:
    """A parsed Monero ``job`` (all fields validated, fail-closed).

    Carries the upstream job fields the rig needs + the server folds at submit. ``blob``
    / ``seed_hash`` / ``target`` are kept as the hex strings the upstream sent (the
    server splices the nonce into ``blob`` and feeds ``seed_hash`` as the verifier
    seed). ``height`` is advisory.
    """

    job_id: str
    blob: str
    target: str
    seed_hash: str
    height: int


def parse_monero_job(message: dict) -> MoneroJob:
    """Parse a Monero ``job`` object into a validated :class:`MoneroJob` (fail-closed).

    Accepts either the bare job object ``{blob, job_id, target, seed_hash, height}`` OR
    a ``job`` push envelope ``{"method": "job", "params": {...}}`` (xmrig's notify
    shape). Validates that ``blob`` / ``target`` / ``seed_hash`` are hex, that the blob
    is long enough to hold a 4-byte nonce at offset 39, and that ``seed_hash`` is the
    32-byte RandomX seed. Raises ``ValueError`` (fail-closed) on anything malformed — the
    relay catches it in ``_ingest_upstream_job`` and drops the job without crashing.
    """

    if not isinstance(message, dict):
        raise ValueError(MONERO_TRANSLATE_BAD_JOB)
    # Unwrap a {"method":"job","params":{...}} push envelope (xmrig's notify shape);
    # a bare job object (as the login result carries) is used as-is.
    if message.get("method") == "job" and isinstance(message.get("params"), dict):
        body = message["params"]
    else:
        body = message
    job_id = body.get("job_id")
    blob = body.get("blob")
    target = body.get("target")
    seed_hash = body.get("seed_hash")
    height = body.get("height", 0)

    if not all(isinstance(v, str) and v for v in (job_id, blob, target, seed_hash)):
        raise ValueError(MONERO_TRANSLATE_BAD_JOB)
    # Validate the byte-sensitive fields up front (fail-closed): blob is hex and long
    # enough to hold the nonce at offset 39; seed_hash is the 32-byte RandomX seed;
    # target decompresses to a positive 256-bit target.
    try:
        blob_bytes = bytes.fromhex(_strip0x(blob))
        seed_bytes = bytes.fromhex(_strip0x(seed_hash))
    except ValueError as exc:
        raise ValueError(MONERO_TRANSLATE_BAD_JOB) from exc
    if len(blob_bytes) < MONERO_NONCE_OFFSET + NONCE_LEN:
        raise ValueError(MONERO_TRANSLATE_BAD_JOB)
    if len(seed_bytes) != 32:
        raise ValueError(MONERO_TRANSLATE_BAD_JOB)
    target_to_full_256(target)  # raises on a malformed/zero target
    if not isinstance(height, int):
        try:
            height = int(height)
        except (TypeError, ValueError):
            height = 0
    return MoneroJob(
        job_id=job_id,
        blob=blob,
        target=target,
        seed_hash=seed_hash,
        height=height,
    )


@dataclass(slots=True)
class MoneroJobTranslator:
    """The REAL R2 RandomX translator: ``(lane, upstream_job) -> InternalJob``.

    Construct ONE per relay, sharing the relay's :class:`JobTranslationMap` (so the
    minted internal job ids and the reverse map stay coherent). Unlike the Scrypt
    translator it needs NO subscription provider — a Monero pool hands a COMPLETE blob
    (no coinbase fold), and the per-connection nonce-extra split is the SERVER's job at
    submit time (it owns the blob cache). It is a plain callable (``__call__``) so it
    drops straight into :attr:`DispatcherRelay.job_translator`.

    The translated :class:`InternalJob`:
    * ``internal_job_id`` minted from the shared map (opaque; the upstream id never
      leaks downward),
    * ``upstream_job_id`` = the job's ``job_id`` (advisory provenance; R3 maps it back
      for the upstream submit),
    * ``net_difficulty`` = :func:`net_difficulty_from_target` (the REAL pool/net target
      on the 2**256 scale),
    * ``pool_difficulty`` = the SAME net difficulty (a Monero pool quotes ONE target per
      job — the share target IS the job target; there is no separate set_difficulty).
      The server's vardiff owns the per-connection pool target it actually sends.
    * ``payload`` = a ``job`` body a RandomX rig can mine + the server reconstructs from
      (the ``blob`` / ``seed_hash`` / ``target`` / ``height`` + the algo tag), re-keyed
      under the internal job id by :meth:`InternalJob.to_notification`.

    CREDIT-ONLY: no reward/payout/chain symbol; reads no secret. ``payload`` carries
    only public stratum fields.
    """

    job_map: JobTranslationMap
    clock: Callable[[], datetime] = utc_now

    def __call__(self, lane: Lane, upstream_job: dict) -> InternalJob | None:
        return self.translate(lane, upstream_job)

    def translate(self, lane: Lane, upstream_job: dict) -> InternalJob | None:
        """Translate one upstream Monero ``job`` into an :class:`InternalJob`.

        A malformed job surfaces as ``ValueError`` (which the relay's
        ``_ingest_upstream_job`` catches and drops fail-soft). Returns the translated
        job otherwise; there is no "no subscription yet" drop on this lane (the blob is
        self-contained — the first job is immediately minable).
        """

        job = parse_monero_job(upstream_job)
        net_difficulty = net_difficulty_from_target(job.target)
        # A Monero pool quotes ONE target per job: the share (pool) target IS the job
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
            extranonce="",  # RandomX assigns the per-connection nonce-extra at the server
            clean_jobs=True,  # a new Monero job always supersedes the prior one
            created_at=self.clock(),
        )

    @staticmethod
    def _build_payload(job: MoneroJob) -> dict[str, Any]:
        """The ``job`` payload body a RandomX rig mines (downward half) + the server folds.

        Carries the upstream ``blob`` verbatim (the bytes the rig hashes, with the nonce
        at [39:43] the rig fills) plus the ``seed_hash`` (the per-epoch VM key Alice
        re-hashes against), ``target`` and ``height``. The server re-keys this under
        Alice's internal job id via :meth:`InternalJob.to_notification` and CACHES it so
        ``_monero_blob_for`` can reconstruct the exact blob at submit; the upstream job
        id is NOT included.
        """

        return {
            "algo": "XMR_RANDOMX",
            "blob": job.blob,
            "target": job.target,
            "seed_hash": job.seed_hash,
            "height": job.height,
        }

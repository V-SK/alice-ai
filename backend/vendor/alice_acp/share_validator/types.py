"""The :class:`ShareVerifier` Protocol + the value objects the validator passes.

A verifier is the per-algorithm PoW oracle: given the work a rig was assigned plus
the nonce/extranonce it found, it RE-HASHES (Alice = source of truth) and reports the
result hash, the difficulty it clears (``d = MAX_TARGET / H``), whether it is valid,
and a stable reason. The validator core (:mod:`alice_acp.share_validator.validator`)
owns classification + dedup + accounting + the :class:`ValidatedShare` emission and is
verifier-agnostic, so RandomX can later move to a verify-cluster without touching the
pipeline (doc §2.2 / §3 Q7).

CREDIT-ONLY: these objects carry only opaque work facts; no payout/reward/chain
symbol exists here. ``SELF-REPORT NEVER COUNTS`` — there is no field on
:class:`VerifyOutcome` that copies the rig's claimed difficulty; the credited
magnitude is strictly Alice's recomputed ``result_difficulty``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol

from alice_acp.evidence.types import ensure_no_raw_secret

#: The maximum 256-bit target — ``2**256 - 1``. A PoW result hash ``H`` interpreted
#: as a 256-bit integer clears difficulty ``d = MAX_TARGET / H`` (the standard
#: pool/stratum difficulty-1 baseline used by Bitcoin-family, RandomX, KawPoW, and
#: Scrypt pools alike, modulo the per-coin difficulty-1 constant which is folded into
#: the per-lane ``pool_target_difficulty`` / ``net_target_difficulty`` the caller
#: supplies). Using the full 2**256 keeps the verifier coin-agnostic: a verifier
#: reports the *raw* ``2**256 / H`` and the validator compares it against the targets
#: the lane was configured with.
MAX_TARGET = (1 << 256) - 1


def difficulty_from_hash(
    result_hash: bytes, *, byteorder: str = "little", max_target: int = MAX_TARGET
) -> Decimal:
    """Difficulty a PoW result hash clears: ``max_target / int(H)`` as a ``Decimal``.

    ``result_hash`` is the 32-byte PoW output. ``byteorder`` selects how it is read
    as an integer — RandomX (xmrig reverses the digest) and Scrypt read the hash
    **little-endian**, so ``"little"`` is the default those two verifiers use. KawPoW is
    the OPPOSITE: Ethash/T-Rex compare the final hash to the boundary as a BIG-endian
    256-bit number (``cpp-kawpow ethash::is_less_or_equal``), so the KawPoW verifier passes
    ``byteorder="big"`` (a little-endian read there rejects every real share at any
    non-trivial target — MEASURED). A hash of all-zero bytes (integer 0) is treated as clearing
    the maximum difficulty (it is below every target). The result is a strictly
    positive ``Decimal`` so an accepted share always derives a positive credit score.

    ``max_target`` is the difficulty-1 numerator. It DEFAULTS to the full 2**256
    :data:`MAX_TARGET` — the coin-agnostic scale RandomX (XMR) and KawPoW (RVN) use
    natively (their difficulty genuinely is ``2**256 / H``). The Litecoin Scrypt lane
    instead clears against the scrypt-stratum difficulty-1 target
    ``0x0000FFFF0000…`` (``2**240`` = the Bitcoin diff-1 × 2**16, ~2**16 smaller than
    2**256); that lane passes its own ``max_target`` so the reported share difficulty
    lands on the conventional scrypt-pool scale the miner's ``mining.set_difficulty``
    quotes (the network difficulty the translator reports from ``nbits`` is the
    Bitcoin diff-1 / ≈2**224 scale, 2**16 smaller — the server scales it up by 2**16
    when classifying a solution). Callers that omit ``max_target`` are byte-for-byte
    unchanged (the RandomX/KawPoW scale).
    """

    if not isinstance(result_hash, (bytes, bytearray)):
        raise TypeError("result_hash must be bytes")
    if len(result_hash) != 32:
        raise ValueError("result_hash must be 32 bytes")
    value = int.from_bytes(bytes(result_hash), byteorder)  # type: ignore[arg-type]
    if value <= 0:
        # An all-zero (or impossibly low) hash clears the maximum representable
        # difficulty. Never divide by zero; never report a non-positive difficulty.
        return Decimal(max_target)
    return Decimal(max_target) / Decimal(value)


@dataclass(frozen=True, slots=True)
class VerifyWork:
    """The work a rig was assigned + the candidate solution it submitted.

    This is the verifier's input: everything Alice needs to RE-HASH the candidate
    independently. ``seed`` is the per-epoch seed (RandomX ``seed_hash`` / KawPoW
    seedhash; unused by Scrypt). ``header`` is the algorithm's pre-image / blob /
    block header (the bytes the nonce is mixed into). ``nonce`` + ``extranonce`` are
    the rig's claimed solution. All ``bytes`` so a verifier never re-parses hex.

    ``epoch`` and ``block_number`` are the KawPoW-only integer inputs (default 0,
    unused by RandomX/Scrypt): KawPoW mixes the block HEIGHT into the hash AND keys
    its per-epoch DAG on the epoch, so the verifier needs BOTH. They are SERVER-
    sourced from the cached job (``epoch = height // KAWPOW_EPOCH_LENGTH``), NEVER
    client-supplied — the novel-epoch DoS defense (a hostile rig cannot make Alice
    build an arbitrary-epoch DAG). Adding them as explicit ints (rather than packing
    into ``extranonce``) keeps the RandomX/Scrypt paths byte-for-byte unchanged.
    """

    algorithm: str
    seed: bytes
    header: bytes
    nonce: bytes
    extranonce: bytes = b""
    epoch: int = 0
    block_number: int = 0

    def __post_init__(self) -> None:
        if not self.algorithm:
            raise ValueError("algorithm must be non-empty")
        for field_name, value in (
            ("seed", self.seed),
            ("header", self.header),
            ("nonce", self.nonce),
            ("extranonce", self.extranonce),
        ):
            if not isinstance(value, (bytes, bytearray)):
                raise TypeError(f"{field_name} must be bytes")
        for field_name, value in (("epoch", self.epoch), ("block_number", self.block_number)):
            if not isinstance(value, int) or isinstance(value, bool):
                raise TypeError(f"{field_name} must be an int")
            if value < 0:
                raise ValueError(f"{field_name} must be non-negative")


@dataclass(frozen=True, slots=True)
class VerifyOutcome:
    """A verifier's verdict on one :class:`VerifyWork`.

    ``result_hash`` is the 32-byte hash Alice computed. ``result_difficulty`` is
    ``MAX_TARGET / int(result_hash)`` — the difficulty Alice's OWN re-hash clears
    (NEVER the rig's claim; SELF-REPORT NEVER COUNTS). ``valid`` is ``True`` only when
    the re-hash succeeded and produced a usable hash; a verifier that cannot run
    (missing native lib / dataset / cluster) MUST return ``valid=False`` with a
    reason — it MUST NOT fabricate a passing hash (fail-closed). ``reason`` is a
    stable machine code (never a raw library error string).
    """

    result_hash: bytes
    result_difficulty: Decimal
    valid: bool
    reason: str

    def __post_init__(self) -> None:
        if not isinstance(self.result_hash, (bytes, bytearray)):
            raise TypeError("result_hash must be bytes")
        if not isinstance(self.result_difficulty, Decimal):
            raise TypeError("result_difficulty must be Decimal")
        if not self.reason:
            raise ValueError("reason must be non-empty")


class VerifierUnavailable(RuntimeError):
    """The verifier backend cannot run (missing lib / dataset / cluster).

    The validator catches this and FAILS CLOSED (no record → provider ``None`` →
    ``under_review``). It is a distinct, structurally-inert signal: an unavailable
    verifier is NEVER an accept. Carries only a stable reason code, never a raw
    library/transport error (which could leak host detail).
    """


class ShareVerifier(Protocol):
    """Per-algorithm PoW re-hash oracle. ``verify(VerifyWork) -> VerifyOutcome``.

    Implementations MUST be deterministic for a given :class:`VerifyWork` and MUST
    fail closed: a backend that cannot run returns ``valid=False`` (or raises
    :class:`VerifierUnavailable`), NEVER a fabricated passing hash. ``algorithm`` is
    the algorithm string the verifier handles (``XMR_RANDOMX`` / ``RVN_KAWPOW`` /
    ``LTC_SCRYPT``), so a registry can route a submission to the right leg.
    """

    @property
    def algorithm(self) -> str:
        ...

    def verify(self, work: VerifyWork) -> VerifyOutcome:
        ...


@dataclass(frozen=True, slots=True)
class SubmissionIdentity:
    """The server-owned identity a submission is attributed to (doc §2.2).

    These are the SAME values the credit path binds against: ``pool_id`` +
    ``alice_collection_address`` are the per-lane server-owned routing/binding;
    ``worker_name`` is the H_a server-assigned opaque worker (``alc-w-…``);
    ``session_id`` is the issued shadow session. None is a client-self-asserted
    payout address — ``ensure_no_raw_secret`` guards every string.
    """

    pool_id: str
    worker_name: str
    session_id: str
    alice_collection_address: str

    def __post_init__(self) -> None:
        required = (
            self.pool_id,
            self.worker_name,
            self.session_id,
            self.alice_collection_address,
        )
        if any(not value for value in required):
            raise ValueError("submission identity fields must be non-empty")
        for field_name, value in (
            ("pool_id", self.pool_id),
            ("worker_name", self.worker_name),
            ("session_id", self.session_id),
            ("alice_collection_address", self.alice_collection_address),
        ):
            ensure_no_raw_secret(value, field_name=field_name)


@dataclass(frozen=True, slots=True)
class RawSubmission:
    """A raw rig submission handed to :meth:`ShareValidator.validate` (doc §2.2).

    Carries the algorithm, the work (job ``seed``/``header`` + the rig's
    ``nonce``/``extranonce``), the server-owned ``identity``, and the TWO targets the
    lane is configured with: ``pool_target_difficulty`` (``d>=`` => the credited
    SHARE) and ``net_target_difficulty`` (``d>=`` => also a relay-worthy SOLUTION).
    ``pool_job_id`` is advisory provenance only (it is NOT folded into the credited
    canonical hash, which the server reconstructs deterministically — see
    :func:`reconstructed_canonical_share_hash`). ``claimed_difficulty`` is the rig's
    SELF-REPORT, carried for audit/telemetry ONLY and NEVER credited.
    """

    algorithm: str
    seed: bytes
    header: bytes
    nonce: bytes
    extranonce: bytes
    identity: SubmissionIdentity
    pool_target_difficulty: Decimal
    net_target_difficulty: Decimal
    pool_job_id: str = ""
    claimed_difficulty: Decimal | None = None
    #: KawPoW-only SERVER-sourced epoch + block height (default 0; unused by
    #: RandomX/Scrypt). Threaded onto the :class:`VerifyWork` so the KawPoW verifier
    #: keys its DAG on ``epoch`` and mixes ``block_number`` into the hash. NEVER
    #: client-supplied — the server derives them from the cached job (the
    #: novel-epoch DoS defense).
    epoch: int = 0
    block_number: int = 0

    def __post_init__(self) -> None:
        if not self.algorithm:
            raise ValueError("algorithm must be non-empty")
        for field_name, value in (
            ("seed", self.seed),
            ("header", self.header),
            ("nonce", self.nonce),
            ("extranonce", self.extranonce),
        ):
            if not isinstance(value, (bytes, bytearray)):
                raise TypeError(f"{field_name} must be bytes")
        for field_name, value in (
            ("pool_target_difficulty", self.pool_target_difficulty),
            ("net_target_difficulty", self.net_target_difficulty),
        ):
            if not isinstance(value, Decimal):
                raise TypeError(f"{field_name} must be Decimal")
            if value <= Decimal("0"):
                raise ValueError(f"{field_name} must be positive")
        if self.net_target_difficulty < self.pool_target_difficulty:
            # Network difficulty is always at least the pool difficulty (a solution
            # is necessarily also a share). A misconfigured pair is rejected up front.
            raise ValueError("net_target_difficulty must be >= pool_target_difficulty")
        if self.claimed_difficulty is not None and not isinstance(
            self.claimed_difficulty, Decimal
        ):
            raise TypeError("claimed_difficulty must be Decimal or None")
        if self.pool_job_id:
            ensure_no_raw_secret(self.pool_job_id, field_name="pool_job_id")

    def to_verify_work(self) -> VerifyWork:
        return VerifyWork(
            algorithm=self.algorithm,
            seed=bytes(self.seed),
            header=bytes(self.header),
            nonce=bytes(self.nonce),
            extranonce=bytes(self.extranonce),
            epoch=self.epoch,
            block_number=self.block_number,
        )


@dataclass(slots=True)
class ValidationCounters:
    """``Accepted = received - invalid - duplicate`` (doc §2.2, the §1.2 invariant).

    A mutable per-validator tally advanced by :meth:`ShareValidator.validate`.
    ``received`` counts every submission handed in; ``invalid`` counts PoW
    mismatch / low-difficulty / verifier-unavailable rejects; ``duplicate`` counts
    dedup hits; ``accepted`` is the derived ``received - invalid - duplicate`` (the
    credited-share count). ``solutions`` is the subset of accepted that also cleared
    the network target (relay-worthy). The invariant
    ``accepted == received - invalid - duplicate`` is asserted on every read.
    """

    received: int = 0
    invalid: int = 0
    duplicate: int = 0
    accepted: int = 0
    solutions: int = 0

    def assert_invariant(self) -> None:
        if self.accepted != self.received - self.invalid - self.duplicate:
            raise AssertionError("share_validator_accepted_invariant_violated")

    def snapshot(self) -> ValidationCounters:
        self.assert_invariant()
        return ValidationCounters(
            received=self.received,
            invalid=self.invalid,
            duplicate=self.duplicate,
            accepted=self.accepted,
            solutions=self.solutions,
        )


@dataclass(frozen=True, slots=True)
class ValidationDecision:
    """The outcome of one :meth:`ShareValidator.validate` call (doc §2.2).

    ``is_share`` => Alice's re-hash cleared the pool target and a
    :class:`ValidatedShare` was emitted into the store (the CREDITED unit).
    ``is_solution`` => it additionally cleared the network target (relay-worthy; the
    M1 relay consumes this). ``result_difficulty`` is the credited magnitude — Alice's
    OWN recomputed difficulty (never the rig's claim). ``reason`` is the stable verdict
    code. ``dedup_key`` is the ``sha256(algorithm‖seed‖header‖nonce‖extranonce‖
    worker_name)`` this submission was deduped on. ``canonical_share_hash`` is the
    value carried into the emitted :class:`ValidatedShare` (``None`` when no share was
    emitted). ``counters`` is a snapshot AFTER this submission was accounted.

    A low-diff / invalid / duplicate submission produces ``is_share=False`` and emits
    NO record (doc §2.2 fail-closed): nothing is written, nothing is credited.
    """

    accepted: bool
    is_share: bool
    is_solution: bool
    result_hash: bytes
    result_difficulty: Decimal
    reason: str
    dedup_key: str
    counters: ValidationCounters
    canonical_share_hash: str | None = None
    validated_share_id: str | None = None
    recorded: bool = False
    validated_at: datetime | None = None
    claimed_difficulty: Decimal | None = field(default=None)

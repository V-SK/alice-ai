"""``ShareValidator`` — the validate() core (doc §2.2; Milestone 1's critical core).

Given a raw rig submission, this:

1. RE-HASHES it via the per-algorithm :class:`ShareVerifier` (Alice = source of truth).
2. CLASSIFIES against two targets: ``result_difficulty >= pool_target`` => ``is_share``
   (the CREDITED unit); ``>= net_target`` => ``is_solution`` (additionally relay-worthy
   for the M1 relay). SELF-REPORT NEVER COUNTS — the credited difficulty is ALWAYS
   Alice's recomputed ``result_difficulty``.
3. DEDUPS on ``sha256(algorithm‖seed‖header‖nonce‖extranonce‖worker_name)`` via the
   durable :class:`ShareDedupStore`.
4. Maintains ``Accepted = received - invalid - duplicate`` (:class:`ValidationCounters`).
5. For ``is_share``: emits ONE :class:`ValidatedShare` (the M0 contract) into the
   :class:`ValidatedShareStore`, with ``canonical_share_hash`` computed CONSISTENTLY
   with how ``server.py::_reconstruct_share_proof`` builds the proof + how
   ``canonical_share_hash(proof)`` computes it — so the merged
   :class:`ProxyPoolEvidenceProvider` + :func:`cross_check_self_validated_share`
   CONFIRM the credit (else they fail-close to ``HASH_MISMATCH`` = no credit).

Low-diff / invalid / duplicate → reject, NO record (doc §2.2 fail-closed). Verifier
unavailable (missing native lib / dataset / cluster) → reject, NO record → the provider
later yields ``None`` → ``under_review`` (error is NEVER an accept).

THE HASH-CONSISTENCY SEAM (the load-bearing finding). ``canonical_share_hash`` is NOT
the hash of the raw PoW nonce — it is the hash of the SERVER'S reconstructed proof
ENVELOPE. ``server.py::_reconstruct_share_proof`` mints a deterministic
``share_nonce`` / ``share_hash`` / ``pool_job_id`` from
``(pool_id, worker_name, cursor_index, observed_at)`` and ``canonical_share_hash(proof)``
hashes ``{alice_collection_address, algorithm, pool_id, pool_job_id, session_id,
share_difficulty, share_hash, share_nonce, worker_id}``. So the validator computes the
SAME hash via :func:`reconstructed_canonical_share_hash`, which delegates to the server's
own reconstruction at ``cursor_index=0`` (one un-spent validated share per (pool, worker)
per credit poll — the provider drains the oldest, the scheduler credits it at cursor 0).
The actual PoW ``result_hash`` / ``header_hash`` / ``nonce`` are stored on the
:class:`ValidatedShare` as opaque facts (for receipts / audit re-hash), but the
credit-binding hash is the reconstructed-envelope hash.

CREDIT-ONLY: the validator sets no reward/payout/chain symbol; it emits only opaque
facts; ``ensure_no_raw_secret`` is enforced by :class:`ValidatedShare` /
:class:`SubmissionIdentity` on every string.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from alice_acp.mining_proofs.canonical import canonical_share_hash
from alice_acp.shadow_server.pool_evidence_providers import (
    InMemoryValidatedShareStore,
    PoolShareCursorUnavailable,
    ValidatedShare,
    ValidatedShareStore,
)
from alice_acp.shadow_server.server import (
    RECONSTRUCTED_SHARE_DIFFICULTY,
    ShadowServerHarness,
)
from alice_acp.shadow_server.types import Lane, ShadowSession
from alice_acp.share_validator.dedup_store import (
    InMemoryShareDedupStore,
    ShareDedupClaim,
    ShareDedupStore,
    ShareDedupUnavailable,
    submission_dedup_key,
)
from alice_acp.share_validator.types import (
    RawSubmission,
    ShareVerifier,
    ValidationCounters,
    ValidationDecision,
    VerifierUnavailable,
)

SHARE_VALIDATOR_SHARE_RECORDED = "SHARE_VALIDATOR_SHARE_RECORDED"
SHARE_VALIDATOR_SOLUTION_RECORDED = "SHARE_VALIDATOR_SOLUTION_RECORDED"
SHARE_VALIDATOR_LOW_DIFFICULTY = "SHARE_VALIDATOR_LOW_DIFFICULTY"
SHARE_VALIDATOR_INVALID_POW = "SHARE_VALIDATOR_INVALID_POW"
SHARE_VALIDATOR_DUPLICATE = "SHARE_VALIDATOR_DUPLICATE"
SHARE_VALIDATOR_VERIFIER_UNAVAILABLE = "SHARE_VALIDATOR_VERIFIER_UNAVAILABLE"
SHARE_VALIDATOR_NO_VERIFIER = "SHARE_VALIDATOR_NO_VERIFIER"

#: A module-level harness used purely as a PURE-FUNCTION reconstruction oracle for the
#: canonical hash (it touches no ledger state when only ``_reconstruct_session`` /
#: ``_reconstruct_share_proof`` are called — those are ``@staticmethod`` builders). The
#: validator never credits through it; credit happens later in the scheduler against
#: the deployed harness. Constructing one here keeps the hash computation independent of
#: any ledger instance.
_RECONSTRUCTION_ORACLE = ShadowServerHarness()


def reconstructed_canonical_share_hash(
    *,
    session: ShadowSession,
    lane: Lane,
    pool_id: str,
    alice_collection_address: str,
    worker_name: str,
    share_difficulty: Decimal,
    cursor_index: int = 0,
    observed_at: datetime,
    harness: ShadowServerHarness | None = None,
) -> str:
    """The canonical hash the server WILL compute for this share at credit time.

    Delegates to the SERVER'S OWN ``_reconstruct_session`` + ``_reconstruct_share_proof``
    (the exact builders ``credit_attested_shares`` uses) and then
    ``canonical_share_hash`` — so the value the validator carries into the
    :class:`ValidatedShare` is byte-for-byte what
    :func:`cross_check_self_validated_share` recomputes from the reconstructed proof.
    A mismatch here would fail-close to ``HASH_MISMATCH`` (no credit); computing it via
    the server's builders is what guarantees the match.

    ``cursor_index`` is 0 for the common single-un-spent-share-per-poll case (the
    provider drains the oldest un-spent share and the scheduler credits it at cursor 0).
    ``observed_at`` MUST equal the credit poll's ``observed_at`` (the deploy passes the
    same clock to the validator and the scheduler) — it is folded into the deterministic
    identifiers. ``share_difficulty`` MUST equal the value the scheduler reconstructs
    with (see :data:`RECONSTRUCTED_SHARE_DIFFICULTY` note in the validator).
    """

    oracle = harness or _RECONSTRUCTION_ORACLE
    rebuilt = oracle._reconstruct_session(
        lane=lane,
        pool_id=pool_id,
        alice_collection_address=alice_collection_address,
        worker_name=worker_name,
        session=session,
    )
    proof = oracle._reconstruct_share_proof(
        session=rebuilt,
        lane=lane,
        pool_id=pool_id,
        alice_collection_address=alice_collection_address,
        worker_name=worker_name,
        cursor_index=cursor_index,
        observed_at=observed_at,
        share_difficulty=share_difficulty,
    )
    return canonical_share_hash(proof)


@dataclass(slots=True)
class ShareValidator:
    """Re-hash → classify → dedup → account → emit a :class:`ValidatedShare`.

    Inject one :class:`ShareVerifier` per algorithm (``verifiers`` keyed by algorithm
    string), the durable :class:`ValidatedShareStore` the M0 provider drains, the
    durable :class:`ShareDedupStore`, and a clock. The credit-time ``observed_at`` +
    ``share_difficulty`` the scheduler will reconstruct with are supplied per-lane so
    the emitted canonical hash matches (see :func:`reconstructed_canonical_share_hash`).

    The deployed scheduler calls ``credit_attested_shares`` with the DEFAULT flat
    :data:`RECONSTRUCTED_SHARE_DIFFICULTY` for the reconstruction (the real validated
    difficulty rides the ``SelfValidatedShareAuthority.share_difficulty`` override) —
    so ``hash_difficulty`` (the difficulty the canonical hash is reconstructed with)
    defaults to that flat value while ``credited_difficulty`` (carried on the
    :class:`ValidatedShare` + the authority override) is Alice's REAL recomputed
    ``result_difficulty``. This split is exactly the M0 ``test_exit_via_scheduler_tick``
    discipline.
    """

    verifiers: dict[str, ShareVerifier]
    validated_share_store: ValidatedShareStore = field(default_factory=InMemoryValidatedShareStore)
    dedup_store: ShareDedupStore = field(default_factory=InMemoryShareDedupStore)
    clock: Callable[[], datetime] = field(default=datetime.now)
    counters: ValidationCounters = field(default_factory=ValidationCounters)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def validate(
        self,
        submission: RawSubmission,
        *,
        session: ShadowSession,
        lane: Lane,
        credit_observed_at: datetime,
        hash_difficulty: Decimal = RECONSTRUCTED_SHARE_DIFFICULTY,
        cursor_index: int = 0,
        validated_share_id: str | None = None,
    ) -> ValidationDecision:
        """Validate ONE raw submission. The Milestone 1 critical-core entry point.

        ``session`` / ``lane`` / ``credit_observed_at`` / ``hash_difficulty`` /
        ``cursor_index`` are what the SERVER will reconstruct the credited proof with —
        they let the validator compute the matching ``canonical_share_hash`` up front.
        ``credit_observed_at`` MUST equal the credit poll's ``observed_at``;
        ``hash_difficulty`` MUST equal the difficulty the scheduler reconstructs with
        (the flat default in production). ``validated_share_id`` defaults to the
        submission's dedup key (so the store's ``(pool, worker, validated_share_id)``
        idempotency agrees with the validator's dedup layer).

        Returns a :class:`ValidationDecision`. Counters advance on EVERY call:
        ``received`` always; then exactly one of ``invalid`` (PoW mismatch / low-diff /
        verifier-unavailable) or ``duplicate`` (dedup hit) or ``accepted`` (is_share).
        """

        identity = submission.identity
        with self._lock:
            self.counters.received += 1

            # --- 1. re-hash (Alice = source of truth) -------------------------------
            verifier = self.verifiers.get(submission.algorithm)
            dedup_key = submission_dedup_key(
                algorithm=submission.algorithm,
                seed=bytes(submission.seed),
                header=bytes(submission.header),
                nonce=bytes(submission.nonce),
                extranonce=bytes(submission.extranonce),
                worker_name=identity.worker_name,
            )
            if verifier is None:
                # No verifier configured for this algorithm → fail-closed (invalid).
                return self._reject(
                    invalid=True,
                    result_hash=b"\x00" * 32,
                    result_difficulty=Decimal("0"),
                    reason=SHARE_VALIDATOR_NO_VERIFIER,
                    dedup_key=dedup_key,
                    claimed=submission.claimed_difficulty,
                )
            try:
                outcome = verifier.verify(submission.to_verify_work())
            except VerifierUnavailable:
                # Backend cannot run (missing lib / dataset / cluster) → fail-closed.
                # Distinct from a PoW mismatch: it is a correctness incident, not a
                # cheat, but it is STILL never an accept. Counted as invalid.
                return self._reject(
                    invalid=True,
                    result_hash=b"\x00" * 32,
                    result_difficulty=Decimal("0"),
                    reason=SHARE_VALIDATOR_VERIFIER_UNAVAILABLE,
                    dedup_key=dedup_key,
                    claimed=submission.claimed_difficulty,
                )

            if not outcome.valid:
                return self._reject(
                    invalid=True,
                    result_hash=outcome.result_hash,
                    result_difficulty=outcome.result_difficulty,
                    reason=SHARE_VALIDATOR_INVALID_POW,
                    dedup_key=dedup_key,
                    claimed=submission.claimed_difficulty,
                )

            # --- 2. classify against the two targets (SELF-REPORT NEVER COUNTS) -----
            # The credited magnitude is ALWAYS Alice's recomputed result_difficulty.
            result_difficulty = outcome.result_difficulty
            is_share = result_difficulty >= submission.pool_target_difficulty
            is_solution = is_share and result_difficulty >= submission.net_target_difficulty

            if not is_share:
                # Below the pool target → not a credited share. No record.
                return self._reject(
                    invalid=True,
                    result_hash=outcome.result_hash,
                    result_difficulty=result_difficulty,
                    reason=SHARE_VALIDATOR_LOW_DIFFICULTY,
                    dedup_key=dedup_key,
                    claimed=submission.claimed_difficulty,
                )

            # --- 3. dedup -----------------------------------------------------------
            try:
                decision = self.dedup_store.claim(ShareDedupClaim(dedup_key=dedup_key))
            except ShareDedupUnavailable:
                # Durable dedup store unavailable → fail-closed (never risk a
                # double-credit). Counted as invalid (no record emitted).
                return self._reject(
                    invalid=True,
                    result_hash=outcome.result_hash,
                    result_difficulty=result_difficulty,
                    reason=SHARE_VALIDATOR_VERIFIER_UNAVAILABLE,
                    dedup_key=dedup_key,
                    claimed=submission.claimed_difficulty,
                )
            if not decision.first_seen:
                # A re-submitted (or replayed) nonce for the same work+worker.
                self.counters.duplicate += 1
                self.counters.assert_invariant()
                return ValidationDecision(
                    accepted=False,
                    is_share=False,
                    is_solution=False,
                    result_hash=outcome.result_hash,
                    result_difficulty=result_difficulty,
                    reason=SHARE_VALIDATOR_DUPLICATE,
                    dedup_key=dedup_key,
                    counters=self.counters.snapshot(),
                    claimed_difficulty=submission.claimed_difficulty,
                )

            # --- 4. emit ONE ValidatedShare (the M0 contract) -----------------------
            share_id = validated_share_id or dedup_key
            canonical_hash = reconstructed_canonical_share_hash(
                session=session,
                lane=lane,
                pool_id=identity.pool_id,
                alice_collection_address=identity.alice_collection_address,
                worker_name=identity.worker_name,
                share_difficulty=hash_difficulty,
                cursor_index=cursor_index,
                observed_at=credit_observed_at,
            )
            # CROSS-PROCESS HASH BINDING (the credit-correctness seam): the carried
            # ``canonical_share_hash`` was reconstructed at ``observed_at=credit_observed_at``
            # (cursor 0). The credit server reconstructs the proof to re-derive that hash,
            # and ``cross_check_self_validated_share`` recompute-and-compares — so the
            # reconstruction ``observed_at`` MUST be re-derivable on the credit side. We
            # carry it AS ``validated_at`` (the credit server reads it via the provider's
            # ``next_reconstruction_observed_at`` hint and reconstructs at exactly this
            # instant). Using ``self.clock()`` here would carry a DIFFERENT reading than
            # the one folded into the hash, so cross-process (transport clock != credit
            # server tick clock) the recomputed hash would never match → drained-but-
            # not-credited. ``credit_observed_at`` is ≈ the same instant, so the audit
            # meaning of ``validated_at`` (when Alice validated the share) is preserved.
            validated_at = credit_observed_at
            share = ValidatedShare(
                pool_id=identity.pool_id,
                worker_name=identity.worker_name,
                alice_collection_address=identity.alice_collection_address,
                session_id=identity.session_id,
                algorithm=submission.algorithm,
                validated_share_id=share_id,
                # The CREDITED magnitude is Alice's OWN recomputed difficulty, never
                # the rig's claim — SELF-REPORT NEVER COUNTS.
                share_difficulty=result_difficulty,
                canonical_share_hash=canonical_hash,
                # Opaque PoW facts for receipts / independent re-hash (doc §2.8): the
                # real result hash and the submitted nonce. The header_hash field holds
                # Alice's recomputed result hash (a 64-hex sha256-shaped value) for
                # audit; the credit binding uses canonical_share_hash above.
                header_hash=_as_sha256_hex(outcome.result_hash),
                share_nonce=_nonce_label(submission),
                validated_at=validated_at,
                # CROSS-PROCESS CREDIT PLANE (doc §2.8): carry the SIGNED session the
                # transport minted so a SEPARATE credit-server process — whose own
                # ledger never saw this issuance — can re-verify its signature against
                # the shared auth-secret and credit the bound share. Only carried when
                # the session envelope holds its public anti-replay nonce (so the HMAC
                # is re-computable cross-process); a legacy/in-process session without
                # one simply rides as before (no carry) and credits only in-process.
                carried_session=session if session.session_nonce is not None else None,
            )
            try:
                recorded = self.validated_share_store.record(share)
            except PoolShareCursorUnavailable:
                # The store is down. The dedup key was already spent above, so this
                # submission will not double-credit on retry; surface it as invalid so
                # the front NACKs and ops alerts. (The credited count is not advanced.)
                self.counters.invalid += 1
                self.counters.assert_invariant()
                return ValidationDecision(
                    accepted=False,
                    is_share=False,
                    is_solution=False,
                    result_hash=outcome.result_hash,
                    result_difficulty=result_difficulty,
                    reason=SHARE_VALIDATOR_VERIFIER_UNAVAILABLE,
                    dedup_key=dedup_key,
                    counters=self.counters.snapshot(),
                    claimed_difficulty=submission.claimed_difficulty,
                )

            # Accepted: a credited share (the §1.2 invariant unit).
            self.counters.accepted += 1
            if is_solution:
                self.counters.solutions += 1
            self.counters.assert_invariant()
            return ValidationDecision(
                accepted=True,
                is_share=True,
                is_solution=is_solution,
                result_hash=outcome.result_hash,
                result_difficulty=result_difficulty,
                reason=(
                    SHARE_VALIDATOR_SOLUTION_RECORDED
                    if is_solution
                    else SHARE_VALIDATOR_SHARE_RECORDED
                ),
                dedup_key=dedup_key,
                counters=self.counters.snapshot(),
                canonical_share_hash=canonical_hash,
                validated_share_id=share_id,
                recorded=recorded,
                validated_at=validated_at,
                claimed_difficulty=submission.claimed_difficulty,
            )

    def _reject(
        self,
        *,
        invalid: bool,
        result_hash: bytes,
        result_difficulty: Decimal,
        reason: str,
        dedup_key: str,
        claimed: Decimal | None,
    ) -> ValidationDecision:
        """Account an invalid submission and return a non-crediting decision.

        Caller holds ``self._lock``. ``invalid`` advances the invalid counter (PoW
        mismatch / low-diff / verifier-unavailable / no-verifier). No record is emitted.
        """

        if invalid:
            self.counters.invalid += 1
        self.counters.assert_invariant()
        return ValidationDecision(
            accepted=False,
            is_share=False,
            is_solution=False,
            result_hash=result_hash,
            result_difficulty=result_difficulty,
            reason=reason,
            dedup_key=dedup_key,
            counters=self.counters.snapshot(),
            claimed_difficulty=claimed,
        )


def _as_sha256_hex(result_hash: bytes) -> str:
    """Render a 32-byte PoW hash as a 64-char lowercase hex digest.

    The :class:`ValidatedShare` ``header_hash`` field validates as a sha256-shaped
    string; a RandomX/KawPoW/Scrypt result hash is exactly 32 bytes, so its hex form is
    a well-formed 64-hex value carried for receipts/audit (it is NOT the credit-binding
    hash — that is ``canonical_share_hash``).
    """

    raw = bytes(result_hash)
    if len(raw) != 32:
        raw = (raw + b"\x00" * 32)[:32]
    return raw.hex()


def _nonce_label(submission: RawSubmission) -> str:
    """A non-empty, secret-safe label for the submitted nonce (audit field).

    The :class:`ValidatedShare` ``share_nonce`` must be non-empty and secret-safe; the
    raw nonce is opaque bytes, so we carry its hex form prefixed for provenance.
    """

    return "rxn-" + bytes(submission.nonce).hex() + "-" + bytes(submission.extranonce).hex()

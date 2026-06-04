from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from alice_acp.evidence.types import (
    ensure_no_raw_secret,
    validate_aware_timestamp,
    validate_sha256,
)
from alice_acp.mining_proofs.canonical import canonical_share_hash
from alice_acp.mining_proofs.types import MiningShareProof

EvidenceSourceType = Literal["fixture", "manual", "export"]
PoolRejectedShareResult = Literal["rejected", "stale", "duplicate", "invalid"]
PoolEvidenceAuthorityStatus = Literal["matched", "under_review", "rejected"]

POOL_AUTHORITY_CONFIRMED = "POOL_AUTHORITY_CONFIRMED"
POOL_AUTHORITY_ACCEPTED_ABSENT = "POOL_AUTHORITY_ACCEPTED_ABSENT"
POOL_AUTHORITY_CLIENT_NONACCEPTED = "POOL_AUTHORITY_CLIENT_NONACCEPTED"
POOL_AUTHORITY_DUPLICATE_SHARE = "POOL_AUTHORITY_DUPLICATE_SHARE"
POOL_AUTHORITY_INVALID_SHARE = "POOL_AUTHORITY_INVALID_SHARE"
POOL_AUTHORITY_POOL_MISMATCH = "POOL_AUTHORITY_POOL_MISMATCH"
POOL_AUTHORITY_COLLECTION_ADDRESS_MISMATCH = "POOL_AUTHORITY_COLLECTION_ADDRESS_MISMATCH"
POOL_AUTHORITY_REJECTED_SHARE = "POOL_AUTHORITY_REJECTED_SHARE"
POOL_AUTHORITY_SESSION_MISMATCH = "POOL_AUTHORITY_SESSION_MISMATCH"
POOL_AUTHORITY_STALE_SHARE = "POOL_AUTHORITY_STALE_SHARE"
POOL_AUTHORITY_WORKER_MISMATCH = "POOL_AUTHORITY_WORKER_MISMATCH"

# --- PRL (pearlhash) epoch-credit authority (parallel, NO share hash) --------
# pearlhash has NO per-share counter and NO canonical per-share hash (see
# docs/PRL-EPOCH-CREDIT-DESIGN.md §2.0). Its real per-account work signal is the
# hourly EPOCH CREDIT at GET /api/account/<addr>. So the PRL lane CANNOT use the
# share-hash gate (cross_check_pool_authority, which requires the proof's
# canonical_share_hash to be in accepted_share_hashes) without FORGING a hash. The
# sibling types + cross-check below grant on a newly-spent epoch_label instead,
# applying the SAME pool/worker/session/collection binding checks MINUS the hash
# membership. The share-hash gate and PoolEvidenceSnapshot's
# `accepted_share_count == len(hashes)` invariant are left UNTOUCHED.
# --- Self-validated share authority (Milestone 0 / doc §2.2, §2.4) -----------
# The proxy pool makes ALICE'S OWN re-hash the credit authority for the share-hash
# legs (XMR/RVN/LTC), replacing "trust the upstream accepted-share count" with
# "trust Alice's validator" (doc §3 Q1, RESOLVED: self-re-hash PRIMARY + SUFFICIENT).
# The upstream pool never even sees pool-diff shares, so self-validation is the
# ONLY model that can credit the pool-diff stream. The gate below applies the SAME
# pool/worker/session/collection-address binding checks + ``pool_result=="accepted"``
# as :func:`cross_check_pool_authority`, but GRANTS on Alice's own
# ``canonical_share_hash`` matching the proof (the value the validator computed and
# carried into the evidence) AND a positive validated ``share_difficulty`` — there
# is NO ``accepted_share_hashes`` membership lookup against an upstream pool. It
# returns the SAME :class:`PoolEvidenceAuthorityResult` shape so the evaluator's
# downstream handling is byte-for-byte identical to the other two lanes.
SELF_VALIDATED_AUTHORITY_CONFIRMED = "SELF_VALIDATED_AUTHORITY_CONFIRMED"
SELF_VALIDATED_AUTHORITY_HASH_MISMATCH = "SELF_VALIDATED_AUTHORITY_HASH_MISMATCH"
SELF_VALIDATED_AUTHORITY_NON_POSITIVE_DIFFICULTY = (
    "SELF_VALIDATED_AUTHORITY_NON_POSITIVE_DIFFICULTY"
)
SELF_VALIDATED_AUTHORITY_CLIENT_NONACCEPTED = "SELF_VALIDATED_AUTHORITY_CLIENT_NONACCEPTED"
SELF_VALIDATED_AUTHORITY_POOL_MISMATCH = "SELF_VALIDATED_AUTHORITY_POOL_MISMATCH"
SELF_VALIDATED_AUTHORITY_WORKER_MISMATCH = "SELF_VALIDATED_AUTHORITY_WORKER_MISMATCH"
SELF_VALIDATED_AUTHORITY_SESSION_MISMATCH = "SELF_VALIDATED_AUTHORITY_SESSION_MISMATCH"
SELF_VALIDATED_AUTHORITY_COLLECTION_ADDRESS_MISMATCH = (
    "SELF_VALIDATED_AUTHORITY_COLLECTION_ADDRESS_MISMATCH"
)

PRL_EPOCH_AUTHORITY_CONFIRMED = "PRL_EPOCH_AUTHORITY_CONFIRMED"
PRL_EPOCH_AUTHORITY_NO_NEW_EPOCH = "PRL_EPOCH_AUTHORITY_NO_NEW_EPOCH"
PRL_EPOCH_AUTHORITY_CLIENT_NONACCEPTED = "PRL_EPOCH_AUTHORITY_CLIENT_NONACCEPTED"
PRL_EPOCH_AUTHORITY_POOL_MISMATCH = "PRL_EPOCH_AUTHORITY_POOL_MISMATCH"
PRL_EPOCH_AUTHORITY_WORKER_MISMATCH = "PRL_EPOCH_AUTHORITY_WORKER_MISMATCH"
PRL_EPOCH_AUTHORITY_SESSION_MISMATCH = "PRL_EPOCH_AUTHORITY_SESSION_MISMATCH"
PRL_EPOCH_AUTHORITY_COLLECTION_ADDRESS_MISMATCH = (
    "PRL_EPOCH_AUTHORITY_COLLECTION_ADDRESS_MISMATCH"
)

#: Scale factor turning a pearlhash epoch ``share`` (a small fraction in roughly
#: [0, 1] — this account's fraction of the epoch's useful work) into a positive,
#: integer-ish ``rewardable_score`` on the SAME footing as the other lanes'
#: per-accepted-share weight (each share there scores ~1; see
#: ``RECONSTRUCTED_SHARE_DIFFICULTY``). With this default a small but non-trivial
#: hourly work share (~1%) scores ~10 and a dominant share (~50%) scores ~500, so a
#: typical epoch lands a handful-to-hundreds of credit units — comparable in
#: magnitude to a poll's worth of per-share credit elsewhere. CREDIT-ONLY and
#: lane-budget-normalized downstream (settlement divides each lane budget pro-rata
#: by these scores), so the ABSOLUTE value only sets PRL's internal granularity.
#: OWNER INPUT NEEDED: tune at deploy once real pearlhash epoch ``share`` values
#: are observed (doc §2.4). A flat unit (Decimal("1")) is used as the fallback when
#: an epoch's ``share`` is absent/zero (see :func:`prl_epoch_rewardable_score`).
PRL_EPOCH_SHARE_SCALE = Decimal("1000")

#: Flat-unit fallback magnitude for a matured epoch whose ``share`` is absent/zero
#: (so a matured epoch always credits SOMETHING, just without proportionality).
#: NOT ``amount`` — that is the PRL-denominated payout and is token/price-coupled,
#: which a credit-only lane must not track (doc §2.4).
PRL_EPOCH_FLAT_UNIT_SCORE = Decimal("1")

_SUPPORTED_SOURCE_TYPES = frozenset({"fixture", "manual", "export"})
_REJECTED_REASON_BY_RESULT: dict[str, str] = {
    "rejected": POOL_AUTHORITY_REJECTED_SHARE,
    "stale": POOL_AUTHORITY_STALE_SHARE,
    "duplicate": POOL_AUTHORITY_DUPLICATE_SHARE,
    "invalid": POOL_AUTHORITY_INVALID_SHARE,
}


@dataclass(frozen=True, slots=True)
class RejectedPoolShareEvidence:
    share_hash: str
    pool_result: PoolRejectedShareResult

    def __post_init__(self) -> None:
        validate_sha256(self.share_hash, field_name="share_hash")
        if self.pool_result not in _REJECTED_REASON_BY_RESULT:
            raise ValueError("pool_result must be rejected, stale, duplicate, or invalid")


@dataclass(frozen=True, slots=True)
class PoolEvidenceAuthority:
    pool_id: str
    worker_name: str
    session_id: str
    accepted_share_hashes: tuple[str, ...]
    rejected_share_hashes: tuple[str, ...]
    generated_at: datetime
    source_type: EvidenceSourceType
    alice_collection_address: str | None = None
    rejected_share_results: tuple[RejectedPoolShareEvidence, ...] = ()

    def __post_init__(self) -> None:
        required = (self.pool_id, self.worker_name, self.session_id, self.source_type)
        if any(not value for value in required):
            raise ValueError("pool evidence authority fields must be non-empty")
        if self.source_type not in _SUPPORTED_SOURCE_TYPES:
            raise ValueError("source_type must be fixture, manual, or export")
        for field_name, value in (
            ("pool_id", self.pool_id),
            ("worker_name", self.worker_name),
            ("session_id", self.session_id),
        ):
            ensure_no_raw_secret(value, field_name=field_name)
        if self.source_type == "export" and self.alice_collection_address is None:
            raise ValueError("export pool evidence must bind alice_collection_address")
        if self.alice_collection_address is not None:
            ensure_no_raw_secret(
                self.alice_collection_address,
                field_name="alice_collection_address",
            )
        validate_aware_timestamp("generated_at", self.generated_at)

        accepted = _validated_unique_hashes(
            self.accepted_share_hashes,
            field_name="accepted_share_hashes",
        )
        rejected = _validated_unique_hashes(
            self.rejected_share_hashes,
            field_name="rejected_share_hashes",
        )
        if accepted & rejected:
            raise ValueError("accepted and rejected hashes must not overlap")

        rejected_detail_hashes = set()
        for detail in self.rejected_share_results:
            if detail.share_hash not in rejected:
                raise ValueError("rejected_share_results must reference rejected hashes")
            if detail.share_hash in rejected_detail_hashes:
                raise ValueError("rejected_share_results must be unique per hash")
            rejected_detail_hashes.add(detail.share_hash)

    def rejected_reason_for(self, share_hash: str) -> str:
        for detail in self.rejected_share_results:
            if detail.share_hash == share_hash:
                return _REJECTED_REASON_BY_RESULT[detail.pool_result]
        return POOL_AUTHORITY_REJECTED_SHARE


@dataclass(frozen=True, slots=True)
class PoolEvidenceAuthorityResult:
    status: PoolEvidenceAuthorityStatus
    rewardable_candidate: bool
    reason_code: str
    canonical_share_hash: str

    @property
    def matched(self) -> bool:
        return self.status == "matched"


def cross_check_pool_authority(
    proof: MiningShareProof,
    evidence: PoolEvidenceAuthority,
) -> PoolEvidenceAuthorityResult:
    share_hash = canonical_share_hash(proof)
    if proof.pool_id != evidence.pool_id:
        return _rejected(POOL_AUTHORITY_POOL_MISMATCH, share_hash)
    if proof.pool_worker_name != evidence.worker_name:
        return _rejected(POOL_AUTHORITY_WORKER_MISMATCH, share_hash)
    if proof.session_id != evidence.session_id:
        return _rejected(POOL_AUTHORITY_SESSION_MISMATCH, share_hash)
    if (
        evidence.alice_collection_address is not None
        and proof.alice_collection_address != evidence.alice_collection_address
    ):
        return _rejected(POOL_AUTHORITY_COLLECTION_ADDRESS_MISMATCH, share_hash)
    if proof.pool_result != "accepted":
        return _rejected(POOL_AUTHORITY_CLIENT_NONACCEPTED, share_hash)
    if share_hash in evidence.rejected_share_hashes:
        return _rejected(evidence.rejected_reason_for(share_hash), share_hash)
    if share_hash not in evidence.accepted_share_hashes:
        return _under_review(POOL_AUTHORITY_ACCEPTED_ABSENT, share_hash)
    return PoolEvidenceAuthorityResult(
        status="matched",
        rewardable_candidate=True,
        reason_code=POOL_AUTHORITY_CONFIRMED,
        canonical_share_hash=share_hash,
    )


@dataclass(frozen=True, slots=True)
class SelfValidatedShareAuthority:
    """Self-validated share evidence — Alice's OWN re-hash is the authority.

    Emitted by the :class:`ProxyPoolEvidenceProvider` when it drained one un-spent
    validated share for ``(pool_id, worker_name)`` from the durable
    ``ValidatedShareStore``. It carries the SAME pool/worker/session/collection
    binding fields the share-hash authority carries (for the identical cross-check),
    plus Alice's OWN ``canonical_share_hash`` (the value Alice's validator computed
    for the share) and the validated ``share_difficulty`` (the real credit
    magnitude, carried through the ``rewardable_score_override`` seam). It is a
    SIBLING of :class:`PoolEvidenceAuthority`; it is NOT a
    :class:`PoolEvidenceAuthority` and never flows through the upstream share-hash
    gate. There is NO ``accepted_share_hashes`` set — self-validation needs no
    upstream lookup (doc §2.2, §2.4, §3 Q1).
    """

    pool_id: str
    worker_name: str
    session_id: str
    alice_collection_address: str
    canonical_share_hash: str
    share_difficulty: Decimal
    generated_at: datetime
    source_type: EvidenceSourceType

    def __post_init__(self) -> None:
        required = (
            self.pool_id,
            self.worker_name,
            self.session_id,
            self.alice_collection_address,
            self.source_type,
        )
        if any(not value for value in required):
            raise ValueError("self-validated share authority fields must be non-empty")
        if self.source_type not in _SUPPORTED_SOURCE_TYPES:
            raise ValueError("source_type must be fixture, manual, or export")
        for field_name, value in (
            ("pool_id", self.pool_id),
            ("worker_name", self.worker_name),
            ("session_id", self.session_id),
            ("alice_collection_address", self.alice_collection_address),
        ):
            ensure_no_raw_secret(value, field_name=field_name)
        validate_sha256(self.canonical_share_hash, field_name="canonical_share_hash")
        if not isinstance(self.share_difficulty, Decimal):
            raise TypeError("share_difficulty must be Decimal")
        validate_aware_timestamp("generated_at", self.generated_at)


def cross_check_self_validated_share(
    proof: MiningShareProof,
    evidence: SelfValidatedShareAuthority,
) -> PoolEvidenceAuthorityResult:
    """Self-validated gate: SAME binding checks as the share-hash gate, NO upstream.

    Applies the identical pool/worker/session/collection-address binding checks and
    the same ``proof.pool_result == "accepted"`` requirement as
    :func:`cross_check_pool_authority`, then GRANTS iff the evidence's
    ``canonical_share_hash`` (the value Alice's validator computed) equals the
    proof's recomputed :func:`canonical_share_hash` AND the validated
    ``share_difficulty`` is strictly positive — there is NO
    ``accepted_share_hashes`` membership test against an upstream pool (doc §2.2 /
    §3 Q1). Returns the SAME :class:`PoolEvidenceAuthorityResult` shape as the
    share-hash gate so the evaluator's downstream handling is byte-for-byte
    identical.
    """

    share_hash = canonical_share_hash(proof)
    if proof.pool_id != evidence.pool_id:
        return _rejected(SELF_VALIDATED_AUTHORITY_POOL_MISMATCH, share_hash)
    if proof.pool_worker_name != evidence.worker_name:
        return _rejected(SELF_VALIDATED_AUTHORITY_WORKER_MISMATCH, share_hash)
    if proof.session_id != evidence.session_id:
        return _rejected(SELF_VALIDATED_AUTHORITY_SESSION_MISMATCH, share_hash)
    if proof.alice_collection_address != evidence.alice_collection_address:
        return _rejected(SELF_VALIDATED_AUTHORITY_COLLECTION_ADDRESS_MISMATCH, share_hash)
    if proof.pool_result != "accepted":
        return _rejected(SELF_VALIDATED_AUTHORITY_CLIENT_NONACCEPTED, share_hash)
    if evidence.canonical_share_hash != share_hash:
        # Binds THIS credit unit to THIS validated share: a worker's share can never
        # be credited against another's validated record.
        return _under_review(SELF_VALIDATED_AUTHORITY_HASH_MISMATCH, share_hash)
    if evidence.share_difficulty <= Decimal("0"):
        return _under_review(SELF_VALIDATED_AUTHORITY_NON_POSITIVE_DIFFICULTY, share_hash)
    return PoolEvidenceAuthorityResult(
        status="matched",
        rewardable_candidate=True,
        reason_code=SELF_VALIDATED_AUTHORITY_CONFIRMED,
        canonical_share_hash=share_hash,
    )


@dataclass(frozen=True, slots=True)
class PrlEpochEvidenceAuthority:
    """PRL (pearlhash) epoch-credit evidence — the parallel, NO-share-hash authority.

    Emitted by :class:`PearlhashPoolEvidenceProvider` ONLY when it spent a
    never-before-seen ``epoch_label`` for ``(pool_id, alice_collection_address)``
    (the durable epoch cursor's one-credit-per-new-epoch guarantee). It carries the
    SAME pool/worker/session/collection binding fields the share-hash authority
    carries (for the identical cross-check), plus the granted epoch's label +
    magnitude signal. It is a SIBLING of :class:`PoolEvidenceAuthority`; it is NOT a
    :class:`PoolEvidenceAuthority` and never flows through the share-hash gate.

    ``worker_name`` is carried for parity/audit only — pearlhash's account signal is
    per-ADDRESS, so the credit key is ``(pool, address, epoch_label)``, not the
    worker (doc §0.2). ``epoch_share`` is the per-epoch fractional-work signal
    (``None`` when absent); ``epoch_amount`` is the PRL-denominated payout (carried
    for audit only — NEVER used as the credit magnitude; see
    :data:`PRL_EPOCH_SHARE_SCALE`).
    """

    pool_id: str
    worker_name: str
    session_id: str
    alice_collection_address: str
    epoch_label: str
    epoch_share: Decimal | None
    epoch_amount: Decimal
    generated_at: datetime
    source_type: EvidenceSourceType

    def __post_init__(self) -> None:
        required = (
            self.pool_id,
            self.worker_name,
            self.session_id,
            self.alice_collection_address,
            self.epoch_label,
            self.source_type,
        )
        if any(not value for value in required):
            raise ValueError("prl epoch evidence authority fields must be non-empty")
        if self.source_type not in _SUPPORTED_SOURCE_TYPES:
            raise ValueError("source_type must be fixture, manual, or export")
        for field_name, value in (
            ("pool_id", self.pool_id),
            ("worker_name", self.worker_name),
            ("session_id", self.session_id),
            ("alice_collection_address", self.alice_collection_address),
            ("epoch_label", self.epoch_label),
        ):
            ensure_no_raw_secret(value, field_name=field_name)
        for field_name, value in (
            ("epoch_share", self.epoch_share),
            ("epoch_amount", self.epoch_amount),
        ):
            if value is not None and not isinstance(value, Decimal):
                raise TypeError(f"{field_name} must be Decimal or None")
        if self.epoch_share is not None and self.epoch_share < Decimal("0"):
            raise ValueError("epoch_share must be non-negative")
        validate_aware_timestamp("generated_at", self.generated_at)


def prl_epoch_rewardable_score(evidence: PrlEpochEvidenceAuthority) -> Decimal:
    """Magnitude for a granted PRL epoch: ``epoch_share * PRL_EPOCH_SHARE_SCALE``.

    Falls back to the flat unit (:data:`PRL_EPOCH_FLAT_UNIT_SCORE`) when the epoch's
    ``share`` is absent or non-positive, so a matured epoch always credits a
    strictly positive score (the ledger's ``rewardable_score > 0`` guard then
    passes). Deliberately NOT a function of ``amount`` (the token-coupled payout).
    See doc §2.4 + :data:`PRL_EPOCH_SHARE_SCALE`.
    """

    share = evidence.epoch_share
    if share is None or share <= Decimal("0"):
        return PRL_EPOCH_FLAT_UNIT_SCORE
    scaled = share * PRL_EPOCH_SHARE_SCALE
    if scaled <= Decimal("0"):
        return PRL_EPOCH_FLAT_UNIT_SCORE
    return scaled


def cross_check_prl_epoch_authority(
    proof: MiningShareProof,
    evidence: PrlEpochEvidenceAuthority,
) -> PoolEvidenceAuthorityResult:
    """PRL epoch gate: SAME binding checks as the share-hash gate MINUS the hash.

    Applies the identical pool/worker/session/collection-address binding checks and
    the same ``proof.pool_result == "accepted"`` requirement as
    :func:`cross_check_pool_authority`, then GRANTS on a non-empty granted
    ``epoch_label`` — there is NO ``accepted_share_hashes`` membership test, because
    pearlhash exposes no per-share hash (doc §2.3, Option A). The provider only
    constructs a :class:`PrlEpochEvidenceAuthority` when it actually spent a NEW
    epoch, so reaching this function already implies a fresh epoch; the non-empty
    ``epoch_label`` check is a belt-and-braces guard. Returns the SAME
    :class:`PoolEvidenceAuthorityResult` shape as the share-hash gate so the
    evaluator's downstream handling is byte-for-byte identical. The
    ``canonical_share_hash`` on the result is the proof's INERT reconstructed hash
    (the PRL gate never inspects it; it is not forged and not matched).
    """

    share_hash = canonical_share_hash(proof)
    if proof.pool_id != evidence.pool_id:
        return _rejected(PRL_EPOCH_AUTHORITY_POOL_MISMATCH, share_hash)
    if proof.pool_worker_name != evidence.worker_name:
        return _rejected(PRL_EPOCH_AUTHORITY_WORKER_MISMATCH, share_hash)
    if proof.session_id != evidence.session_id:
        return _rejected(PRL_EPOCH_AUTHORITY_SESSION_MISMATCH, share_hash)
    if proof.alice_collection_address != evidence.alice_collection_address:
        return _rejected(PRL_EPOCH_AUTHORITY_COLLECTION_ADDRESS_MISMATCH, share_hash)
    if proof.pool_result != "accepted":
        return _rejected(PRL_EPOCH_AUTHORITY_CLIENT_NONACCEPTED, share_hash)
    if not evidence.epoch_label:
        return _under_review(PRL_EPOCH_AUTHORITY_NO_NEW_EPOCH, share_hash)
    return PoolEvidenceAuthorityResult(
        status="matched",
        rewardable_candidate=True,
        reason_code=PRL_EPOCH_AUTHORITY_CONFIRMED,
        canonical_share_hash=share_hash,
    )


def _validated_unique_hashes(values: tuple[str, ...], *, field_name: str) -> set[str]:
    unique = set(values)
    if len(unique) != len(values):
        raise ValueError(f"{field_name} must be unique")
    for value in values:
        validate_sha256(value, field_name=field_name)
    return unique


def _rejected(reason_code: str, share_hash: str) -> PoolEvidenceAuthorityResult:
    return PoolEvidenceAuthorityResult(
        status="rejected",
        rewardable_candidate=False,
        reason_code=reason_code,
        canonical_share_hash=share_hash,
    )


def _under_review(reason_code: str, share_hash: str) -> PoolEvidenceAuthorityResult:
    return PoolEvidenceAuthorityResult(
        status="under_review",
        rewardable_candidate=False,
        reason_code=reason_code,
        canonical_share_hash=share_hash,
    )

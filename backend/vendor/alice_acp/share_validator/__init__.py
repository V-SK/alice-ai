"""Milestone 1 — the SHARE-VALIDATOR (Alice's Oracle / Verifier).

This is the missing PoW oracle promised by ``docs/PROXY-POOL-DESIGN.md`` §2.2: the
component that makes **Alice's own re-hash the source of truth** for the proxy
pool's share-hash legs (XMR/RVN/LTC). Milestone 0 (production HEAD ``014dcb0``)
shipped the *seam* — the :class:`ValidatedShare` contract, the
:class:`ValidatedShareStore` (InMemory + Jsonl), the
:class:`ProxyPoolEvidenceProvider`, the :class:`SelfValidatedShareAuthority` +
:func:`cross_check_self_validated_share`. This package is the *producer* that
POPULATES the ``ValidatedShareStore`` with re-hashed, deduped, difficulty-classified
shares so that merged provider can credit them.

The pipeline (doc §2.2):

    raw submission  (algorithm, job, nonce, extranonce, identity, two targets)
        -> ShareVerifier.verify(VerifyWork) -> VerifyOutcome
           (result_hash, result_difficulty = MAX_TARGET / H, valid, reason)
        -> classify:  d >= pool_target  => is_share    (the CREDITED unit)
                      d >= net_target   => is_solution (additionally relay-worthy)
        -> dedup on  sha256(algorithm‖seed‖header‖nonce‖extranonce‖worker_name)
                      via a durable, fsync'd, replay-on-construct store
        -> Accepted = received - invalid - duplicate
        -> for is_share: emit ONE ValidatedShare (the M0 contract) into the
           ValidatedShareStore, with canonical_share_hash computed CONSISTENTLY
           with how server.py::_reconstruct_share_proof + canonical_share_hash
           build the proof — so cross_check_self_validated_share confirms (not
           HASH_MISMATCH).

THE LOAD-BEARING DISCIPLINE: SELF-REPORT NEVER COUNTS. The credited difficulty is
ALWAYS Alice's recomputed ``result_difficulty`` (the §4.4.2 / Qubic
``_buffer[ownComputorIdx]=0`` discipline made mechanical). The rig's claimed
difficulty is copied nowhere into the credited magnitude.

CREDIT-ONLY: the validator sets no reward/payout/chain symbol; ``paid_acu`` is
structurally unreachable from here (it emits only opaque facts into the store);
``ensure_no_raw_secret`` is applied to every identity/work string.

LICENSE: Scrypt via ``hashlib.scrypt`` (stdlib); RandomX via FFI over ``librandomx``
(BSD-3 — NOT xmrig/GPL); KawPoW via a reference binding — NO GPL is imported. Where a
native lib cannot be built/installed in the current environment the binding STRUCTURE
is implemented and the verifier FAILS CLOSED (it never fakes a hash as valid).
"""

from __future__ import annotations

from alice_acp.share_validator.dedup_store import (
    InMemoryShareDedupStore,
    JsonlShareDedupStore,
    ShareDedupClaim,
    ShareDedupDecision,
    ShareDedupStore,
    ShareDedupUnavailable,
    submission_dedup_key,
)
from alice_acp.share_validator.types import (
    MAX_TARGET,
    RawSubmission,
    ShareVerifier,
    SubmissionIdentity,
    ValidationCounters,
    ValidationDecision,
    VerifierUnavailable,
    VerifyOutcome,
    VerifyWork,
    difficulty_from_hash,
)
from alice_acp.share_validator.validator import (
    SHARE_VALIDATOR_INVALID_POW,
    SHARE_VALIDATOR_LOW_DIFFICULTY,
    SHARE_VALIDATOR_VERIFIER_UNAVAILABLE,
    ShareValidator,
    reconstructed_canonical_share_hash,
)
from alice_acp.share_validator.verifiers.kawpow import (
    KAWPOW_BACKEND_UNAVAILABLE,
    LocalKawPowVerifier,
    librandomx_kawpow_available,
)
from alice_acp.share_validator.verifiers.randomx import (
    RANDOMX_BACKEND_UNAVAILABLE,
    LocalRandomXVerifier,
    librandomx_available,
)
from alice_acp.share_validator.verifiers.scrypt import (
    LTC_DIFF1_TARGET,
    LocalScryptVerifier,
    ScryptParams,
)

__all__ = [
    "KAWPOW_BACKEND_UNAVAILABLE",
    "LTC_DIFF1_TARGET",
    # types / protocol
    "MAX_TARGET",
    "RANDOMX_BACKEND_UNAVAILABLE",
    "SHARE_VALIDATOR_INVALID_POW",
    "SHARE_VALIDATOR_LOW_DIFFICULTY",
    "SHARE_VALIDATOR_VERIFIER_UNAVAILABLE",
    # dedup store
    "InMemoryShareDedupStore",
    "JsonlShareDedupStore",
    "LocalKawPowVerifier",
    "LocalRandomXVerifier",
    # verifiers
    "LocalScryptVerifier",
    "RawSubmission",
    "ScryptParams",
    "ShareDedupClaim",
    "ShareDedupDecision",
    "ShareDedupStore",
    "ShareDedupUnavailable",
    # validator core
    "ShareValidator",
    "ShareVerifier",
    "SubmissionIdentity",
    "ValidationCounters",
    "ValidationDecision",
    "VerifierUnavailable",
    "VerifyOutcome",
    "VerifyWork",
    "difficulty_from_hash",
    "librandomx_available",
    "librandomx_kawpow_available",
    "reconstructed_canonical_share_hash",
    "submission_dedup_key",
]

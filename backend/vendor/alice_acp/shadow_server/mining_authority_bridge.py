"""Server-side bridge from mining-proof authority evaluation to the shadow ledger.

Phase B closes the MINING proof -> credit loop on the SERVER side, CREDIT-ONLY,
mirroring how Phase A closed the AI inference credit loop. The shadow ledger
(:mod:`alice_acp.shadow_server.ledger`) already consumes a
:class:`~alice_acp.shadow_server.types.MiningProofAuthorityResult` on the
``authority_result`` field of a :class:`MiningProofIngestRequest`; what was
missing was the *server* component that actually produces that result from the
authoritative proof-authority evaluator.

This bridge does exactly three things, as specified for Phase B:

1. Reconstruct the domain objects (``SignedMiningSession`` + ``MiningShareProof``
   + ``PoolEvidenceAuthority | None``) from server-trusted inputs.
2. Call :func:`evaluate_public_beta_proof_authority` (the single source of truth
   for proof-authority decisions; this module never re-implements any guard).
3. Adapt the resulting :class:`ProofAuthorityDecision` into the
   :class:`MiningProofAuthorityResult` the ledger consumes, deriving the
   ``rewardable_score`` from the validated share.

Hard constraints honoured here (load-bearing):

* CREDIT-ONLY. This module never sets ``live_reward_enabled`` /
  ``payout_executor_enabled``, never emits a payout address, never performs a
  chain write, and never touches ``paid_acu`` (which stays ``"0"`` end to end).
* No guard is removed or weakened. Authority is only ever *granted* by the
  authoritative evaluator returning ``rewardable_candidate``; everything else
  flows through unchanged so the ledger keeps it ``under_review`` / ``rejected``.
* No real pool/network call happens *in this module*. Pool evidence is supplied
  through the :class:`PoolEvidenceProvider` seam. The default provider yields
  ``None`` (so a proof with no evidence stays ``under_review`` -- it is *never*
  auto-passed). The deterministic offline test shim lives in
  :mod:`alice_acp.shadow_server.fake_pool_evidence`, and the Phase H_b REAL
  per-pool providers (which DO poll the upstream pool API server-side, but only
  through an injectable HTTP client) live in
  :mod:`alice_acp.shadow_server.pool_evidence_providers`.

The Phase B "B2" follow-up -- real pool-API evidence for all four lanes -- has
landed as Phase H_b in
:mod:`alice_acp.shadow_server.pool_evidence_providers`. This bridge is unchanged
by it: H_b plugs new :class:`PoolEvidenceProvider` implementations into the SAME
seam, so every authority/dedup guard below still runs exactly once.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Protocol

from alice_acp.mining_proofs.authority import (
    ProofAuthorityDecision,
    evaluate_public_beta_proof_authority,
)
from alice_acp.mining_proofs.collector import (
    DEFAULT_DIFFICULTY_JUMP_FACTOR,
    DEFAULT_MAX_FUTURE_SKEW,
    DEFAULT_MINIMUM_SHARE_DIFFICULTY,
)
from alice_acp.mining_proofs.pool_evidence import (
    PoolEvidenceAuthority,
    PrlEpochEvidenceAuthority,
    SelfValidatedShareAuthority,
)
from alice_acp.mining_proofs.types import MiningShareProof
from alice_acp.mining_session.types import SignedMiningSession
from alice_acp.shadow_server.types import (
    ZERO_DECIMAL,
    MiningProofAuthorityResult,
)

# --- B2 follow-up seam (LANDED as Phase H_b) ---------------------------------
# The real pool-API integration (query the upstream pool's per-worker accepted
# shares server-side and build a PoolEvidenceAuthority from the server-read
# delta) is implemented in Phase H_b as the per-lane providers in
# :mod:`alice_acp.shadow_server.pool_evidence_providers`. Those providers make
# the only real network calls (through an injectable HTTP client; tests use
# fixtures) and plug into the PoolEvidenceProvider seam below. THIS module still
# makes no network call and still defaults to NoPoolEvidenceProvider, so an
# unconfigured deployment stays fail-closed (every proof under_review).
B2_REAL_POOL_API_TODO = (
    "B2 landed as Phase H_b: real per-lane pool-API evidence providers live in "
    "alice_acp.shadow_server.pool_evidence_providers (HTTP client injectable; "
    "no network calls are made in this bridge module)."
)

#: ``server_pool_authority`` marks results that were produced by this
#: server-side bridge (as opposed to anything a client might have asserted).
#: It matches the default ``source_type`` on :class:`MiningProofAuthorityResult`.
SERVER_AUTHORITY_SOURCE_TYPE = "server_pool_authority"

#: Documented assumption for owner confirmation. The credit score for an
#: accepted mining share is derived 1:1 from the *validated* share difficulty
#: (the same quantity the strong collector accumulates as
#: ``total_share_difficulty`` and that the existing HTTP contract carries as
#: ``share_difficulty`` / ``verified_score``). This keeps mining credit on the
#: same "more verified work => more score" footing as inference credit, and the
#: settlement layer (70/15/15 pool split + lane budgets) divides the lane budget
#: pro-rata by these scores. OWNER INPUT NEEDED: confirm difficulty is the
#: intended reward-weight unit, or supply the production weighting curve.
REWARDABLE_SCORE_DERIVATION = "validated_share_difficulty"


class PoolEvidenceProvider(Protocol):
    """Supplies authoritative pool evidence for a reconstructed share proof.

    Implementations MUST be side-effect free with respect to the network in
    Phase B. Returning ``None`` means "no authoritative evidence available",
    which the evaluator treats as ``under_review`` (evidence required) -- it is
    never interpreted as an acceptance.
    """

    def evidence_for(
        self,
        session: SignedMiningSession,
        proof: MiningShareProof,
    ) -> PoolEvidenceAuthority | PrlEpochEvidenceAuthority | SelfValidatedShareAuthority | None:
        ...


@dataclass(frozen=True, slots=True)
class NoPoolEvidenceProvider:
    """Default provider: never produces evidence.

    With this provider every proof stays ``under_review``
    (``PROOF_AUTHORITY_EVIDENCE_REQUIRED``), exactly preserving the pre-Phase-B
    server behaviour. This is the production default until the B2 real pool-API
    fetcher is built.
    """

    def evidence_for(
        self,
        session: SignedMiningSession,
        proof: MiningShareProof,
    ) -> PoolEvidenceAuthority | PrlEpochEvidenceAuthority | SelfValidatedShareAuthority | None:
        return None


#: Module-level singleton used as the default evidence provider. Using a
#: singleton (rather than constructing in the argument default) keeps the
#: "production default = no auto-pass" behaviour while satisfying ruff B008.
NO_POOL_EVIDENCE_PROVIDER: PoolEvidenceProvider = NoPoolEvidenceProvider()


def derive_rewardable_score(
    decision: ProofAuthorityDecision,
    proof: MiningShareProof,
) -> Decimal:
    """Derive the ledger ``rewardable_score`` for an authority decision.

    Only an ``accepted`` (``rewardable_candidate``) decision yields a positive
    score; every other status yields ``0`` so the ledger's
    ``rewardable_score > 0`` guard keeps it ``under_review``. The score is taken
    1:1 from the proof's *validated* ``share_difficulty`` (see
    :data:`REWARDABLE_SCORE_DERIVATION`) -- the authoritative evaluator has
    already enforced the difficulty floor, target match and pool confirmation by
    the time a decision is ``accepted``. The proof contract guarantees
    ``share_difficulty`` is a strictly positive ``Decimal``, so an accepted
    decision always derives a strictly positive score.

    OWNER INPUT NEEDED: confirm share difficulty is the intended reward-weight
    unit (it mirrors ``ProofCollectorSummary.total_share_difficulty`` and the
    existing HTTP ``share_difficulty`` / ``verified_score`` contract), or supply
    the production weighting curve. This is the documented Phase B assumption.
    """

    if not decision.accepted:
        return ZERO_DECIMAL
    # PRL epoch path (doc §2.4): when the decision carries a score override, the
    # magnitude is the validated EPOCH work (``epoch.share * SHARE_SCALE`` with a
    # flat-unit fallback), NOT the reconstructed proof's flat ``share_difficulty``.
    # This is the single PRL-specific seam in the otherwise unchanged derivation;
    # every share-hash lane leaves ``rewardable_score_override`` None and keeps
    # deriving 1:1 from ``share_difficulty`` exactly as before.
    if decision.rewardable_score_override is not None:
        override = decision.rewardable_score_override
        if override <= ZERO_DECIMAL:
            return ZERO_DECIMAL
        return override
    score = proof.share_difficulty
    if score <= ZERO_DECIMAL:
        # Defensive: an accepted decision should never carry a non-positive
        # difficulty (the proof contract forbids it), but never emit a
        # non-positive "accepted" score.
        return ZERO_DECIMAL
    return score


def adapt_decision_to_authority_result(
    decision: ProofAuthorityDecision,
    proof: MiningShareProof,
) -> MiningProofAuthorityResult:
    """Adapt a :class:`ProofAuthorityDecision` to a ledger authority result.

    Mapping (decision field -> result field):

    * ``status`` -> ``status`` (the proof-authority statuses
      ``rewardable_candidate`` / ``under_review`` / ``rejected`` /
      ``nonrewardable`` are all valid ledger ``AuthorityStatus`` values, and the
      ledger reads ``.accepted`` as ``status in {accepted, rewardable_candidate}``
      -- so ``rewardable_candidate`` is the only status that grants credit).
    * ``reason_code`` -> ``reason_code`` (verbatim, for audit parity).
    * ``canonical_share_hash`` -> ``canonical_share_hash`` (the ledger
      cross-checks this against any request-supplied hash and dedups on it).
    * proof-derived score -> ``rewardable_score`` (see
      :func:`derive_rewardable_score`).
    * ``source_type`` is fixed to :data:`SERVER_AUTHORITY_SOURCE_TYPE` to mark
      this as server-produced, never client-asserted.
    """

    return MiningProofAuthorityResult(
        status=decision.status,
        reason_code=decision.reason_code,
        rewardable_score=derive_rewardable_score(decision, proof),
        canonical_share_hash=decision.canonical_share_hash,
        source_type=SERVER_AUTHORITY_SOURCE_TYPE,
    )


def evaluate_mining_proof_authority(
    session: SignedMiningSession,
    proof: MiningShareProof,
    *,
    observed_at: datetime,
    pool_evidence: PoolEvidenceAuthority
    | PrlEpochEvidenceAuthority
    | SelfValidatedShareAuthority
    | None,
    previous_share_difficulty: Decimal | None = None,
    minimum_share_difficulty: Decimal = DEFAULT_MINIMUM_SHARE_DIFFICULTY,
    difficulty_jump_factor: Decimal = DEFAULT_DIFFICULTY_JUMP_FACTOR,
    max_future_skew: timedelta = DEFAULT_MAX_FUTURE_SKEW,
) -> MiningProofAuthorityResult:
    """Evaluate proof authority on the server and adapt it for the ledger.

    This is the reusable bridge entry point: it delegates the whole decision to
    :func:`evaluate_public_beta_proof_authority` (so every difficulty / session
    / pool-evidence guard is enforced exactly once, in one place) and then
    adapts the decision. When ``pool_evidence`` is ``None`` the evaluator returns
    ``under_review`` (``PROOF_AUTHORITY_EVIDENCE_REQUIRED``); this bridge passes
    that straight through and never upgrades it.
    """

    decision = evaluate_public_beta_proof_authority(
        session,
        proof,
        observed_at=observed_at,
        pool_evidence=pool_evidence,
        previous_share_difficulty=previous_share_difficulty,
        minimum_share_difficulty=minimum_share_difficulty,
        difficulty_jump_factor=difficulty_jump_factor,
        max_future_skew=max_future_skew,
    )
    return adapt_decision_to_authority_result(decision, proof)


def authority_result_for_proof(
    session: SignedMiningSession,
    proof: MiningShareProof,
    *,
    observed_at: datetime,
    evidence_provider: PoolEvidenceProvider = NO_POOL_EVIDENCE_PROVIDER,
    previous_share_difficulty: Decimal | None = None,
    minimum_share_difficulty: Decimal = DEFAULT_MINIMUM_SHARE_DIFFICULTY,
    difficulty_jump_factor: Decimal = DEFAULT_DIFFICULTY_JUMP_FACTOR,
    max_future_skew: timedelta = DEFAULT_MAX_FUTURE_SKEW,
) -> MiningProofAuthorityResult:
    """End-to-end helper: fetch pool evidence via the provider, then evaluate.

    The default :class:`NoPoolEvidenceProvider` makes this preserve the prior
    server behaviour (every proof under_review). Tests inject a CONFIRMED
    provider; the real pool-API provider is the B2 follow-up
    (:data:`B2_REAL_POOL_API_TODO`).
    """

    pool_evidence = evidence_provider.evidence_for(session, proof)
    return evaluate_mining_proof_authority(
        session,
        proof,
        observed_at=observed_at,
        pool_evidence=pool_evidence,
        previous_share_difficulty=previous_share_difficulty,
        minimum_share_difficulty=minimum_share_difficulty,
        difficulty_jump_factor=difficulty_jump_factor,
        max_future_skew=max_future_skew,
    )

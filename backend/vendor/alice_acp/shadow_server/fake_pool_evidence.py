"""Deterministic, offline FAKE pool-evidence shim for the mining credit loop.

This is a TEST/DEV stand-in for the real pool-API evidence fetcher. It builds a
:class:`PoolEvidenceAuthority` that CONFIRMS a given reconstructed share proof
by deterministically deriving the authoritative fields from the proof itself
(same ``pool_id`` / ``worker_name`` / ``session_id`` / collection address, and
the proof's canonical share hash listed as accepted). It performs NO network
I/O and reads NO external state.

It exists so the Phase B server-side mining credit loop is testable end to end
(session issue -> proof ingest with authority wired -> ShadowWorkRecord ->
settle -> credit) without depending on a live mining pool. Replacing this shim
with a real, audited pool-API evidence source is the "B2" follow-up; see
:data:`alice_acp.shadow_server.mining_authority_bridge.B2_REAL_POOL_API_TODO`.

DO NOT use this provider in any production / reward-bearing path. It will
CONFIRM any structurally-valid proof, which is precisely why it is gated to
tests and local development only.
"""

from __future__ import annotations

from dataclasses import dataclass

from alice_acp.mining_proofs.canonical import canonical_share_hash
from alice_acp.mining_proofs.pool_evidence import (
    EvidenceSourceType,
    PoolEvidenceAuthority,
    RejectedPoolShareEvidence,
)
from alice_acp.mining_proofs.types import MiningShareProof
from alice_acp.mining_session.types import SignedMiningSession

#: Marks evidence built by this shim in audit/debug surfaces. ``"fixture"`` is
#: one of the source types accepted by :class:`PoolEvidenceAuthority`; using it
#: (rather than ``"export"``) keeps the shim from being mistaken for a real
#: pool export and avoids the export-only collection-address binding rule.
FAKE_POOL_EVIDENCE_SOURCE_TYPE: EvidenceSourceType = "fixture"


def fake_confirmed_pool_evidence(
    proof: MiningShareProof,
    *,
    source_type: EvidenceSourceType = FAKE_POOL_EVIDENCE_SOURCE_TYPE,
) -> PoolEvidenceAuthority:
    """Build deterministic CONFIRMED pool evidence for ``proof``.

    The returned evidence matches the proof on every identity field the
    cross-check enforces and lists the proof's canonical share hash as accepted,
    so :func:`cross_check_pool_authority` returns ``POOL_AUTHORITY_CONFIRMED``.
    This is a stand-in for the real pool-API response (B2); no network call is
    made.
    """

    share_hash = canonical_share_hash(proof)
    return PoolEvidenceAuthority(
        pool_id=proof.pool_id,
        worker_name=proof.pool_worker_name,
        session_id=proof.session_id,
        accepted_share_hashes=(share_hash,),
        rejected_share_hashes=(),
        rejected_share_results=(),
        generated_at=proof.accepted_at,
        source_type=source_type,
        alice_collection_address=proof.alice_collection_address,
    )


def fake_rejected_pool_evidence(
    proof: MiningShareProof,
    *,
    pool_result: str = "rejected",
    source_type: EvidenceSourceType = FAKE_POOL_EVIDENCE_SOURCE_TYPE,
) -> PoolEvidenceAuthority:
    """Build deterministic REJECTING pool evidence for ``proof`` (test helper).

    Lists the proof's canonical share hash as rejected so the authority decision
    is ``rejected``. Useful for proving the loop does NOT credit a pool-rejected
    share. No network call is made.
    """

    share_hash = canonical_share_hash(proof)
    return PoolEvidenceAuthority(
        pool_id=proof.pool_id,
        worker_name=proof.pool_worker_name,
        session_id=proof.session_id,
        accepted_share_hashes=(),
        rejected_share_hashes=(share_hash,),
        rejected_share_results=(RejectedPoolShareEvidence(share_hash, pool_result),),  # type: ignore[arg-type]
        generated_at=proof.accepted_at,
        source_type=source_type,
        alice_collection_address=proof.alice_collection_address,
    )


@dataclass(frozen=True, slots=True)
class FakeConfirmingPoolEvidenceProvider:
    """A :class:`PoolEvidenceProvider` that CONFIRMS every structurally-valid proof.

    Test/dev only -- a deterministic stand-in for the real pool-API provider
    (B2). It ignores ``session`` (the proof already carries the bound session
    identity) and returns :func:`fake_confirmed_pool_evidence` for the proof.
    """

    source_type: EvidenceSourceType = FAKE_POOL_EVIDENCE_SOURCE_TYPE

    def evidence_for(
        self,
        session: SignedMiningSession,
        proof: MiningShareProof,
    ) -> PoolEvidenceAuthority | None:
        return fake_confirmed_pool_evidence(proof, source_type=self.source_type)

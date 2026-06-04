from __future__ import annotations

from alice_acp.shadow_server.ledger import ShadowRewardLedger
from alice_acp.shadow_server.types import MiningProofIngestRequest, ProofIngestResult


def ingest_shadow_mining_proof(
    ledger: ShadowRewardLedger,
    request: MiningProofIngestRequest,
) -> ProofIngestResult:
    return ledger.ingest_mining_proof(request)

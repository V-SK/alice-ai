from __future__ import annotations

from alice_acp.shadow_server.ledger import ShadowRewardLedger
from alice_acp.shadow_server.types import InferenceCompletionRequest, ProofIngestResult


def complete_shadow_inference(
    ledger: ShadowRewardLedger,
    request: InferenceCompletionRequest,
) -> ProofIngestResult:
    return ledger.complete_inference(request)

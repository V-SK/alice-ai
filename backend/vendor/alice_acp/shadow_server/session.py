from __future__ import annotations

from alice_acp.shadow_server.ledger import ShadowRewardLedger
from alice_acp.shadow_server.types import SessionIssueResult, ShadowSessionIssueRequest


def issue_shadow_session(
    ledger: ShadowRewardLedger,
    request: ShadowSessionIssueRequest,
) -> SessionIssueResult:
    return ledger.issue_session(request)

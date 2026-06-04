from __future__ import annotations

from datetime import datetime

from alice_acp.mining_accounting import MiningShadowLedgerReport
from alice_acp.mining_dashboard.types import MinerDashboardReport
from alice_acp.mining_device import BackendCapabilityResult
from alice_acp.mining_proofs import (
    PROOF_COLLECTION_ADDRESS_MISMATCH,
    PROOF_DUPLICATE,
    PROOF_REPLAY_PAYLOAD_MISMATCH,
)
from alice_acp.mining_scheduler import SchedulerPreemptionDecision
from alice_acp.mining_session import (
    SESSION_EXPIRED,
    SignedMiningSession,
    validate_signed_mining_session,
)
from alice_acp.mining_supervisor import SupervisorSnapshot

DASHBOARD_SHADOW_ONLY = "DASHBOARD_SHADOW_ONLY"
DASHBOARD_LIVE_DISABLED = "DASHBOARD_LIVE_DISABLED"

_DUPLICATE_REPLAY_TAMPER_REASONS = frozenset(
    {
        PROOF_COLLECTION_ADDRESS_MISMATCH,
        PROOF_DUPLICATE,
        PROOF_REPLAY_PAYLOAD_MISMATCH,
    }
)


def build_miner_dashboard_report(
    *,
    session: SignedMiningSession,
    backend: BackendCapabilityResult,
    supervisor: SupervisorSnapshot,
    shadow_report: MiningShadowLedgerReport,
    ai_decision: SchedulerPreemptionDecision,
    observed_at: datetime,
) -> MinerDashboardReport:
    validation = validate_signed_mining_session(session, observed_at=observed_at)
    rejected_entries = tuple(
        entry for entry in shadow_report.entries if entry.status == "shadow_rejected"
    )
    not_counted_entries = tuple(
        entry for entry in shadow_report.entries if entry.status == "shadow_not_counted"
    )
    duplicate_replay_tamper_count = sum(
        1
        for entry in (*rejected_entries, *not_counted_entries)
        if entry.reason_code in _DUPLICATE_REPLAY_TAMPER_REASONS
    )
    return MinerDashboardReport(
        miner_passport_id=session.passport_id,
        device_backend_status=backend.status,
        device_backend_reason=backend.reason_code,
        mining_status=supervisor.state,
        pool_session_status=_pool_session_status(validation.accepted, validation.reason_code),
        pool_session_reason=validation.reason_code or "SESSION_VALID",
        accepted_shares=shadow_report.accepted_share_count,
        rejected_shares=len(rejected_entries),
        duplicate_replayed_tampered_rejected_count=duplicate_replay_tamper_count,
        estimated_mining_acu=shadow_report.total_mining_acu,
        estimated_reward_status="shadow_only",
        ai_preemption_status=ai_decision.action,
        ai_preemption_reason=ai_decision.reason_code,
        alice_collection_address=session.alice_collection_address,
        paid_acu=shadow_report.paid_acu,
        live_reward_enabled=False,
        miner_rvn_wallet_required=False,
    )


def _pool_session_status(accepted: bool, reason_code: str | None) -> str:
    if accepted:
        return "valid"
    if reason_code == SESSION_EXPIRED:
        return "expired"
    return "invalid"

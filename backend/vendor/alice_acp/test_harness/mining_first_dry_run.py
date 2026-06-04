from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import sqlalchemy as sa

from alice_acp.mining_accounting import MiningShadowLedgerReport, run_mining_shadow_accounting_flow
from alice_acp.mining_dashboard import MinerDashboardReport, build_miner_dashboard_report
from alice_acp.mining_device import BackendCapabilityResult
from alice_acp.mining_download import MiningDownloadContentContract
from alice_acp.mining_pool import PoolShareEvent, build_share_proof
from alice_acp.mining_pool.types import AcceptedShareProof
from alice_acp.mining_scheduler import (
    AiDemandSignal,
    DeviceEligibility,
    SchedulerPreemptionDecision,
    apply_preemption_decision,
    decide_preemption,
)
from alice_acp.mining_session import SignedMiningSession, signature_envelope_for_session_fields
from alice_acp.mining_supervisor import MiningSupervisorStateMachine, SupervisorSnapshot

DEFAULT_ISSUED_AT = datetime(2026, 5, 24, 12, 0, tzinfo=UTC)
DEFAULT_EXPIRES_AT = DEFAULT_ISSUED_AT + timedelta(hours=1)
DEFAULT_COMPLETED_AT = DEFAULT_ISSUED_AT + timedelta(minutes=10)
DEFAULT_RAW_REF_HASH = "8" * 64


@dataclass(frozen=True, slots=True)
class MiningFirstDryRunReport:
    run_id: str
    session: SignedMiningSession
    backend: BackendCapabilityResult
    supervisor: SupervisorSnapshot
    ai_decision: SchedulerPreemptionDecision
    shadow_report: MiningShadowLedgerReport
    dashboard_report: MinerDashboardReport
    download_content: MiningDownloadContentContract

    def as_dict(self) -> dict[str, object]:
        return {
            "run_id": self.run_id,
            "session_id": self.session.session_id,
            "passport_id": self.session.passport_id,
            "backend_reason": self.backend.reason_code,
            "supervisor_state": self.supervisor.state,
            "ai_preemption_action": self.ai_decision.action,
            "ai_preemption_reason": self.ai_decision.reason_code,
            "total_mining_acu": str(self.shadow_report.total_mining_acu),
            "paid_acu": str(self.shadow_report.paid_acu),
            "dashboard": self.dashboard_report.as_dict(),
            "download_content": self.download_content.as_dict(),
        }


def run_mining_first_dry_run(
    engine: sa.Engine,
    *,
    server_secret: bytes,
    run_id: str = "mining-first-dry-run",
) -> MiningFirstDryRunReport:
    session = synthetic_signed_session(run_id)
    proof = synthetic_accepted_share_proof(session, run_id=run_id)
    backend = synthetic_backend()
    supervisor_machine = MiningSupervisorStateMachine()
    supervisor_machine.start(session_id=session.session_id, backend=backend)

    ai_decision = decide_preemption(
        supervisor=supervisor_machine.snapshot,
        device=synthetic_device(),
        ai_demand=synthetic_ai_demand(run_id),
    )
    supervisor = apply_preemption_decision(supervisor_machine, ai_decision)
    shadow_report = run_mining_shadow_accounting_flow(
        engine,
        server_secret=server_secret,
        run_id=run_id,
        session=session,
        proofs=(proof,),
        completed_at=DEFAULT_COMPLETED_AT,
    )
    dashboard = build_miner_dashboard_report(
        session=session,
        backend=backend,
        supervisor=supervisor,
        shadow_report=shadow_report,
        ai_decision=ai_decision,
        observed_at=DEFAULT_COMPLETED_AT,
    )
    return MiningFirstDryRunReport(
        run_id=run_id,
        session=session,
        backend=backend,
        supervisor=supervisor,
        ai_decision=ai_decision,
        shadow_report=shadow_report,
        dashboard_report=dashboard,
        download_content=MiningDownloadContentContract(),
    )


def synthetic_signed_session(run_id: str) -> SignedMiningSession:
    fields = {
        "session_id": f"{run_id}-session",
        "passport_id": f"{run_id}-passport",
        "attempt_id": f"{run_id}-attempt",
        "pool_id": "alice-rvn-pool-shadow",
        "algorithm": "RVN_KAWPOW",
        "alice_collection_address": "RVN_ALICE_COLLECTION_SHADOW",
        "worker_id": f"{run_id}-worker",
        "issued_at": DEFAULT_ISSUED_AT,
        "expires_at": DEFAULT_EXPIRES_AT,
        "route_policy_version": "route-policy-v1",
        "session_policy_version": "session-policy-v1",
        "mode": "ALICE_REWARDED_MINING",
    }
    return SignedMiningSession(
        signature=signature_envelope_for_session_fields(fields, signed_at=DEFAULT_ISSUED_AT),
        **fields,
    )


def synthetic_accepted_share_proof(
    session: SignedMiningSession,
    *,
    run_id: str,
) -> AcceptedShareProof:
    event = PoolShareEvent(
        pool_id=session.pool_id,
        session_id=session.session_id,
        worker_id=session.worker_id,
        share_id=f"{run_id}-share-001",
        nonce=f"{run_id}-nonce-001",
        submitted_at=DEFAULT_ISSUED_AT + timedelta(minutes=5),
        share_difficulty=Decimal("1000"),
        pool_result="accepted",
        raw_ref_hash=DEFAULT_RAW_REF_HASH,
        evidence_ref=f"evidence://mining/{run_id}/share-001",
        observed_collection_address=session.alice_collection_address,
    )
    result = build_share_proof(session, event)
    if result.proof is None:
        raise RuntimeError(f"synthetic accepted share proof failed: {result.reason_code}")
    return result.proof


def synthetic_backend() -> BackendCapabilityResult:
    return BackendCapabilityResult(
        status="capable",
        mining_supported=True,
        ai_supported=True,
        cpu_idle_supported=True,
        backend="cuda_kawpow",
        reason_code="GPU_MINING_CAPABLE",
    )


def synthetic_device() -> DeviceEligibility:
    return DeviceEligibility(
        device_id="local-gpu-001",
        device_class="nvidia_cuda",
        region="local",
        route_capabilities=("ai_inference_dry_run",),
        mining_supported=True,
        ai_supported=True,
    )


def synthetic_ai_demand(run_id: str) -> AiDemandSignal:
    return AiDemandSignal(
        demand_id=f"{run_id}-ai-demand",
        admitted=True,
        device_class="nvidia_cuda",
        region="local",
        route_capability="ai_inference_dry_run",
    )

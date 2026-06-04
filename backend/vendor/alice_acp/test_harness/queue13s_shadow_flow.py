from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from alice_acp.shadow_server import (
    MAIN_POOL_AI,
    MAIN_POOL_GPU_RVN,
    SESSION_KIND_INFERENCE,
    SESSION_KIND_MINING,
    XMR_POOL,
    InferenceCompletionRequest,
    MiningProofAuthorityResult,
    MiningProofIngestRequest,
    ShadowRewardLedger,
    ShadowServerHarness,
    ShadowSessionIssueRequest,
    default_settlement_window,
)
from alice_acp.shadow_server.demand_admission import FakeDemandAdmissionStore


@dataclass(frozen=True, slots=True)
class Queue13SShadowFlowResult:
    rvn_status: str
    xmr_status: str
    inference_status: str
    settlement_total_devices: int
    revenue_record_count: int


def run_queue13s_shadow_flow() -> Queue13SShadowFlowResult:
    observed_at = datetime(2026, 5, 26, 16, 0, tzinfo=UTC)
    # Phase E: this self-contained dry-run is its own verified-demand source and
    # signing authority. It registers the inference demand id (C1) and supplies a
    # fixed HMAC secret (C3). Reward/payout/chain stay OFF.
    demand_store = FakeDemandAdmissionStore()
    inference_demand_id = "demand-queue13s-dryrun-001"
    demand_store.register_demand(
        demand_session_id=inference_demand_id,
        passport_id="passport-vs-mac",
        device_id="device-vs-mac-m2max",
    )
    server = ShadowServerHarness(
        ledger=ShadowRewardLedger(
            server_secret=b"queue13s-dryrun-secret",
            demand_store=demand_store,
        )
    )
    rvn = server.session_issue(
        ShadowSessionIssueRequest(
            passport_id="passport-narissa",
            device_id="device-narissa-3070ti",
            lane=MAIN_POOL_GPU_RVN,
            session_kind=SESSION_KIND_MINING,
            worker_id="alice_q13s_trex",
            requested_at=observed_at,
        )
    ).session
    xmr = server.session_issue(
        ShadowSessionIssueRequest(
            passport_id="passport-vs-mac",
            device_id="device-vs-mac-m2max",
            lane=XMR_POOL,
            session_kind=SESSION_KIND_MINING,
            worker_id="alice_q13s_xmr",
            requested_at=observed_at,
        )
    ).session
    inference = server.inference_admit(
        ShadowSessionIssueRequest(
            passport_id="passport-vs-mac",
            device_id="device-vs-mac-m2max",
            lane=MAIN_POOL_AI,
            session_kind=SESSION_KIND_INFERENCE,
            model_id="alice-qwen3.5-4b-heretic-light-mlx@4bit",
            requested_at=observed_at,
            demand_session_id=inference_demand_id,
        )
    ).session
    assert rvn is not None
    assert xmr is not None
    assert inference is not None

    rvn_result = server.proof_ingest(
        MiningProofIngestRequest(
            proof_id="rvn-proof-1",
            session_id=rvn.session_id,
            session_signature=rvn.signature,
            lane=MAIN_POOL_GPU_RVN,
            pool_result="accepted",
            share_difficulty=Decimal("104"),
            accepted_count=104,
            rejected_count=0,
            observed_at=observed_at,
            algorithm="RVN_KAWPOW",
            worker_id=rvn.worker_id or "",
            hashrate=Decimal("23.0"),
            canonical_share_hash="a" * 64,
            pool_evidence_ref="evidence://shadow/rvn-proof-1",
            authority_result=MiningProofAuthorityResult(
                status="accepted",
                reason_code="POOL_AUTHORITY_CONFIRMED",
                rewardable_score=Decimal("104"),
                canonical_share_hash="a" * 64,
            ),
        )
    )
    xmr_result = server.proof_ingest(
        MiningProofIngestRequest(
            proof_id="xmr-proof-1",
            session_id=xmr.session_id,
            session_signature=xmr.signature,
            lane=XMR_POOL,
            pool_result="accepted",
            share_difficulty=Decimal("38"),
            accepted_count=38,
            rejected_count=0,
            observed_at=observed_at,
            algorithm="XMR_RANDOMX",
            worker_id=xmr.worker_id or "",
            hashrate=Decimal("2632"),
            canonical_share_hash="b" * 64,
            pool_evidence_ref="evidence://shadow/xmr-proof-1",
            authority_result=MiningProofAuthorityResult(
                status="accepted",
                reason_code="POOL_AUTHORITY_CONFIRMED",
                rewardable_score=Decimal("38"),
                canonical_share_hash="b" * 64,
            ),
        )
    )
    inference_result = server.inference_complete(
        InferenceCompletionRequest(
            proof_id="inference-proof-1",
            session_id=inference.session_id,
            session_signature=inference.signature,
            model_id=inference.model_id or "",
            input_tokens=64,
            output_tokens=32,
            context_length=4096,
            latency_ms=Decimal("1200"),
            model_class="tier1_local_llm",
            verified_inference_acu=Decimal("96"),
            observed_at=observed_at,
            simulated_api_payment=Decimal("1.25"),
        )
    )
    settlement = server.shadow_window(default_settlement_window(observed_at))
    return Queue13SShadowFlowResult(
        rvn_status=rvn_result.status,
        xmr_status=xmr_result.status,
        inference_status=inference_result.status,
        settlement_total_devices=len(settlement.device_credits),
        revenue_record_count=len(server.ledger.revenue_records),
    )

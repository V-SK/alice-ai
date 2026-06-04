"""Test harness namespace for local ACP readiness evidence."""

from alice_acp.test_harness.benchmark import (
    BenchmarkConfig,
    run_abrs_benchmark,
    run_phase_b_benchmark_readiness,
)
from alice_acp.test_harness.failover import (
    SimulatedCrashBeforeCommit,
    SimulatedResponseLossAfterCommit,
    run_failover_drill,
    simulate_crash_before_commit,
    simulate_response_loss_after_commit,
)
from alice_acp.test_harness.phase_b_dry_run import run_phase_b_contract_dry_run
from alice_acp.test_harness.reports import (
    FAILOVER_SCOPE_NOTE,
    PHASE_C_REASON_WHY_NOT_LIVE,
    REASON_WHY_NOT_LIVE,
    BenchmarkReport,
    FailoverReport,
    FailoverScenarioReport,
    PhaseBBenchmarkReadinessReport,
    PhaseBDryRunReport,
    PhaseCBenchmarkReadinessReport,
    PhaseCDryRunReport,
    ShadowLedgerReport,
)
from alice_acp.test_harness.shadow_ledger import (
    default_tranche_assessment,
    make_reservation_request,
    run_shadow_ledger_flow,
    seed_budget_accounts,
)

__all__ = (
    "FAILOVER_SCOPE_NOTE",
    "PHASE_C_REASON_WHY_NOT_LIVE",
    "PHASE_F_REASON_WHY_NOT_LIVE",
    "PHASE_G_REASON_WHY_NOT_LIVE",
    "REASON_WHY_NOT_LIVE",
    "AuditPacketCommitRange",
    "BenchmarkConfig",
    "BenchmarkReport",
    "ExternalAuditPacket",
    "FailoverReport",
    "FailoverScenarioReport",
    "PhaseBBenchmarkReadinessReport",
    "PhaseBDryRunReport",
    "PhaseCBenchmarkReadinessReport",
    "PhaseCDryRunReport",
    "PhaseFReadinessReport",
    "PhaseGReadinessReport",
    "ShadowLedgerReport",
    "SimulatedCrashBeforeCommit",
    "SimulatedResponseLossAfterCommit",
    "ValidationCommandResult",
    "build_external_audit_packet",
    "build_phase_d_readiness_report",
    "build_phase_e_readiness_report",
    "build_phase_f_readiness_report",
    "build_phase_g_readiness_report",
    "default_tranche_assessment",
    "make_reservation_request",
    "run_abrs_benchmark",
    "run_failover_drill",
    "run_phase_b_benchmark_readiness",
    "run_phase_b_contract_dry_run",
    "run_phase_c_benchmark_readiness",
    "run_phase_c_contract_dry_run",
    "run_shadow_ledger_flow",
    "seed_budget_accounts",
    "simulate_crash_before_commit",
    "simulate_response_loss_after_commit",
)


def __getattr__(name: str) -> object:
    if name == "run_phase_c_benchmark_readiness":
        from alice_acp.test_harness.phase_c_benchmark import run_phase_c_benchmark_readiness

        return run_phase_c_benchmark_readiness
    if name == "run_phase_c_contract_dry_run":
        from alice_acp.test_harness.phase_c_dry_run import run_phase_c_contract_dry_run

        return run_phase_c_contract_dry_run
    if name == "build_phase_d_readiness_report":
        from alice_acp.test_harness.phase_d_readiness import build_phase_d_readiness_report

        return build_phase_d_readiness_report
    if name == "build_phase_e_readiness_report":
        from alice_acp.test_harness.phase_e_readiness import build_phase_e_readiness_report

        return build_phase_e_readiness_report
    if name in {
        "PHASE_F_REASON_WHY_NOT_LIVE",
        "PhaseFReadinessReport",
        "build_phase_f_readiness_report",
    }:
        from alice_acp.test_harness import phase_f_readiness

        return getattr(phase_f_readiness, name)
    if name in {
        "PHASE_G_REASON_WHY_NOT_LIVE",
        "PhaseGReadinessReport",
        "build_phase_g_readiness_report",
    }:
        from alice_acp.test_harness import phase_g_readiness

        return getattr(phase_g_readiness, name)
    if name in {
        "AuditPacketCommitRange",
        "ExternalAuditPacket",
        "ValidationCommandResult",
        "build_external_audit_packet",
    }:
        from alice_acp.test_harness import audit_packet

        return getattr(audit_packet, name)
    raise AttributeError(name)

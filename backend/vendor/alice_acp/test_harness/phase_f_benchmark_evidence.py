from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from alice_acp.evidence import (
    EvidenceCustodyRecord,
    LocalEvidenceRegistry,
    SignedApprovalEnvelope,
    validate_evidence_custody,
    validate_signed_approval_envelope,
)
from alice_acp.evidence.registry import EvidenceRecordNotFoundError
from alice_acp.evidence.types import parse_evidence_ref
from alice_acp.test_harness.phase_d_benchmark import (
    PhaseDBenchmarkGateReport,
    RepresentativeBenchmarkProfile,
    evaluate_representative_abrs_benchmark,
)
from alice_acp.test_harness.reports import BenchmarkReport


@dataclass(frozen=True, slots=True)
class PhaseFBenchmarkEvidencePacket:
    benchmark: BenchmarkReport
    profile: RepresentativeBenchmarkProfile
    hardware_profile_ref: str
    postgres_config_ref: str
    partition_profile_ref: str
    workload_profile_ref: str
    latency_metrics_ref: str
    throughput_metrics_ref: str
    outcome_counts_ref: str
    retryable_separation_ref: str
    invariant_counters_ref: str
    burst_coverage_ref: str
    external_review_approval_ref: str

    def __post_init__(self) -> None:
        for ref in _packet_evidence_refs(self):
            parsed = parse_evidence_ref(ref)
            if parsed.scheme != "evidence":
                raise ValueError("Phase F benchmark evidence refs must use evidence://")
        parsed_approval = parse_evidence_ref(self.external_review_approval_ref)
        if parsed_approval.scheme != "approval":
            raise ValueError("external_review_approval_ref must use approval://")


@dataclass(frozen=True, slots=True)
class PhaseFBenchmarkEvidenceReport:
    phase_d_report: PhaseDBenchmarkGateReport
    subject: str
    reason_codes: tuple[str, ...]
    representative_benchmark_evidence_ready: bool = False
    production_benchmark_ready: bool = False
    live_reward_ready: bool = False


@dataclass(frozen=True, slots=True)
class _RequiredBenchmarkCustodyRef:
    field_name: str
    artifact_type: str
    label: str

    @property
    def missing_code(self) -> str:
        return f"PHASE_F_BENCHMARK_{self.label}_CUSTODY_MISSING"

    @property
    def invalid_code(self) -> str:
        return f"PHASE_F_BENCHMARK_{self.label}_CUSTODY_INVALID"

    @property
    def signoff_missing_code(self) -> str:
        return f"PHASE_F_BENCHMARK_SIGNOFF_MISSING_{self.label}_BINDING"


REQUIRED_BENCHMARK_CUSTODY_REFS: tuple[_RequiredBenchmarkCustodyRef, ...] = (
    _RequiredBenchmarkCustodyRef("hardware_profile_ref", "benchmark_hardware_profile", "HARDWARE"),
    _RequiredBenchmarkCustodyRef("postgres_config_ref", "benchmark_postgres_config", "POSTGRES"),
    _RequiredBenchmarkCustodyRef(
        "partition_profile_ref",
        "benchmark_partition_profile",
        "PARTITION",
    ),
    _RequiredBenchmarkCustodyRef("workload_profile_ref", "benchmark_workload_profile", "WORKLOAD"),
    _RequiredBenchmarkCustodyRef("latency_metrics_ref", "benchmark_latency_metrics", "LATENCY"),
    _RequiredBenchmarkCustodyRef(
        "throughput_metrics_ref",
        "benchmark_throughput_metrics",
        "THROUGHPUT",
    ),
    _RequiredBenchmarkCustodyRef("outcome_counts_ref", "benchmark_outcome_counts", "OUTCOMES"),
    _RequiredBenchmarkCustodyRef(
        "retryable_separation_ref",
        "benchmark_retryable_separation",
        "RETRYABLE_SEPARATION",
    ),
    _RequiredBenchmarkCustodyRef(
        "invariant_counters_ref",
        "benchmark_invariant_counters",
        "INVARIANT_COUNTERS",
    ),
    _RequiredBenchmarkCustodyRef("burst_coverage_ref", "benchmark_burst_coverage", "BURST"),
)


def validate_phase_f_benchmark_evidence(
    packet: PhaseFBenchmarkEvidencePacket,
    registry: LocalEvidenceRegistry,
    custody_records: tuple[EvidenceCustodyRecord, ...],
    signed_approval: SignedApprovalEnvelope | None,
    *,
    subject: str,
    now: datetime | None = None,
) -> PhaseFBenchmarkEvidenceReport:
    phase_d_report = evaluate_representative_abrs_benchmark(packet.benchmark, packet.profile)
    reason_codes: list[str] = list(phase_d_report.reason_codes)
    if not phase_d_report.representative_benchmark_ready:
        reason_codes.append("PHASE_D_BENCHMARK_GATE_BLOCKED")
    _append_benchmark_accounting_reason_codes(packet.benchmark, reason_codes)

    custody_by_ref = {record.artifact_ref: record for record in custody_records}
    approved_refs = _approved_ref_hashes(signed_approval)

    for required in REQUIRED_BENCHMARK_CUSTODY_REFS:
        ref = getattr(packet, required.field_name)
        custody = custody_by_ref.get(ref)
        if custody is None:
            reason_codes.append(required.missing_code)
        else:
            custody_report = validate_evidence_custody(
                custody,
                registry,
                subject=subject,
                artifact_type=required.artifact_type,
                now=now,
            )
            if not custody_report.custody_ready:
                reason_codes.append(required.invalid_code)
                reason_codes.extend(custody_report.reason_codes)
        if ref not in approved_refs:
            reason_codes.append(required.signoff_missing_code)
        else:
            _append_hash_mismatch_if_needed(
                registry,
                ref,
                approved_refs[ref],
                reason_codes,
            )

    if signed_approval is None:
        reason_codes.append("PHASE_F_BENCHMARK_SIGNED_APPROVAL_MISSING")
    else:
        if signed_approval.approval_ref != packet.external_review_approval_ref:
            reason_codes.append("PHASE_F_BENCHMARK_SIGNED_APPROVAL_REF_MISMATCH")
        signoff_report = validate_signed_approval_envelope(
            signed_approval,
            registry,
            subject=subject,
            approval_scope="benchmark_signoff",
            now=now,
        )
        reason_codes.extend(signoff_report.reason_codes)

    return PhaseFBenchmarkEvidenceReport(
        phase_d_report=phase_d_report,
        subject=subject,
        reason_codes=tuple(dict.fromkeys(reason_codes)),
        representative_benchmark_evidence_ready=not reason_codes,
        production_benchmark_ready=False,
        live_reward_ready=False,
    )


def _packet_evidence_refs(packet: PhaseFBenchmarkEvidencePacket) -> tuple[str, ...]:
    return tuple(
        getattr(packet, required.field_name) for required in REQUIRED_BENCHMARK_CUSTODY_REFS
    )


def _append_benchmark_accounting_reason_codes(
    benchmark: BenchmarkReport,
    reason_codes: list[str],
) -> None:
    backend = benchmark.database_or_storage_backend.strip().lower()
    if backend != "postgres":
        reason_codes.append("BENCHMARK_BACKEND_NOT_POSTGRES")
    if backend == "sqlite":
        reason_codes.append("SQLITE_SERIALIZABLE_PROOF_REJECTED")

    counted_outcomes = (
        benchmark.accepted_count + benchmark.rejected_count + benchmark.retryable_count
    )
    if counted_outcomes != benchmark.total_requests:
        reason_codes.append("BENCHMARK_OUTCOME_COUNTS_DO_NOT_SUM")
    if benchmark.retryable_count > 0 and benchmark.accepted_count + benchmark.rejected_count >= (
        benchmark.total_requests
    ):
        reason_codes.append("RETRYABLE_COUNTED_AS_SUCCESS")


def _approved_ref_hashes(approval: SignedApprovalEnvelope | None) -> dict[str, str]:
    if approval is None:
        return {}
    return dict(
        zip(
            approval.approved_evidence_refs,
            approval.approved_content_sha256,
            strict=True,
        )
    )


def _append_hash_mismatch_if_needed(
    registry: LocalEvidenceRegistry,
    ref: str,
    approved_hash: str,
    reason_codes: list[str],
) -> None:
    try:
        record = registry.require(ref)
    except EvidenceRecordNotFoundError:
        return
    except ValueError:
        return
    if record.content_sha256 != approved_hash:
        reason_codes.append("PHASE_F_BENCHMARK_SIGNOFF_HASH_MISMATCH")

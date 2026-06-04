from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

PHASE_D_HA_SCOPE_NOTE = (
    "Phase D evaluates local HA drill evidence only; production HA readiness "
    "requires an approved external active-passive or consensus drill."
)

HADeploymentOption = Literal["active_passive_single_writer", "consensus_storage"]


@dataclass(frozen=True, slots=True)
class ABRSHADrillEvidence:
    deployment_option: HADeploymentOption
    drill_executed: bool = False
    split_brain_prevention: bool = False
    zero_data_loss: bool = False
    duplicate_reservation_after_failover: int = 0
    lost_reservation_after_failover: int = 0
    negative_budget_after_failover: int = 0
    idempotent_retries_consistent: bool = False
    failover_seconds: float | None = None
    p99_recovery_reservation_latency_ms: float | None = None
    external_drill_approved: bool = False
    drill_artifact_ref: str | None = None
    drill_environment_ref: str | None = None
    external_approval_ref: str | None = None

    def __post_init__(self) -> None:
        if self.duplicate_reservation_after_failover < 0:
            raise ValueError("duplicate_reservation_after_failover must be non-negative")
        if self.lost_reservation_after_failover < 0:
            raise ValueError("lost_reservation_after_failover must be non-negative")
        if self.negative_budget_after_failover < 0:
            raise ValueError("negative_budget_after_failover must be non-negative")
        if self.failover_seconds is not None and self.failover_seconds < 0:
            raise ValueError("failover_seconds must be non-negative")
        if (
            self.p99_recovery_reservation_latency_ms is not None
            and self.p99_recovery_reservation_latency_ms < 0
        ):
            raise ValueError("p99_recovery_reservation_latency_ms must be non-negative")
        _validate_evidence_ref("drill_artifact_ref", self.drill_artifact_ref)
        _validate_evidence_ref("drill_environment_ref", self.drill_environment_ref)
        _validate_evidence_ref("external_approval_ref", self.external_approval_ref)


@dataclass(frozen=True, slots=True)
class ABRSHAReadinessReport:
    deployment_option: HADeploymentOption
    reason_codes: tuple[str, ...]
    production_ha_ready: bool = False
    live_reward_ready: bool = False
    scope_note: str = PHASE_D_HA_SCOPE_NOTE

    @property
    def blocked(self) -> bool:
        return not self.production_ha_ready


def evaluate_abrs_ha_drill(evidence: ABRSHADrillEvidence) -> ABRSHAReadinessReport:
    reason_codes: list[str] = []
    if not evidence.drill_executed:
        reason_codes.append("ABRS_HA_DRILL_NOT_EXECUTED")
    if not evidence.split_brain_prevention:
        reason_codes.append("SPLIT_BRAIN_PREVENTION_NOT_PROVEN")
    if not evidence.zero_data_loss:
        reason_codes.append("ZERO_DATA_LOSS_NOT_PROVEN")
    if evidence.duplicate_reservation_after_failover:
        reason_codes.append("DUPLICATE_RESERVATION_AFTER_FAILOVER")
    if evidence.lost_reservation_after_failover:
        reason_codes.append("LOST_RESERVATION_AFTER_FAILOVER")
    if evidence.negative_budget_after_failover:
        reason_codes.append("NEGATIVE_BUDGET_AFTER_FAILOVER")
    if not evidence.idempotent_retries_consistent:
        reason_codes.append("IDEMPOTENT_RETRY_CONSISTENCY_NOT_PROVEN")
    if evidence.failover_seconds is None:
        reason_codes.append("FAILOVER_TIME_NOT_REPORTED")
    elif evidence.failover_seconds > 30:
        reason_codes.append("FAILOVER_TARGET_EXCEEDED")
    if evidence.p99_recovery_reservation_latency_ms is None:
        reason_codes.append("P99_RECOVERY_LATENCY_NOT_REPORTED")
    if not evidence.external_drill_approved:
        reason_codes.append("EXTERNAL_HA_DRILL_APPROVAL_MISSING")
    if not evidence.drill_artifact_ref:
        reason_codes.append("HA_DRILL_ARTIFACT_REF_MISSING")
    if not evidence.drill_environment_ref:
        reason_codes.append("HA_DRILL_ENVIRONMENT_REF_MISSING")
    if not evidence.external_approval_ref:
        reason_codes.append("EXTERNAL_HA_DRILL_APPROVAL_REF_MISSING")

    production_ha_ready = not reason_codes
    return ABRSHAReadinessReport(
        deployment_option=evidence.deployment_option,
        reason_codes=tuple(reason_codes),
        production_ha_ready=production_ha_ready,
        live_reward_ready=False,
    )


def _validate_evidence_ref(name: str, value: str | None) -> None:
    if value is None:
        return
    if not value.strip() or "://" not in value or any(character.isspace() for character in value):
        raise ValueError(f"{name} must be an opaque evidence URI")

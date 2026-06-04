from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

FOUNDATION_INTERNAL_BUDGET = "FOUNDATION_INTERNAL_BUDGET"
PUBLIC_MINER_BUCKET = "PUBLIC_MINER_BUCKET"
R5_SEEDED_ADVERSARIAL = "R5_seeded_adversarial"
R1_LOW_RISK_SEED = "R1_low_risk_seed"

SeedClass = Literal[
    "pool_share_fraud",
    "cached_answer_replay",
    "P1_sanitizer_bypass",
    "runtime_integrity_fraud",
    "model_substitution_35B_plus",
    "speculative_route_fraud",
    "requester_miner_collusion",
    "cluster_sybil",
    "quality_drift",
    "validation_fraud",
]

SEED_CLASSES: tuple[SeedClass, ...] = (
    "pool_share_fraud",
    "cached_answer_replay",
    "P1_sanitizer_bypass",
    "runtime_integrity_fraud",
    "model_substitution_35B_plus",
    "speculative_route_fraud",
    "requester_miner_collusion",
    "cluster_sybil",
    "quality_drift",
    "validation_fraud",
)

SeedRouteClass = Literal["R5_seeded_adversarial", "R1_low_risk_seed"]


@dataclass(frozen=True, slots=True)
class SeedCase:
    seed_id: str
    seed_class: SeedClass
    budget_namespace: str
    route_source_class: SeedRouteClass
    dual_blind_subject_ref: str
    verifier_visible_evidence_ref: str
    ground_truth_ref: str
    risk_signal_refs: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        required = (
            self.seed_id,
            self.budget_namespace,
            self.route_source_class,
            self.dual_blind_subject_ref,
            self.verifier_visible_evidence_ref,
            self.ground_truth_ref,
        )
        if any(not value for value in required):
            raise ValueError("seed case fields must be non-empty")
        if self.budget_namespace != FOUNDATION_INTERNAL_BUDGET:
            raise ValueError("seed cases must use FOUNDATION_INTERNAL_BUDGET")
        if self.verifier_visible_evidence_ref == self.ground_truth_ref:
            raise ValueError("ground truth must be separated from verifier-visible evidence")


@dataclass(frozen=True, slots=True)
class SeedMTTDReport:
    seed_count_by_class: dict[str, int] = field(default_factory=dict)
    p50_mttd_seconds: int = 0
    p95_mttd_seconds: int = 0
    public_miner_bucket_debits: int = 0
    dual_blind_violations: int = 0


@dataclass(frozen=True, slots=True)
class SeedExecutionReport:
    seed_id: str
    seed_class: str
    reservation_id: str | None
    accepted: bool
    budget_namespace: str
    route_source_class: str
    public_miner_bucket_debits: int
    dual_blind_preserved: bool
    reason_code: str | None = None

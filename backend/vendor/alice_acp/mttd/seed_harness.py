from __future__ import annotations

from alice_acp.mttd.types import (
    FOUNDATION_INTERNAL_BUDGET,
    R5_SEEDED_ADVERSARIAL,
    SeedCase,
    SeedClass,
    SeedRouteClass,
)


def create_seed_case(
    *,
    seed_id: str,
    seed_class: SeedClass,
    verifier_visible_evidence_ref: str,
    ground_truth_ref: str,
    route_source_class: SeedRouteClass = R5_SEEDED_ADVERSARIAL,
    dual_blind_subject_ref: str | None = None,
) -> SeedCase:
    return SeedCase(
        seed_id=seed_id,
        seed_class=seed_class,
        budget_namespace=FOUNDATION_INTERNAL_BUDGET,
        route_source_class=route_source_class,
        dual_blind_subject_ref=dual_blind_subject_ref or f"seed-subject://{seed_id}",
        verifier_visible_evidence_ref=verifier_visible_evidence_ref,
        ground_truth_ref=ground_truth_ref,
    )

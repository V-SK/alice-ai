from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

import sqlalchemy as sa

import alice_acp.abrs.service as abrs_service
import alice_acp.settlement.service as settlement_service
from alice_acp.abrs import ReservationRequest
from alice_acp.mining_accounting.acu import estimate_mining_acu
from alice_acp.mining_accounting.types import (
    ZERO_ACU,
    MiningAcuFormula,
    MiningAcuInput,
    MiningShadowLedgerEntry,
    MiningShadowLedgerReport,
)
from alice_acp.mining_pool.types import AcceptedShareProof
from alice_acp.mining_proofs import MiningProofCollector
from alice_acp.mining_session.types import SignedMiningSession
from alice_acp.settlement import VerificationResult
from alice_acp.test_harness.shadow_ledger import default_tranche_assessment, seed_budget_accounts

SHADOW_ENTRY_COUNTED = "SHADOW_ENTRY_COUNTED"
SHADOW_ENTRY_NOT_COUNTED = "SHADOW_ENTRY_NOT_COUNTED"
MINING_SHADOW_NO_REWARDABLE_ACU = "MINING_SHADOW_NO_REWARDABLE_ACU"
MINING_SHADOW_ACCOUNTING_SPINE_BOUND = "MINING_SHADOW_ACCOUNTING_SPINE_BOUND"


@dataclass(slots=True)
class MiningShadowLedgerAdapter:
    session: SignedMiningSession
    formula: MiningAcuFormula = field(default_factory=MiningAcuFormula)
    entries: list[MiningShadowLedgerEntry] = field(default_factory=list)
    collector: MiningProofCollector = field(init=False)

    def __post_init__(self) -> None:
        self.collector = MiningProofCollector(self.session)

    def ingest_proof(self, proof: AcceptedShareProof) -> MiningShadowLedgerEntry:
        collection_result = self.collector.collect(proof)
        if not collection_result.counted:
            entry = MiningShadowLedgerEntry(
                status=(
                    "shadow_not_counted"
                    if collection_result.status == "duplicate"
                    else "shadow_rejected"
                ),
                reason_code=collection_result.reason_code,
                session_id=proof.session_id,
                worker_id=proof.worker_id,
                mining_acu=ZERO_ACU,
                formula_version=self.formula.formula_version,
                proof_identity=proof.identity,
                evidence_refs=(proof.evidence_ref,),
            )
            self.entries.append(entry)
            return entry

        estimate = estimate_mining_acu(
            MiningAcuInput(
                proof=proof,
                pool_validity="pool_accepted",
                formula=self.formula,
            )
        )
        entry = MiningShadowLedgerEntry(
            status="shadow_counted" if estimate.rewardable else "shadow_rejected",
            reason_code=estimate.reason_code,
            session_id=proof.session_id,
            worker_id=proof.worker_id,
            mining_acu=estimate.mining_acu,
            formula_version=estimate.formula_version,
            proof_identity=proof.identity,
            evidence_refs=estimate.evidence_refs,
        )
        self.entries.append(entry)
        return entry

    @property
    def total_mining_acu(self) -> Decimal:
        return sum(
            (entry.mining_acu for entry in self.entries if entry.status == "shadow_counted"),
            ZERO_ACU,
        )

    def report(
        self,
        *,
        run_id: str,
        paid_acu: Decimal = ZERO_ACU,
        reservation_id: str | None = None,
        liability_id: str | None = None,
        service_functions: tuple[str, ...] = (),
    ) -> MiningShadowLedgerReport:
        return MiningShadowLedgerReport(
            run_id=run_id,
            session_id=self.session.session_id,
            entries=tuple(self.entries),
            total_mining_acu=self.total_mining_acu,
            paid_acu=paid_acu,
            reservation_id=reservation_id,
            liability_id=liability_id,
            abrs_binding_status=(
                "supported_without_migration"
                if reservation_id is not None
                else "shadow_only_no_rewardable_acu"
            ),
            service_functions=service_functions,
        )


def build_mining_reservation_request(
    *,
    run_id: str,
    session: SignedMiningSession,
    mining_acu: Decimal,
    reservation_expires_at: datetime | None = None,
    epoch_id: str | None = None,
    host_id: str = "local-mining-shadow-host",
    accelerator_id: str = "gpu-rvn-kawpow-shadow",
    formula_version: str = "mining-rvn-kawpow-share-difficulty-v1",
    tranche_policy_version: str = "mining-shadow-tranche-v1",
) -> ReservationRequest:
    if mining_acu <= ZERO_ACU:
        raise ValueError("mining_acu must be positive for ABRS reservation")
    return ReservationRequest(
        admission_id=f"{run_id}-admission",
        attempt_id=session.attempt_id,
        route_contract_id=f"{session.pool_id}-{session.algorithm}-{session.route_policy_version}",
        epoch_id=epoch_id or f"{run_id}-epoch",
        source_budget_id=f"{run_id}-mining-source-budget",
        mode_budget_id=f"{run_id}-rvn-kawpow-mode-budget",
        passport_id=session.passport_id,
        wallet_id=f"alice-collection-{session.alice_collection_address}",
        host_id=host_id,
        accelerator_id=accelerator_id,
        max_rewardable_acu=mining_acu,
        formula_version=formula_version,
        tranche_policy_version=tranche_policy_version,
        reservation_expires_at=reservation_expires_at or session.expires_at,
        cluster_status="none",
    )


def build_mining_verification_result(
    *,
    reservation_id: str,
    request: ReservationRequest,
    entry: MiningShadowLedgerEntry,
    completed_at: datetime,
) -> VerificationResult:
    if entry.status != "shadow_counted":
        raise ValueError("only counted mining shadow entries can become verification results")
    return VerificationResult(
        reservation_id=reservation_id,
        admission_id=request.admission_id,
        attempt_id=request.attempt_id,
        verifier_verdict="pass",
        verified_acu=entry.mining_acu,
        verification_completed_at=completed_at,
        applicable_signal_snapshot={
            "mining_shadow": True,
            "session_id": entry.session_id,
            "worker_id": entry.worker_id,
            "formula_version": entry.formula_version,
        },
        evidence_refs=entry.evidence_refs,
    )


def run_mining_shadow_accounting_flow(
    engine: sa.Engine,
    *,
    server_secret: bytes,
    run_id: str,
    session: SignedMiningSession,
    proofs: tuple[AcceptedShareProof, ...],
    completed_at: datetime,
) -> MiningShadowLedgerReport:
    adapter = MiningShadowLedgerAdapter(session)
    for proof in proofs:
        adapter.ingest_proof(proof)

    counted_entries = tuple(entry for entry in adapter.entries if entry.status == "shadow_counted")
    if not counted_entries or adapter.total_mining_acu <= ZERO_ACU:
        return adapter.report(run_id=run_id)

    request = build_mining_reservation_request(
        run_id=run_id,
        session=session,
        mining_acu=adapter.total_mining_acu,
        formula_version=adapter.formula.formula_version,
    )
    seed_budget_accounts(engine, request)
    reservation = abrs_service.reserve_reward_budget(
        engine,
        request,
        server_secret=server_secret,
        actor_service="alice_acp.mining_accounting.shadow",
        retry_base_sleep_seconds=0,
        retry_jitter_seconds=0,
    )
    if not reservation.accepted or reservation.reservation_id is None:
        raise RuntimeError(f"mining shadow reservation failed: {reservation.reason_code}")

    verification = build_mining_verification_result(
        reservation_id=reservation.reservation_id,
        request=request,
        entry=_combined_entry(adapter, counted_entries),
        completed_at=completed_at,
    )
    liability = settlement_service.consume_reservation_and_create_liability(
        engine,
        verification,
        server_secret=server_secret,
        actor_service="alice_acp.mining_accounting.shadow",
    )
    if not liability.accepted or liability.liability_id is None:
        raise RuntimeError(f"mining shadow liability failed: {liability.reason_code}")

    split = settlement_service.split_pending_liability(
        engine,
        liability.liability_id,
        default_tranche_assessment(),
        tranche_policy_version=request.tranche_policy_version,
        server_secret=server_secret,
        actor_service="alice_acp.mining_accounting.shadow",
    )
    if not split.accepted:
        raise RuntimeError(f"mining shadow tranche split failed: {split.reason_code}")

    return adapter.report(
        run_id=run_id,
        paid_acu=_paid_acu(engine, liability.liability_id),
        reservation_id=reservation.reservation_id,
        liability_id=liability.liability_id,
        service_functions=(
            "alice_acp.abrs.service.reserve_reward_budget",
            "alice_acp.settlement.service.consume_reservation_and_create_liability",
            "alice_acp.settlement.service.split_pending_liability",
        ),
    )


def _combined_entry(
    adapter: MiningShadowLedgerAdapter,
    counted_entries: tuple[MiningShadowLedgerEntry, ...],
) -> MiningShadowLedgerEntry:
    return MiningShadowLedgerEntry(
        status="shadow_counted",
        reason_code=SHADOW_ENTRY_COUNTED,
        session_id=adapter.session.session_id,
        worker_id=adapter.session.worker_id,
        mining_acu=adapter.total_mining_acu,
        formula_version=adapter.formula.formula_version,
        evidence_refs=tuple(
            evidence_ref
            for entry in counted_entries
            for evidence_ref in entry.evidence_refs
        ),
    )


def _paid_acu(engine: sa.Engine, liability_id: str) -> Decimal:
    with engine.connect() as connection:
        return connection.execute(
            sa.text(
                """
                SELECT paid_acu
                FROM settlement_liability
                WHERE liability_id = :liability_id
                """
            ),
            {"liability_id": liability_id},
        ).scalar_one()

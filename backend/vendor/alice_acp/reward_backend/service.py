from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, localcontext

from alice_acp.reward_backend.storage import InMemoryRewardBackendStore, RewardBackendStore
from alice_acp.reward_backend.types import (
    ANTI_CHEAT_DUPLICATE,
    ANTI_CHEAT_PASSED,
    ANTI_CHEAT_REJECTED,
    ANTI_CHEAT_TAMPERED,
    ANTI_CHEAT_UNDER_REVIEW,
    AUTHORITY_ACCEPTED,
    AUTHORITY_NONREWARDABLE,
    AUTHORITY_REJECTED,
    AUTHORITY_REWARDABLE_CANDIDATE,
    AUTHORITY_UNDER_REVIEW,
    REASON_ANTI_CHEAT_DUPLICATE,
    REASON_ANTI_CHEAT_REJECTED,
    REASON_ANTI_CHEAT_TAMPERED,
    REASON_ANTI_CHEAT_UNDER_REVIEW,
    REASON_AUTHORITY_NONREWARDABLE,
    REASON_AUTHORITY_REJECTED,
    REASON_AUTHORITY_UNDER_REVIEW,
    REASON_DUPLICATE_CANONICAL_PROOF,
    REASON_DUPLICATE_SOURCE,
    REASON_FOUNDATION_REVENUE_EXCLUDED,
    REASON_KILL_SWITCH,
    REASON_NON_POSITIVE_SCORE,
    REASON_STAGED,
    REASON_WINDOW_MISMATCH,
    REASON_WINDOW_UNKNOWN,
    SOURCE_FOUNDATION_REVENUE,
    STATUS_EXCLUDED,
    STATUS_REJECTED,
    STATUS_STAGED,
    STATUS_UNDER_REVIEW,
    SUPPORTED_REWARD_LANES,
    ZERO_DECIMAL,
    FoundationRevenueRecord,
    RewardAccountingSourceRecord,
    RewardBackendConfig,
    RewardBalanceView,
    RewardStageResult,
    RewardWindow,
    StagedRewardRecord,
    utc_now,
    validate_public_identifier,
)

DECIMAL_PRECISION = 80


@dataclass(slots=True)
class RewardBackend:
    config: RewardBackendConfig = field(default_factory=RewardBackendConfig)
    store: RewardBackendStore = field(default_factory=InMemoryRewardBackendStore)
    kill_switch_enabled: bool = False

    def health(self) -> dict[str, object]:
        return {
            "ok": True,
            "service": "q19-server-reward-backend-contract",
            "contract_version": self.config.contract_version,
            "local_contract_only": self.config.local_contract_only,
            "public_service_enabled": self.config.public_service_enabled,
            "live_reward_enabled": self.config.live_reward_enabled,
            "payout_executor_enabled": self.config.payout_executor_enabled,
            "chain_transfer_enabled": self.config.chain_transfer_enabled,
            "kill_switch_enabled": self.kill_switch_enabled,
            "supported_lanes": SUPPORTED_REWARD_LANES,
        }

    def register_window(self, window: RewardWindow) -> RewardWindow:
        self.store.register_window(window)
        return window

    def set_kill_switch(
        self,
        enabled: bool,
        *,
        actor: str = "local-q19-contract",
        reason: str = "operator_reward_pause",
        recorded_at: datetime | None = None,
    ) -> None:
        validate_public_identifier("actor", actor)
        validate_public_identifier("reason", reason)
        timestamp = recorded_at or utc_now()
        self.kill_switch_enabled = enabled
        self.store.append_kill_switch_event(
            enabled=enabled,
            actor=actor,
            reason=reason,
            recorded_at=timestamp,
        )

    def stage_source(self, source: RewardAccountingSourceRecord) -> RewardStageResult:
        if self.kill_switch_enabled:
            return self._result(
                STATUS_REJECTED,
                REASON_KILL_SWITCH,
                source,
                consume_source_id=False,
            )

        window = self.store.get_window(source.window_id)
        if window is None:
            return self._result(
                STATUS_REJECTED,
                REASON_WINDOW_UNKNOWN,
                source,
                consume_source_id=False,
            )
        if not window.starts_at <= source.observed_at < window.ends_at:
            return self._result(
                STATUS_REJECTED,
                REASON_WINDOW_MISMATCH,
                source,
                consume_source_id=False,
            )

        if self.store.source_seen(source.source_id):
            return self._result(
                STATUS_REJECTED,
                REASON_DUPLICATE_SOURCE,
                source,
                consume_source_id=False,
            )

        if source.source_kind == SOURCE_FOUNDATION_REVENUE:
            revenue = FoundationRevenueRecord(
                source_id=source.source_id,
                window_id=source.window_id,
                session_id=source.session_id,
                amount=source.foundation_revenue_amount,
                recorded_at=source.observed_at,
            )
            result = RewardStageResult(
                status=STATUS_EXCLUDED,
                reason_code=REASON_FOUNDATION_REVENUE_EXCLUDED,
                source_id=source.source_id,
                window_id=source.window_id,
                foundation_revenue=revenue,
            )
            self.store.record_stage_result(result)
            return result

        if source.canonical_proof_hash is not None:
            owner = self.store.canonical_hash_owner(source.canonical_proof_hash)
            if owner is not None and owner != source.source_id:
                return self._result(
                    STATUS_REJECTED,
                    REASON_DUPLICATE_CANONICAL_PROOF,
                    source,
                )

        source_status = _source_nonrewardable_status(source)
        if source_status is not None:
            status, reason_code = source_status
            return self._result(status, reason_code, source)

        staged_record = StagedRewardRecord(
            source_id=source.source_id,
            window_id=source.window_id,
            session_id=source.session_id,
            proof_id=source.proof_id,
            passport_id=source.passport_id,
            device_id=source.device_id,
            lane=source.lane,
            rewardable_score=source.rewardable_score,
            authority_status=source.authority_status,
            anti_cheat_status=source.anti_cheat_status,
            recorded_at=source.observed_at,
            canonical_proof_hash=source.canonical_proof_hash,
        )
        result = RewardStageResult(
            status=STATUS_STAGED,
            reason_code=REASON_STAGED,
            source_id=source.source_id,
            window_id=source.window_id,
            staged_record=staged_record,
        )
        self.store.record_stage_result(result)
        return result

    def balance_for(
        self,
        *,
        window_id: str,
        passport_id: str,
        device_id: str,
    ) -> RewardBalanceView:
        validate_public_identifier("window_id", window_id)
        validate_public_identifier("passport_id", passport_id)
        validate_public_identifier("device_id", device_id)
        window = self.store.get_window(window_id)
        records = self.store.staged_records_for_window(window_id)
        denominator = sum((record.rewardable_score for record in records), ZERO_DECIMAL)
        identity_records = tuple(
            record
            for record in records
            if record.passport_id == passport_id and record.device_id == device_id
        )
        staged_score = sum(
            (record.rewardable_score for record in identity_records),
            ZERO_DECIMAL,
        )
        simulated_credit = ZERO_DECIMAL
        if window is not None and denominator > ZERO_DECIMAL and staged_score > ZERO_DECIMAL:
            with localcontext() as context:
                context.prec = DECIMAL_PRECISION
                simulated_credit = (
                    window.miner_window_emission_cap * staged_score / denominator
                )
        lane_scores: dict[str, Decimal] = {}
        for record in identity_records:
            lane_scores[record.lane] = lane_scores.get(record.lane, ZERO_DECIMAL) + (
                record.rewardable_score
            )
        return RewardBalanceView(
            window_id=window_id,
            passport_id=passport_id,
            device_id=device_id,
            staged_score=staged_score,
            denominator_score=denominator,
            simulated_credit=simulated_credit,
            lane_scores=lane_scores,  # type: ignore[arg-type]
            staged_record_count=len(identity_records),
        )

    def foundation_revenue_records(self) -> tuple[FoundationRevenueRecord, ...]:
        return self.store.foundation_revenue_records()

    def _result(
        self,
        status: str,
        reason_code: str,
        source: RewardAccountingSourceRecord,
        *,
        consume_source_id: bool = True,
    ) -> RewardStageResult:
        result = RewardStageResult(
            status=status,  # type: ignore[arg-type]
            reason_code=reason_code,
            source_id=source.source_id,
            window_id=source.window_id,
        )
        self.store.record_stage_result(result, consume_source_id=consume_source_id)
        return result


def _source_nonrewardable_status(
    source: RewardAccountingSourceRecord,
) -> tuple[str, str] | None:
    if source.anti_cheat_status == ANTI_CHEAT_TAMPERED:
        return STATUS_REJECTED, REASON_ANTI_CHEAT_TAMPERED
    if source.anti_cheat_status == ANTI_CHEAT_DUPLICATE:
        return STATUS_REJECTED, REASON_ANTI_CHEAT_DUPLICATE
    if source.anti_cheat_status == ANTI_CHEAT_REJECTED:
        return STATUS_REJECTED, REASON_ANTI_CHEAT_REJECTED
    if source.anti_cheat_status == ANTI_CHEAT_UNDER_REVIEW:
        return STATUS_UNDER_REVIEW, REASON_ANTI_CHEAT_UNDER_REVIEW
    if source.anti_cheat_status != ANTI_CHEAT_PASSED:
        return STATUS_REJECTED, REASON_ANTI_CHEAT_REJECTED

    if source.authority_status == AUTHORITY_REJECTED:
        return STATUS_REJECTED, source.reason_code or REASON_AUTHORITY_REJECTED
    if source.authority_status == AUTHORITY_NONREWARDABLE:
        return STATUS_REJECTED, source.reason_code or REASON_AUTHORITY_NONREWARDABLE
    if source.authority_status == AUTHORITY_UNDER_REVIEW:
        return STATUS_UNDER_REVIEW, source.reason_code or REASON_AUTHORITY_UNDER_REVIEW
    if source.authority_status not in (AUTHORITY_ACCEPTED, AUTHORITY_REWARDABLE_CANDIDATE):
        return STATUS_REJECTED, source.reason_code or REASON_AUTHORITY_REJECTED
    if source.rewardable_score <= ZERO_DECIMAL:
        return STATUS_REJECTED, REASON_NON_POSITIVE_SCORE
    return None

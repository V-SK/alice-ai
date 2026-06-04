from __future__ import annotations

from datetime import datetime
from decimal import Decimal

from alice_acp.evidence.types import validate_aware_timestamp
from alice_acp.ops_monitor.types import (
    ACTION_ALLOW,
    ACTION_CAP,
    ACTION_KILL_SWITCH_REVIEW,
    ACTION_REJECT,
    ACTION_UNDER_REVIEW,
    REASON_DUPLICATE_PROOF_SPIKE,
    REASON_KILL_SWITCH_REVIEW_RECOMMENDED,
    REASON_MINER_HEARTBEAT_STALE,
    REASON_MINER_ONLINE_RATE_LOW,
    REASON_MODEL_INFERENCE_ERROR_SPIKE,
    REASON_MODEL_INFERENCE_QUEUE_DEPTH_HIGH,
    REASON_MODEL_INFERENCE_TIMEOUT_SPIKE,
    REASON_PAYOUT_MISMATCH_SEEN,
    REASON_POOL_EVIDENCE_MISSING_RATE_HIGH,
    REASON_PROOF_REJECTION_SPIKE,
    REASON_STALE_PROOF_SPIKE,
    REASON_TAMPER_PROOF_SEEN,
    SEVERITY_CRITICAL,
    SEVERITY_WARNING,
    MinerHeartbeatSample,
    MinerHeartbeatSummary,
    ModelInferenceHealthCounters,
    OpsMonitorAlertPayload,
    OpsMonitorEvaluation,
    OpsMonitorSnapshot,
    OpsMonitorThresholds,
    RecommendedAction,
)


def summarize_miner_heartbeats(
    samples: tuple[MinerHeartbeatSample, ...],
    *,
    observed_at: datetime | None = None,
    thresholds: OpsMonitorThresholds | None = None,
) -> MinerHeartbeatSummary:
    active_thresholds = thresholds or OpsMonitorThresholds()
    if observed_at is not None:
        validate_aware_timestamp("observed_at", observed_at)
    summary_observed_at = observed_at or (samples[0].observed_at if samples else None)
    if summary_observed_at is None:
        raise ValueError("observed_at_required_when_heartbeat_samples_are_empty")

    expected_samples = tuple(sample for sample in samples if sample.expected_online)
    if not expected_samples:
        return MinerHeartbeatSummary(
            observed_at=summary_observed_at,
            expected_count=0,
            online_count=0,
            stale_count=0,
            offline_count=0,
        )

    stale_miner_ids: list[str] = []
    offline_miner_ids: list[str] = []
    online_count = 0
    for sample in expected_samples:
        heartbeat_age = sample.heartbeat_age_seconds()
        if heartbeat_age is None:
            offline_miner_ids.append(sample.miner_id)
        elif heartbeat_age > active_thresholds.max_heartbeat_age_seconds:
            stale_miner_ids.append(sample.miner_id)
        else:
            online_count += 1

    return MinerHeartbeatSummary(
        observed_at=expected_samples[0].observed_at,
        expected_count=len(expected_samples),
        online_count=online_count,
        stale_count=len(stale_miner_ids),
        offline_count=len(offline_miner_ids),
        stale_miner_ids=tuple(stale_miner_ids),
        offline_miner_ids=tuple(offline_miner_ids),
    )


def evaluate_ops_monitor(
    snapshot: OpsMonitorSnapshot,
    thresholds: OpsMonitorThresholds | None = None,
) -> OpsMonitorEvaluation:
    active_thresholds = thresholds or OpsMonitorThresholds()
    miner_summary = summarize_miner_heartbeats(
        snapshot.miner_heartbeats,
        observed_at=snapshot.observed_at,
        thresholds=active_thresholds,
    )
    alerts: list[OpsMonitorAlertPayload] = []

    _append_miner_alerts(alerts, miner_summary, snapshot, active_thresholds)
    _append_proof_alerts(alerts, snapshot, active_thresholds)
    if snapshot.model_inference is not None:
        _append_model_alerts(alerts, snapshot.model_inference, snapshot, active_thresholds)

    preliminary_action = _recommended_action(
        tuple(alerts),
        kill_switch_review_requested=snapshot.kill_switch_review_requested,
        thresholds=active_thresholds,
    )
    if preliminary_action == ACTION_KILL_SWITCH_REVIEW:
        alerts.append(
            _alert(
                snapshot,
                alert_id="kill_switch_review",
                severity=SEVERITY_CRITICAL,
                reason_code=REASON_KILL_SWITCH_REVIEW_RECOMMENDED,
                message="ops monitor recommends kill-switch review only",
                recommended_action=ACTION_KILL_SWITCH_REVIEW,
                metric_value=_critical_alert_count(tuple(alerts)),
                threshold_value=active_thresholds.kill_switch_review_critical_alerts,
            )
        )

    action = _recommended_action(
        tuple(alerts),
        kill_switch_review_requested=snapshot.kill_switch_review_requested,
        thresholds=active_thresholds,
    )

    return OpsMonitorEvaluation(
        observed_at=snapshot.observed_at,
        miner_heartbeat=miner_summary,
        proof_ingest=snapshot.proof_ingest,
        model_inference=snapshot.model_inference,
        alerts=tuple(alerts),
        recommended_action=action,
        kill_switch_review_recommended=action == ACTION_KILL_SWITCH_REVIEW,
        kill_switch_auto_triggered=False,
        production_mutation_allowed=False,
        live_reward_enabled=False,
        payout_executor_enabled=False,
        chain_transfer_enabled=False,
        contract_version=snapshot.contract_version,
    )


def _append_miner_alerts(
    alerts: list[OpsMonitorAlertPayload],
    summary: MinerHeartbeatSummary,
    snapshot: OpsMonitorSnapshot,
    thresholds: OpsMonitorThresholds,
) -> None:
    if summary.stale_count or summary.offline_count:
        alerts.append(
            _alert(
                snapshot,
                alert_id="miner_heartbeat_stale",
                severity=SEVERITY_WARNING,
                reason_code=REASON_MINER_HEARTBEAT_STALE,
                message="miner heartbeat is stale or missing",
                recommended_action=ACTION_CAP,
                metric_value=summary.stale_count + summary.offline_count,
                threshold_value=0,
            )
        )

    online_rate = summary.online_rate
    if online_rate <= thresholds.critical_miner_online_rate:
        severity = SEVERITY_CRITICAL
        action = ACTION_UNDER_REVIEW
        threshold_value = thresholds.critical_miner_online_rate
    elif online_rate < thresholds.warning_miner_online_rate:
        severity = SEVERITY_WARNING
        action = ACTION_CAP
        threshold_value = thresholds.warning_miner_online_rate
    else:
        return
    alerts.append(
        _alert(
            snapshot,
            alert_id="miner_online_rate_low",
            severity=severity,
            reason_code=REASON_MINER_ONLINE_RATE_LOW,
            message="miner online heartbeat rate is below threshold",
            recommended_action=action,
            metric_value=online_rate,
            threshold_value=threshold_value,
        )
    )


def _append_proof_alerts(
    alerts: list[OpsMonitorAlertPayload],
    snapshot: OpsMonitorSnapshot,
    thresholds: OpsMonitorThresholds,
) -> None:
    proof = snapshot.proof_ingest
    _append_rate_alert(
        alerts,
        snapshot,
        rate=proof.rejection_rate,
        warning=thresholds.warning_proof_rejection_rate,
        critical=thresholds.critical_proof_rejection_rate,
        alert_id="proof_rejection_spike",
        reason_code=REASON_PROOF_REJECTION_SPIKE,
        message="proof ingest rejection rate exceeded threshold",
    )
    _append_rate_alert(
        alerts,
        snapshot,
        rate=proof.duplicate_rate,
        warning=thresholds.warning_duplicate_rate,
        critical=thresholds.critical_duplicate_rate,
        alert_id="duplicate_proof_spike",
        reason_code=REASON_DUPLICATE_PROOF_SPIKE,
        message="duplicate proof rate exceeded threshold",
    )
    _append_rate_alert(
        alerts,
        snapshot,
        rate=proof.stale_rate,
        warning=thresholds.warning_stale_rate,
        critical=thresholds.critical_stale_rate,
        alert_id="stale_proof_spike",
        reason_code=REASON_STALE_PROOF_SPIKE,
        message="stale proof rate exceeded threshold",
    )
    _append_rate_alert(
        alerts,
        snapshot,
        rate=proof.pool_evidence_missing_rate,
        warning=thresholds.warning_pool_evidence_missing_rate,
        critical=thresholds.critical_pool_evidence_missing_rate,
        alert_id="pool_evidence_missing_rate_high",
        reason_code=REASON_POOL_EVIDENCE_MISSING_RATE_HIGH,
        message="pool-side evidence missing rate exceeded threshold",
    )

    if proof.tamper_count > 0:
        alerts.append(
            _alert(
                snapshot,
                alert_id="tamper_proof_seen",
                severity=SEVERITY_CRITICAL,
                reason_code=REASON_TAMPER_PROOF_SEEN,
                message="tampered proof counter is non-zero",
                recommended_action=ACTION_REJECT,
                metric_value=proof.tamper_count,
                threshold_value=0,
            )
        )
    if proof.payout_mismatch_count > 0:
        alerts.append(
            _alert(
                snapshot,
                alert_id="payout_mismatch_seen",
                severity=SEVERITY_CRITICAL,
                reason_code=REASON_PAYOUT_MISMATCH_SEEN,
                message="payout mismatch counter is non-zero",
                recommended_action=ACTION_REJECT,
                metric_value=proof.payout_mismatch_count,
                threshold_value=0,
            )
        )


def _append_model_alerts(
    alerts: list[OpsMonitorAlertPayload],
    model: ModelInferenceHealthCounters,
    snapshot: OpsMonitorSnapshot,
    thresholds: OpsMonitorThresholds,
) -> None:
    _append_rate_alert(
        alerts,
        snapshot,
        rate=model.error_rate,
        warning=thresholds.warning_model_error_rate,
        critical=thresholds.critical_model_error_rate,
        alert_id="model_inference_error_spike",
        reason_code=REASON_MODEL_INFERENCE_ERROR_SPIKE,
        message="model inference error rate exceeded threshold",
    )
    _append_rate_alert(
        alerts,
        snapshot,
        rate=model.timeout_rate,
        warning=thresholds.warning_model_timeout_rate,
        critical=thresholds.critical_model_timeout_rate,
        alert_id="model_inference_timeout_spike",
        reason_code=REASON_MODEL_INFERENCE_TIMEOUT_SPIKE,
        message="model inference timeout rate exceeded threshold",
    )
    if model.queued_count >= thresholds.critical_model_queue_depth:
        alerts.append(
            _alert(
                snapshot,
                alert_id="model_inference_queue_depth_high",
                severity=SEVERITY_CRITICAL,
                reason_code=REASON_MODEL_INFERENCE_QUEUE_DEPTH_HIGH,
                message="model inference queue depth exceeded critical threshold",
                recommended_action=ACTION_UNDER_REVIEW,
                metric_value=model.queued_count,
                threshold_value=thresholds.critical_model_queue_depth,
            )
        )
    elif model.queued_count >= thresholds.warning_model_queue_depth:
        alerts.append(
            _alert(
                snapshot,
                alert_id="model_inference_queue_depth_high",
                severity=SEVERITY_WARNING,
                reason_code=REASON_MODEL_INFERENCE_QUEUE_DEPTH_HIGH,
                message="model inference queue depth exceeded warning threshold",
                recommended_action=ACTION_CAP,
                metric_value=model.queued_count,
                threshold_value=thresholds.warning_model_queue_depth,
            )
        )


def _append_rate_alert(
    alerts: list[OpsMonitorAlertPayload],
    snapshot: OpsMonitorSnapshot,
    *,
    rate: Decimal,
    warning: Decimal,
    critical: Decimal,
    alert_id: str,
    reason_code: str,
    message: str,
) -> None:
    if rate >= critical:
        alerts.append(
            _alert(
                snapshot,
                alert_id=alert_id,
                severity=SEVERITY_CRITICAL,
                reason_code=reason_code,
                message=message,
                recommended_action=ACTION_UNDER_REVIEW,
                metric_value=rate,
                threshold_value=critical,
            )
        )
    elif rate >= warning:
        alerts.append(
            _alert(
                snapshot,
                alert_id=alert_id,
                severity=SEVERITY_WARNING,
                reason_code=reason_code,
                message=message,
                recommended_action=ACTION_CAP,
                metric_value=rate,
                threshold_value=warning,
            )
        )


def _recommended_action(
    alerts: tuple[OpsMonitorAlertPayload, ...],
    *,
    kill_switch_review_requested: bool,
    thresholds: OpsMonitorThresholds,
) -> RecommendedAction:
    if kill_switch_review_requested:
        return ACTION_KILL_SWITCH_REVIEW
    if _critical_alert_count(alerts) >= thresholds.kill_switch_review_critical_alerts:
        return ACTION_KILL_SWITCH_REVIEW
    if any(alert.recommended_action == ACTION_REJECT for alert in alerts):
        return ACTION_REJECT
    if any(alert.severity == SEVERITY_CRITICAL for alert in alerts):
        return ACTION_UNDER_REVIEW
    if any(alert.severity == SEVERITY_WARNING for alert in alerts):
        return ACTION_CAP
    return ACTION_ALLOW


def _critical_alert_count(alerts: tuple[OpsMonitorAlertPayload, ...]) -> int:
    return sum(1 for alert in alerts if alert.severity == SEVERITY_CRITICAL)


def _alert(
    snapshot: OpsMonitorSnapshot,
    *,
    alert_id: str,
    severity: str,
    reason_code: str,
    message: str,
    recommended_action: str,
    metric_value: Decimal | int | str | None,
    threshold_value: Decimal | int | str | None,
) -> OpsMonitorAlertPayload:
    return OpsMonitorAlertPayload(
        alert_id=alert_id,
        severity=severity,  # type: ignore[arg-type]
        reason_code=reason_code,
        message=message,
        observed_at=snapshot.observed_at,
        recommended_action=recommended_action,  # type: ignore[arg-type]
        metric_value=metric_value,
        threshold_value=threshold_value,
        contract_version=snapshot.contract_version,
    )

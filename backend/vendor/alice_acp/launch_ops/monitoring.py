from __future__ import annotations

from decimal import Decimal

from alice_acp.launch_ops.types import (
    HEALTH_BAD,
    HEALTH_DEGRADED,
    HEALTH_UNKNOWN,
    READINESS_BAD,
    READINESS_DEGRADED,
    READINESS_GREEN,
    REASON_COMPONENT_BAD,
    REASON_COMPONENT_DEGRADED,
    REASON_HEARTBEAT_STALE,
    REASON_JSONL_GROWTH_STALLED,
    REASON_LIVE_REWARD_DEFAULT_OFF,
    REASON_MODEL_QUEUE_DEPTH_HIGH,
    REASON_PAYOUT_EXECUTOR_DEFAULT_OFF,
    REASON_PROOF_REJECTION_SPIKE,
    REASON_RATE_LIMIT_SPIKE,
    REASON_RESERVE_BUDGET_LOW,
    REASON_RESERVE_BUDGET_MISSING,
    SEVERITY_CRITICAL,
    SEVERITY_WARNING,
    LaunchOpsAlert,
    LaunchOpsEvaluation,
    LaunchOpsSnapshot,
    LaunchOpsThresholds,
)


def evaluate_launch_ops(
    snapshot: LaunchOpsSnapshot,
    thresholds: LaunchOpsThresholds | None = None,
) -> LaunchOpsEvaluation:
    thresholds = thresholds or LaunchOpsThresholds()
    alerts: list[LaunchOpsAlert] = []

    for component in snapshot.components:
        if component.status == HEALTH_BAD:
            alerts.append(
                LaunchOpsAlert(
                    severity=SEVERITY_CRITICAL,
                    reason_code=REASON_COMPONENT_BAD,
                    message=f"{component.component} reported bad health",
                    component=component.component,
                )
            )
        elif component.status in (HEALTH_DEGRADED, HEALTH_UNKNOWN):
            alerts.append(
                LaunchOpsAlert(
                    severity=SEVERITY_WARNING,
                    reason_code=REASON_COMPONENT_DEGRADED,
                    message=f"{component.component} reported degraded health",
                    component=component.component,
                )
            )

        heartbeat_age = component.heartbeat_age_seconds(now=snapshot.observed_at)
        if heartbeat_age is not None and heartbeat_age > thresholds.max_heartbeat_age_seconds:
            severity = (
                SEVERITY_CRITICAL
                if heartbeat_age > thresholds.max_heartbeat_age_seconds * 2
                else SEVERITY_WARNING
            )
            alerts.append(
                LaunchOpsAlert(
                    severity=severity,
                    reason_code=REASON_HEARTBEAT_STALE,
                    message=f"{component.component} heartbeat is stale",
                    component=component.component,
                )
            )

    for metric in snapshot.jsonl_growth:
        if (
            metric.expected_to_grow
            and metric.window_seconds >= thresholds.max_jsonl_idle_seconds
            and metric.growth_bytes == 0
        ):
            alerts.append(
                LaunchOpsAlert(
                    severity=SEVERITY_WARNING,
                    reason_code=REASON_JSONL_GROWTH_STALLED,
                    message=f"{metric.stream_name} jsonl growth stalled",
                )
            )

    rejection_rate = snapshot.proof_rejection.rejection_rate
    if rejection_rate >= thresholds.critical_proof_rejection_rate:
        alerts.append(
            LaunchOpsAlert(
                severity=SEVERITY_CRITICAL,
                reason_code=REASON_PROOF_REJECTION_SPIKE,
                message="miner proof rejection rate exceeded critical threshold",
                component="miner_proof",
            )
        )
    elif rejection_rate >= thresholds.warning_proof_rejection_rate:
        alerts.append(
            LaunchOpsAlert(
                severity=SEVERITY_WARNING,
                reason_code=REASON_PROOF_REJECTION_SPIKE,
                message="miner proof rejection rate exceeded warning threshold",
                component="miner_proof",
            )
        )

    if snapshot.api_chat_queue.model_queue_depth >= thresholds.critical_model_queue_depth:
        alerts.append(
            LaunchOpsAlert(
                severity=SEVERITY_CRITICAL,
                reason_code=REASON_MODEL_QUEUE_DEPTH_HIGH,
                message="api chat model queue depth exceeded critical threshold",
                component="api_chat",
            )
        )
    elif snapshot.api_chat_queue.model_queue_depth >= thresholds.warning_model_queue_depth:
        alerts.append(
            LaunchOpsAlert(
                severity=SEVERITY_WARNING,
                reason_code=REASON_MODEL_QUEUE_DEPTH_HIGH,
                message="api chat model queue depth exceeded warning threshold",
                component="api_chat",
            )
        )

    if snapshot.rate_limits.total_limited >= thresholds.critical_rate_limit_events:
        alerts.append(
            LaunchOpsAlert(
                severity=SEVERITY_CRITICAL,
                reason_code=REASON_RATE_LIMIT_SPIKE,
                message="rate limit counters exceeded critical threshold",
                component="api_chat",
            )
        )
    elif snapshot.rate_limits.total_limited >= thresholds.warning_rate_limit_events:
        alerts.append(
            LaunchOpsAlert(
                severity=SEVERITY_WARNING,
                reason_code=REASON_RATE_LIMIT_SPIKE,
                message="rate limit counters exceeded warning threshold",
                component="api_chat",
            )
        )

    reserve_budget = snapshot.reserve_budget
    if not reserve_budget.reserve_budget_confirmed or reserve_budget.reserve_ratio is None:
        alerts.append(
            LaunchOpsAlert(
                severity=SEVERITY_CRITICAL,
                reason_code=REASON_RESERVE_BUDGET_MISSING,
                message="reserve budget evidence is missing",
                component="reward_backend",
            )
        )
    elif reserve_budget.reserve_ratio < thresholds.required_reserve_ratio:
        alerts.append(
            LaunchOpsAlert(
                severity=SEVERITY_CRITICAL,
                reason_code=REASON_RESERVE_BUDGET_LOW,
                message="reserve budget ratio is below required threshold",
                component="reward_backend",
            )
        )

    alert_tuple = tuple(alerts)
    hard_blockers = _hard_blockers(alert_tuple)
    status = _status_for_alerts(alert_tuple)

    return LaunchOpsEvaluation(
        status=status,
        alerts=alert_tuple,
        hard_blockers=hard_blockers,
        can_open_monitoring_observation=status == READINESS_GREEN,
        can_open_miner_public_entry=status == READINESS_GREEN and not hard_blockers,
        deferred_capability_gates=_deferred_capability_gates(),
        can_start_live_reward_window=False,
        can_start_payout_executor=False,
        live_reward_enabled=snapshot.gates.live_reward_enabled,
        payout_executor_enabled=snapshot.gates.payout_executor_enabled,
    )


def _status_for_alerts(alerts: tuple[LaunchOpsAlert, ...]) -> str:
    if any(alert.severity == SEVERITY_CRITICAL for alert in alerts):
        return READINESS_BAD
    if any(alert.severity == SEVERITY_WARNING for alert in alerts):
        return READINESS_DEGRADED
    return READINESS_GREEN


def _hard_blockers(alerts: tuple[LaunchOpsAlert, ...]) -> tuple[str, ...]:
    # Operational blockers only. Reward/payout being default-off is a *deferred
    # capability* (it gates the still-closed reward window, NOT production-primary
    # serving), and is reported via deferred_capability_gates. This decouples
    # "adapter live as primary public adapter" from "reward window open".
    blockers = [
        alert.reason_code for alert in alerts if alert.severity == SEVERITY_CRITICAL
    ]
    return tuple(dict.fromkeys(blockers))


def _deferred_capability_gates() -> tuple[str, ...]:
    # Reward + payout stay default-off; they are NOT operational blockers of the
    # production-primary public adapter, only of the (still-closed) reward window.
    return (REASON_LIVE_REWARD_DEFAULT_OFF, REASON_PAYOUT_EXECUTOR_DEFAULT_OFF)


def rejection_rate_percent(snapshot: LaunchOpsSnapshot) -> Decimal:
    return snapshot.proof_rejection.rejection_rate * Decimal("100")

from __future__ import annotations

from alice_acp.mining_scheduler.types import (
    AiDemandSignal,
    DeviceEligibility,
    SchedulerPreemptionDecision,
)
from alice_acp.mining_supervisor import MiningSupervisorStateMachine, SupervisorSnapshot

MINING_PRIORITY_DEFAULT_CONTINUE = "MINING_PRIORITY_DEFAULT_CONTINUE"
AI_DEMAND_INELIGIBLE_FOR_DEVICE = "AI_DEMAND_INELIGIBLE_FOR_DEVICE"
AI_DEMAND_THROTTLE_MINING = "AI_DEMAND_THROTTLE_MINING"
AI_DEMAND_PAUSE_MINING = "AI_DEMAND_PAUSE_MINING"
AI_DEMAND_RUN_TASK = "AI_DEMAND_RUN_TASK"
AI_DEMAND_CLEARED_RESUME_MINING = "AI_DEMAND_CLEARED_RESUME_MINING"
AI_TASK_FAILED_RESUME_MINING = "AI_TASK_FAILED_RESUME_MINING"


def decide_preemption(
    *,
    supervisor: SupervisorSnapshot,
    device: DeviceEligibility,
    ai_demand: AiDemandSignal | None = None,
) -> SchedulerPreemptionDecision:
    if ai_demand is None:
        return _default_or_resume(supervisor, reason_code=MINING_PRIORITY_DEFAULT_CONTINUE)
    if ai_demand.status == "failed":
        return _default_or_resume(
            supervisor,
            reason_code=AI_TASK_FAILED_RESUME_MINING,
            demand_id=ai_demand.demand_id,
        )
    if ai_demand.status == "cleared" or not ai_demand.admitted:
        return _default_or_resume(
            supervisor,
            reason_code=AI_DEMAND_CLEARED_RESUME_MINING,
            demand_id=ai_demand.demand_id,
        )
    if not _eligible(device, ai_demand):
        return SchedulerPreemptionDecision(
            action="continue_mining",
            reason_code=AI_DEMAND_INELIGIBLE_FOR_DEVICE,
            demand_id=ai_demand.demand_id,
        )
    if supervisor.state in {"throttled_for_ai", "paused_for_ai"}:
        return SchedulerPreemptionDecision(
            action="run_ai_task",
            reason_code=AI_DEMAND_RUN_TASK,
            demand_id=ai_demand.demand_id,
        )
    if ai_demand.mode == "exclusive_device":
        return SchedulerPreemptionDecision(
            action="pause_mining",
            reason_code=AI_DEMAND_PAUSE_MINING,
            demand_id=ai_demand.demand_id,
        )
    return SchedulerPreemptionDecision(
        action="throttle_mining",
        reason_code=AI_DEMAND_THROTTLE_MINING,
        demand_id=ai_demand.demand_id,
    )


def apply_preemption_decision(
    machine: MiningSupervisorStateMachine,
    decision: SchedulerPreemptionDecision,
) -> SupervisorSnapshot:
    if decision.action == "throttle_mining":
        return machine.apply_ai_demand(demand_exists=True)
    if decision.action == "pause_mining":
        return machine.pause_for_ai()
    if decision.action == "resume_mining":
        return machine.resume_after_ai()
    return machine.snapshot


def _default_or_resume(
    supervisor: SupervisorSnapshot,
    *,
    reason_code: str,
    demand_id: str | None = None,
) -> SchedulerPreemptionDecision:
    if supervisor.state in {"throttled_for_ai", "paused_for_ai"}:
        return SchedulerPreemptionDecision(
            action="resume_mining",
            reason_code=reason_code,
            demand_id=demand_id,
        )
    return SchedulerPreemptionDecision(
        action="continue_mining",
        reason_code=reason_code,
        demand_id=demand_id,
    )


def _eligible(device: DeviceEligibility, ai_demand: AiDemandSignal) -> bool:
    return (
        device.mining_supported
        and device.ai_supported
        and device.device_class == ai_demand.device_class
        and device.region == ai_demand.region
        and ai_demand.route_capability in device.route_capabilities
    )

"""Mining-first scheduler preemption contracts."""

from alice_acp.mining_scheduler.preemption import (
    AI_DEMAND_CLEARED_RESUME_MINING,
    AI_DEMAND_INELIGIBLE_FOR_DEVICE,
    AI_DEMAND_PAUSE_MINING,
    AI_DEMAND_RUN_TASK,
    AI_DEMAND_THROTTLE_MINING,
    AI_TASK_FAILED_RESUME_MINING,
    MINING_PRIORITY_DEFAULT_CONTINUE,
    apply_preemption_decision,
    decide_preemption,
)
from alice_acp.mining_scheduler.types import (
    AiDemandSignal,
    DeviceEligibility,
    SchedulerPreemptionDecision,
)

__all__ = [
    "AI_DEMAND_CLEARED_RESUME_MINING",
    "AI_DEMAND_INELIGIBLE_FOR_DEVICE",
    "AI_DEMAND_PAUSE_MINING",
    "AI_DEMAND_RUN_TASK",
    "AI_DEMAND_THROTTLE_MINING",
    "AI_TASK_FAILED_RESUME_MINING",
    "MINING_PRIORITY_DEFAULT_CONTINUE",
    "AiDemandSignal",
    "DeviceEligibility",
    "SchedulerPreemptionDecision",
    "apply_preemption_decision",
    "decide_preemption",
]

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SchedulerAction = Literal[
    "continue_mining",
    "throttle_mining",
    "pause_mining",
    "run_ai_task",
    "resume_mining",
]
AiDemandStatus = Literal["active", "cleared", "failed"]
AiDemandMode = Literal["shared_device", "exclusive_device"]


@dataclass(frozen=True, slots=True)
class DeviceEligibility:
    device_id: str
    device_class: str
    region: str
    route_capabilities: tuple[str, ...]
    mining_supported: bool
    ai_supported: bool

    def __post_init__(self) -> None:
        if not self.device_id or not self.device_class or not self.region:
            raise ValueError("device eligibility identifiers must be non-empty")
        if any(not capability for capability in self.route_capabilities):
            raise ValueError("route capabilities must be non-empty")


@dataclass(frozen=True, slots=True)
class AiDemandSignal:
    demand_id: str
    admitted: bool
    device_class: str
    region: str
    route_capability: str
    status: AiDemandStatus = "active"
    mode: AiDemandMode = "shared_device"

    def __post_init__(self) -> None:
        required = (
            self.demand_id,
            self.device_class,
            self.region,
            self.route_capability,
            self.status,
            self.mode,
        )
        if any(not value for value in required):
            raise ValueError("AI demand signal fields must be non-empty")


@dataclass(frozen=True, slots=True)
class SchedulerPreemptionDecision:
    action: SchedulerAction
    reason_code: str
    demand_id: str | None = None

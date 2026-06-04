from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

SupervisorState = Literal[
    "stopped",
    "starting",
    "mining",
    "throttled_for_ai",
    "paused_for_ai",
    "restarting",
    "failed",
]


@dataclass(frozen=True, slots=True)
class SupervisorSnapshot:
    state: SupervisorState
    session_id: str | None = None
    reason_code: str | None = None
    ai_demand_active: bool = False

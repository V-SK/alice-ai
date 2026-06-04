from __future__ import annotations

from dataclasses import dataclass, field

from alice_acp.mining_device.types import BackendCapabilityResult
from alice_acp.mining_supervisor.types import SupervisorSnapshot

SUPERVISOR_STARTED = "SUPERVISOR_STARTED"
SUPERVISOR_STOPPED = "SUPERVISOR_STOPPED"
SUPERVISOR_THROTTLED_FOR_AI = "SUPERVISOR_THROTTLED_FOR_AI"
SUPERVISOR_PAUSED_FOR_AI = "SUPERVISOR_PAUSED_FOR_AI"
SUPERVISOR_AI_DEMAND_CLEARED = "SUPERVISOR_AI_DEMAND_CLEARED"
SUPERVISOR_AI_RESUME_AFTER_FAILURE = "SUPERVISOR_AI_RESUME_AFTER_FAILURE"
SUPERVISOR_RESTARTING_AFTER_CRASH = "SUPERVISOR_RESTARTING_AFTER_CRASH"
SUPERVISOR_FAILED_AFTER_CRASH = "SUPERVISOR_FAILED_AFTER_CRASH"
SUPERVISOR_BACKEND_UNSUPPORTED = "SUPERVISOR_BACKEND_UNSUPPORTED"


@dataclass(slots=True)
class MiningSupervisorStateMachine:
    snapshot: SupervisorSnapshot = field(
        default_factory=lambda: SupervisorSnapshot(state="stopped")
    )

    def start(
        self,
        *,
        session_id: str,
        backend: BackendCapabilityResult,
    ) -> SupervisorSnapshot:
        if not session_id:
            raise ValueError("session_id must be non-empty")
        if not backend.mining_supported:
            self.snapshot = SupervisorSnapshot(
                state="failed",
                session_id=session_id,
                reason_code=backend.reason_code or SUPERVISOR_BACKEND_UNSUPPORTED,
            )
            return self.snapshot
        self.snapshot = SupervisorSnapshot(
            state="mining",
            session_id=session_id,
            reason_code=SUPERVISOR_STARTED,
        )
        return self.snapshot

    def stop(self) -> SupervisorSnapshot:
        self.snapshot = SupervisorSnapshot(
            state="stopped",
            session_id=self.snapshot.session_id,
            reason_code=SUPERVISOR_STOPPED,
        )
        return self.snapshot

    def record_crash(self, *, restart_allowed: bool) -> SupervisorSnapshot:
        self.snapshot = SupervisorSnapshot(
            state="restarting" if restart_allowed else "failed",
            session_id=self.snapshot.session_id,
            reason_code=(
                SUPERVISOR_RESTARTING_AFTER_CRASH
                if restart_allowed
                else SUPERVISOR_FAILED_AFTER_CRASH
            ),
        )
        return self.snapshot

    def apply_ai_demand(self, *, demand_exists: bool) -> SupervisorSnapshot:
        if demand_exists and self.snapshot.state == "mining":
            self.snapshot = SupervisorSnapshot(
                state="throttled_for_ai",
                session_id=self.snapshot.session_id,
                reason_code=SUPERVISOR_THROTTLED_FOR_AI,
                ai_demand_active=True,
            )
            return self.snapshot
        if not demand_exists and self.snapshot.state in {"throttled_for_ai", "paused_for_ai"}:
            self.snapshot = SupervisorSnapshot(
                state="mining",
                session_id=self.snapshot.session_id,
                reason_code=SUPERVISOR_AI_DEMAND_CLEARED,
                ai_demand_active=False,
            )
            return self.snapshot
        return self.snapshot

    def pause_for_ai(self) -> SupervisorSnapshot:
        if self.snapshot.state in {"mining", "throttled_for_ai"}:
            self.snapshot = SupervisorSnapshot(
                state="paused_for_ai",
                session_id=self.snapshot.session_id,
                reason_code=SUPERVISOR_PAUSED_FOR_AI,
                ai_demand_active=True,
            )
        return self.snapshot

    def resume_after_ai(self) -> SupervisorSnapshot:
        if self.snapshot.state in {"throttled_for_ai", "paused_for_ai"}:
            self.snapshot = SupervisorSnapshot(
                state="mining",
                session_id=self.snapshot.session_id,
                reason_code=SUPERVISOR_AI_RESUME_AFTER_FAILURE,
                ai_demand_active=False,
            )
        return self.snapshot

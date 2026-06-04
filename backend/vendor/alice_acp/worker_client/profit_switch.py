"""M6: the GPU PROFIT-SWITCH supervisor (dispatch plan §5) -- CLIENT-AUTONOMOUS.

Each worker runs a dual-state loop. There is NO server push: the worker itself
decides when to mine and when to serve AI, driven only by whether an AI job is
available and a local idle timer.

States:

* **MINING_PRL (default):** mine PRL on the GPU **and** lightweight long-poll the
  Alice AI queue over plain HTTP (no GPU cost). This is where the worker spends
  most of its time.
* **SERVING_AI:** an AI job arrived -> STOP PRL (GPU memory is exclusive: the PRL
  DAG/working-set vs the model weights), load the model if cold, serve, and keep
  serving while jobs keep arriving. Each served job RESETS a 10-minute idle timer.
* **10-min idle (no AI demand) -> unload the model, RESTART PRL -> MINING_PRL.**

Cold-start mitigation (plan §5, V to confirm ①+②):

* ``accept_early`` -- the FIRST request after a >10-min idle is slow (stopping PRL
  + loading the model is seconds..tens-of-seconds). The supervisor exposes this so
  the client/UI can show "spinning up compute" rather than time out.
* ``warm_model_tier`` -- a small always-warm tier (the 4B) the verification VPS /
  client keeps ready to answer the first request while the GPU warms behind it.

GATE COUPLING (M6): the profit-switch only switches to AI when the device is BOTH
(a) offered an AI job AND (b) eligible -- i.e. it has passed the server's 72h
mandatory-mining entry gate. Until the device is gate-eligible, an AI job offer is
IGNORED and the worker stays mining PRL (it is serving its mandatory window). The
gate state is set by the client from the server's pull response (the edge rejects
an under-72h pull with ``api_chat_mining_entry_gate_under_72h``); this supervisor
treats ``gate_eligible=False`` as "no eligible AI demand".

This module is a PURE state machine driven by explicit ``now`` timestamps (the same
time-injection convention as the rest of the codebase) so the transitions are unit
-testable WITHOUT real mining hardware or a real GPU. The actual stop/start of PRL
and the model load/serve are delegated to injected callbacks (the CLI wires these
to the real mining runtime + the AI role); the default callbacks are inert no-ops
so a test/dry-run exercises the full transition logic with no side effects.

CREDIT-ONLY: nothing here reads/writes a secret, key, payout, or ``paid_acu``. It
only sequences local PRL<->AI compute on the worker's own box.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Literal

from alice_acp.api_chat.types import utc_now
from alice_acp.api_chat.validators import validate_aware_timestamp

PROFIT_SWITCH_CONTRACT_VERSION = "alice-worker-profit-switch-contract-v1"

#: The dual states (plan §5). PRL is the DEFAULT; AI is on demand.
ProfitSwitchState = Literal["mining_prl", "serving_ai"]
STATE_MINING_PRL: ProfitSwitchState = "mining_prl"
STATE_SERVING_AI: ProfitSwitchState = "serving_ai"

#: The idle window after the last AI job before the worker unloads the model and
#: returns to PRL mining (plan §5: "10-min idle -> restart PRL").
DEFAULT_AI_IDLE_COOLDOWN = timedelta(minutes=10)

#: Reason codes for each transition (audit / the client's structured logs).
REASON_PRL_STARTED = "profit_switch_prl_started"
REASON_SWITCHED_TO_AI = "profit_switch_switched_to_ai"
REASON_AI_JOB_SERVED = "profit_switch_ai_job_served"
REASON_AI_IDLE_COOLDOWN_ELAPSED = "profit_switch_ai_idle_cooldown_elapsed_back_to_prl"
REASON_AI_JOB_IGNORED_NOT_ELIGIBLE = "profit_switch_ai_job_ignored_device_not_gate_eligible"
REASON_AI_JOB_IGNORED_NO_DEMAND = "profit_switch_ai_job_ignored_no_demand"
REASON_HOLDING_AI = "profit_switch_holding_ai_within_cooldown"
REASON_HOLDING_PRL = "profit_switch_holding_prl_no_demand"


@dataclass(frozen=True, slots=True)
class ProfitSwitchSnapshot:
    """An immutable view of the supervisor's state after a transition.

    ``state`` is the current dual state; ``ai_idle_deadline`` is when (in
    SERVING_AI) the worker will fall back to PRL if no further AI job arrives;
    ``jobs_served_this_session`` counts AI jobs served since the last switch INTO
    AI; ``model_warm`` is whether the GPU model is loaded (warm) right now;
    ``cold_start_pending`` is True when the NEXT AI job will pay the cold-start
    (the model is not warm) so the client/UI can signal "spinning up compute".
    """

    state: ProfitSwitchState
    reason_code: str
    ai_idle_deadline: datetime | None = None
    jobs_served_this_session: int = 0
    model_warm: bool = False
    cold_start_pending: bool = True

    @property
    def is_mining(self) -> bool:
        return self.state == STATE_MINING_PRL

    @property
    def is_serving_ai(self) -> bool:
        return self.state == STATE_SERVING_AI

    def to_public_dict(self) -> dict[str, object]:
        return {
            "contract_version": PROFIT_SWITCH_CONTRACT_VERSION,
            "state": self.state,
            "reason_code": self.reason_code,
            "ai_idle_deadline": (
                self.ai_idle_deadline.isoformat() if self.ai_idle_deadline is not None else None
            ),
            "jobs_served_this_session": self.jobs_served_this_session,
            "model_warm": self.model_warm,
            "cold_start_pending": self.cold_start_pending,
            # Credit-only: the switch only sequences local compute.
            "paid_acu": "0",
        }


def _noop() -> None:
    """Default inert side-effect callback (a dry-run / unit test exercises logic)."""


@dataclass(slots=True)
class ProfitSwitchSupervisor:
    """The client-autonomous PRL<->AI dual-state machine (plan §5).

    Drive it with the worker loop's signals:

    * :meth:`start` -- enter MINING_PRL (the default) and start PRL.
    * :meth:`on_ai_job` -- an AI job is available NOW: switch to AI if the device is
      gate-eligible (stop PRL, load the model if cold, serve), else ignore it and
      keep mining (the device is still serving its mandatory 72h window).
    * :meth:`on_ai_job_served` -- mark one AI job served (resets the idle timer).
    * :meth:`poll` -- the idempotent tick: if SERVING_AI and the idle cooldown has
      elapsed with no new job, unload + restart PRL -> MINING_PRL.

    Side effects (stop/start PRL, load/unload the model) run through injected
    callbacks so the transition logic is testable in isolation; defaults are inert.
    ``gate_eligible`` is the device's 72h-entry-gate standing as last seen from the
    server; set it via :meth:`set_gate_eligible` (the client reads it off the pull
    response). While ``False``, AI demand is declined and the worker mines.
    """

    ai_idle_cooldown: timedelta = DEFAULT_AI_IDLE_COOLDOWN
    #: The small always-warm tier (plan §5 cold-start ②) used to answer the first
    #: request while a GPU warms behind it. Carried for the client/VPS to consult;
    #: the switch logic does not require it (it is a mitigation hint, not a gate).
    warm_model_tier: str | None = None
    #: Side-effect hooks (the CLI wires these to the real mining runtime + AI role).
    start_prl: Callable[[], None] = _noop
    stop_prl: Callable[[], None] = _noop
    load_model: Callable[[], None] = _noop
    unload_model: Callable[[], None] = _noop
    #: The device's 72h-entry-gate standing (from the server's pull response). When
    #: False, an AI job offer is ignored and the worker keeps mining PRL.
    gate_eligible: bool = True

    _state: ProfitSwitchState = field(default=STATE_MINING_PRL, init=False)
    _ai_idle_deadline: datetime | None = field(default=None, init=False)
    _jobs_served_this_session: int = field(default=0, init=False)
    _model_warm: bool = field(default=False, init=False)
    _started: bool = field(default=False, init=False)

    def __post_init__(self) -> None:
        if self.ai_idle_cooldown <= timedelta(0):
            raise ValueError("ai_idle_cooldown must be positive")

    # ----------------------------------------------------------------- state ---
    @property
    def state(self) -> ProfitSwitchState:
        return self._state

    @property
    def model_warm(self) -> bool:
        return self._model_warm

    def set_gate_eligible(self, eligible: bool) -> None:
        """Record the device's 72h-entry-gate standing (from the server)."""
        self.gate_eligible = bool(eligible)

    # ------------------------------------------------------------ lifecycle ---
    def start(self) -> ProfitSwitchSnapshot:
        """Enter the DEFAULT MINING_PRL state and start PRL (idempotent)."""
        if not self._started or self._state != STATE_MINING_PRL:
            self._enter_mining_prl()
            self._started = True
        return self._snapshot(REASON_PRL_STARTED)

    def on_ai_job(self, *, now: datetime | None = None) -> ProfitSwitchSnapshot:
        """An AI job is available NOW -> switch to SERVING_AI if gate-eligible.

        * Not gate-eligible -> IGNORE the offer (the device is still serving its
          mandatory 72h window); stay MINING_PRL.
        * Eligible + currently MINING_PRL -> stop PRL, load the model (cold-start
          here), switch to SERVING_AI, and arm the idle deadline.
        * Already SERVING_AI -> stay (the per-job reset is :meth:`on_ai_job_served`).
        """
        observed_at = now or utc_now()
        validate_aware_timestamp("now", observed_at)
        if not self.gate_eligible:
            # The device has not passed the 72h gate; decline AI + keep mining.
            return self._snapshot(REASON_AI_JOB_IGNORED_NOT_ELIGIBLE)
        if self._state == STATE_MINING_PRL:
            # GPU memory is exclusive: stop PRL before loading the model.
            self.stop_prl()
            self.load_model()
            self._model_warm = True
            self._state = STATE_SERVING_AI
            self._jobs_served_this_session = 0
            self._arm_idle_deadline(observed_at)
            return self._snapshot(REASON_SWITCHED_TO_AI)
        # Already serving AI.
        return self._snapshot(REASON_HOLDING_AI)

    def on_ai_job_served(self, *, now: datetime | None = None) -> ProfitSwitchSnapshot:
        """Mark one AI job served: count it + RESET the 10-min idle timer.

        Keeps the worker in SERVING_AI and the model warm. If called while mining
        (no active AI session) it is a no-op transition (defensive)."""
        observed_at = now or utc_now()
        validate_aware_timestamp("now", observed_at)
        if self._state != STATE_SERVING_AI:
            return self._snapshot(REASON_HOLDING_PRL)
        self._jobs_served_this_session += 1
        self._arm_idle_deadline(observed_at)
        return self._snapshot(REASON_AI_JOB_SERVED)

    def poll(self, *, now: datetime | None = None) -> ProfitSwitchSnapshot:
        """The idempotent tick the worker loop calls between polls.

        In SERVING_AI: if the 10-min idle cooldown has elapsed since the last served
        job (no new AI demand), UNLOAD the model + RESTART PRL -> MINING_PRL. Before
        the deadline, hold in SERVING_AI (the model stays warm). In MINING_PRL it is
        a no-op hold (the worker keeps mining + long-polling the AI queue).
        """
        observed_at = now or utc_now()
        validate_aware_timestamp("now", observed_at)
        if self._state == STATE_SERVING_AI:
            if self._ai_idle_deadline is not None and observed_at >= self._ai_idle_deadline:
                # 10-min idle with no AI demand -> unload + back to PRL.
                self._enter_mining_prl()
                return self._snapshot(REASON_AI_IDLE_COOLDOWN_ELAPSED)
            return self._snapshot(REASON_HOLDING_AI)
        return self._snapshot(REASON_HOLDING_PRL)

    # ------------------------------------------------------------- internals ---
    def _enter_mining_prl(self) -> None:
        """Transition INTO MINING_PRL: unload the model (if warm) + (re)start PRL."""
        if self._model_warm:
            self.unload_model()
            self._model_warm = False
        self._state = STATE_MINING_PRL
        self._ai_idle_deadline = None
        self._jobs_served_this_session = 0
        self.start_prl()

    def _arm_idle_deadline(self, observed_at: datetime) -> None:
        self._ai_idle_deadline = observed_at + self.ai_idle_cooldown

    def _snapshot(self, reason_code: str) -> ProfitSwitchSnapshot:
        return ProfitSwitchSnapshot(
            state=self._state,
            reason_code=reason_code,
            ai_idle_deadline=self._ai_idle_deadline,
            jobs_served_this_session=self._jobs_served_this_session,
            model_warm=self._model_warm,
            # The NEXT AI job pays cold-start whenever the model is not warm.
            cold_start_pending=not self._model_warm,
        )

    def snapshot(self) -> ProfitSwitchSnapshot:
        """A NON-mutating read of the current state (diagnostics / the wire view).

        Unlike :meth:`poll` this never transitions; it reports whichever hold the
        machine is currently in (so callers can inspect state without advancing it).
        """
        return self._snapshot(
            REASON_HOLDING_AI if self._state == STATE_SERVING_AI else REASON_HOLDING_PRL
        )

    def to_public_dict(self) -> dict[str, object]:
        return {
            "contract_version": PROFIT_SWITCH_CONTRACT_VERSION,
            "ai_idle_cooldown_seconds": int(self.ai_idle_cooldown.total_seconds()),
            "warm_model_tier": self.warm_model_tier,
            "gate_eligible": self.gate_eligible,
            "snapshot": self.snapshot().to_public_dict(),
            "paid_acu": "0",
        }

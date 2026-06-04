"""Co-located API-chat inference worker (Phase A, credit-only, reward OFF).

This module closes the AI inference *credit* loop end to end in CO-LOCATED
mode: a single in-process worker leases a queued job from the
``ApiChatWorkerTransportHarness``, admits an inference demand session against a
``ShadowServerHarness``, runs an :class:`InferenceBackend`, records the
verified-inference ACU credit via ``inference_complete``, and acks the queue.

Hard constraints preserved by this module (do NOT weaken):

* No reward / payout / chain. ``live_reward_enabled`` and
  ``payout_executor_enabled`` stay ``False`` on every request this module
  builds, ``paid_acu`` remains ``"0"``, and no miner payout address is ever
  supplied. The shadow ledger keeps its own ``*_forbidden`` rejections.
* Staging-internal-only / local-contract-only. This worker never opens a
  network transport and never sets ``network_transport_enabled``.

Privacy wrinkle (documented, intentional): the durable queue carries only the
``prompt_hash`` of a request, never the raw prompt. The credit is computed from
*usage* (token counts + latency), not from prompt content, so the
:class:`FakeInferenceBackend` used here only needs job metadata to synthesize a
usage record.

STEP 0 (this build) adds the privacy-preserving raw-prompt/completion handoff
the TODO below flagged: an in-process, ``job_id``-keyed
:class:`~alice_acp.api_chat_gateway.inference_side_channel.InferenceJobSideChannel`
carries the raw prompt from the gateway to this worker, and the worker hands the
raw prompt + raw completion to a server-side
:class:`~alice_acp.api_chat_gateway.inference_side_channel.InferenceRecountSidecar`
that purges the raw text SYNCHRONOUSLY on every exit path (ack / nack / timeout /
exception) before the queue is acked. The durable queue still carries ONLY the
``prompt_hash``; nothing raw lands in ``WorkerQueueRecord`` /
``InferenceJobResultDTO`` / ``ShadowWorkRecord``.

TODO(prod follow-up): wiring a REAL model backend requires a privacy-preserving
raw-prompt handoff from the gateway to the co-located worker that does NOT land
the raw prompt in the durable queue (e.g. an in-process side-channel keyed by
``job_id`` that is purged on ack/nack). The HANDOFF + sidecar are STEP 0 (done);
the REAL backend that consumes the prompt is STEP 1, and the real server-side
token recount inside the sidecar is STEP 2. Until then the
:class:`FakeInferenceBackend` synthesizes a deterministic, clearly-fake
completion so the handoff path is exercised end to end.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from decimal import Decimal
from typing import Protocol, runtime_checkable

from alice_acp.api_chat.contracts import stable_hash
from alice_acp.api_chat.types import utc_now, validate_public_identifier
from alice_acp.api_chat.validators import validate_aware_timestamp, validate_sha256
from alice_acp.api_chat_gateway.inference_side_channel import (
    InferenceJobSideChannel,
    InferenceRecountResultDTO,
    InferenceRecountSidecar,
    SideChannelTransport,
)
from alice_acp.api_chat_gateway.local_harness import ApiChatWorkerTransportHarness
from alice_acp.api_chat_gateway.worker_bridge import (
    REASON_WORKER_JOB_COMPLETED,
    InferenceJobRequestDTO,
    InferenceJobResultDTO,
    WorkerLane,
)
from alice_acp.api_chat_gateway.worker_queue import (
    WorkerQueueLeaseDTO,
    WorkerQueueOperationDTO,
)
from alice_acp.api_chat_gateway.worker_transport import (
    WorkerAuthHandleDTO,
    WorkerAuthScope,
)
from alice_acp.shadow_server.server import ShadowServerHarness
from alice_acp.shadow_server.types import (
    MAIN_POOL_AI,
    SESSION_KIND_INFERENCE,
    InferenceCompletionRequest,
    SessionIssueResult,
    ShadowSession,
    ShadowSessionIssueRequest,
    ShadowWorkRecord,
)

COLOCATED_INFERENCE_WORKER_CONTRACT_VERSION = (
    "api-chat-colocated-inference-worker-contract-v1"
)

REASON_COLOCATED_WORKER_LEASE_EMPTY = "api_chat_colocated_worker_lease_empty"
REASON_COLOCATED_WORKER_LEASE_REJECTED = "api_chat_colocated_worker_lease_rejected"
REASON_COLOCATED_WORKER_ADMIT_REJECTED = "api_chat_colocated_worker_admit_rejected"
REASON_COLOCATED_WORKER_COMPLETE_REJECTED = "api_chat_colocated_worker_complete_rejected"
REASON_COLOCATED_WORKER_BACKEND_FAILED = "api_chat_colocated_worker_backend_failed"
REASON_COLOCATED_WORKER_ACK_REJECTED = "api_chat_colocated_worker_ack_rejected"
REASON_COLOCATED_WORKER_CREDITED = "api_chat_colocated_worker_credited"

# Map a worker model tier (ApiChatModelClass) to the shadow inference-ACU model
# class. Larger general / roleplay models map to the heavier ACU tier.
_TIER1_MODEL_CLASSES = frozenset(
    {"alice_lite_4b", "alice_standard_9b", "rp_lite_9b"}
)
_TIER2_MODEL_CLASSES = frozenset(
    {"alice_pro_27b", "alice_pro_35b_moe", "rp_pro_27b"}
)
# tier1 caps context at 32768, tier2 at 65536 (see shadow_server.inference_acu).
_TIER_CONTEXT_LENGTH = {"tier1_local_llm": 32_768, "tier2_local_llm": 65_536}


def acu_model_class_for_tier(model_tier: str) -> str:
    """Return the shadow inference-ACU model class for a worker model tier."""
    if model_tier in _TIER1_MODEL_CLASSES:
        return "tier1_local_llm"
    if model_tier in _TIER2_MODEL_CLASSES:
        return "tier2_local_llm"
    raise ValueError(f"unsupported inference-acu model tier: {model_tier}")


@dataclass(frozen=True, slots=True)
class InferenceBackendResult:
    """Usage produced by an :class:`InferenceBackend` for one job.

    The credit is computed from this *usage*, never from raw prompt text. The
    ``model_id`` MUST be a shadow-compatible identifier (``alice-...``) and is
    bound onto the admitted session so ``inference_complete`` matches it.
    """

    model_id: str
    model_class: str
    input_tokens: int
    output_tokens: int
    context_length: int
    latency_ms: Decimal
    output_hash: str

    def __post_init__(self) -> None:
        validate_public_identifier("model_id", self.model_id)
        if not self.model_id.startswith("alice-"):
            raise ValueError("inference backend model_id must start with 'alice-'")
        if self.model_class not in _TIER_CONTEXT_LENGTH:
            raise ValueError("inference backend model_class is unsupported")
        if self.input_tokens <= 0 or self.output_tokens <= 0:
            raise ValueError("inference backend token counts must be positive")
        if self.context_length <= 0:
            raise ValueError("inference backend context_length must be positive")
        if self.input_tokens + self.output_tokens > self.context_length:
            raise ValueError("inference backend usage exceeds context_length")
        if self.latency_ms <= Decimal("0"):
            raise ValueError("inference backend latency_ms must be positive")
        validate_sha256(self.output_hash, field_name="output_hash")


@runtime_checkable
class InferenceBackend(Protocol):
    """Produces a usage record for a leased job.

    Implementations MUST derive usage only from job metadata available in the
    durable queue (model tier / token budgets); they MUST NOT depend on raw
    prompt text (which the queue does not carry) and MUST NOT perform real
    network or model calls in any test-exercised path.
    """

    def run(self, job: InferenceJobRequestDTO) -> InferenceBackendResult: ...


@runtime_checkable
class InferenceTextBackend(Protocol):
    """Optional STEP-0 capability: a backend that ALSO produces a raw completion.

    A metadata-only :class:`InferenceBackend` is enough for the credit loop (the
    ACU is derived from usage, not text). But STEP 0's privacy handoff needs a
    raw completion to route to the :class:`InferenceRecountSidecar` (the place
    STEP 2's recount will compute on). A backend MAY implement this richer
    method, taking the raw prompt delivered over the side-channel and returning
    both the usage record AND the raw completion text. The REAL model backend is
    STEP 1; until then :class:`FakeInferenceBackend` implements this with a
    deterministic, clearly-fake completion so the handoff is exercised.
    """

    def run_with_prompt(
        self, job: InferenceJobRequestDTO, *, prompt: str
    ) -> tuple[InferenceBackendResult, str]: ...


@dataclass(frozen=True, slots=True)
class FakeInferenceBackend:
    """Deterministic, offline inference backend for Phase A credit-close + tests.

    Token usage is derived deterministically from job metadata and clamped well
    inside the job's ``max_input_tokens`` / ``max_output_tokens`` budgets so the
    resulting :class:`InferenceJobResultDTO` always validates. No real model and
    no network are involved.
    """

    model_id_by_tier: dict[str, str] = field(
        default_factory=lambda: {
            "alice_lite_4b": "alice-lite-4b-mlx@4bit",
            "alice_standard_9b": "alice-standard-9b-mlx@4bit",
            "alice_pro_27b": "alice-pro-27b-mlx@4bit",
            "alice_pro_35b_moe": "alice-pro-35b-moe-mlx@4bit",
            "rp_lite_9b": "alice-rp-lite-9b-mlx@4bit",
            "rp_pro_27b": "alice-rp-pro-27b-mlx@4bit",
        }
    )

    def run(self, job: InferenceJobRequestDTO) -> InferenceBackendResult:
        model_class = acu_model_class_for_tier(job.model_tier)
        model_id = self.model_id_by_tier.get(
            job.model_tier, f"alice-{job.model_tier.replace('_', '-')}@contract"
        )
        # Deterministic, modest usage that stays within both the job token
        # budgets and the ACU policy context cap.
        input_tokens = max(1, min(job.max_input_tokens, 32))
        output_tokens = max(1, min(job.max_output_tokens, 16))
        policy_cap = _TIER_CONTEXT_LENGTH[model_class]
        context_length = min(job.max_input_tokens + job.max_output_tokens, policy_cap)
        if context_length < input_tokens + output_tokens:
            context_length = input_tokens + output_tokens
        latency_ms = Decimal("1200")
        output_hash = stable_hash(
            {
                "contract": COLOCATED_INFERENCE_WORKER_CONTRACT_VERSION,
                "job_id": job.job_id,
                "prompt_hash": job.prompt_hash,
                "model_id": model_id,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
            }
        )
        return InferenceBackendResult(
            model_id=model_id,
            model_class=model_class,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            context_length=context_length,
            latency_ms=latency_ms,
            output_hash=output_hash,
        )

    def run_with_prompt(
        self, job: InferenceJobRequestDTO, *, prompt: str
    ) -> tuple[InferenceBackendResult, str]:
        """STEP 0: usage record + a deterministic, clearly-fake completion.

        The completion is synthesized from job metadata (NOT echoing the prompt
        verbatim, though it is raw text that must still be walled out of the
        durable/credit plane). The REAL model output is STEP 1. The ``prompt`` is
        accepted (proving the side-channel delivered it) but does not change the
        deterministic usage record :meth:`run` produces.
        """

        if not isinstance(prompt, str) or not prompt:
            raise ValueError("inference prompt must be a non-empty string")
        result = self.run(job)
        completion = (
            f"[fake-completion job={job.job_id} model={result.model_id} "
            f"tokens={result.output_tokens}] STEP 1 will replace this with real text."
        )
        return result, completion


@dataclass(frozen=True, slots=True)
class ColocatedInferenceOutcome:
    """Result of one ``process_next`` invocation."""

    status: str
    reason_code: str
    job_id: str | None = None
    demand_session_id: str | None = None
    session_id: str | None = None
    queue_operation: WorkerQueueOperationDTO | None = None
    work_record: ShadowWorkRecord | None = None
    job_result: InferenceJobResultDTO | None = None
    # STEP 0: redacted (hashes + counts only) recount result from the sidecar.
    # Carries NO raw text. STEP 2 fills in the real server_recount_* counts.
    recount_result: InferenceRecountResultDTO | None = None

    @property
    def credited(self) -> bool:
        return self.status == "credited"

    def to_public_dict(self) -> dict[str, object]:
        return {
            "contract_version": COLOCATED_INFERENCE_WORKER_CONTRACT_VERSION,
            "status": self.status,
            "reason_code": self.reason_code,
            "job_id": self.job_id,
            "demand_session_id": self.demand_session_id,
            "session_id": self.session_id,
            "verified_inference_acu": (
                str(self.work_record.verified_score)
                if self.work_record is not None
                else None
            ),
            "score_kind": (
                self.work_record.score_kind if self.work_record is not None else None
            ),
            # STEP 0: redacted recount summary (hashes + counts only) or None.
            "recount_result": (
                self.recount_result.to_public_dict()
                if self.recount_result is not None
                else None
            ),
            # Reward / payout / chain stay OFF: these are asserted, never derived.
            "live_reward_enabled": False,
            "payout_executor_enabled": False,
            "paid_acu": "0",
            "raw_prompt_persisted": False,
            "raw_response_persisted": False,
        }


@dataclass(slots=True)
class ColocatedInferenceWorker:
    """In-process worker that closes the inference credit loop, reward OFF.

    It binds a single device/passport/worker identity, leases jobs from the
    harness, admits an inference-demand session on the shadow ledger, runs the
    :class:`InferenceBackend`, and records ``verified_inference_acu`` credit via
    ``inference_complete`` before acking the queue. ``demand_session_id`` issued
    at admit time is threaded through to the credit record (the ledger reads it
    off the stored session) and surfaced on the outcome for traceability.
    """

    harness: ApiChatWorkerTransportHarness
    shadow: ShadowServerHarness
    device_id: str
    passport_id: str
    worker_id: str
    key_hash: str
    backend: InferenceBackend = field(default_factory=FakeInferenceBackend)
    # STEP 0: the SAME transient side-channel the gateway publishes raw prompts
    # to (keyed by job_id). The worker takes the prompt here (single-shot pop) to
    # feed the backend. When the gateway and worker share a harness they MUST
    # share this side-channel instance; the default is a private one for tests
    # that drive the worker directly.
    side_channel: SideChannelTransport = field(default_factory=InferenceJobSideChannel)
    # STEP 0: server-side transient holder for the raw prompt + completion. STEP
    # 2's real token recount computes here; STEP 0 purges the raw text on every
    # exit path before the queue is acked.
    recount_sidecar: InferenceRecountSidecar = field(default_factory=InferenceRecountSidecar)

    def __post_init__(self) -> None:
        validate_public_identifier("device_id", self.device_id)
        validate_public_identifier("passport_id", self.passport_id)
        validate_public_identifier("worker_id", self.worker_id)
        validate_sha256(self.key_hash, field_name="key_hash")

    def _register_and_admit(
        self,
        *,
        model_id: str,
        job_id: str,
        observed_at: datetime,
    ) -> SessionIssueResult:
        """Attest the leased job as verified AI demand, then admit (C1).

        The co-located worker only leases REAL queued jobs from the harness, so
        a leased ``job_id`` IS verified demand. We register it on the shadow
        ledger's demand store (if that store accepts registrations) and pass it
        as ``demand_session_id`` so the fail-closed admission gate resolves it.
        If the deployment wired the default fail-closed store, registration is a
        no-op and admission stays NOT admitted -- the credit loop fails closed.
        """

        demand_session_id = f"demand-colocated-{job_id}"
        register = getattr(self.shadow.ledger.demand_store, "register_demand", None)
        if callable(register):
            register(
                demand_session_id=demand_session_id,
                passport_id=self.passport_id,
                device_id=self.device_id,
            )
        return self.shadow.inference_admit(
            ShadowSessionIssueRequest(
                passport_id=self.passport_id,
                device_id=self.device_id,
                lane=MAIN_POOL_AI,
                session_kind=SESSION_KIND_INFERENCE,
                worker_id=self.worker_id,
                model_id=model_id,
                requested_at=observed_at,
                demand_session_id=demand_session_id,
                # Reward / payout stay OFF; no miner payout address.
                live_reward_enabled=False,
                payout_executor_enabled=False,
                miner_provided_payout_address=None,
            )
        )

    def process_next(self, *, now: datetime | None = None) -> ColocatedInferenceOutcome:
        observed_at = _observed_at(now)
        leased = self.harness.lease(self._auth(("lease",)), now=observed_at)
        if leased.status == "empty":
            return ColocatedInferenceOutcome(
                status="idle",
                reason_code=REASON_COLOCATED_WORKER_LEASE_EMPTY,
                queue_operation=leased,
            )
        if leased.status != "accepted" or leased.lease is None:
            return ColocatedInferenceOutcome(
                status="rejected",
                reason_code=REASON_COLOCATED_WORKER_LEASE_REJECTED,
                queue_operation=leased,
            )
        return self._process_lease(leased.lease, observed_at)

    def drain(
        self,
        *,
        max_jobs: int = 64,
        now: datetime | None = None,
    ) -> tuple[ColocatedInferenceOutcome, ...]:
        if max_jobs <= 0:
            raise ValueError("max_jobs must be positive")
        observed_at = _observed_at(now)
        outcomes: list[ColocatedInferenceOutcome] = []
        for _ in range(max_jobs):
            outcome = self.process_next(now=observed_at)
            if outcome.status == "idle":
                break
            outcomes.append(outcome)
        return tuple(outcomes)

    def _process_lease(
        self,
        lease: WorkerQueueLeaseDTO,
        observed_at: datetime,
    ) -> ColocatedInferenceOutcome:
        record = self._record_for_lease(lease)
        if record is None:
            # Lease vanished (e.g. reaped) between lease and processing.
            return ColocatedInferenceOutcome(
                status="rejected",
                reason_code=REASON_COLOCATED_WORKER_LEASE_REJECTED,
                job_id=lease.job_id,
            )
        job = record.job
        assigned_worker = record.assigned_worker

        # STEP 0: take the raw prompt off the transient side-channel (single-shot
        # pop). The durable queue only ever carried the prompt_hash; the raw
        # prompt rode the side-channel. A REAL backend (STEP 1) requires it; the
        # FakeInferenceBackend tolerates its absence (metadata-only) but, when a
        # prompt IS present, also produces a raw completion for the recount
        # sidecar. The local `raw_prompt` / `raw_completion` strings below never
        # leave this method except into the purging sidecar context.
        raw_prompt = self.side_channel.take_prompt(job.job_id)
        try:
            backend_result, raw_completion = self._run_backend(job, raw_prompt)
        except (ValueError, RuntimeError):
            nack = self.harness.nack(
                self._auth(("fail",)),
                lease,
                reason_code=REASON_COLOCATED_WORKER_BACKEND_FAILED,
                retry=False,
                now=observed_at,
            )
            return ColocatedInferenceOutcome(
                status="failed",
                reason_code=REASON_COLOCATED_WORKER_BACKEND_FAILED,
                job_id=job.job_id,
                queue_operation=nack,
            )

        admit = self._register_and_admit(
            model_id=backend_result.model_id,
            job_id=job.job_id,
            observed_at=observed_at,
        )
        if not admit.accepted or admit.session is None:
            return self._nack_outcome(
                lease,
                job_id=job.job_id,
                status="rejected",
                reason_code=REASON_COLOCATED_WORKER_ADMIT_REJECTED,
                ledger_reason=admit.reason_code,
                observed_at=observed_at,
            )
        session = admit.session

        # STEP 0: hold the raw prompt + completion in the server-side recount
        # sidecar for the duration of credit + ack, then PURGE synchronously on
        # EVERY exit path (normal return, nack, ack-failure, raised exception,
        # GeneratorExit/timeout). STEP 2 replaces `hold.server_recount()` with a
        # real re-tokenize and credits min(declared, server_recount). The outcome
        # is BUILT AFTER the context exits so the redacted recount result it
        # carries honestly reflects the post-purge state.
        with self.recount_sidecar.recount(
            job_id=job.job_id,
            raw_prompt=raw_prompt if raw_prompt is not None else _synthetic_prompt(job),
            raw_completion=raw_completion,
            declared_input_tokens=backend_result.input_tokens,
            declared_output_tokens=backend_result.output_tokens,
            now=observed_at,
        ) as hold:
            # STEP 2 will derive these from a real re-tokenize of the held raw
            # prompt + completion; STEP 0 returns the declared counts.
            hold.server_recount()
            outcome = self._credit_and_ack(
                lease=lease,
                job=job,
                session=session,
                assigned_worker=assigned_worker,
                backend_result=backend_result,
                observed_at=observed_at,
            )
        # Raw text is purged here. Attach the redacted (hashes + counts only)
        # recount result and return.
        recount_result = self.recount_sidecar.last_result(job.job_id)
        return replace(outcome, recount_result=recount_result)

    def _credit_and_ack(
        self,
        *,
        lease: WorkerQueueLeaseDTO,
        job: InferenceJobRequestDTO,
        session: ShadowSession,
        assigned_worker: str | None,
        backend_result: InferenceBackendResult,
        observed_at: datetime,
    ) -> ColocatedInferenceOutcome:
        """Record the verified-inference credit, then ack the queue.

        Runs INSIDE the recount-sidecar context (raw text still held) so that
        every exit path -- including the nack/ack-failure early returns here and
        any exception -- triggers the sidecar's synchronous raw-text purge.
        """

        completion = self.shadow.inference_complete(
            self._completion_request(
                session=session,
                job=job,
                backend_result=backend_result,
                observed_at=observed_at,
            )
        )
        if not completion.accepted or completion.record is None:
            return self._nack_outcome(
                lease,
                job_id=job.job_id,
                status="rejected",
                reason_code=REASON_COLOCATED_WORKER_COMPLETE_REJECTED,
                ledger_reason=completion.reason_code,
                demand_session_id=session.demand_session_id,
                session_id=session.session_id,
                observed_at=observed_at,
            )

        result = InferenceJobResultDTO(
            job_id=job.job_id,
            status="completed",
            reason_code=REASON_WORKER_JOB_COMPLETED,
            model_tier=job.model_tier,
            lane=job.lane,
            prompt_hash=job.prompt_hash,
            assigned_worker=assigned_worker,
            max_input_tokens=job.max_input_tokens,
            max_output_tokens=job.max_output_tokens,
            timeout_ms=job.timeout_ms,
            input_tokens=backend_result.input_tokens,
            output_tokens=backend_result.output_tokens,
            output_hash=backend_result.output_hash,
            completed_at=observed_at,
        )
        ack = self.harness.ack(
            self._auth(("complete",)),
            lease,
            result,
            now=observed_at,
        )
        if ack.status != "accepted":
            # Credit is already recorded on the ledger; the queue ack failed.
            return ColocatedInferenceOutcome(
                status="rejected",
                reason_code=REASON_COLOCATED_WORKER_ACK_REJECTED,
                job_id=job.job_id,
                demand_session_id=session.demand_session_id,
                session_id=session.session_id,
                queue_operation=ack,
                work_record=completion.record,
                job_result=result,
            )

        return ColocatedInferenceOutcome(
            status="credited",
            reason_code=REASON_COLOCATED_WORKER_CREDITED,
            job_id=job.job_id,
            demand_session_id=session.demand_session_id,
            session_id=session.session_id,
            queue_operation=ack,
            work_record=completion.record,
            job_result=result,
        )

    def _run_backend(
        self,
        job: InferenceJobRequestDTO,
        raw_prompt: str | None,
    ) -> tuple[InferenceBackendResult, str]:
        """Run the backend, returning the usage record + a raw completion.

        When the side-channel delivered a prompt AND the backend implements the
        optional :class:`InferenceTextBackend` capability, the backend consumes
        the prompt and produces a raw completion (STEP 1 wires the REAL model
        here). Otherwise we run the metadata-only backend and synthesize a
        deterministic placeholder completion so the recount-sidecar handoff is
        still exercised. Either way the raw completion is held only transiently
        by the sidecar and is purged before ack.
        """

        if raw_prompt is not None and isinstance(self.backend, InferenceTextBackend):
            return self.backend.run_with_prompt(job, prompt=raw_prompt)
        backend_result = self.backend.run(job)
        return backend_result, _synthetic_completion(job, backend_result)

    def _completion_request(
        self,
        *,
        session: ShadowSession,
        job: InferenceJobRequestDTO,
        backend_result: InferenceBackendResult,
        observed_at: datetime,
    ) -> InferenceCompletionRequest:
        proof_id = _proof_id(job_id=job.job_id, session_id=session.session_id)
        return InferenceCompletionRequest(
            proof_id=proof_id,
            session_id=session.session_id,
            session_signature=session.signature,
            model_id=backend_result.model_id,
            input_tokens=backend_result.input_tokens,
            output_tokens=backend_result.output_tokens,
            context_length=backend_result.context_length,
            latency_ms=backend_result.latency_ms,
            model_class=backend_result.model_class,
            observed_at=observed_at,
            # Credit-only: never simulate an API payment in Phase A.
            simulated_api_payment=Decimal("0"),
        )

    def _nack_outcome(
        self,
        lease: WorkerQueueLeaseDTO,
        *,
        job_id: str,
        status: str,
        reason_code: str,
        ledger_reason: str,
        observed_at: datetime,
        demand_session_id: str | None = None,
        session_id: str | None = None,
    ) -> ColocatedInferenceOutcome:
        nack = self.harness.nack(
            self._auth(("fail",)),
            lease,
            reason_code=reason_code,
            retry=False,
            now=observed_at,
        )
        return ColocatedInferenceOutcome(
            status=status,
            reason_code=ledger_reason or reason_code,
            job_id=job_id,
            demand_session_id=demand_session_id,
            session_id=session_id,
            queue_operation=nack,
        )

    def _record_for_lease(self, lease: WorkerQueueLeaseDTO):
        queue = self.harness.queue
        if queue is None:
            return None
        for record in queue.records():
            if (
                record.record_id == lease.record_id
                and record.status == "leased"
                and record.lease_id == lease.lease_id
            ):
                return record
        return None

    def _auth(self, scopes: tuple[WorkerAuthScope, ...]) -> WorkerAuthHandleDTO:
        return WorkerAuthHandleDTO(
            worker_id=self.worker_id,
            passport_id=self.passport_id,
            key_id="colocated-inference-worker",
            key_hash=self.key_hash,
            scopes=scopes,
            authenticated_at=utc_now(),
        )


def colocated_worker_heartbeat_lane() -> WorkerLane:
    """The lane a co-located worker advertises for general chat dispatch."""
    return "general"


def _synthetic_prompt(job: InferenceJobRequestDTO) -> str:
    """Deterministic placeholder prompt for the recount sidecar.

    Used only when the side-channel delivered no prompt (e.g. a worker driven
    directly in a test) so the sidecar still has non-empty raw text to hold +
    purge. STEP 1's real prompt arrives over the side-channel instead.
    """

    return f"[synthetic-prompt job={job.job_id} prompt_hash={job.prompt_hash}]"


def _synthetic_completion(
    job: InferenceJobRequestDTO,
    backend_result: InferenceBackendResult,
) -> str:
    """Deterministic placeholder completion for a metadata-only backend.

    A pure :class:`InferenceBackend` produces no text; the recount sidecar still
    needs a raw completion to hold + purge so its structure + purge guarantee are
    exercised. STEP 1's REAL model output replaces this.
    """

    return (
        f"[synthetic-completion job={job.job_id} model={backend_result.model_id} "
        f"output_tokens={backend_result.output_tokens}]"
    )


def _observed_at(now: datetime | None) -> datetime:
    observed_at = now or utc_now()
    validate_aware_timestamp("now", observed_at)
    return observed_at


def _proof_id(*, job_id: str, session_id: str) -> str:
    digest = stable_hash(
        {
            "contract": COLOCATED_INFERENCE_WORKER_CONTRACT_VERSION,
            "job_id": job_id,
            "session_id": session_id,
        }
    )
    return f"colocated-inference-{digest[:24]}"

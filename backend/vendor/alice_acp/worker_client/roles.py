"""Worker ROLES -- the extensibility seam (owner-approved).

A worker client runs ONE role at a time. ONLY the AI-inference role is built now;
the PRL-mining role is reserved as a sibling so a future GPU profit-switch
(AI<->PRL) ADDS a ``PrlMiningRole`` WITHOUT touching the pull-loop client or the
protocol. A role knows:

* its protocol ``role`` tag + the catalog tiers it can serve (its capability);
* how to RUN one leased job -> a completion + usage (the AI role runs the real
  model on the GPU via :class:`RealModelTextBackend`).

The PRL role would instead expose a mining capability + run a mining job; the
pull-loop client is role-agnostic, so adding it is purely additive.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from alice_acp.api_chat.model_catalog import canonical_model_class
from alice_acp.api_chat_gateway.worker_bridge import InferenceJobRequestDTO
from alice_acp.api_chat_gateway.worker_pull_protocol import WorkerCapabilityDTO, WorkerRole
from alice_acp.local_inference.backend import RealModelTextBackend
from alice_acp.local_inference.runtimes import GenerationParams


@dataclass(frozen=True, slots=True)
class RunResult:
    """The output of running one job: the completion text + the usage record."""

    completion: str
    model_id: str
    model_class: str
    input_tokens: int
    output_tokens: int
    context_length: int
    latency_ms: str
    output_hash: str


@runtime_checkable
class WorkerRoleImpl(Protocol):
    """A worker role: declares a capability + runs one leased job."""

    @property
    def role(self) -> WorkerRole: ...

    def capability(self, *, free_memory_gb: int) -> WorkerCapabilityDTO: ...

    def run_job(self, *, job: _JobView, prompt: str, control_nonce: str) -> RunResult: ...


def conditioning_for(prompt: str, control_nonce: str) -> str:
    """The conditioning text the worker ACTUALLY runs (M3 SEAL 1).

    The server-minted control nonce is folded into a CONTROL prefix in front of
    the user prompt -- NOT mixed into the user content the recount tokenizes for
    credit. This is byte-identical to the verifier's
    :func:`alice_acp.services.verification_vps.scorer.bind_control_nonce`, so the
    worker and the verifier condition on the SAME (nonce + prompt) and a logprob
    re-score is meaningful. Re-exported from the scorer so there is ONE definition
    of the nonce binding shared by both sides.
    """
    from alice_acp.services.verification_vps.scorer import bind_control_nonce

    return bind_control_nonce(prompt, control_nonce)


@dataclass(frozen=True, slots=True)
class _JobView:
    """The minimal job view a role needs to run (mirrors the lease wire fields)."""

    job_id: str
    model_tier: str
    lane: str
    max_input_tokens: int
    max_output_tokens: int
    timeout_ms: int

    def to_request(self) -> InferenceJobRequestDTO:
        # The backend's usage clamp needs the budgets + tier; the prompt_hash is
        # recomputed server-side, so a placeholder valid hash is fine here (the
        # client never sends this request DTO anywhere -- it is internal to the
        # backend's usage derivation).
        return InferenceJobRequestDTO(
            job_id=self.job_id,
            model_tier=self.model_tier,  # type: ignore[arg-type]
            lane=self.lane,  # type: ignore[arg-type]
            prompt_hash="0" * 64,
            max_input_tokens=self.max_input_tokens,
            max_output_tokens=self.max_output_tokens,
            timeout_ms=self.timeout_ms,
            requested_at=datetime.now(UTC),
        )


@dataclass(slots=True)
class AiInferenceRole:
    """The AI-inference role: run the REAL model on the worker's GPU.

    Holds one resident :class:`RealModelTextBackend` per tier (lazy-loaded on
    first use). ``served_tiers`` are the catalog tiers this worker advertises;
    ``backend_factory`` builds the backend for a tier (the CLI wires this to the
    GPU runtime adapter -- e.g. the OpenAI-server/CUDA adapter on narissa).
    """

    served_tiers: tuple[str, ...]
    backend_factory: object  # Callable[[str], RealModelTextBackend]
    runtime: str = "cuda"
    default_params: GenerationParams | None = None
    _backends: dict[str, RealModelTextBackend] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if not self.served_tiers:
            raise ValueError("AiInferenceRole requires at least one served tier")
        object.__setattr__(
            self,
            "served_tiers",
            tuple(dict.fromkeys(canonical_model_class(t) for t in self.served_tiers)),  # type: ignore[arg-type]
        )
        if self._backends is None:
            self._backends = {}

    @property
    def role(self) -> WorkerRole:
        return "ai_inference"

    def capability(self, *, free_memory_gb: int) -> WorkerCapabilityDTO:
        return WorkerCapabilityDTO(
            model_tiers=self.served_tiers,  # type: ignore[arg-type]
            runtime=self.runtime,  # type: ignore[arg-type]
            max_concurrent_jobs=1,
            free_memory_gb=free_memory_gb,
            role="ai_inference",
        )

    def _backend_for(self, model_tier: str) -> RealModelTextBackend:
        canonical = canonical_model_class(model_tier)  # type: ignore[arg-type]
        backend = self._backends.get(canonical)
        if backend is None:
            backend = self.backend_factory(canonical)  # type: ignore[operator]
            self._backends[canonical] = backend
        return backend

    def run_job(self, *, job: _JobView, prompt: str, control_nonce: str) -> RunResult:
        backend = self._backend_for(job.model_tier)
        # M3 SEAL 1: condition on the server-minted control nonce (a control
        # prefix), NOT the bare user prompt, so the served tokens are bound to
        # THIS request and the verifier (which scores under the same binding) can
        # tell real compute from a cached/replayed answer.
        conditioned = conditioning_for(prompt, control_nonce)
        text_result = backend.run_with_prompt(job.to_request(), prompt=conditioned)
        usage = text_result.usage
        return RunResult(
            completion=text_result.text,
            model_id=usage.model_id,
            model_class=usage.model_class,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            context_length=usage.context_length,
            latency_ms=str(usage.latency_ms),
            output_hash=usage.output_hash,
        )

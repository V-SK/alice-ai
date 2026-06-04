"""The SHARED real-model inference backend.

This module implements the real-model backend that satisfies the existing
co-located inference seam in
``alice_acp.api_chat_gateway.colocated_inference_worker``. That seam's
``InferenceBackend.run(job)`` deliberately CANNOT see the prompt -- the durable
queue carries only ``prompt_hash`` (privacy). The TODO in that module
(``wiring a REAL model backend requires a privacy-preserving raw-prompt
handoff``) is exactly the gap this module closes: it adds an
``InferenceTextBackend`` that EXTENDS the existing protocol with
``run_with_prompt(job, *, prompt)`` -- the prompt arrives via an in-process
side-channel, never via the queue.

Sharing: this backend is run by BOTH lanes.
* Track A (LOCAL): the local shell calls ``run_with_prompt`` directly with the
  user's prompt, on the user's hardware, with no network and no credit.
* AI-lane network worker (STEP 1, later -- NOT wired here): will call the same
  ``run_with_prompt`` behind STEP 0's side-channel, then feed the returned usage
  into ``inference_complete`` for credit. That wiring is out of scope here.

The backend loads a pinned artifact via a ``RuntimeAdapter`` (MLX / llama.cpp /
CUDA / cpu) and runs real inference: prompt -> completion, real token counts,
server-measured latency, output_hash. In this build env it is exercised with
``StubRuntimeAdapter`` (no weights, no GPU); the real adapters drop in unchanged.

``run(job)`` (the prompt-free path) is preserved so the existing co-located
plumbing keeps working: it fails closed (it MUST NOT fabricate a completion
without a prompt). Only ``run_with_prompt`` produces real output.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from alice_acp.api_chat.validators import validate_sha256
from alice_acp.api_chat_gateway.colocated_inference_worker import (
    InferenceBackend,
    InferenceBackendResult,
    acu_model_class_for_tier,
)
from alice_acp.api_chat_gateway.worker_bridge import InferenceJobRequestDTO
from alice_acp.local_inference.pinned_models import (
    PinnedModelArtifact,
    pinned_artifact,
)
from alice_acp.local_inference.runtimes import (
    GenerationParams,
    GenerationResult,
    LoadedModel,
    RuntimeAdapter,
)

REASON_BACKEND_REQUIRES_PROMPT = "local_inference_backend_requires_prompt"
REASON_BACKEND_MODEL_ID_MISMATCH = "local_inference_backend_model_id_mismatch"


@dataclass(frozen=True, slots=True)
class InferenceTextResult:
    """A completed real generation: the usage record plus the raw text.

    ``usage`` is the queue/credit-compatible :class:`InferenceBackendResult`
    (no raw text -- only counts + hash). ``text`` is the actual completion,
    returned ONLY to the in-process caller (the local shell returns it to the
    user; the network worker would discard it after hashing). ``text`` is never
    persisted to the durable queue or any ledger by this backend.
    """

    usage: InferenceBackendResult
    text: str

    @property
    def model_id(self) -> str:
        return self.usage.model_id


@runtime_checkable
class InferenceTextBackend(InferenceBackend, Protocol):
    """The shared seam: real inference from a prompt via an in-process handoff.

    Extends the existing ``InferenceBackend`` (so a ``run(job)`` path still
    exists for the co-located plumbing) with the prompt-bearing method the real
    model needs. The prompt is passed as a keyword-only argument to make the
    privacy contract explicit at every call site.
    """

    def run_with_prompt(
        self,
        job: InferenceJobRequestDTO,
        *,
        prompt: str,
    ) -> InferenceTextResult: ...


@dataclass(slots=True)
class RealModelTextBackend:
    """Loads a pinned model via a ``RuntimeAdapter`` and runs real inference.

    The model is loaded lazily on first use and cached on the instance (one
    backend instance == one resident model). ``run_with_prompt`` performs the
    real prompt -> completion decode and returns both the credit-compatible
    usage record and the completion text. ``run(job)`` fails closed: it never
    fabricates a completion without a prompt.
    """

    artifact: PinnedModelArtifact
    adapter: RuntimeAdapter
    snapshot_dir: object  # pathlib.Path; typed loosely to avoid import churn
    default_params: GenerationParams = field(default_factory=GenerationParams)
    _model: LoadedModel | None = None

    def _ensure_loaded(self) -> LoadedModel:
        if self._model is None:
            self._model = self.adapter.load(self.artifact, self.snapshot_dir)  # type: ignore[arg-type]
        return self._model

    def run_with_prompt(
        self,
        job: InferenceJobRequestDTO,
        *,
        prompt: str,
    ) -> InferenceTextResult:
        if not prompt:
            raise ValueError(REASON_BACKEND_REQUIRES_PROMPT)
        model = self._ensure_loaded()
        params = GenerationParams(
            max_output_tokens=min(job.max_output_tokens, self.default_params.max_output_tokens),
            temperature=self.default_params.temperature,
            seed=self.default_params.seed,
        )
        generation = model.generate(prompt, params)
        usage = self._usage_from_generation(job, generation)
        return InferenceTextResult(usage=usage, text=generation.text)

    def run(self, job: InferenceJobRequestDTO) -> InferenceBackendResult:
        # Prompt-free path: a real model cannot produce a real completion from a
        # hash. Fail closed rather than fabricate usage. The co-located worker's
        # FakeInferenceBackend remains the prompt-free credit-close path.
        raise RuntimeError(REASON_BACKEND_REQUIRES_PROMPT)

    def _usage_from_generation(
        self,
        job: InferenceJobRequestDTO,
        generation: GenerationResult,
    ) -> InferenceBackendResult:
        model_class = acu_model_class_for_tier(job.model_tier)
        model = self._ensure_loaded()
        # Clamp usage inside the loaded model's context window and the ACU
        # policy cap that InferenceBackendResult enforces.
        context_length = min(
            model.context_length,
            job.max_input_tokens + job.max_output_tokens,
        )
        input_tokens = generation.input_tokens
        output_tokens = generation.output_tokens
        if input_tokens + output_tokens > context_length:
            context_length = input_tokens + output_tokens
        validate_sha256(generation.output_hash, field_name="output_hash")
        if self.artifact.model_id != model.model_id:
            raise ValueError(REASON_BACKEND_MODEL_ID_MISMATCH)
        return InferenceBackendResult(
            model_id=self.artifact.model_id,
            model_class=model_class,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            context_length=context_length,
            latency_ms=generation.latency_ms,
            output_hash=generation.output_hash,
        )


def build_real_backend(
    *,
    model_class: str,
    runtime: str,
    adapter: RuntimeAdapter,
    snapshot_dir: object,
    default_params: GenerationParams | None = None,
) -> RealModelTextBackend:
    """Build a :class:`RealModelTextBackend` for a tier + runtime.

    The artifact is looked up from the pinned catalog; the adapter + snapshot
    dir are supplied by the local shell (which selected them from detected
    hardware and resolved the weights).
    """
    artifact = pinned_artifact(model_class, runtime)  # type: ignore[arg-type]
    return RealModelTextBackend(
        artifact=artifact,
        adapter=adapter,
        snapshot_dir=snapshot_dir,
        default_params=default_params or GenerationParams(),
    )

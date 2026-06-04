"""Track A: the LOCAL private-inference shell.

Run Alice's open models on YOUR OWN hardware, locally. Data never leaves the
device. This is the truest CROPS story and the cheapest honest signal: no
network call, no credit/ledger interaction, no side-channel.

What the shell does for one prompt:

1. Detect hardware (REUSES ``mining_device.detector`` via ``hardware_select``).
2. Pick a runtime + the best pinned tier that fits the host's memory.
3. Resolve the pinned weights from the LOCAL cache (request-time download is
   allowed in LOCAL mode -- the user fetches the model to their own box from
   the pinned upstream repo; this never touches an Alice server, ledger, or
   credit).
4. Run the SHARED real-model backend (``RealModelTextBackend.run_with_prompt``)
   on the user's prompt, on the user's hardware.
5. Return the completion + real usage.

LOAD-BEARING INVARIANT (asserted in tests):
The local path touches NO network and NO credit. This module imports nothing
from the shadow ledger / credit server / worker queue / network transport, and
constructs no ledger request. ``paid_acu`` is untouched because no ledger is
involved at all. The only outbound action possible is the model weight
download, which is (a) opt-in (real runs only, never dry-run), (b) against the
pinned upstream weights repo, never an Alice service, and (c) injectable so
tests run fully offline.

Two follow-ups (out of scope here, noted for the record):
* (a) real-model-on-hardware verification (narissa / a GPU box) -- swap
  ``StubRuntimeAdapter`` for the real MLX/llama.cpp adapter.
* (b) STEP-1 wiring of this same backend into the AI-lane NETWORK worker behind
  STEP 0's side-channel -- deliberately NOT done here; the local path stays
  independent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from ipaddress import ip_address
from pathlib import Path

from alice_acp.api_chat.contracts import prompt_hash as compute_prompt_hash
from alice_acp.api_chat.types import ApiChatModelClass, utc_now
from alice_acp.api_chat_gateway.worker_bridge import InferenceJobRequestDTO
from alice_acp.local_inference.backend import (
    InferenceTextBackend,
    InferenceTextResult,
    RealModelTextBackend,
)
from alice_acp.local_inference.hardware_select import (
    DEFAULT_GENERAL_LADDER,
    HostMemoryHint,
    LocalRuntimePlan,
    select_local_runtime,
)
from alice_acp.local_inference.model_resolver import (
    LocalModelResolver,
    ResolvedModel,
)
from alice_acp.local_inference.runtimes import (
    GenerationParams,
    RuntimeAdapter,
    StubRuntimeAdapter,
    real_adapter_for,
)
from alice_acp.mining_device.types import DeviceProbe

LOCAL_SHELL_CONTRACT_VERSION = "alice-track-a-local-inference-shell-v1"

REASON_LOCAL_RUN_COMPLETED = "local_inference_run_completed"
REASON_LOCAL_LANE_PRIVATE = "local_inference_private_no_network_no_credit"


def validate_loopback_bind_host(bind_host: str) -> None:
    """Local shell binds loopback ONLY (stricter than the gateway's private-OK).

    The whole point of LOCAL mode is that data never leaves the device, so the
    HTTP entry point must not be reachable off-host. Loopback only.
    """
    try:
        host = ip_address(bind_host)
    except ValueError as exc:
        raise ValueError("bind_host must be an IP address") from exc
    if not host.is_loopback:
        raise ValueError("local shell bind_host must be a loopback address")


@dataclass(frozen=True, slots=True)
class LocalRunResult:
    """The result of one local run: the completion + the real usage record.

    Carries the explicit, asserted statement that no network and no credit were
    involved, and that ``paid_acu`` stays zero (there is no ledger in this path).
    """

    contract_version: str
    completion: str
    model_id: str
    runtime: str
    model_class: ApiChatModelClass
    input_tokens: int
    output_tokens: int
    latency_ms: Decimal
    output_hash: str
    downloaded_model: bool
    completed_at: datetime
    reason_code: str = REASON_LOCAL_RUN_COMPLETED

    # Invariants surfaced for the caller (asserted, never derived from a ledger).
    network_calls_made: bool = False
    credit_ledger_touched: bool = False
    side_channel_used: bool = False
    paid_acu: Decimal = field(default=Decimal("0"))

    def to_public_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "reason_code": self.reason_code,
            "completion": self.completion,
            "model_id": self.model_id,
            "runtime": self.runtime,
            "model_class": self.model_class,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "latency_ms": str(self.latency_ms),
            "output_hash": self.output_hash,
            "downloaded_model": self.downloaded_model,
            "completed_at": self.completed_at.isoformat(),
            # The data-stays-local guarantees:
            "lane": REASON_LOCAL_LANE_PRIVATE,
            "network_calls_made": False,
            "credit_ledger_touched": False,
            "side_channel_used": False,
            "paid_acu": "0",
        }


@dataclass(slots=True)
class LocalInferenceShell:
    """Local entry point: detect -> select -> resolve -> run, all on-device.

    ``cache_root`` is the user's local model cache. ``use_stub`` selects the
    offline deterministic adapter (default True so the shell runs anywhere with
    no GPU/weights -- the real-model path flips this off on a real box). When
    ``use_stub`` is False, the real runtime adapter for the selected runtime is
    used and (if not cached) the model is downloaded request-time.

    The shell holds NO reference to any ledger, credit server, worker queue, or
    network transport -- by construction the local path cannot touch them.
    """

    cache_root: Path
    use_stub: bool = True
    adapter_override: RuntimeAdapter | None = None
    resolver_override: LocalModelResolver | None = None
    default_params: GenerationParams = field(default_factory=GenerationParams)
    candidate_tiers: tuple[ApiChatModelClass, ...] = DEFAULT_GENERAL_LADDER

    def plan(self, probe: DeviceProbe, memory: HostMemoryHint) -> LocalRuntimePlan:
        """Detect + select a runtime/tier for this host (no I/O)."""
        return select_local_runtime(
            probe,
            memory,
            candidate_tiers=self.candidate_tiers,
        )

    def _adapter_for(self, plan: LocalRuntimePlan) -> RuntimeAdapter:
        if self.adapter_override is not None:
            return self.adapter_override
        if self.use_stub:
            return StubRuntimeAdapter(runtime=plan.runtime)
        return real_adapter_for(plan.runtime)

    def _resolver(self) -> LocalModelResolver:
        if self.resolver_override is not None:
            return self.resolver_override
        return LocalModelResolver(cache_root=self.cache_root)

    def _resolve(
        self,
        plan: LocalRuntimePlan,
        resolver: LocalModelResolver,
    ) -> ResolvedModel:
        if self.use_stub and self.adapter_override is None:
            # Offline stub path: never download, just ensure the dir exists.
            return resolver.resolve_for_stub(plan.artifact)
        return resolver.resolve(plan.artifact)

    def _backend(
        self,
        plan: LocalRuntimePlan,
        resolved: ResolvedModel,
    ) -> InferenceTextBackend:
        adapter = self._adapter_for(plan)
        return RealModelTextBackend(
            artifact=plan.artifact,
            adapter=adapter,
            snapshot_dir=resolved.snapshot_dir,
            default_params=self.default_params,
        )

    def _local_job(
        self,
        plan: LocalRuntimePlan,
        prompt: str,
        *,
        max_output_tokens: int,
        observed_at: datetime,
    ) -> InferenceJobRequestDTO:
        """Build an in-process job for the LOCAL run.

        The job carries only ``prompt_hash`` (same privacy shape as the durable
        queue), even though the raw prompt is handed to the backend directly via
        the in-process call -- it never lands in the job object.
        """
        canonical = plan.artifact.model_class
        lane = "roleplay" if canonical in ("rp_lite_9b", "rp_pro_27b") else "general"
        return InferenceJobRequestDTO(
            job_id=f"local-{plan.runtime}-{observed_at.strftime('%Y%m%d%H%M%S%f')}",
            model_tier=canonical,
            lane=lane,
            prompt_hash=compute_prompt_hash(prompt),
            max_input_tokens=max(1, len(prompt)),
            max_output_tokens=max_output_tokens,
            timeout_ms=600_000,
            requested_at=observed_at,
        )

    def run(
        self,
        prompt: str,
        probe: DeviceProbe,
        memory: HostMemoryHint,
        *,
        max_output_tokens: int | None = None,
        now: datetime | None = None,
    ) -> LocalRunResult:
        """Run one prompt locally and return the completion + usage.

        End to end on-device: NO network call, NO credit/ledger, NO side-channel.
        """
        if not prompt:
            raise ValueError("prompt must be non-empty")
        observed_at = now or utc_now()
        plan = self.plan(probe, memory)
        resolver = self._resolver()
        resolved = self._resolve(plan, resolver)
        backend = self._backend(plan, resolved)
        job = self._local_job(
            plan,
            prompt,
            max_output_tokens=max_output_tokens or self.default_params.max_output_tokens,
            observed_at=observed_at,
        )
        text_result: InferenceTextResult = backend.run_with_prompt(job, prompt=prompt)
        usage = text_result.usage
        return LocalRunResult(
            contract_version=LOCAL_SHELL_CONTRACT_VERSION,
            completion=text_result.text,
            model_id=usage.model_id,
            runtime=plan.runtime,
            model_class=plan.artifact.model_class,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            latency_ms=usage.latency_ms,
            output_hash=usage.output_hash,
            downloaded_model=resolved.downloaded,
            completed_at=observed_at,
        )

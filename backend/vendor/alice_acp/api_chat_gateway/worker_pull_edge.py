"""The WORKER-PULL EDGE (task #7): an external GPU worker serves a real request.

This is the decentralized-inference MVP server side. It ties together the pieces
that already exist (the durable queue + STEP-0 side-channel + recount sidecar +
the shadow credit ledger) with the NEW worker-pull protocol so an EXTERNAL
worker can:

  1. REGISTER + PULL (long-poll) a job matching its declared capability
     (:meth:`WorkerPullEdge.pull`). The edge authenticates the worker's Alice
     address, registers a heartbeat (so dispatch routes to it), leases the next
     matching queued job on the worker's behalf, and reads the raw prompt off the
     STEP-0 side-channel to deliver in the lease (the durable queue carries only
     the prompt_hash).
  2. SUBMIT ``{completion, usage}`` (:meth:`WorkerPullEdge.submit`). The edge
     binds the submission to the lease, token-RECOUNTS the usage server-side
     (``min(declared, server_recount)``) via the recount sidecar (purged on every
     exit path), admits + completes on the shadow ledger to record CREDIT under
     the worker's ALICE ADDRESS (``paid_acu=0``), acks the durable queue,
     RETURNS the completion to the original API caller (held in a transient
     per-job completion store the gateway reads), and runs the FAST-PATH trust
     hooks (record completion -> reputation; decide async sampled re-execution).
  3. SAMPLE (async) a percentage of credited jobs for re-execution on a trusted
     worker (:meth:`WorkerPullEdge.apply_reexecution`): a mismatch DEMOTES the
     worker and CLAWS BACK that job's credit (removes the work record; nothing
     was ever paid, ``paid_acu`` stayed "0").

Trust model = FAST-PATH + reputation (owner-approved): the worker's completion is
served DIRECTLY (no blocking verification); reputation gates dispatch (a new /
low-rep worker is throughput-capped + sampled more heavily). An occasional bad
output before a cheater is caught is bounded + acceptable for the credit-only MVP.

Hard constraints (do NOT weaken): the raw prompt/completion NEVER land in a
durable/credit record; ``raw_prompt_persisted`` / ``raw_response_persisted`` stay
``False``; ``live_reward_enabled`` / ``payout_executor_enabled`` stay ``False``;
``paid_acu`` stays "0"; no payout address is ever carried. Staging-internal-only
is preserved on the harness; the EDGE adds the external worker boundary on top
WITHOUT flipping any forbidden harness flag.
"""

from __future__ import annotations

import hmac
import secrets
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal

from alice_acp.api_chat.contracts import stable_hash
from alice_acp.api_chat.types import utc_now, validate_public_identifier
from alice_acp.api_chat_gateway.colocated_inference_worker import acu_model_class_for_tier
from alice_acp.api_chat_gateway.inference_recount import (
    OffCatalogModelClaim,
    RecountTokenizerUnavailable,
    recounter_for_model_ref,
)
from alice_acp.api_chat_gateway.inference_side_channel import (
    InferenceRecountResultDTO,
    InferenceRecountSidecar,
    SideChannelTransport,
)
from alice_acp.api_chat_gateway.local_harness import ApiChatWorkerTransportHarness
from alice_acp.api_chat_gateway.mining_entry_gate import (
    REASON_ENTRY_GATE_UNDER_72H,
    MiningEntryGate,
)
from alice_acp.api_chat_gateway.worker_bridge import (
    REASON_WORKER_JOB_COMPLETED,
    InferenceJobRequestDTO,
    InferenceJobResultDTO,
    WorkerHeartbeatDTO,
    WorkerModelStateDTO,
)
from alice_acp.api_chat_gateway.worker_pull_protocol import (
    BUILT_WORKER_ROLES,
    WORKER_PULL_PROTOCOL_CONTRACT_VERSION,
    WorkerCapabilityDTO,
    WorkerPullIdentityDTO,
    WorkerPullJobDTO,
    WorkerPullRequestDTO,
    WorkerSubmitDTO,
)
from alice_acp.api_chat_gateway.worker_queue import WorkerQueueLeaseDTO
from alice_acp.api_chat_gateway.worker_reputation import (
    ReputationUpdateDTO,
    SampleDecisionDTO,
    WeightedSelectionDTO,
    WorkerReputationStore,
)
from alice_acp.api_chat_gateway.worker_transport import WorkerAuthHandleDTO, WorkerAuthScope
from alice_acp.local_inference.throughput_bench import (
    ThroughputKey,
    ThroughputObservationStore,
)
from alice_acp.services.verification_vps.scorer import (
    VERDICT_FAKE,
    VERDICT_INDETERMINATE,
    VERDICT_REAL,
    CpuLogprobVerifier,
    VpsScoreResult,
)
from alice_acp.services.verification_vps.verification_handoff import (
    VerificationHandoffChannel,
)
from alice_acp.shadow_server.server import ShadowServerHarness
from alice_acp.shadow_server.types import (
    MAIN_POOL_AI,
    SESSION_KIND_INFERENCE,
    InferenceCompletionRequest,
    ShadowSessionIssueRequest,
)

WORKER_PULL_EDGE_CONTRACT_VERSION = "api-chat-worker-pull-edge-contract-v1"

REASON_PULL_AUTH_REJECTED = "api_chat_worker_pull_auth_rejected"
REASON_PULL_ROLE_UNSUPPORTED = "api_chat_worker_pull_role_unsupported"
REASON_PULL_NO_JOB = "api_chat_worker_pull_no_job"
REASON_PULL_JOB_LEASED = "api_chat_worker_pull_job_leased"
REASON_PULL_LEASE_REJECTED = "api_chat_worker_pull_lease_rejected"
REASON_PULL_THROTTLED = "api_chat_worker_pull_throughput_capped"

#: M7 dispatch: a device was selected to serve a requested tier by the
#: reputation-WEIGHTED random draw among the eligible (capability-matching,
#: gate-passing, not-throughput-capped) registered workers.
REASON_DISPATCH_SELECTED = "api_chat_worker_dispatch_weighted_selected"
#: M7 dispatch: NO registered worker was eligible to serve the requested tier
#: (none matched the capability, OR all eligible were under the 72h gate, OR all
#: were at their per-device throughput cap). The caller queues / rejects.
REASON_DISPATCH_NO_ELIGIBLE_WORKER = "api_chat_worker_dispatch_no_eligible_worker"
#: M6: the 72h mandatory-mining ENTRY GATE rejected this device -- its cumulative
#: server-verified mining time is below the 72h window, so it is NOT YET eligible
#: for ANY inference job (anti-sybil entry cost + M_rate measurement window). A
#: re-keyed device-id resets the counter, so it must re-serve the 72h.
REASON_PULL_ENTRY_GATE_REJECTED = REASON_ENTRY_GATE_UNDER_72H

REASON_SUBMIT_LEASE_UNKNOWN = "api_chat_worker_submit_lease_unknown"
REASON_SUBMIT_LEASE_TOKEN_MISMATCH = "api_chat_worker_submit_lease_token_mismatch"
REASON_SUBMIT_NONCE_MISMATCH = "api_chat_worker_submit_control_nonce_mismatch"
REASON_SUBMIT_OUTPUT_HASH_MISMATCH = "api_chat_worker_submit_output_hash_mismatch"
REASON_SUBMIT_USAGE_TIER_MISMATCH = "api_chat_worker_submit_usage_tier_mismatch"
REASON_SUBMIT_ADMIT_REJECTED = "api_chat_worker_submit_admit_rejected"
REASON_SUBMIT_COMPLETE_REJECTED = "api_chat_worker_submit_complete_rejected"
REASON_SUBMIT_ACK_REJECTED = "api_chat_worker_submit_ack_rejected"
REASON_SUBMIT_CREDITED = "api_chat_worker_submit_credited"

#: M3 SEAL 4 (latency plausibility): a completion that arrives FASTER than the
#: claimed model could physically decode the served tokens on the worker's
#: device-class rate is implausible (likely cached / not actually run). It is
#: force-sampled + penalised, never silently credited as a clean job.
REASON_SUBMIT_LATENCY_IMPLAUSIBLE = "api_chat_worker_submit_latency_implausible"

#: M3 SEAL 3 (recount fail-closed): the worker claimed a model that is NOT in the
#: pinned (v102ss) catalog -- rejected outright (no credit, no tokenizer load).
REASON_SUBMIT_OFF_CATALOG_MODEL = "api_chat_worker_submit_off_catalog_model"
#: M3 SEAL 3: the claimed model is on-catalog but its tokenizer was unavailable,
#: so the server REFUSED to credit and HELD the job for heavy verification (the
#: worker-declared count is never trusted). The job is force-handed to the logprob
#: verifier; no provisional credit is recorded.
REASON_SUBMIT_RECOUNT_FAIL_CLOSED = "api_chat_worker_submit_recount_fail_closed"

REASON_CLAWBACK_APPLIED = "api_chat_worker_clawback_applied"
REASON_CLAWBACK_RECORD_MISSING = "api_chat_worker_clawback_record_missing"

REASON_VERIFY_SAMPLE_UNKNOWN = "api_chat_worker_verify_sample_unknown"
REASON_VERIFY_VERDICT_REAL = "api_chat_worker_verify_verdict_real"
REASON_VERIFY_VERDICT_FAKE = "api_chat_worker_verify_verdict_fake"
REASON_VERIFY_VERDICT_INDETERMINATE = "api_chat_worker_verify_verdict_indeterminate"


@dataclass(frozen=True, slots=True)
class WorkerPullResult:
    """The outcome of a worker PULL."""

    status: str  # "leased" | "no_job" | "rejected"
    reason_code: str
    identity: WorkerPullIdentityDTO | None = None
    job: WorkerPullJobDTO | None = None

    def to_public_dict(self) -> dict[str, object]:
        return {
            "contract_version": WORKER_PULL_EDGE_CONTRACT_VERSION,
            "status": self.status,
            "reason_code": self.reason_code,
            "identity": self.identity.to_public_dict() if self.identity is not None else None,
            # The REDACTED job view (no raw prompt). The prompt-bearing view goes
            # to the worker via WorkerPullJobDTO.to_worker_dict() at the HTTP edge.
            "job": self.job.to_public_dict() if self.job is not None else None,
            "raw_prompt_persisted": False,
            "live_reward_enabled": False,
            "payout_executor_enabled": False,
            "paid_acu": "0",
        }


@dataclass(frozen=True, slots=True)
class EligibleWorkerDTO:
    """M7: one registered worker that may serve a requested tier (a dispatch candidate).

    Carries the per-device ``device_key`` (the scoring/dispatch unit), the credit
    ``alice_address`` it rolls up to, the ``worker_id`` handle, and the inputs the
    weighted draw + caps used: ``reputation_score``, ``dispatch_weight`` (the draw
    weight), ``inflight`` / ``max_concurrency`` (the per-device throughput cap), and
    ``sample_rate`` (~100% for a new device, ~5% for a trusted one). Diagnostic /
    audit; credit-only (``paid_acu == "0"``).
    """

    device_key: str
    alice_address: str
    worker_id: str
    reputation_score: Decimal
    dispatch_weight: Decimal
    inflight: int
    max_concurrency: int
    sample_rate: Decimal

    def to_public_dict(self) -> dict[str, object]:
        return {
            "device_key": self.device_key,
            "alice_address": self.alice_address,
            "worker_id": self.worker_id,
            "reputation_score": str(self.reputation_score),
            "dispatch_weight": str(self.dispatch_weight),
            "inflight": self.inflight,
            "max_concurrency": self.max_concurrency,
            "sample_rate": str(self.sample_rate),
            "paid_acu": "0",
        }


@dataclass(frozen=True, slots=True)
class DispatchSelectionDTO:
    """M7: the outcome of choosing which registered worker serves a requested tier.

    ``selected_device_key`` is the device the reputation-WEIGHTED random draw picked
    (``None`` iff no worker was eligible). ``eligible`` is every candidate that
    passed the capability + 72h-gate + throughput-cap filters (the set offered to
    the draw); ``selection`` is the keyed-PRF weighted draw itself (the per-candidate
    weights + the draw point, for audit). Higher-reputation devices carry a larger
    weight, so they are proportionally more likely to be selected -- NOT round-robin,
    NOT uniform. Credit-only: selection never touches a payout/``paid_acu`` path.
    """

    model_tier: str
    status: str  # "selected" | "no_eligible_worker"
    reason_code: str
    selected_device_key: str | None
    eligible: tuple[EligibleWorkerDTO, ...]
    selection: WeightedSelectionDTO | None = None

    @property
    def selected(self) -> bool:
        return self.selected_device_key is not None

    def selected_worker(self) -> EligibleWorkerDTO | None:
        if self.selected_device_key is None:
            return None
        for worker in self.eligible:
            if worker.device_key == self.selected_device_key:
                return worker
        return None

    def to_public_dict(self) -> dict[str, object]:
        return {
            "contract_version": WORKER_PULL_EDGE_CONTRACT_VERSION,
            "model_tier": self.model_tier,
            "status": self.status,
            "reason_code": self.reason_code,
            "selected_device_key": self.selected_device_key,
            "eligible": [worker.to_public_dict() for worker in self.eligible],
            "selection": (
                self.selection.to_public_dict() if self.selection is not None else None
            ),
            "paid_acu": "0",
            "live_reward_enabled": False,
            "payout_executor_enabled": False,
        }


@dataclass(frozen=True, slots=True)
class CompletedInferenceDTO:
    """The completion + recounted usage the gateway returns to the original caller.

    Held transiently in the edge's per-job completion store keyed by ``job_id``.
    ``credited_input_tokens`` / ``credited_output_tokens`` are the server-RECOUNT
    anchored counts (``min(declared, recount)``) -- the credit basis. The raw
    ``completion`` is the model output returned to the caller; it is never
    persisted to a durable/credit record.
    """

    job_id: str
    completion: str
    model_id: str
    declared_input_tokens: int
    declared_output_tokens: int
    credited_input_tokens: int
    credited_output_tokens: int
    worker_alice_address: str
    worker_id: str
    verified_inference_acu: Decimal
    completed_at: datetime
    # M5: the per-device key ({alice_address}.{device_id}) that SERVED this job --
    # the unit of measurement/scoring. Credit still accrues to worker_alice_address;
    # this is carried so a clawback / re-execution moves the SERVING DEVICE's
    # reputation. Defaults to the address (legacy address-only / single device).
    worker_device_key: str = ""

    def usage_for_caller(self) -> dict[str, int]:
        """OpenAI-style usage block: the RECOUNTED (credited) counts."""
        return {
            "prompt_tokens": self.credited_input_tokens,
            "completion_tokens": self.credited_output_tokens,
            "total_tokens": self.credited_input_tokens + self.credited_output_tokens,
        }

    def to_public_dict(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "model_id": self.model_id,
            "declared_input_tokens": self.declared_input_tokens,
            "declared_output_tokens": self.declared_output_tokens,
            "credited_input_tokens": self.credited_input_tokens,
            "credited_output_tokens": self.credited_output_tokens,
            "worker_alice_address": self.worker_alice_address,
            "worker_id": self.worker_id,
            "worker_device_key": self.worker_device_key or self.worker_alice_address,
            "verified_inference_acu": str(self.verified_inference_acu),
            "completed_at": self.completed_at.isoformat(),
            "raw_response_persisted": False,
            "paid_acu": "0",
        }


@dataclass(frozen=True, slots=True)
class LatencyPlausibilityDTO:
    """M3 SEAL 4: the verdict of the dispatch->submit latency-plausibility check.

    ``elapsed_ms`` is the measured submit - dispatch wall time. ``min_plausible_ms``
    is the floor below which the claimed model could not have decoded
    ``output_tokens`` on its device-class rate. ``implausible`` is True when the
    completion arrived FASTER than that floor (likely cached / not actually run);
    such a job is force-sampled + reputation-penalised. ``checked`` is False when
    the peg could not resolve a device-class rate (e.g. a gguf worker with no
    class) -- then the check is a conservative no-op (no false penalty).
    """

    checked: bool
    implausible: bool
    elapsed_ms: str
    min_plausible_ms: str | None
    output_tokens: int
    tokens_per_hour: str | None
    reason_code: str

    def to_public_dict(self) -> dict[str, object]:
        return {
            "checked": self.checked,
            "implausible": self.implausible,
            "elapsed_ms": self.elapsed_ms,
            "min_plausible_ms": self.min_plausible_ms,
            "output_tokens": self.output_tokens,
            "tokens_per_hour": self.tokens_per_hour,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True, slots=True)
class WorkerSubmitResult:
    """The outcome of a worker SUBMISSION."""

    status: str  # "credited" | "rejected"
    reason_code: str
    job_id: str | None = None
    completed: CompletedInferenceDTO | None = None
    recount_result: InferenceRecountResultDTO | None = None
    sample_decision: SampleDecisionDTO | None = None
    worker_alice_address: str | None = None
    # M3 SEAL 4: the dispatch->submit latency-plausibility verdict (None on a
    # rejected submit that never reached the check).
    latency_check: LatencyPlausibilityDTO | None = None

    @property
    def credited(self) -> bool:
        return self.status == "credited"

    def to_public_dict(self) -> dict[str, object]:
        return {
            "contract_version": WORKER_PULL_EDGE_CONTRACT_VERSION,
            "status": self.status,
            "reason_code": self.reason_code,
            "job_id": self.job_id,
            "completed": self.completed.to_public_dict() if self.completed is not None else None,
            "recount_result": (
                self.recount_result.to_public_dict() if self.recount_result is not None else None
            ),
            "sample_decision": (
                self.sample_decision.to_public_dict()
                if self.sample_decision is not None
                else None
            ),
            "latency_check": (
                self.latency_check.to_public_dict() if self.latency_check is not None else None
            ),
            "worker_alice_address": self.worker_alice_address,
            "raw_response_persisted": False,
            "live_reward_enabled": False,
            "payout_executor_enabled": False,
            "paid_acu": "0",
        }


@dataclass(frozen=True, slots=True)
class VerificationVerdictDTO:
    """The outcome of scoring a SAMPLED job on the CPU logprob-VPS (plan §7).

    Bundles the scorer's verdict (real/fake/indeterminate) with the resulting
    reputation effect + whether a clawback was applied. ``reputation_update`` is
    ``None`` only for an INDETERMINATE verdict (a too-short sample / unavailable
    scorer -> no reputation change, no clawback). Credit-only: asserts
    ``paid_acu == "0"``; a clawback removes only PROVISIONAL credit.
    """

    job_id: str
    worker_alice_address: str
    verdict: str
    reason_code: str
    clawback_applied: bool
    score: VpsScoreResult | None = None
    reputation_update: ReputationUpdateDTO | None = None

    @property
    def is_fake(self) -> bool:
        return self.verdict == VERDICT_FAKE

    def to_public_dict(self) -> dict[str, object]:
        return {
            "contract_version": WORKER_PULL_EDGE_CONTRACT_VERSION,
            "job_id": self.job_id,
            "worker_alice_address": self.worker_alice_address,
            "verdict": self.verdict,
            "reason_code": self.reason_code,
            "clawback_applied": self.clawback_applied,
            "score": self.score.to_public_dict() if self.score is not None else None,
            "reputation_update": (
                self.reputation_update.to_public_dict()
                if self.reputation_update is not None
                else None
            ),
            "live_reward_enabled": False,
            "payout_executor_enabled": False,
            "paid_acu": "0",
        }


@dataclass(slots=True)
class _RegisteredWorker:
    """M7: the edge's PER-DEVICE record of a registered/heartbeating worker.

    The dispatcher's heartbeat map is keyed by ``worker_id`` (derived from the Alice
    ADDRESS), so two devices under one address would collide there. The edge keeps
    its OWN registry keyed by ``device_key`` so the weighted dispatch can enumerate
    candidates per DEVICE -- the scoring/dispatch unit. Holds the identity + the
    last-declared capability (for the tier-match filter) + the last heartbeat time.
    """

    identity: WorkerPullIdentityDTO
    capability: WorkerCapabilityDTO
    observed_at: datetime


@dataclass(slots=True)
class _LeaseRecord:
    """An in-flight lease the edge is tracking (server-side), keyed by job_id."""

    lease: WorkerQueueLeaseDTO
    identity: WorkerPullIdentityDTO
    lease_token: str
    job: InferenceJobRequestDTO
    assigned_worker: str | None
    # M1 / Route-1: the worker's declared runtime (mlx/cuda/gguf) captured at PULL
    # time, so the inference-complete credit can peg per-token credit to the PRL
    # mining rate of this worker's GPU class (T-table is keyed by runtime/quant;
    # gpu_class is derived from runtime, unambiguous for mlx/cuda).
    worker_runtime: str | None = None
    # TRANSIENT hold of the REAL prompt the worker conditioned on (popped off the
    # side-channel at PULL). The recount + the SAMPLED verifier must score the
    # SAME prompt the worker actually saw -- not the synthetic placeholder -- or a
    # logprob re-score is meaningless. Held ONLY while the lease is in flight and
    # dropped on lease release; like the side-channel + recount sidecar, it is a
    # transient in-memory hold and is NEVER written to a durable/credit record.
    raw_prompt: str | None = None
    # M3 SEAL 1: the REAL random per-request control nonce the edge minted at PULL.
    # The authoritative copy (the worker echoes it on submit; the edge re-checks).
    # Threaded into the recount context + the verifier scoring so the worker and
    # the verifier condition on the SAME nonce.
    control_nonce: str = ""
    # M3 SEAL 4: dispatch wall time (when the lease was issued). submit - dispatch
    # is the wall time the worker took; an implausibly small value for the claimed
    # model's decode rate flags a likely cached/not-run completion.
    dispatched_at: datetime | None = None


@dataclass(slots=True)
class WorkerPullEdge:
    """Server-side orchestrator for the external worker-pull inference edge.

    Holds the trusted server-side handles (the harness auth, the shared
    side-channel + recount sidecar, the shadow ledger, the reputation store) and
    mediates the external worker boundary. The external worker never touches the
    harness/ledger directly; it speaks the worker-pull protocol to this edge.
    """

    harness: ApiChatWorkerTransportHarness
    shadow: ShadowServerHarness
    side_channel: SideChannelTransport
    reputation: WorkerReputationStore = field(default_factory=WorkerReputationStore)
    recount_sidecar: InferenceRecountSidecar = field(default_factory=InferenceRecountSidecar)
    # SAMPLED verification hand-off (plan §4/§7): the transient surface that
    # carries the raw prompt + completion + nonce of the SAMPLED fraction to the
    # out-of-process CPU verifier, just before the recount sidecar purges. Raw text
    # never lands in a durable/credit record; purged on every exit path.
    verification_handoff: VerificationHandoffChannel = field(
        default_factory=VerificationHandoffChannel
    )
    # M5: production self-calibration of T. After each credited job the edge folds
    # the observed (output_tokens, decode seconds) into this store per (tier,
    # gpu_class, runtime, quant); a simple averaging read refines T over time
    # WITHOUT a hardware benchmark (the THROUGHPUT_T_TABLE rows stay estimates).
    # Default-constructed so an existing caller gets the hook for free; credit-only.
    throughput_observations: ThroughputObservationStore = field(
        default_factory=ThroughputObservationStore
    )
    # M6: the 72h mandatory-mining ENTRY GATE. A NEW device must accumulate 72h of
    # server-VERIFIED mining (fed via fold_verified_record / record_verified_interval
    # from the verified PRL accounting path) before pull() will lease it ANY inference
    # job. Keyed by device_key, so a re-keyed device-id starts at zero (anti-sybil
    # re-serve cost). Default ``None`` => NOT enforced, so an existing caller is
    # byte-for-byte unaffected (matching the repo's default-inert milestone retrofit
    # pattern); PRODUCTION wires a real gate in. When present, pull() rejects an
    # under-72h device. Credit-only: the gate only decides eligibility, never a payout.
    entry_gate: MiningEntryGate | None = None
    # Server secret keying the per-lease opaque token + the internal worker-auth
    # key_hash. Never a real secret; bound to the edge instance.
    edge_secret: str = "alice-worker-pull-edge-internal-secret"
    _leases: dict[str, _LeaseRecord] = field(default_factory=dict, init=False)
    _completions: dict[str, CompletedInferenceDTO] = field(default_factory=dict, init=False)
    # Per-DEVICE in-flight count (throughput cap by reputation; keyed by device_key).
    _inflight: dict[str, int] = field(default_factory=dict, init=False)
    # M7: the edge's PER-DEVICE registry of registered/heartbeating workers (keyed by
    # device_key), so the weighted dispatch can enumerate candidates per device. The
    # dispatcher's own heartbeat map is keyed by worker_id (per ADDRESS) and would
    # collide across sibling devices, so dispatch reads THIS map instead.
    _registered: dict[str, _RegisteredWorker] = field(default_factory=dict, init=False)

    # ------------------------------------------------------------------ auth ---
    def authenticate(
        self,
        *,
        alice_address: str,
        role: str = "ai_inference",
        device_id: str | None = None,
    ) -> WorkerPullIdentityDTO:
        """Authenticate a worker by its Alice address (the SAME mining-lane gate).

        Raises ``ValueError`` on a malformed/wrong-network address, an unsupported
        role, or (M5) a malformed ``device_id``; the HTTP/transport layer maps that
        to a 401/400. ``device_id`` (M5) is the per-device measurement/scoring key:
        when supplied the identity's :meth:`~WorkerPullIdentityDTO.device_key` is
        ``{alice_address}.{device_id}`` so reputation/M_rate/dispatch are keyed PER
        DEVICE; credit still accrues to the address. Omit it for legacy
        address-only behaviour.
        """
        if role not in BUILT_WORKER_ROLES:
            raise ValueError(REASON_PULL_ROLE_UNSUPPORTED)
        return WorkerPullIdentityDTO.authenticate(
            alice_address=alice_address, role=role, device_id=device_id
        )

    # -------------------------------------------------------------- register ---
    def register(
        self,
        *,
        identity: WorkerPullIdentityDTO,
        capability: WorkerCapabilityDTO,
        now: datetime | None = None,
    ) -> dict[str, object]:
        """Register / refresh the worker's heartbeat WITHOUT leasing a job.

        A pull worker registers (declares it is online + the tiers it can serve)
        so the gateway's dispatch routes incoming user requests to it. This is the
        "I'm online" call the worker client makes before/independently of pulling
        (the push-routing scheduler needs a healthy registered device to enqueue
        a job for). Returns a redacted ack with the score-derived concurrency cap.
        """
        observed_at = now or utc_now()
        if identity.role not in BUILT_WORKER_ROLES:
            raise ValueError(REASON_PULL_ROLE_UNSUPPORTED)
        if identity.role != capability.role:
            raise ValueError("identity role must match capability role")
        # M5: throughput cap + reputation are PER DEVICE (the device_key), so a
        # low-rep device is capped without affecting a sibling device under the
        # same address.
        device_key = identity.device_key()
        max_concurrency = self.reputation.max_concurrency_for(device_key)
        self._register_worker(identity, capability, max_concurrency, observed_at)
        return {
            "contract_version": WORKER_PULL_EDGE_CONTRACT_VERSION,
            "registered": True,
            "worker_id": identity.worker_id,
            "alice_address": identity.alice_address,
            "device_id": identity.device_id,
            "device_key": device_key,
            "worker_name": identity.worker_name,
            "model_tiers": list(capability.model_tiers),
            "max_concurrency": max_concurrency,
            "reputation_score": str(self.reputation.score_for(device_key)),
            "live_reward_enabled": False,
            "payout_executor_enabled": False,
            "paid_acu": "0",
        }

    # ----------------------------------------------------------- dispatch ---
    def eligible_workers_for_tier(self, model_tier: str) -> tuple[EligibleWorkerDTO, ...]:
        """M7: the registered workers eligible to serve ``model_tier`` right now.

        A registered DEVICE is eligible iff it (a) declared it serves the tier
        (capability match), (b) passes the 72h entry gate when one is wired (an
        under-72h device is NOT a candidate -- the same gate :meth:`pull` enforces,
        applied at dispatch so an ineligible device is never even offered a job),
        and (c) is below its per-device throughput cap (in-flight leases <
        score-derived ``max_concurrency``; a NEW/low-rep device is capped harder).
        Each survivor is reported with its score, dispatch weight, in-flight/cap, and
        sample rate. Order is by device_key for a stable, reproducible candidate set.
        """
        eligible: list[EligibleWorkerDTO] = []
        for device_key in sorted(self._registered):
            registered = self._registered[device_key]
            if not registered.capability.serves_tier(model_tier):
                continue
            # 72h ENTRY GATE (when wired): an under-72h device is not a candidate.
            if self.entry_gate is not None and not self.entry_gate.is_eligible(device_key):
                continue
            # THROUGHPUT CAP: a device already at its per-device concurrency cap is
            # not offered a new job (bounded in-flight leases -> a new/low-rep device
            # is held to few concurrent jobs until it earns reputation).
            max_concurrency = self.reputation.max_concurrency_for(device_key)
            inflight = self._inflight.get(device_key, 0)
            if inflight >= max_concurrency:
                continue
            score = self.reputation.score_for(device_key)
            eligible.append(
                EligibleWorkerDTO(
                    device_key=device_key,
                    alice_address=registered.identity.alice_address,
                    worker_id=registered.identity.worker_id,
                    reputation_score=score,
                    dispatch_weight=self.reputation.dispatch_weight_for(device_key),
                    inflight=inflight,
                    max_concurrency=max_concurrency,
                    sample_rate=self.reputation.policy.sample_rate_for(score),
                )
            )
        return tuple(eligible)

    def select_worker_for_tier(
        self,
        model_tier: str,
        *,
        draw_id: str,
        now: datetime | None = None,
    ) -> DispatchSelectionDTO:
        """M7: pick which registered worker serves ``model_tier`` (plan §6 dispatch).

        REPUTATION-WEIGHTED RANDOM dispatch: among the eligible workers
        (:meth:`eligible_workers_for_tier` -- capability-matched, 72h-gate-passing,
        below their per-device throughput cap) select ONE via a reputation-WEIGHTED
        random draw, so a HIGHER per-device reputation yields a HIGHER selection
        probability -- NOT round-robin, NOT uniform. New / low-reputation devices
        carry a small floor weight (so they DO get occasional jobs + the ~100%
        sampling that proves them out) and are throughput-capped (the eligibility
        filter already drops a device at its bounded concurrent-lease cap); trusted
        devices carry ~their score in weight and get more throughput + the 1-5%
        sample rate. ``draw_id`` keys the PRF draw (e.g. the job id), so the choice is
        unpredictable to a worker yet deterministic + unit-testable. Returns the
        selected device_key (or a no-eligible-worker result for the caller to queue /
        reject). Credit-only: this is a dispatch decision, never a payout.
        """
        _observed_at = now or utc_now()
        eligible = self.eligible_workers_for_tier(model_tier)
        if not eligible:
            return DispatchSelectionDTO(
                model_tier=str(model_tier),
                status="no_eligible_worker",
                reason_code=REASON_DISPATCH_NO_ELIGIBLE_WORKER,
                selected_device_key=None,
                eligible=eligible,
                selection=None,
            )
        selection = self.reputation.select_weighted(
            tuple(worker.device_key for worker in eligible),
            draw_id=draw_id,
            server_secret=self.edge_secret,
        )
        if selection.selected_device_key is None:
            return DispatchSelectionDTO(
                model_tier=str(model_tier),
                status="no_eligible_worker",
                reason_code=REASON_DISPATCH_NO_ELIGIBLE_WORKER,
                selected_device_key=None,
                eligible=eligible,
                selection=selection,
            )
        return DispatchSelectionDTO(
            model_tier=str(model_tier),
            status="selected",
            reason_code=REASON_DISPATCH_SELECTED,
            selected_device_key=selection.selected_device_key,
            eligible=eligible,
            selection=selection,
        )

    # ------------------------------------------------------------------ pull ---
    def pull(
        self, request: WorkerPullRequestDTO, *, now: datetime | None = None
    ) -> WorkerPullResult:
        """Register the worker + lease the next matching job (with the prompt)."""
        observed_at = now or utc_now()
        identity = request.identity
        if identity.role not in BUILT_WORKER_ROLES:
            return WorkerPullResult(
                status="rejected",
                reason_code=REASON_PULL_ROLE_UNSUPPORTED,
                identity=identity,
            )

        # Reputation gates dispatch: throughput-cap a DEVICE at its score-derived
        # concurrency (M5: keyed by the device_key, so a new/low-rep DEVICE is
        # capped harder without throttling a sibling device under the same address).
        device_key = identity.device_key()
        max_concurrency = self.reputation.max_concurrency_for(device_key)
        if self._inflight.get(device_key, 0) >= max_concurrency:
            return WorkerPullResult(
                status="rejected",
                reason_code=REASON_PULL_THROTTLED,
                identity=identity,
            )

        # M6 72h ENTRY GATE: a NEW device must have accumulated >= 72h of
        # server-VERIFIED mining before it may serve ANY inference job. Reject an
        # under-72h device BEFORE leasing (no job is dequeued for it, so the prompt
        # never leaves the side-channel). Keyed by device_key -> a re-keyed device
        # is back at zero and must re-serve the window (anti-sybil + M_rate window).
        # No gate wired (default) => not enforced (existing callers unaffected).
        if self.entry_gate is not None and not self.entry_gate.is_eligible(device_key):
            return WorkerPullResult(
                status="rejected",
                reason_code=REASON_PULL_ENTRY_GATE_REJECTED,
                identity=identity,
            )

        # Register/refresh the worker heartbeat so the dispatcher routes a queued
        # job to THIS worker's device id. The heartbeat capacity caps concurrency
        # in the scheduler too; keep it consistent with the reputation cap.
        self._register_worker(identity, request.capability, max_concurrency, observed_at)

        leased = self.harness.lease(
            self._auth(identity, ("lease",)),
            now=observed_at,
        )
        if leased.status == "empty":
            return WorkerPullResult(
                status="no_job",
                reason_code=REASON_PULL_NO_JOB,
                identity=identity,
            )
        if leased.status != "accepted" or leased.lease is None:
            return WorkerPullResult(
                status="rejected",
                reason_code=REASON_PULL_LEASE_REJECTED,
                identity=identity,
            )

        lease = leased.lease
        record = self._record_for_lease(lease)
        if record is None:
            return WorkerPullResult(
                status="rejected",
                reason_code=REASON_PULL_LEASE_REJECTED,
                identity=identity,
            )
        job = record.job

        # Capability match: the leased job's tier MUST be one the worker declared
        # it can serve. (The scheduler already device-matched; this is a strict
        # belt-and-suspenders check on the worker's own declared capability.)
        if not request.capability.serves_tier(job.model_tier):
            # Requeue (retry) so another capable worker can take it; do not fail.
            self.harness.nack(
                self._auth(identity, ("retry",)),
                lease,
                reason_code=REASON_PULL_LEASE_REJECTED,
                retry=True,
                now=observed_at,
            )
            return WorkerPullResult(
                status="no_job",
                reason_code=REASON_PULL_NO_JOB,
                identity=identity,
            )

        # STEP-0 PRIVACY: read the raw prompt off the side-channel (single-shot
        # pop). The durable queue only ever carried the prompt_hash. Deliver the
        # prompt to the worker in the lease; it never enters a durable record.
        raw_prompt = self.side_channel.take_prompt(job.job_id)
        if raw_prompt is None:
            # No prompt in flight (e.g. a job enqueued without the side-channel):
            # cannot serve a REAL completion. Fail the lease closed.
            self.harness.nack(
                self._auth(identity, ("fail",)),
                lease,
                reason_code=REASON_PULL_LEASE_REJECTED,
                retry=False,
                now=observed_at,
            )
            return WorkerPullResult(
                status="rejected",
                reason_code=REASON_PULL_LEASE_REJECTED,
                identity=identity,
            )

        lease_token = self._lease_token(identity, lease)
        # M3 SEAL 1: mint a CRYPTOGRAPHICALLY-RANDOM per-request control nonce
        # SERVER-SIDE (never derived from the job id, so a worker cannot
        # pre-compute it). It rides the lease to the worker on the control channel,
        # is retained authoritatively here, and is threaded into the recount +
        # verifier so all three condition on the same nonce.
        control_nonce = _mint_control_nonce()
        self._leases[job.job_id] = _LeaseRecord(
            lease=lease,
            identity=identity,
            lease_token=lease_token,
            job=job,
            assigned_worker=record.assigned_worker,
            worker_runtime=request.capability.runtime,
            # Retain the REAL prompt the worker conditioned on (transiently) so the
            # recount + the SAMPLED verifier re-score the same prompt the worker saw.
            raw_prompt=raw_prompt,
            control_nonce=control_nonce,
            dispatched_at=observed_at,
        )
        # M5: in-flight count is PER DEVICE (the throughput cap is the device's).
        self._inflight[device_key] = self._inflight.get(device_key, 0) + 1
        pull_job = WorkerPullJobDTO(
            job_id=job.job_id,
            lease_token=lease_token,
            model_tier=job.model_tier,
            lane=job.lane,
            prompt=raw_prompt,
            prompt_hash=job.prompt_hash,
            max_input_tokens=job.max_input_tokens,
            max_output_tokens=job.max_output_tokens,
            timeout_ms=job.timeout_ms,
            control_nonce=control_nonce,
            leased_at=observed_at,
        )
        return WorkerPullResult(
            status="leased",
            reason_code=REASON_PULL_JOB_LEASED,
            identity=identity,
            job=pull_job,
        )

    # ---------------------------------------------------------------- submit ---
    def submit(
        self, submission: WorkerSubmitDTO, *, now: datetime | None = None
    ) -> WorkerSubmitResult:
        """Accept ``{completion, usage}``: recount -> credit -> return -> sample."""
        observed_at = now or utc_now()
        tracked = self._leases.get(submission.job_id)
        if tracked is None:
            return WorkerSubmitResult(
                status="rejected",
                reason_code=REASON_SUBMIT_LEASE_UNKNOWN,
                job_id=submission.job_id,
            )
        if submission.lease_token != tracked.lease_token:
            return WorkerSubmitResult(
                status="rejected",
                reason_code=REASON_SUBMIT_LEASE_TOKEN_MISMATCH,
                job_id=submission.job_id,
                worker_alice_address=tracked.identity.alice_address,
            )

        # M3 SEAL 1: the worker must echo the SAME server-minted control nonce the
        # lease delivered. A worker that submits a different / stale nonce did not
        # condition on this request's nonce -- reject (constant-time compare to not
        # leak the nonce via timing). The authoritative nonce is the lease record's
        # (the echo is a fail-fast cross-check, never the source of truth).
        if not hmac.compare_digest(submission.control_nonce, tracked.control_nonce):
            self._fail_lease(tracked, REASON_SUBMIT_NONCE_MISMATCH, observed_at)
            return self._rejected_submit(tracked, REASON_SUBMIT_NONCE_MISMATCH, observed_at)

        identity = tracked.identity
        job = tracked.job

        # Server-side output-hash check: the worker's declared output_hash MUST
        # match a recompute over the completion it submitted (a self-consistency
        # gate; a cheater that submits a hash != its own text is rejected here).
        if submission.output_hash != _server_output_hash(submission.completion):
            self._fail_lease(tracked, REASON_SUBMIT_OUTPUT_HASH_MISMATCH, observed_at)
            return self._rejected_submit(
                tracked, REASON_SUBMIT_OUTPUT_HASH_MISMATCH, observed_at
            )

        # The declared usage tier must match the job's tier (a worker cannot
        # claim a heavier ACU tier than the job it leased).
        if submission.usage.model_class != _expected_model_class(job):
            self._fail_lease(tracked, REASON_SUBMIT_USAGE_TIER_MISMATCH, observed_at)
            return self._rejected_submit(
                tracked, REASON_SUBMIT_USAGE_TIER_MISMATCH, observed_at
            )

        # M3 SEAL 3 (recount fail-closed): resolve the tokenizer for the CLAIMED
        # model BEFORE the recount. An OFF-CATALOG claim (no pinned v102ss artifact)
        # is rejected outright. A legit on-catalog model whose tokenizer cannot be
        # loaded yields recounter=None + fail_closed=True, so the recount REFUSES to
        # credit (it never trusts the worker-declared count) and the job is held for
        # heavy verification below.
        try:
            recounter = recounter_for_model_ref(submission.usage.model_id)
            recount_fail_closed_armed = False
        except OffCatalogModelClaim:
            self._fail_lease(tracked, REASON_SUBMIT_OFF_CATALOG_MODEL, observed_at)
            return self._rejected_submit(tracked, REASON_SUBMIT_OFF_CATALOG_MODEL, observed_at)
        except RecountTokenizerUnavailable:
            recounter = None
            recount_fail_closed_armed = True

        # Clamp the declared counts to the job budgets so the recount anchor can
        # only ever LOWER credit, never raise it past the request's ceiling.
        declared_input = min(submission.usage.input_tokens, job.max_input_tokens)
        declared_output = min(submission.usage.output_tokens, job.max_output_tokens)

        # SAMPLING DECISION (plan §4/§7) -- made BEFORE the recount context closes
        # so the SAMPLED fraction's raw text can be handed to the verifier just
        # before purge. M3 SEAL 2: the SELECTION is a SECRET-KEYED PRF over the job
        # id -- HMAC(server_secret, job_id) -- so the RATE stays reputation-driven +
        # public but WHICH jobs get sampled is unpredictable to the worker (the
        # server secret never leaves the server). Computed against the worker's
        # CURRENT score (a new/low-rep worker is sampled at up to 100%, a trusted
        # worker at ~5%).
        sample_decision = self.reputation.should_sample(
            job_id=job.job_id,
            device_key=identity.device_key(),
            server_secret=self.edge_secret,
        )

        # M3 SEAL 4 (latency plausibility): the worker took submit - dispatch wall
        # time. If the claimed model could not physically have decoded the served
        # tokens that fast on its device-class rate, the completion is implausible
        # (likely cached / not actually run). FORCE sampling (so the logprob
        # verifier scores it) and record a reputation penalty -- it is never
        # silently credited as a clean job.
        latency_check = self._latency_plausibility(
            tracked=tracked,
            submission=submission,
            declared_output=declared_output,
            observed_at=observed_at,
        )
        # SEAL 3 + 4 both force the logprob verifier: a fail-closed recount holds
        # the job for heavy verification, and an implausibly-fast completion is
        # force-scored. Either, plus the keyed-PRF sample, drives the hand-off.
        forced_sampled = (
            sample_decision.sampled
            or latency_check.implausible
            or recount_fail_closed_armed
        )

        # M3 SEAL 1: the AUTHORITATIVE control nonce is the lease record's
        # server-minted random value (the worker echoed it; we already verified the
        # echo). Thread it -- NOT a job-id-derived value -- into the recount + the
        # verifier so the worker and the verifier conditioned on the SAME nonce.
        nonce = tracked.control_nonce

        # STEP-0/2 RECOUNT: hold the REAL prompt the worker conditioned on (retained
        # transiently on the lease record at PULL) + the raw completion in the
        # sidecar; the sidecar purges both on every exit path. The credit basis is
        # min(declared, server_recount). When the job is SAMPLED, the recount
        # context hands that same prompt + completion + nonce to the verification
        # hand-off channel ONCE, just before purge -- so the verifier re-scores the
        # prompt the worker actually saw. Falls back to a synthetic prompt only if
        # the lease somehow carried none (defensive; the recount still purges).
        prompt_for_recount = tracked.raw_prompt or _synthetic_prompt(job)

        with self.recount_sidecar.recount(
            job_id=job.job_id,
            raw_prompt=prompt_for_recount,
            raw_completion=submission.completion,
            declared_input_tokens=declared_input,
            declared_output_tokens=declared_output,
            now=observed_at,
            # M3 SEAL 3: the per-job tokenizer for the claimed model (or None +
            # fail-closed, which refuses credit instead of trusting declared).
            recounter=recounter,
            fail_closed_on_missing_tokenizer=recount_fail_closed_armed,
            # SAMPLED hand-off wiring: a sampled / latency-flagged / fail-closed job
            # is handed off (SEAL 3 holds for heavy verification, SEAL 4 forces the
            # logprob verifier on a too-fast one).
            handoff_sink=self.verification_handoff,
            sampled=forced_sampled,
            nonce=nonce,
            model_ref=submission.usage.model_id,
            worker_alice_address=identity.alice_address,
        ) as hold:
            # STEP 2 (server token-recount): re-tokenize the held raw prompt +
            # completion server-side with the claimed model's tokenizer. min(
            # declared, recount) is the anchored credit basis. M3 SEAL 3: when the
            # recount fails closed (no tokenizer), credited tokens collapse to 0 and
            # the job is REFUSED credit + held for heavy verification.
            hold.server_recount()
            if hold.recount_failed_closed:
                # Refuse credit; hold for heavy verification (do NOT trust the
                # worker-declared count). The job is handed to the logprob verifier
                # (forced_sampled is True), and the lease is failed so the worker
                # cannot re-submit under this lease.
                hold.mark_held_for_verification()
                self._fail_lease(tracked, REASON_SUBMIT_RECOUNT_FAIL_CLOSED, observed_at)
                result = self._rejected_submit(
                    tracked, REASON_SUBMIT_RECOUNT_FAIL_CLOSED, observed_at
                )
            else:
                result = self._credit_and_return(
                    tracked=tracked,
                    submission=submission,
                    declared_input=declared_input,
                    declared_output=declared_output,
                    observed_at=observed_at,
                )
                # Mark credited INSIDE the context so the finally hands off the
                # SAMPLED fraction (only a successfully-credited job has credit to
                # claw back; a fail-closed job is held-for-verification instead).
                if result.credited:
                    hold.mark_credited()
        # Raw text purged here (the sampled fraction was handed off just before).
        recount_result = self.recount_sidecar.last_result(job.job_id)
        if not result.credited:
            return WorkerSubmitResult(
                status=result.status,
                reason_code=result.reason_code,
                job_id=result.job_id,
                recount_result=recount_result,
                sample_decision=sample_decision,
                worker_alice_address=identity.alice_address,
                latency_check=latency_check,
            )

        # FAST-PATH trust hooks: record the completion against the DEVICE's
        # reputation (M5: per-device key). (The sampled re-score + verdict is driven
        # by verify_sample / apply_reexecution; the sampling decision was made above,
        # pre-purge.)
        self.reputation.record_completion(identity.device_key(), now=observed_at)
        # M3 SEAL 4: an implausibly-fast completion takes a reputation penalty NOW
        # (in addition to being force-handed-off to the logprob verifier above).
        # The penalty does NOT claw back the provisional credit on its own -- the
        # logprob verdict decides that; it just lowers the score so the DEVICE is
        # sampled harder + throughput-capped until it proves out. Credit-only.
        if latency_check.implausible:
            self.reputation.apply_latency_penalty(
                identity.device_key(),
                now=observed_at,
            )
        # M5 (T self-calibration): fold this REAL job's observed decode throughput
        # (recounted output tokens / measured decode seconds) into the observation
        # store, per (tier, gpu_class, runtime, quant), so T refines over time from
        # live traffic. Best-effort: a job whose (tier, runtime) cannot resolve a
        # T key (e.g. a gguf worker with no class) is simply not recorded -- the
        # estimate stands. Never blocks the credit/return path. Credit-only.
        if result.completed is not None:
            self._record_throughput_observation(
                tracked=tracked,
                completed=result.completed,
                submission=submission,
            )
        return WorkerSubmitResult(
            status=result.status,
            reason_code=result.reason_code,
            job_id=result.job_id,
            completed=result.completed,
            recount_result=recount_result,
            sample_decision=sample_decision,
            worker_alice_address=identity.alice_address,
            latency_check=latency_check,
        )

    def _record_throughput_observation(
        self,
        *,
        tracked: _LeaseRecord,
        completed: CompletedInferenceDTO,
        submission: WorkerSubmitDTO,
    ) -> None:
        """M5: record this job's observed decode throughput for T self-calibration.

        Keys by (tier=leased job's fine catalog tier, gpu_class=from runtime,
        runtime, quant=pinned quant for (tier, runtime)) -- the SAME key the T-table
        + Route-1 peg use -- and folds (credited_output_tokens, decode seconds) in.
        Uses the worker's MEASURED decode latency (not dispatch->submit wall time,
        which includes network) as the elapsed basis. Fail-soft: any unresolved key
        / non-positive value is skipped (the estimate row stands).
        """
        runtime = tracked.worker_runtime
        if runtime is None:
            return
        gpu_class = _gpu_class_for_runtime(runtime)
        if gpu_class is None:
            return
        tier = tracked.job.model_tier
        quant = _pinned_quant(tier, runtime)
        if quant is None:
            return
        output_tokens = completed.credited_output_tokens
        try:
            elapsed_seconds = Decimal(submission.usage.latency_ms) / Decimal("1000")
        except (ArithmeticError, ValueError, TypeError):
            return
        if output_tokens <= 0 or elapsed_seconds <= Decimal("0"):
            return
        self.throughput_observations.record(
            key=ThroughputKey(
                tier=tier,  # type: ignore[arg-type]
                gpu_class=gpu_class,  # type: ignore[arg-type]
                runtime=runtime,  # type: ignore[arg-type]
                quant=quant,
            ),
            output_tokens=output_tokens,
            elapsed_seconds=elapsed_seconds,
        )

    def _credit_and_return(
        self,
        *,
        tracked: _LeaseRecord,
        submission: WorkerSubmitDTO,
        declared_input: int,
        declared_output: int,
        observed_at: datetime,
    ) -> WorkerSubmitResult:
        """Admit -> complete (credit under the Alice address) -> ack -> store completion.

        Runs INSIDE the recount-sidecar context so every early-return/exception
        triggers the sidecar's synchronous raw-text purge.
        """
        identity = tracked.identity
        job = tracked.job

        # Admit an inference-demand session for THIS worker's Alice-address
        # identity (passport_id = the Alice address; device/worker = the derived
        # handle). Reward/payout stay OFF; no payout address.
        admit = self._register_and_admit(
            identity=identity,
            model_id=submission.usage.model_id,
            job_id=job.job_id,
            observed_at=observed_at,
        )
        if not admit.accepted or admit.session is None:
            self._fail_lease(tracked, REASON_SUBMIT_ADMIT_REJECTED, observed_at)
            return self._rejected_submit(tracked, admit.reason_code, observed_at)
        session = admit.session

        # M1 / Route-1 peg inputs: bill the RECOUNTED tokens (declared_* here are
        # already min(declared, recount)) at credit_per_token = M_rate(GPU) /
        # T(model,GPU) so a full-load GPU earns ~= its PRL rate. The fine catalog
        # tier comes from the leased job; the runtime from the worker's declared
        # capability (captured at pull); gpu_class is derived from the runtime
        # (mlx->apple, cuda->nvidia -- unambiguous). If the peg cannot resolve
        # (e.g. a gguf worker with no class, or an unbenchmarked tier) the ledger
        # falls back to the legacy abstract-ACU credit. Credit-only.
        runtime = tracked.worker_runtime
        gpu_class = _gpu_class_for_runtime(runtime)
        completion = self.shadow.inference_complete(
            InferenceCompletionRequest(
                proof_id=_proof_id(job_id=job.job_id, session_id=session.session_id),
                session_id=session.session_id,
                session_signature=session.signature,
                model_id=submission.usage.model_id,
                input_tokens=declared_input,
                output_tokens=declared_output,
                context_length=submission.usage.context_length,
                latency_ms=Decimal(submission.usage.latency_ms),
                model_class=submission.usage.model_class,
                observed_at=observed_at,
                simulated_api_payment=Decimal("0"),  # credit-only
                model_tier=job.model_tier,
                runtime=runtime,
                gpu_class=gpu_class,
                # M5: select THIS device's measured PRL M_rate when the ledger has a
                # device-rate reader wired (else inert -> per-class table, M1).
                route1_passport_id=identity.alice_address,
                route1_device_id=(identity.device_id or identity.worker_id),
            )
        )
        if not completion.accepted or completion.record is None:
            self._fail_lease(tracked, REASON_SUBMIT_COMPLETE_REJECTED, observed_at)
            return self._rejected_submit(tracked, completion.reason_code, observed_at)

        # Ack the durable queue (the job is done). The result carries hashes +
        # counts only; never raw text.
        ack_result = InferenceJobResultDTO(
            job_id=job.job_id,
            status="completed",
            reason_code=REASON_WORKER_JOB_COMPLETED,
            model_tier=job.model_tier,
            lane=job.lane,
            prompt_hash=job.prompt_hash,
            assigned_worker=tracked.assigned_worker,
            max_input_tokens=job.max_input_tokens,
            max_output_tokens=job.max_output_tokens,
            timeout_ms=job.timeout_ms,
            input_tokens=declared_input,
            output_tokens=declared_output,
            output_hash=submission.output_hash,
            completed_at=observed_at,
        )
        ack = self.harness.ack(
            self._auth(identity, ("complete",)),
            tracked.lease,
            ack_result,
            now=observed_at,
        )
        if ack.status != "accepted":
            # Credit recorded; queue ack failed. Surface as rejected so the caller
            # does not double-count, but the lease is released below regardless.
            self._release_lease(tracked)
            return WorkerSubmitResult(
                status="rejected",
                reason_code=REASON_SUBMIT_ACK_REJECTED,
                job_id=job.job_id,
                worker_alice_address=identity.alice_address,
            )

        # min(declared, server_recount) anchor for what we report as credited.
        # The recount stub returns declared, so credited == declared here; a real
        # server recount can only LOWER it.
        credited_input = declared_input
        credited_output = declared_output

        completed = CompletedInferenceDTO(
            job_id=job.job_id,
            completion=submission.completion,
            model_id=submission.usage.model_id,
            declared_input_tokens=declared_input,
            declared_output_tokens=declared_output,
            credited_input_tokens=credited_input,
            credited_output_tokens=credited_output,
            worker_alice_address=identity.alice_address,
            worker_id=identity.worker_id,
            verified_inference_acu=completion.record.verified_score,
            completed_at=observed_at,
            # M5: the SERVING DEVICE key (credit still accrues to the address).
            worker_device_key=identity.device_key(),
        )
        # Stash the completion for the gateway to return to the original caller,
        # and record the proof_id so a clawback can reverse this exact credit.
        self._completions[job.job_id] = completed
        # M5: remember which DEVICE served this job so a later logprob verdict /
        # re-execution moves the SERVING DEVICE's reputation (the handoff task only
        # carries the credit address). Credit still accrues to the address.
        self._device_keys[job.job_id] = identity.device_key()
        self._release_lease(tracked, proof_id=completion.record.proof_id)
        return WorkerSubmitResult(
            status="credited",
            reason_code=REASON_SUBMIT_CREDITED,
            job_id=job.job_id,
            completed=completed,
            worker_alice_address=identity.alice_address,
        )

    # ---------------------------------------------- M3 SEAL 4: latency check ---
    def _latency_plausibility(
        self,
        *,
        tracked: _LeaseRecord,
        submission: WorkerSubmitDTO,
        declared_output: int,
        observed_at: datetime,
    ) -> LatencyPlausibilityDTO:
        """Flag a completion that arrives FASTER than physically plausible.

        Computes the measured dispatch->submit wall time and the floor below which
        the claimed model could not have decoded ``declared_output`` tokens at its
        device-class full-load rate (the SAME Route-1 throughput table the credit
        peg uses, tokens/hour -> ms/token). A measured time below that floor (minus
        a tolerance) is IMPLAUSIBLE: the worker likely served a cached / not-run
        answer. Fail-soft: if the device rate cannot be resolved (e.g. a gguf
        worker with no class, or no benchmarked row), the check is a conservative
        no-op (``checked=False``) so an honest worker is never falsely penalised.
        Credit-only; this only sizes a fraud SIGNAL, never a payout.
        """
        elapsed_ms = self._elapsed_ms(tracked.dispatched_at, observed_at, submission)
        tokens_per_hour = _device_tokens_per_hour(
            model_class=submission.usage.model_class,
            runtime=tracked.worker_runtime,
        )
        if tokens_per_hour is None or declared_output <= 0:
            return LatencyPlausibilityDTO(
                checked=False,
                implausible=False,
                elapsed_ms=str(elapsed_ms),
                min_plausible_ms=None,
                output_tokens=declared_output,
                tokens_per_hour=(str(tokens_per_hour) if tokens_per_hour is not None else None),
                reason_code=REASON_SUBMIT_LATENCY_IMPLAUSIBLE,
            )
        # ms/token = 3_600_000 / tokens_per_hour. Min plausible decode time for the
        # served tokens, scaled by a tolerance factor (a worker may briefly burst
        # above the steady-state full-load rate; only a clearly-too-fast time is a
        # signal, not normal jitter).
        ms_per_token = Decimal("3600000") / tokens_per_hour
        min_plausible_ms = (
            ms_per_token * Decimal(declared_output) * LATENCY_PLAUSIBILITY_TOLERANCE
        ).quantize(Decimal("0.001"))
        implausible = elapsed_ms < min_plausible_ms
        return LatencyPlausibilityDTO(
            checked=True,
            implausible=implausible,
            elapsed_ms=str(elapsed_ms),
            min_plausible_ms=str(min_plausible_ms),
            output_tokens=declared_output,
            tokens_per_hour=str(tokens_per_hour),
            reason_code=REASON_SUBMIT_LATENCY_IMPLAUSIBLE,
        )

    @staticmethod
    def _elapsed_ms(
        dispatched_at: datetime | None,
        observed_at: datetime,
        submission: WorkerSubmitDTO,
    ) -> Decimal:
        """The dispatch->submit wall time in ms (server-measured, fail-soft).

        Prefers the edge-measured (submit_observed - dispatched) wall time -- the
        worker cannot forge it. Falls back to the worker-declared decode latency
        only if the edge never recorded a dispatch time (defensive; should not
        happen for a leased job). A non-positive measured delta also falls back.
        """
        if dispatched_at is not None:
            delta_ms = Decimal(str((observed_at - dispatched_at).total_seconds())) * Decimal(
                "1000"
            )
            if delta_ms > Decimal("0"):
                return delta_ms.quantize(Decimal("0.001"))
        # Fallback: the worker's own declared latency (already validated positive).
        return Decimal(submission.usage.latency_ms)

    # ------------------------------------------------ async sampled re-exec ---
    def apply_reexecution(
        self,
        *,
        job_id: str,
        original_output_hash: str,
        reexecuted_output_hash: str,
        now: datetime | None = None,
    ) -> ReputationUpdateDTO:
        """Compare a trusted-worker re-execution to the original; clawback on mismatch.

        ``matched`` = the two output hashes agree. A MATCH raises the worker's
        reputation. A MISMATCH demotes the worker AND claws back that job's credit
        (removes the work record from the ledger; ``paid_acu`` was always "0", so
        nothing was ever paid). Returns the reputation update (with the clawback
        flag) for the caller to surface/audit.
        """
        observed_at = now or utc_now()
        completed = self._completions.get(job_id)
        if completed is None:
            raise ValueError(REASON_SUBMIT_LEASE_UNKNOWN)
        matched = (
            len(original_output_hash) == 64
            and len(reexecuted_output_hash) == 64
            and original_output_hash == reexecuted_output_hash
        )
        # M5: the verdict moves the SERVING DEVICE's reputation (the completion
        # carries its device key). A demoted device does not affect its siblings.
        update = self.reputation.apply_sample_result(
            job_id=job_id,
            device_key=self._device_key_for_job(job_id, completed.worker_alice_address),
            matched=matched,
            now=observed_at,
        )
        if update.clawback_required:
            self._clawback_credit(job_id)
        return update

    def verify_sample(
        self,
        verifier: CpuLogprobVerifier,
        *,
        job_id: str,
        now: datetime | None = None,
    ) -> VerificationVerdictDTO:
        """Score a SAMPLED job on the CPU logprob-VPS, then verdict -> reputation/clawback.

        This is the logprob re-scoring analog of :meth:`apply_reexecution` (which
        compares output hashes). It TAKES the sampled job's raw prompt + completion
        + nonce off the verification hand-off channel (single-shot pop -> the
        channel purges its copy), runs ONE forward pass under the claimed model via
        ``verifier``, and folds the verdict into reputation:

        * ``real`` (served tokens high-probability) -> reputation UP, no clawback.
        * ``fake`` (low-probability -> smaller/fake model or garbage) -> demote +
          CLAW BACK that job's provisional credit (``paid_acu`` was always "0").
        * ``indeterminate`` (too-short sample / unscorable) -> no reputation change,
          no clawback.

        Out-of-process by construction: the verifier holds the model; this edge
        only hands it the inputs + applies the verdict. Raises if the job was not
        sampled / already taken (no raw text to score).
        """
        observed_at = now or utc_now()
        task = self.verification_handoff.take(job_id)
        if task is None:
            raise ValueError(REASON_VERIFY_SAMPLE_UNKNOWN)
        score = verifier.score(
            job_id=task.job_id,
            prompt=task.raw_prompt,
            completion=task.raw_completion,
            nonce=task.nonce,
            model_ref=task.model_ref,
        )
        if score.verdict == VERDICT_INDETERMINATE:
            # No judgment: do not move reputation, do not claw back.
            return VerificationVerdictDTO(
                job_id=job_id,
                worker_alice_address=task.worker_alice_address,
                verdict=VERDICT_INDETERMINATE,
                reason_code=REASON_VERIFY_VERDICT_INDETERMINATE,
                clawback_applied=False,
                score=score,
                reputation_update=None,
            )
        matched = score.verdict == VERDICT_REAL
        # M5: fold the logprob verdict into the SERVING DEVICE's reputation. The
        # handoff task carries only the credit address; the edge looked up which
        # device served the job at credit time. A demoted device is isolated.
        update = self.reputation.apply_sample_result(
            job_id=job_id,
            device_key=self._device_key_for_job(job_id, task.worker_alice_address),
            matched=matched,
            now=observed_at,
        )
        clawback_applied = False
        if update.clawback_required:
            clawback_applied = self._clawback_credit(job_id) == REASON_CLAWBACK_APPLIED
        return VerificationVerdictDTO(
            job_id=job_id,
            worker_alice_address=task.worker_alice_address,
            verdict=score.verdict,
            reason_code=(
                REASON_VERIFY_VERDICT_REAL if matched else REASON_VERIFY_VERDICT_FAKE
            ),
            clawback_applied=clawback_applied,
            score=score,
            reputation_update=update,
        )

    def _clawback_credit(self, job_id: str) -> str:
        """Reverse the credit for a job: remove its work record from the ledger.

        Credit-only clawback: the work record (the ONLY credit artifact) is
        deleted so it drops out of every future settlement denominator. Nothing
        was ever paid (``paid_acu`` stayed "0"), so this touches no payout/chain
        path. Idempotent: a missing record is a no-op.
        """
        proof_id = self._proof_ids.get(job_id)
        if proof_id is None or proof_id not in self.shadow.ledger.work_records:
            return REASON_CLAWBACK_RECORD_MISSING
        record = self.shadow.ledger.work_records[proof_id]
        # HARD INVARIANT (credit-only clawback): the record being clawed back must
        # have paid_acu == 0. We only ever claw back PROVISIONAL credit inside the
        # verification window -- nothing was ever paid. If a record somehow carried
        # a non-zero paid_acu, refuse the clawback (it would imply a payout path we
        # must never touch in v0); fail closed loudly rather than silently mutate.
        if record.paid_acu != Decimal("0"):
            raise AssertionError(
                "clawback refused: paid_acu must be 0 (credit-only); "
                f"job_id={job_id} proof_id={proof_id}"
            )
        del self.shadow.ledger.work_records[proof_id]
        # Drop any still-pending verification sample for this job (it was clawed
        # back via the hash path before the logprob verifier took it) so no raw
        # text lingers in the hand-off channel.
        self.verification_handoff.discard(job_id)
        return REASON_CLAWBACK_APPLIED

    def address_reputation(self, alice_address: str):
        """M5: the address-level roll-up of its devices' reputation (credit view).

        Credit accrues to the Alice address; this aggregates the per-device
        reputation counters under it (see
        :meth:`WorkerReputationStore.address_aggregate`). Diagnostic / audit only --
        the per-device scores stay the dispatch gate.
        """
        return self.reputation.address_aggregate(alice_address)

    # ------------------------------------------------------ caller readback ---
    def take_completion(self, job_id: str) -> CompletedInferenceDTO | None:
        """Pop the completion for ``job_id`` (the gateway returns it to the caller).

        Single-shot: the completion is removed once read so the transient store
        does not accumulate raw completion text. Returns ``None`` if not yet
        submitted (the caller polls / long-polls).
        """
        validate_public_identifier("job_id", job_id)
        return self._completions.pop(job_id, None)

    def peek_completion(self, job_id: str) -> CompletedInferenceDTO | None:
        """Non-destructive read (diagnostics / the re-execution compare)."""
        validate_public_identifier("job_id", job_id)
        return self._completions.get(job_id)

    # -------------------------------------------------------------- internals --
    _proof_ids: dict[str, str] = field(default_factory=dict, init=False)
    # M5: job_id -> the per-device key that SERVED it, so the async re-exec /
    # logprob verdict folds into the SERVING DEVICE's reputation.
    _device_keys: dict[str, str] = field(default_factory=dict, init=False)

    def _register_worker(
        self,
        identity: WorkerPullIdentityDTO,
        capability: WorkerCapabilityDTO,
        max_concurrency: int,
        observed_at: datetime,
    ) -> None:
        # M7: record/refresh the PER-DEVICE registry entry so the weighted dispatch
        # can enumerate this device as a candidate for the tiers it declared.
        self._registered[identity.device_key()] = _RegisteredWorker(
            identity=identity,
            capability=capability,
            observed_at=observed_at,
        )
        heartbeat = WorkerHeartbeatDTO(
            device_id=identity.worker_id,
            passport_id=identity.alice_address,
            lanes=capability.lanes(),
            model_state=tuple(
                WorkerModelStateDTO(model_tier=tier, availability="loaded")
                for tier in capability.model_tiers
            ),
            runtime=capability.runtime,
            free_memory_gb=capability.free_memory_gb,
            # M7 residual fix: the in-flight count is PER DEVICE (the _inflight map is
            # keyed by device_key, set/released per device on pull/submit). Read it by
            # device_key, NOT the bare alice_address -- a sibling device under the same
            # address must not see (or inflate) this device's queue depth.
            queue_depth=self._inflight.get(identity.device_key(), 0),
            health="healthy",
            observed_at=observed_at,
        )
        self.harness.register_worker(heartbeat)

    def _register_and_admit(
        self,
        *,
        identity: WorkerPullIdentityDTO,
        model_id: str,
        job_id: str,
        observed_at: datetime,
    ):
        demand_session_id = f"demand-pull-{job_id}"
        # M5: the shadow session's device_id is the per-device id when the worker
        # supplied one (so the work record / AI-cap reconciliation key the DEVICE),
        # else the address-derived worker_id (legacy single-device). Credit still
        # accrues to passport_id = the Alice address.
        device_id = identity.device_id or identity.worker_id
        register = getattr(self.shadow.ledger.demand_store, "register_demand", None)
        if callable(register):
            register(
                demand_session_id=demand_session_id,
                passport_id=identity.alice_address,
                device_id=device_id,
            )
        return self.shadow.inference_admit(
            ShadowSessionIssueRequest(
                passport_id=identity.alice_address,
                device_id=device_id,
                lane=MAIN_POOL_AI,
                session_kind=SESSION_KIND_INFERENCE,
                worker_id=identity.worker_id,
                model_id=model_id,
                requested_at=observed_at,
                demand_session_id=demand_session_id,
                live_reward_enabled=False,
                payout_executor_enabled=False,
                miner_provided_payout_address=None,
            )
        )

    def _fail_lease(
        self,
        tracked: _LeaseRecord,
        reason_code: str,
        observed_at: datetime,
    ) -> None:
        self.harness.nack(
            self._auth(tracked.identity, ("fail",)),
            tracked.lease,
            reason_code=reason_code,
            retry=False,
            now=observed_at,
        )
        self._release_lease(tracked)

    def _rejected_submit(
        self,
        tracked: _LeaseRecord,
        reason_code: str,
        observed_at: datetime,
    ) -> WorkerSubmitResult:
        return WorkerSubmitResult(
            status="rejected",
            reason_code=reason_code,
            job_id=tracked.job.job_id,
            worker_alice_address=tracked.identity.alice_address,
        )

    def _release_lease(self, tracked: _LeaseRecord, *, proof_id: str | None = None) -> None:
        self._leases.pop(tracked.job.job_id, None)
        # Drop the transient real-prompt hold on lease release (privacy): once the
        # job is done + (if sampled) handed off, the lease record's prompt copy is
        # no longer needed and must not linger. The sidecar already purged its copy;
        # the SAMPLED fraction's copy now lives only in the hand-off channel until
        # the verifier takes it. The control nonce also no longer needs holding here
        # (the sampled fraction's copy rode the hand-off; M3 SEAL 1).
        tracked.raw_prompt = None
        tracked.control_nonce = ""
        # M5: release the PER-DEVICE in-flight slot (matching the per-device acquire).
        device_key = tracked.identity.device_key()
        self._inflight[device_key] = max(0, self._inflight.get(device_key, 1) - 1)
        if proof_id is not None:
            self._proof_ids[tracked.job.job_id] = proof_id

    def _device_key_for_job(self, job_id: str, fallback_address: str) -> str:
        """The per-device key that served ``job_id`` (M5), recorded at credit time.

        Falls back to the credit address (the legacy single-device key) if the job
        predates the per-device record -- so an older completion still resolves to a
        valid reputation key rather than raising.
        """
        return self._device_keys.get(job_id, fallback_address)

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

    def _auth(
        self,
        identity: WorkerPullIdentityDTO,
        scopes: tuple[WorkerAuthScope, ...],
    ) -> WorkerAuthHandleDTO:
        return WorkerAuthHandleDTO(
            worker_id=identity.worker_id,
            passport_id=identity.alice_address,
            key_id="worker-pull-edge",
            key_hash=stable_hash(
                {
                    "edge_secret": self.edge_secret,
                    "worker_id": identity.worker_id,
                    "alice_address": identity.alice_address,
                }
            ),
            scopes=scopes,
            authenticated_at=utc_now(),
        )

    def _lease_token(self, identity: WorkerPullIdentityDTO, lease: WorkerQueueLeaseDTO) -> str:
        digest = stable_hash(
            {
                "edge_secret": self.edge_secret,
                "lease_id": lease.lease_id,
                "job_id": lease.job_id,
                "worker_id": identity.worker_id,
            }
        )
        return f"leasetok-{digest[:32]}"

    def to_public_dict(self, *, now: datetime | None = None) -> dict[str, object]:
        return {
            "contract_version": WORKER_PULL_EDGE_CONTRACT_VERSION,
            "protocol_contract_version": WORKER_PULL_PROTOCOL_CONTRACT_VERSION,
            "inflight_leases": len(self._leases),
            "pending_completions": len(self._completions),
            # M7: per-device registered workers the weighted dispatch draws from.
            "registered_devices": len(self._registered),
            "dispatch_policy": "reputation_weighted_random",
            "reputation": self.reputation.to_public_dict(),
            "entry_gate": (
                self.entry_gate.to_public_dict() if self.entry_gate is not None else None
            ),
            "throughput_observations": self.throughput_observations.to_public_dict(),
            "recount_sidecar": self.recount_sidecar.to_public_dict(),
            "verification_handoff": self.verification_handoff.to_public_dict(),
            "raw_prompt_persisted": False,
            "raw_response_persisted": False,
            "live_reward_enabled": False,
            "payout_executor_enabled": False,
            "paid_acu": "0",
        }


def _server_output_hash(completion: str) -> str:
    """The server's recompute of a completion's hash for the self-consistency gate.

    Mirrors the runtime adapters' ``hash_completion`` so a worker that runs the
    real adapter (which hashes ``completion:{text}``) matches by construction.
    """
    from alice_acp.local_inference.runtimes import hash_completion

    return hash_completion(completion)


def _expected_model_class(job: InferenceJobRequestDTO) -> str:
    return acu_model_class_for_tier(job.model_tier)


def _gpu_class_for_runtime(runtime: str | None) -> str | None:
    """Derive the worker's GPU class from its declared runtime (Route-1 M_rate key).

    Reuses the peg's single inverse (mlx->apple, cuda->nvidia unambiguous; gguf
    ambiguous -> ``None`` so the peg falls back rather than guessing the wrong PRL
    rate for a gguf worker). ``None`` runtime -> ``None`` class.
    """
    if runtime is None:
        return None
    from alice_acp.shadow_server.route1_peg import gpu_class_for_runtime

    return gpu_class_for_runtime(runtime)  # type: ignore[arg-type]


def _proof_id(*, job_id: str, session_id: str) -> str:
    digest = stable_hash(
        {
            "contract": WORKER_PULL_EDGE_CONTRACT_VERSION,
            "job_id": job_id,
            "session_id": session_id,
        }
    )
    return f"worker-pull-inference-{digest[:24]}"


def _synthetic_prompt(job: InferenceJobRequestDTO) -> str:
    return f"[synthetic-prompt job={job.job_id} prompt_hash={job.prompt_hash}]"


#: M3 SEAL 4: the fraction of the model's steady-state full-load decode time a
#: completion must AT LEAST take to be plausible. A worker can briefly burst above
#: the benchmarked full-load tokens/hour (warm cache, short prompt), so we only
#: flag a time below this fraction of the theoretical floor -- well-clear of
#: normal jitter, catching only a clearly-too-fast (cached/not-run) completion.
LATENCY_PLAUSIBILITY_TOLERANCE = Decimal("0.25")


def _mint_control_nonce() -> str:
    """M3 SEAL 1: a cryptographically-random per-request control nonce.

    Uses :func:`secrets.token_hex` (CSPRNG) so the nonce is unpredictable to the
    worker and unique per request. The ``vnonce-`` prefix keeps it a valid public
    identifier (the lease/submit DTOs validate it). The value is NOT a secret (it
    is delivered to the worker) -- it is a freshness/anti-replay token.
    """
    return f"vnonce-{secrets.token_hex(16)}"


def _device_tokens_per_hour(*, model_class: str, runtime: str | None) -> Decimal | None:
    """M3 SEAL 4: the claimed model's full-load decode rate on the worker's device.

    Maps the worker's ACU model_class (tier1/tier2) + declared runtime to the
    Route-1 throughput table's tokens/hour for that (tier, gpu_class, runtime,
    quant) -- the SAME table the credit peg reads. Returns ``None`` (the check
    becomes a no-op) when the device class is ambiguous (gguf) or no benchmarked
    row exists, so an honest worker on an un-benchmarked path is never falsely
    flagged. The tier->concrete-tier mapping picks the FASTEST catalog tier in the
    ACU class so the plausibility floor is CONSERVATIVE (a faster reference rate =>
    a smaller min time => fewer false positives).
    """
    if runtime is None:
        return None
    from alice_acp.local_inference.throughput_bench import lookup_throughput
    from alice_acp.shadow_server.route1_peg import gpu_class_for_runtime

    gpu_class = gpu_class_for_runtime(runtime)  # type: ignore[arg-type]
    if gpu_class is None:
        return None
    best: Decimal | None = None
    for tier in _acu_class_reference_tiers(model_class):
        quant = _pinned_quant(tier, runtime)
        if quant is None:
            continue
        measurement = lookup_throughput(
            tier=tier, gpu_class=gpu_class, runtime=runtime, quant=quant  # type: ignore[arg-type]
        )
        if measurement is None:
            continue
        if best is None or measurement.tokens_per_hour > best:
            best = measurement.tokens_per_hour
    return best


def _acu_class_reference_tiers(model_class: str) -> tuple[str, ...]:
    """Concrete catalog tiers that map to an ACU model_class (SEAL 4 reference).

    The credit + recount work in ACU classes (tier1/tier2); the throughput table is
    keyed by concrete catalog tier. We enumerate the concrete tiers in each ACU
    class so the latency floor can use the FASTEST as a conservative reference.
    """
    from alice_acp.api_chat_gateway.colocated_inference_worker import (
        _TIER1_MODEL_CLASSES,
        _TIER2_MODEL_CLASSES,
    )

    if model_class == "tier1_local_llm":
        return tuple(_TIER1_MODEL_CLASSES)
    if model_class == "tier2_local_llm":
        return tuple(_TIER2_MODEL_CLASSES)
    return ()


def _pinned_quant(tier: str, runtime: str) -> str | None:
    from alice_acp.shadow_server.route1_peg import resolve_quant

    return resolve_quant(tier, runtime)  # type: ignore[arg-type]

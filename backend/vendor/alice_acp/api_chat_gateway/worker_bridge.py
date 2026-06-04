from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import TYPE_CHECKING, Literal

from alice_acp.api_chat.model_catalog import MODEL_PROFILES, canonical_model_class
from alice_acp.api_chat.types import (
    ApiChatModelClass,
    utc_now,
    validate_public_identifier,
)
from alice_acp.api_chat.validators import validate_aware_timestamp, validate_sha256
from alice_acp.api_chat_gateway.model_routing import (
    ApiChatModelRouteScheduler,
    DeviceCapacityDTO,
    ModelRouteDecision,
    ModelRouteRateLimitResult,
    ModelRouteRequest,
)
from alice_acp.api_chat_gateway.types import GatewayMode

if TYPE_CHECKING:
    from alice_acp.api_chat_gateway.worker_queue import (
        DurableWorkerQueue,
        WorkerQueueLeaseDTO,
        WorkerQueueOperationDTO,
    )
    from alice_acp.api_chat_gateway.worker_transport import InternalWorkerTransportDTO

WORKER_BRIDGE_CONTRACT_VERSION = "api-chat-worker-bridge-contract-v1"

REASON_WORKER_BRIDGE_DISABLED = "api_chat_worker_bridge_default_off"
REASON_WORKER_BRIDGE_KILL_SWITCH = "api_chat_worker_bridge_kill_switch_unavailable"
REASON_WORKER_BRIDGE_UNAVAILABLE = "api_chat_worker_bridge_unavailable"
REASON_WORKER_BRIDGE_ROUTED = "api_chat_worker_bridge_routed"
REASON_WORKER_BRIDGE_REMOTE_CALL_FORBIDDEN = "api_chat_worker_bridge_remote_call_forbidden"
REASON_WORKER_ENQUEUE_REQUIRES_ROUTED_JOB = "api_chat_worker_enqueue_requires_routed_job"
REASON_WORKER_ASSIGN_REQUIRES_ENQUEUED_JOB = "api_chat_worker_assign_requires_enqueued_job"
REASON_WORKER_ASSIGN_REQUIRES_ADMITTED_ROUTE = "api_chat_worker_assign_requires_admitted_route"
REASON_WORKER_RESULT_REQUIRES_ASSIGNED_JOB = "api_chat_worker_result_requires_assigned_job"
REASON_WORKER_JOB_ENQUEUED = "api_chat_worker_job_enqueued"
REASON_WORKER_JOB_ASSIGNED = "api_chat_worker_job_assigned"
REASON_WORKER_JOB_COMPLETED = "api_chat_worker_job_completed"
REASON_WORKER_JOB_FAILED = "api_chat_worker_job_failed"
REASON_WORKER_JOB_FALLBACK = "api_chat_worker_job_fallback_result"

WorkerLane = Literal["general", "roleplay"]
WorkerRuntime = Literal["mlx", "cuda", "gguf"]
WorkerHealth = Literal["healthy", "degraded", "unavailable"]
WorkerModelAvailability = Literal["loaded", "cached", "missing"]
WorkerRouteStatus = Literal["routed", "rejected"]
WorkerEnvelopeStatus = Literal["enqueued", "assigned"]
WorkerResultStatus = Literal["completed", "failed", "fallback"]

VALID_WORKER_LANES: tuple[WorkerLane, ...] = ("general", "roleplay")
VALID_WORKER_RUNTIMES: tuple[WorkerRuntime, ...] = ("mlx", "cuda", "gguf")
VALID_WORKER_HEALTH: tuple[WorkerHealth, ...] = (
    "healthy",
    "degraded",
    "unavailable",
)
VALID_MODEL_AVAILABILITY: tuple[WorkerModelAvailability, ...] = (
    "loaded",
    "cached",
    "missing",
)


@dataclass(frozen=True, slots=True)
class WorkerModelStateDTO:
    model_tier: ApiChatModelClass
    availability: WorkerModelAvailability

    def __post_init__(self) -> None:
        canonical = canonical_model_class(self.model_tier)
        if canonical not in MODEL_PROFILES:
            raise ValueError("worker model_tier is unsupported")
        if self.availability not in VALID_MODEL_AVAILABILITY:
            raise ValueError("worker model availability is unsupported")
        object.__setattr__(self, "model_tier", canonical)

    @property
    def lane(self) -> WorkerLane:
        return MODEL_PROFILES[self.model_tier].family

    def to_public_dict(self) -> dict[str, object]:
        return {
            "model_tier": self.model_tier,
            "lane": self.lane,
            "availability": self.availability,
        }


@dataclass(frozen=True, slots=True)
class WorkerHeartbeatDTO:
    device_id: str
    passport_id: str
    lanes: tuple[WorkerLane, ...]
    model_state: tuple[WorkerModelStateDTO, ...]
    runtime: WorkerRuntime
    free_memory_gb: int
    queue_depth: int
    health: WorkerHealth
    observed_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        validate_public_identifier("device_id", self.device_id)
        validate_public_identifier("passport_id", self.passport_id)
        lanes = tuple(dict.fromkeys(self.lanes))
        if not lanes:
            raise ValueError("worker lanes must not be empty")
        for lane in lanes:
            if lane not in VALID_WORKER_LANES:
                raise ValueError("worker lane is unsupported")
        if self.runtime not in VALID_WORKER_RUNTIMES:
            raise ValueError("worker runtime is unsupported")
        if self.free_memory_gb < 0:
            raise ValueError("free_memory_gb must be non-negative")
        if self.queue_depth < 0:
            raise ValueError("queue_depth must be non-negative")
        if self.health not in VALID_WORKER_HEALTH:
            raise ValueError("worker health is unsupported")
        validate_aware_timestamp("observed_at", self.observed_at)
        model_state = tuple(self.model_state)
        if len({state.model_tier for state in model_state}) != len(model_state):
            raise ValueError("worker model_state contains duplicate model_tier")
        advertised = set(lanes)
        for state in model_state:
            if state.lane not in advertised:
                raise ValueError("worker model_state lane is not advertised")
        object.__setattr__(self, "lanes", lanes)
        object.__setattr__(self, "model_state", model_state)

    @property
    def healthy_for_assignment(self) -> bool:
        return self.health == "healthy"

    def to_device_capacity(self) -> DeviceCapacityDTO:
        loaded = tuple(
            state.model_tier
            for state in self.model_state
            if state.availability == "loaded"
        )
        cached = tuple(
            state.model_tier
            for state in self.model_state
            if state.availability == "cached"
        )
        return DeviceCapacityDTO(
            device_id=self.device_id,
            miner_id=self.passport_id,
            platform=_platform_for_runtime(self.runtime),
            memory_gb=max(1, self.free_memory_gb),
            runtimes=(self.runtime,),
            loaded_model_classes=loaded,
            cached_model_classes=cached,
            online=self.healthy_for_assignment,
            current_inference_jobs=1 if self.queue_depth > 0 else 0,
            max_concurrent_requests=1,
            queue_depth=self.queue_depth,
            supports_mining_throttle=True,
            observed_at=self.observed_at,
        )

    def to_public_dict(self) -> dict[str, object]:
        return {
            "device_id": self.device_id,
            "passport_id": self.passport_id,
            "lanes": self.lanes,
            "model_state": tuple(state.to_public_dict() for state in self.model_state),
            "runtime": self.runtime,
            "free_memory_gb": self.free_memory_gb,
            "queue_depth": self.queue_depth,
            "health": self.health,
            "observed_at": self.observed_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class InferenceJobRequestDTO:
    job_id: str
    model_tier: ApiChatModelClass
    lane: WorkerLane
    prompt_hash: str
    max_input_tokens: int
    max_output_tokens: int
    timeout_ms: int
    requested_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        validate_public_identifier("job_id", self.job_id)
        canonical = canonical_model_class(self.model_tier)
        if canonical not in MODEL_PROFILES:
            raise ValueError("job model_tier is unsupported")
        if self.lane not in VALID_WORKER_LANES:
            raise ValueError("job lane is unsupported")
        if MODEL_PROFILES[canonical].family != self.lane:
            raise ValueError("job lane does not match model_tier family")
        validate_sha256(self.prompt_hash, field_name="prompt_hash")
        for field_name, value in (
            ("max_input_tokens", self.max_input_tokens),
            ("max_output_tokens", self.max_output_tokens),
            ("timeout_ms", self.timeout_ms),
        ):
            if value <= 0:
                raise ValueError(f"{field_name} must be positive")
        validate_aware_timestamp("requested_at", self.requested_at)
        object.__setattr__(self, "model_tier", canonical)

    def to_model_route_request(
        self,
        *,
        identity_kind: Literal["anonymous", "api_key", "user_id"] = "anonymous",
    ) -> ModelRouteRequest:
        return ModelRouteRequest(
            request_id=self.job_id,
            mode=_gateway_mode_for_job(self),
            requested_model_class=self.model_tier,
            prompt_hash=self.prompt_hash,
            observed_at=self.requested_at,
            identity_kind=identity_kind,
        )

    def to_public_dict(self) -> dict[str, object]:
        return {
            "job_id": self.job_id,
            "model_tier": self.model_tier,
            "lane": self.lane,
            "prompt_hash": self.prompt_hash,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "timeout_ms": self.timeout_ms,
            "requested_at": self.requested_at.isoformat(),
        }


@dataclass(frozen=True, slots=True)
class WorkerBridgeRouteResult:
    status: WorkerRouteStatus
    reason_code: str
    job: InferenceJobRequestDTO
    route_decision: ModelRouteDecision | None = None
    selected_worker: str | None = None
    rate_limit_result: ModelRouteRateLimitResult | None = None
    fail_closed: bool = False
    model_dispatch_enabled: bool = False
    remote_model_call_performed: bool = False
    public_service_enabled: bool = False
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False

    def __post_init__(self) -> None:
        if self.status not in ("routed", "rejected"):
            raise ValueError("worker route status is unsupported")
        validate_public_identifier("reason_code", self.reason_code)
        if self.selected_worker is not None:
            validate_public_identifier("selected_worker", self.selected_worker)
        _validate_closed_gates(
            model_dispatch_enabled=self.model_dispatch_enabled,
            remote_model_call_performed=self.remote_model_call_performed,
            public_service_enabled=self.public_service_enabled,
            live_reward_enabled=self.live_reward_enabled,
            payout_executor_enabled=self.payout_executor_enabled,
        )
        if self.status == "routed" and self.route_decision is None:
            raise ValueError("routed worker bridge result requires route_decision")
        if self.status == "rejected" and not self.fail_closed:
            raise ValueError("rejected worker bridge result must fail closed")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "contract_version": WORKER_BRIDGE_CONTRACT_VERSION,
            "status": self.status,
            "reason_code": self.reason_code,
            "job": self.job.to_public_dict(),
            "route_decision": _route_decision_dict(self.route_decision),
            "selected_worker": self.selected_worker,
            "rate_limit_result": _rate_limit_dict(self.rate_limit_result),
            "fail_closed": self.fail_closed,
            "model_dispatch_enabled": False,
            "remote_model_call_performed": False,
            "public_service_enabled": False,
            "live_reward_enabled": False,
            "payout_executor_enabled": False,
        }


@dataclass(frozen=True, slots=True)
class InferenceJobEnvelopeDTO:
    job: InferenceJobRequestDTO
    status: WorkerEnvelopeStatus
    reason_code: str
    route_decision: ModelRouteDecision
    assigned_worker: str | None = None
    rate_limit_result: ModelRouteRateLimitResult | None = None
    queued_at: datetime = field(default_factory=utc_now)
    assigned_at: datetime | None = None
    model_dispatch_enabled: bool = False
    remote_model_call_performed: bool = False
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False

    def __post_init__(self) -> None:
        if self.status not in ("enqueued", "assigned"):
            raise ValueError("worker job envelope status is unsupported")
        validate_public_identifier("reason_code", self.reason_code)
        if self.assigned_worker is not None:
            validate_public_identifier("assigned_worker", self.assigned_worker)
        if self.status == "assigned" and self.assigned_worker is None:
            raise ValueError("assigned worker job requires assigned_worker")
        if self.status == "enqueued" and self.assigned_at is not None:
            raise ValueError("enqueued worker job must not have assigned_at")
        validate_aware_timestamp("queued_at", self.queued_at)
        if self.assigned_at is not None:
            validate_aware_timestamp("assigned_at", self.assigned_at)
        _validate_closed_gates(
            model_dispatch_enabled=self.model_dispatch_enabled,
            remote_model_call_performed=self.remote_model_call_performed,
            public_service_enabled=False,
            live_reward_enabled=self.live_reward_enabled,
            payout_executor_enabled=self.payout_executor_enabled,
        )

    def to_public_dict(self) -> dict[str, object]:
        return {
            "contract_version": WORKER_BRIDGE_CONTRACT_VERSION,
            "job": self.job.to_public_dict(),
            "status": self.status,
            "reason_code": self.reason_code,
            "route_decision": self.route_decision.to_public_dict(),
            "assigned_worker": self.assigned_worker,
            "rate_limit_result": _rate_limit_dict(self.rate_limit_result),
            "queued_at": self.queued_at.isoformat(),
            "assigned_at": self.assigned_at.isoformat()
            if self.assigned_at is not None
            else None,
            "model_dispatch_enabled": False,
            "remote_model_call_performed": False,
            "live_reward_enabled": False,
            "payout_executor_enabled": False,
        }


@dataclass(frozen=True, slots=True)
class InferenceJobResultDTO:
    job_id: str
    status: WorkerResultStatus
    reason_code: str
    model_tier: ApiChatModelClass
    lane: WorkerLane
    prompt_hash: str
    assigned_worker: str | None
    max_input_tokens: int
    max_output_tokens: int
    timeout_ms: int
    input_tokens: int = 0
    output_tokens: int = 0
    output_hash: str | None = None
    error_code: str | None = None
    rate_limit_result: ModelRouteRateLimitResult | None = None
    completed_at: datetime = field(default_factory=utc_now)
    model_dispatch_enabled: bool = False
    remote_model_call_performed: bool = False
    raw_response_persisted: bool = False
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False

    def __post_init__(self) -> None:
        validate_public_identifier("job_id", self.job_id)
        if self.status not in ("completed", "failed", "fallback"):
            raise ValueError("worker result status is unsupported")
        validate_public_identifier("reason_code", self.reason_code)
        canonical = canonical_model_class(self.model_tier)
        if canonical not in MODEL_PROFILES:
            raise ValueError("worker result model_tier is unsupported")
        if self.lane not in VALID_WORKER_LANES:
            raise ValueError("worker result lane is unsupported")
        if MODEL_PROFILES[canonical].family != self.lane:
            raise ValueError("worker result lane does not match model_tier family")
        validate_sha256(self.prompt_hash, field_name="prompt_hash")
        if self.assigned_worker is not None:
            validate_public_identifier("assigned_worker", self.assigned_worker)
        for field_name, value in (
            ("max_input_tokens", self.max_input_tokens),
            ("max_output_tokens", self.max_output_tokens),
            ("timeout_ms", self.timeout_ms),
        ):
            if value <= 0:
                raise ValueError(f"{field_name} must be positive")
        if self.input_tokens < 0 or self.output_tokens < 0:
            raise ValueError("worker result token counts must be non-negative")
        if self.output_hash is not None:
            validate_sha256(self.output_hash, field_name="output_hash")
        if self.error_code is not None:
            validate_public_identifier("error_code", self.error_code)
        if self.status == "completed":
            if self.assigned_worker is None:
                raise ValueError("completed worker result requires assigned_worker")
            if self.output_hash is None:
                raise ValueError("completed worker result requires output_hash")
        validate_aware_timestamp("completed_at", self.completed_at)
        _validate_closed_gates(
            model_dispatch_enabled=self.model_dispatch_enabled,
            remote_model_call_performed=self.remote_model_call_performed,
            public_service_enabled=False,
            live_reward_enabled=self.live_reward_enabled,
            payout_executor_enabled=self.payout_executor_enabled,
        )
        if self.raw_response_persisted:
            raise ValueError("api_chat_worker_raw_response_persistence_forbidden")
        object.__setattr__(self, "model_tier", canonical)

    def to_public_dict(self) -> dict[str, object]:
        return {
            "contract_version": WORKER_BRIDGE_CONTRACT_VERSION,
            "job_id": self.job_id,
            "status": self.status,
            "reason_code": self.reason_code,
            "model_tier": self.model_tier,
            "lane": self.lane,
            "prompt_hash": self.prompt_hash,
            "assigned_worker": self.assigned_worker,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "timeout_ms": self.timeout_ms,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "output_hash": self.output_hash,
            "error_code": self.error_code,
            "rate_limit_result": _rate_limit_dict(self.rate_limit_result),
            "completed_at": self.completed_at.isoformat(),
            "model_dispatch_enabled": False,
            "remote_model_call_performed": False,
            "raw_response_persisted": False,
            "live_reward_enabled": False,
            "payout_executor_enabled": False,
        }


@dataclass(frozen=True, slots=True)
class ApiChatWorkerBridgeDispatcher:
    enabled: bool = False
    kill_switch_unavailable: bool = True
    workers: tuple[WorkerHeartbeatDTO, ...] = ()
    queued_jobs: int = 0
    queue_capacity: int = 128
    model_dispatch_enabled: bool = False
    remote_model_call_enabled: bool = False
    public_service_enabled: bool = False
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False

    def __post_init__(self) -> None:
        if self.queued_jobs < 0:
            raise ValueError("queued_jobs must be non-negative")
        if self.queue_capacity <= 0:
            raise ValueError("queue_capacity must be positive")
        if len({worker.device_id for worker in self.workers}) != len(self.workers):
            raise ValueError("worker bridge contains duplicate device_id")
        if len({worker.passport_id for worker in self.workers}) != len(self.workers):
            raise ValueError("worker bridge contains duplicate passport_id")
        if self.remote_model_call_enabled:
            raise ValueError(REASON_WORKER_BRIDGE_REMOTE_CALL_FORBIDDEN)
        _validate_closed_gates(
            model_dispatch_enabled=self.model_dispatch_enabled,
            remote_model_call_performed=False,
            public_service_enabled=self.public_service_enabled,
            live_reward_enabled=self.live_reward_enabled,
            payout_executor_enabled=self.payout_executor_enabled,
        )

    def register_worker(
        self,
        heartbeat: WorkerHeartbeatDTO,
    ) -> ApiChatWorkerBridgeDispatcher:
        workers = tuple(
            worker for worker in self.workers if worker.device_id != heartbeat.device_id
        )
        return replace(self, workers=(*workers, heartbeat))

    def summary(self) -> dict[str, object]:
        healthy_workers = tuple(worker for worker in self.workers if worker.healthy_for_assignment)
        return {
            "contract_version": WORKER_BRIDGE_CONTRACT_VERSION,
            "enabled": self.enabled,
            "kill_switch_unavailable": self.kill_switch_unavailable,
            "worker_count": len(self.workers),
            "healthy_worker_count": len(healthy_workers),
            "queue_capacity": self.queue_capacity,
            "queued_jobs": self.queued_jobs,
            "model_dispatch_enabled": False,
            "remote_model_call_enabled": False,
            "public_service_enabled": False,
            "live_reward_enabled": False,
            "payout_executor_enabled": False,
        }

    def route(
        self,
        job: InferenceJobRequestDTO,
        *,
        rate_limit_result: ModelRouteRateLimitResult | None = None,
    ) -> WorkerBridgeRouteResult:
        if not self.enabled:
            return self._rejected_route(
                job,
                reason_code=REASON_WORKER_BRIDGE_DISABLED,
                rate_limit_result=rate_limit_result,
            )
        if self.kill_switch_unavailable:
            return self._rejected_route(
                job,
                reason_code=REASON_WORKER_BRIDGE_KILL_SWITCH,
                rate_limit_result=rate_limit_result,
            )

        healthy_workers = tuple(worker for worker in self.workers if worker.healthy_for_assignment)
        if not healthy_workers:
            return self._rejected_route(
                job,
                reason_code=REASON_WORKER_BRIDGE_UNAVAILABLE,
                rate_limit_result=rate_limit_result,
            )

        route_decision = ApiChatModelRouteScheduler(
            enabled=True,
            devices=tuple(worker.to_device_capacity() for worker in healthy_workers),
            queued_requests=self.queued_jobs,
            queue_capacity=self.queue_capacity,
        ).route(
            job.to_model_route_request(
                identity_kind=_identity_kind_from_rate_limit(rate_limit_result)
            )
        )
        if route_decision.status == "rejected":
            return self._rejected_route(
                job,
                reason_code=route_decision.reason_code,
                route_decision=route_decision,
                rate_limit_result=rate_limit_result,
            )

        return WorkerBridgeRouteResult(
            status="routed",
            reason_code=REASON_WORKER_BRIDGE_ROUTED,
            job=job,
            route_decision=route_decision,
            selected_worker=route_decision.selected_device_id,
            rate_limit_result=rate_limit_result,
            fail_closed=False,
        )

    def enqueue(self, route_result: WorkerBridgeRouteResult) -> InferenceJobEnvelopeDTO:
        if route_result.status != "routed" or route_result.route_decision is None:
            raise ValueError(REASON_WORKER_ENQUEUE_REQUIRES_ROUTED_JOB)
        return InferenceJobEnvelopeDTO(
            job=route_result.job,
            status="enqueued",
            reason_code=REASON_WORKER_JOB_ENQUEUED,
            route_decision=route_result.route_decision,
            rate_limit_result=route_result.rate_limit_result,
        )

    def dispatch(
        self,
        queue: DurableWorkerQueue,
        route_result: WorkerBridgeRouteResult,
        transport: InternalWorkerTransportDTO,
        *,
        now: datetime | None = None,
    ) -> WorkerQueueOperationDTO:
        return queue.dispatch(transport, self.enqueue(route_result), now=now)

    def lease(
        self,
        queue: DurableWorkerQueue,
        transport: InternalWorkerTransportDTO,
        *,
        now: datetime | None = None,
    ) -> WorkerQueueOperationDTO:
        return queue.lease(transport, now=now)

    def complete_lease(
        self,
        queue: DurableWorkerQueue,
        transport: InternalWorkerTransportDTO,
        lease: WorkerQueueLeaseDTO,
        result: InferenceJobResultDTO,
        *,
        now: datetime | None = None,
    ) -> WorkerQueueOperationDTO:
        return queue.ack(transport, lease, result, now=now)

    def retry_lease(
        self,
        queue: DurableWorkerQueue,
        transport: InternalWorkerTransportDTO,
        lease: WorkerQueueLeaseDTO,
        *,
        reason_code: str,
        now: datetime | None = None,
    ) -> WorkerQueueOperationDTO:
        return queue.nack(
            transport,
            lease,
            reason_code=reason_code,
            retry=True,
            now=now,
        )

    def fail_lease(
        self,
        queue: DurableWorkerQueue,
        transport: InternalWorkerTransportDTO,
        lease: WorkerQueueLeaseDTO,
        *,
        reason_code: str,
        now: datetime | None = None,
    ) -> WorkerQueueOperationDTO:
        return queue.nack(
            transport,
            lease,
            reason_code=reason_code,
            retry=False,
            now=now,
        )

    def renew_lease(
        self,
        queue: DurableWorkerQueue,
        transport: InternalWorkerTransportDTO,
        lease: WorkerQueueLeaseDTO,
        *,
        now: datetime | None = None,
    ) -> WorkerQueueOperationDTO:
        return queue.renew(transport, lease, now=now)

    def assign(self, envelope: InferenceJobEnvelopeDTO) -> InferenceJobEnvelopeDTO:
        if envelope.status != "enqueued":
            raise ValueError(REASON_WORKER_ASSIGN_REQUIRES_ENQUEUED_JOB)
        if envelope.route_decision.status != "admitted":
            raise ValueError(REASON_WORKER_ASSIGN_REQUIRES_ADMITTED_ROUTE)
        if envelope.route_decision.selected_device_id is None:
            raise ValueError(REASON_WORKER_ASSIGN_REQUIRES_ADMITTED_ROUTE)
        return replace(
            envelope,
            status="assigned",
            reason_code=REASON_WORKER_JOB_ASSIGNED,
            assigned_worker=envelope.route_decision.selected_device_id,
            assigned_at=utc_now(),
        )

    def complete(
        self,
        envelope: InferenceJobEnvelopeDTO,
        *,
        output_hash: str,
        input_tokens: int,
        output_tokens: int,
    ) -> InferenceJobResultDTO:
        if envelope.status != "assigned":
            raise ValueError(REASON_WORKER_RESULT_REQUIRES_ASSIGNED_JOB)
        if input_tokens > envelope.job.max_input_tokens:
            raise ValueError("input_tokens exceeds job max_input_tokens")
        if output_tokens > envelope.job.max_output_tokens:
            raise ValueError("output_tokens exceeds job max_output_tokens")
        return InferenceJobResultDTO(
            job_id=envelope.job.job_id,
            status="completed",
            reason_code=REASON_WORKER_JOB_COMPLETED,
            model_tier=envelope.job.model_tier,
            lane=envelope.job.lane,
            prompt_hash=envelope.job.prompt_hash,
            assigned_worker=envelope.assigned_worker,
            max_input_tokens=envelope.job.max_input_tokens,
            max_output_tokens=envelope.job.max_output_tokens,
            timeout_ms=envelope.job.timeout_ms,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            output_hash=output_hash,
            rate_limit_result=envelope.rate_limit_result,
        )

    def fail(
        self,
        envelope: InferenceJobEnvelopeDTO,
        *,
        error_code: str,
    ) -> InferenceJobResultDTO:
        if envelope.status != "assigned":
            raise ValueError(REASON_WORKER_RESULT_REQUIRES_ASSIGNED_JOB)
        return InferenceJobResultDTO(
            job_id=envelope.job.job_id,
            status="failed",
            reason_code=REASON_WORKER_JOB_FAILED,
            model_tier=envelope.job.model_tier,
            lane=envelope.job.lane,
            prompt_hash=envelope.job.prompt_hash,
            assigned_worker=envelope.assigned_worker,
            max_input_tokens=envelope.job.max_input_tokens,
            max_output_tokens=envelope.job.max_output_tokens,
            timeout_ms=envelope.job.timeout_ms,
            error_code=error_code,
            rate_limit_result=envelope.rate_limit_result,
        )

    def fallback_result(
        self,
        source: (
            InferenceJobRequestDTO
            | WorkerBridgeRouteResult
            | InferenceJobEnvelopeDTO
            | InferenceJobResultDTO
        ),
        *,
        reason_code: str = REASON_WORKER_JOB_FALLBACK,
    ) -> InferenceJobResultDTO:
        job = _job_from_source(source)
        return InferenceJobResultDTO(
            job_id=job.job_id,
            status="fallback",
            reason_code=reason_code,
            model_tier=job.model_tier,
            lane=job.lane,
            prompt_hash=job.prompt_hash,
            assigned_worker=_assigned_worker_from_source(source),
            max_input_tokens=job.max_input_tokens,
            max_output_tokens=job.max_output_tokens,
            timeout_ms=job.timeout_ms,
            error_code=reason_code,
            rate_limit_result=_rate_limit_from_source(source),
        )

    def _rejected_route(
        self,
        job: InferenceJobRequestDTO,
        *,
        reason_code: str,
        route_decision: ModelRouteDecision | None = None,
        rate_limit_result: ModelRouteRateLimitResult | None = None,
    ) -> WorkerBridgeRouteResult:
        return WorkerBridgeRouteResult(
            status="rejected",
            reason_code=reason_code,
            job=job,
            route_decision=route_decision,
            rate_limit_result=rate_limit_result,
            fail_closed=True,
        )


def _platform_for_runtime(runtime: WorkerRuntime) -> Literal["mac", "cuda", "cpu"]:
    if runtime == "mlx":
        return "mac"
    if runtime == "cuda":
        return "cuda"
    return "cpu"


def _gateway_mode_for_job(job: InferenceJobRequestDTO) -> GatewayMode:
    if job.lane == "roleplay":
        return "RP Pro" if job.model_tier == "rp_pro_27b" else "RP Lite"
    if job.model_tier == "alice_lite_4b":
        return "Fast"
    if job.model_tier == "alice_standard_9b":
        return "Standard"
    return "Best"


def _identity_kind_from_rate_limit(
    rate_limit_result: ModelRouteRateLimitResult | None,
) -> Literal["anonymous", "api_key", "user_id"]:
    if rate_limit_result is None:
        return "anonymous"
    if rate_limit_result.identity_kind in ("anonymous", "api_key", "user_id"):
        return rate_limit_result.identity_kind
    return "anonymous"


def _job_from_source(
    source: (
        InferenceJobRequestDTO
        | WorkerBridgeRouteResult
        | InferenceJobEnvelopeDTO
        | InferenceJobResultDTO
    ),
) -> InferenceJobRequestDTO:
    if isinstance(source, InferenceJobRequestDTO):
        return source
    if isinstance(source, WorkerBridgeRouteResult | InferenceJobEnvelopeDTO):
        return source.job
    return InferenceJobRequestDTO(
        job_id=source.job_id,
        model_tier=source.model_tier,
        lane=source.lane,
        prompt_hash=source.prompt_hash,
        max_input_tokens=source.max_input_tokens,
        max_output_tokens=source.max_output_tokens,
        timeout_ms=source.timeout_ms,
        requested_at=source.completed_at,
    )


def _assigned_worker_from_source(
    source: (
        InferenceJobRequestDTO
        | WorkerBridgeRouteResult
        | InferenceJobEnvelopeDTO
        | InferenceJobResultDTO
    ),
) -> str | None:
    if isinstance(source, InferenceJobRequestDTO):
        return None
    if isinstance(source, WorkerBridgeRouteResult):
        return source.selected_worker
    return source.assigned_worker


def _rate_limit_from_source(
    source: (
        InferenceJobRequestDTO
        | WorkerBridgeRouteResult
        | InferenceJobEnvelopeDTO
        | InferenceJobResultDTO
    ),
) -> ModelRouteRateLimitResult | None:
    if isinstance(source, InferenceJobRequestDTO):
        return None
    return source.rate_limit_result


def _route_decision_dict(decision: ModelRouteDecision | None) -> dict[str, object] | None:
    if decision is None:
        return None
    return decision.to_public_dict()


def _rate_limit_dict(
    rate_limit_result: ModelRouteRateLimitResult | None,
) -> dict[str, object] | None:
    if rate_limit_result is None:
        return None
    return rate_limit_result.to_public_dict()


def _validate_closed_gates(
    *,
    model_dispatch_enabled: bool,
    remote_model_call_performed: bool,
    public_service_enabled: bool,
    live_reward_enabled: bool,
    payout_executor_enabled: bool,
) -> None:
    if model_dispatch_enabled or remote_model_call_performed:
        raise ValueError(REASON_WORKER_BRIDGE_REMOTE_CALL_FORBIDDEN)
    if public_service_enabled:
        raise ValueError("api_chat_worker_public_service_forbidden")
    if live_reward_enabled:
        raise ValueError("api_chat_worker_live_reward_forbidden")
    if payout_executor_enabled:
        raise ValueError("api_chat_worker_payout_executor_forbidden")

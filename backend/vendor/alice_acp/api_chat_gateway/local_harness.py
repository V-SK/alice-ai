from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

from alice_acp.api_chat.contracts import stable_hash
from alice_acp.api_chat.types import utc_now, validate_public_identifier
from alice_acp.api_chat.validators import validate_aware_timestamp, validate_sha256
from alice_acp.api_chat_gateway.worker_bridge import (
    ApiChatWorkerBridgeDispatcher,
    InferenceJobResultDTO,
    WorkerBridgeRouteResult,
    WorkerHeartbeatDTO,
)
from alice_acp.api_chat_gateway.worker_queue import (
    REASON_WORKER_QUEUE_KILL_SWITCH,
    REASON_WORKER_QUEUE_ROUTE_NOT_ADMITTED,
    WORKER_QUEUE_CONTRACT_VERSION,
    InMemoryWorkerDurableQueue,
    WorkerDurableQueueConfig,
    WorkerQueueLeaseDTO,
    WorkerQueueOperationDTO,
    WorkerQueueRecordDTO,
    WorkerQueueRecordStatus,
)
from alice_acp.api_chat_gateway.worker_transport import (
    WORKER_TRANSPORT_CONTRACT_VERSION,
    InternalWorkerTransportDTO,
    WorkerAuthHandleDTO,
)

WORKER_TRANSPORT_HARNESS_CONTRACT_VERSION = (
    "api-chat-worker-transport-harness-contract-v1"
)

REASON_WORKER_TRANSPORT_HARNESS_DISABLED = (
    "api_chat_worker_transport_harness_default_off"
)
REASON_WORKER_TRANSPORT_HARNESS_GATEWAY_DISABLED = (
    "api_chat_worker_transport_harness_gateway_disabled"
)
REASON_WORKER_TRANSPORT_HARNESS_FORBIDDEN = (
    "api_chat_worker_transport_harness_public_or_network_forbidden"
)
REASON_WORKER_TRANSPORT_HARNESS_INTERNAL_ONLY_REQUIRED = (
    "api_chat_worker_transport_harness_internal_staging_only_required"
)


@dataclass(frozen=True, slots=True)
class WorkerTransportHarnessConfig:
    staging_internal_only: bool
    enabled: bool = False
    kill_switch_unavailable: bool = True
    storage_available: bool = True
    queue_capacity: int = 128
    lease_ttl: timedelta = timedelta(seconds=30)
    max_retries: int = 2
    network_transport_enabled: bool = False
    public_service_enabled: bool = False
    production_api_claim: bool = False
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False

    def __post_init__(self) -> None:
        if not self.staging_internal_only:
            raise ValueError(REASON_WORKER_TRANSPORT_HARNESS_INTERNAL_ONLY_REQUIRED)
        if (
            self.network_transport_enabled
            or self.public_service_enabled
            or self.production_api_claim
            or self.live_reward_enabled
            or self.payout_executor_enabled
        ):
            raise ValueError(REASON_WORKER_TRANSPORT_HARNESS_FORBIDDEN)
        WorkerDurableQueueConfig(
            enabled=self.enabled,
            kill_switch_unavailable=self.kill_switch_unavailable,
            storage_available=self.storage_available,
            queue_capacity=self.queue_capacity,
            lease_ttl=self.lease_ttl,
            max_retries=self.max_retries,
        )

    def to_queue_config(self) -> WorkerDurableQueueConfig:
        return WorkerDurableQueueConfig(
            enabled=self.enabled,
            kill_switch_unavailable=self.kill_switch_unavailable,
            storage_available=self.storage_available,
            queue_capacity=self.queue_capacity,
            lease_ttl=self.lease_ttl,
            max_retries=self.max_retries,
        )

    @classmethod
    def colocated_staging(
        cls,
        *,
        enabled: bool = True,
        queue_capacity: int = 128,
        lease_ttl: timedelta = timedelta(seconds=30),
        max_retries: int = 2,
    ) -> WorkerTransportHarnessConfig:
        """Explicit opt-in config for the CO-LOCATED staging credit loop.

        When ``enabled`` is True this turns on the internal staging queue
        (``enabled=True``, ``kill_switch_unavailable=False``) WITHOUT touching
        any forbidden flag: ``staging_internal_only`` stays True and the
        ``network_transport_enabled`` / ``public_service_enabled`` /
        ``production_api_claim`` / ``live_reward_enabled`` /
        ``payout_executor_enabled`` gates remain False (their validators still
        raise if anyone flips them). Defaults remain safe: pass ``enabled=False``
        for a fail-closed harness.
        """
        return cls(
            staging_internal_only=True,
            enabled=enabled,
            kill_switch_unavailable=not enabled,
            queue_capacity=queue_capacity,
            lease_ttl=lease_ttl,
            max_retries=max_retries,
        )


@dataclass(frozen=True, slots=True)
class WorkerTransportHarnessRecordSnapshotDTO:
    record_id: str
    job_id: str
    model_tier: str
    lane: str
    prompt_hash: str
    status: WorkerQueueRecordStatus
    assigned_worker: str
    retry_count: int
    max_retries: int
    stale_lease_count: int
    queued_at: datetime
    updated_at: datetime
    lease_expires_at: datetime | None = None

    def __post_init__(self) -> None:
        validate_public_identifier("record_id", self.record_id)
        validate_public_identifier("job_id", self.job_id)
        validate_public_identifier("model_tier", self.model_tier)
        validate_public_identifier("lane", self.lane)
        validate_sha256(self.prompt_hash, field_name="prompt_hash")
        validate_public_identifier("assigned_worker", self.assigned_worker)
        if self.retry_count < 0:
            raise ValueError("retry_count must be non-negative")
        if self.max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if self.stale_lease_count < 0:
            raise ValueError("stale_lease_count must be non-negative")
        validate_aware_timestamp("queued_at", self.queued_at)
        validate_aware_timestamp("updated_at", self.updated_at)
        if self.lease_expires_at is not None:
            validate_aware_timestamp("lease_expires_at", self.lease_expires_at)

    @classmethod
    def from_record(
        cls,
        record: WorkerQueueRecordDTO,
    ) -> WorkerTransportHarnessRecordSnapshotDTO:
        return cls(
            record_id=record.record_id,
            job_id=record.job.job_id,
            model_tier=record.job.model_tier,
            lane=record.job.lane,
            prompt_hash=record.job.prompt_hash,
            status=record.status,
            assigned_worker=record.assigned_worker,
            retry_count=record.retry_count,
            max_retries=record.max_retries,
            stale_lease_count=record.stale_lease_count,
            queued_at=record.queued_at,
            updated_at=record.updated_at,
            lease_expires_at=record.lease_expires_at,
        )

    def to_public_dict(self) -> dict[str, object]:
        return {
            "record_id": self.record_id,
            "job_id": self.job_id,
            "model_tier": self.model_tier,
            "lane": self.lane,
            "prompt_hash": self.prompt_hash,
            "status": self.status,
            "assigned_worker": self.assigned_worker,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "stale_lease_count": self.stale_lease_count,
            "queued_at": self.queued_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "lease_expires_at": self.lease_expires_at.isoformat()
            if self.lease_expires_at is not None
            else None,
        }


@dataclass(frozen=True, slots=True)
class WorkerTransportHarnessStateSnapshotDTO:
    staging_internal_only: bool
    enabled: bool
    kill_switch_unavailable: bool
    gateway_enabled: bool
    gateway_kill_switch_unavailable: bool
    storage_available: bool
    queued_count: int
    inflight_count: int
    completed_count: int
    failed_count: int
    records: tuple[WorkerTransportHarnessRecordSnapshotDTO, ...]
    observed_at: datetime = field(default_factory=utc_now)

    def __post_init__(self) -> None:
        if not self.staging_internal_only:
            raise ValueError(REASON_WORKER_TRANSPORT_HARNESS_INTERNAL_ONLY_REQUIRED)
        for field_name, value in (
            ("queued_count", self.queued_count),
            ("inflight_count", self.inflight_count),
            ("completed_count", self.completed_count),
            ("failed_count", self.failed_count),
        ):
            if value < 0:
                raise ValueError(f"{field_name} must be non-negative")
        validate_aware_timestamp("observed_at", self.observed_at)

    def to_public_dict(self) -> dict[str, object]:
        return {
            "contract_version": WORKER_TRANSPORT_HARNESS_CONTRACT_VERSION,
            "worker_transport_contract_version": WORKER_TRANSPORT_CONTRACT_VERSION,
            "worker_queue_contract_version": WORKER_QUEUE_CONTRACT_VERSION,
            "staging_internal_only": True,
            "enabled": self.enabled,
            "kill_switch_unavailable": self.kill_switch_unavailable,
            "gateway_enabled": self.gateway_enabled,
            "gateway_kill_switch_unavailable": self.gateway_kill_switch_unavailable,
            "storage_available": self.storage_available,
            "queued_count": self.queued_count,
            "inflight_count": self.inflight_count,
            "completed_count": self.completed_count,
            "failed_count": self.failed_count,
            "records": tuple(record.to_public_dict() for record in self.records),
            "observed_at": self.observed_at.isoformat(),
            "raw_prompt_persisted": False,
            "raw_token_persisted": False,
            "network_transport_enabled": False,
            "public_service_enabled": False,
            "production_api_claim": False,
            "live_reward_enabled": False,
            "payout_executor_enabled": False,
        }


@dataclass(slots=True)
class ApiChatWorkerTransportHarness:
    config: WorkerTransportHarnessConfig
    dispatcher: ApiChatWorkerBridgeDispatcher = field(
        default_factory=ApiChatWorkerBridgeDispatcher
    )
    queue: InMemoryWorkerDurableQueue | None = None

    def __post_init__(self) -> None:
        if self.queue is None:
            self.queue = InMemoryWorkerDurableQueue(self.config.to_queue_config())

    def register_worker(
        self,
        heartbeat: WorkerHeartbeatDTO,
    ) -> ApiChatWorkerTransportHarness:
        self.dispatcher = self.dispatcher.register_worker(heartbeat)
        return self

    def enqueue(
        self,
        route_result: WorkerBridgeRouteResult,
        auth_handle: WorkerAuthHandleDTO,
        *,
        now: datetime | None = None,
    ) -> WorkerQueueOperationDTO:
        guard = self._new_work_guard()
        if guard is not None:
            return guard
        if route_result.status != "routed":
            return self._rejected_operation(REASON_WORKER_QUEUE_ROUTE_NOT_ADMITTED)
        transport = self._transport(
            action="dispatch",
            auth_handle=auth_handle,
            job_id=route_result.job.job_id,
            payload_hash=stable_hash(route_result.to_public_dict()),
            now=now,
        )
        return self.dispatcher.dispatch(self._queue(), route_result, transport, now=now)

    def lease(
        self,
        auth_handle: WorkerAuthHandleDTO,
        *,
        now: datetime | None = None,
    ) -> WorkerQueueOperationDTO:
        guard = self._new_work_guard()
        if guard is not None:
            return guard
        transport = self._transport(
            action="lease",
            auth_handle=auth_handle,
            now=now,
        )
        return self.dispatcher.lease(self._queue(), transport, now=now)

    def heartbeat(
        self,
        auth_handle: WorkerAuthHandleDTO,
        lease: WorkerQueueLeaseDTO,
        *,
        now: datetime | None = None,
    ) -> WorkerQueueOperationDTO:
        transport = self._transport(
            action="heartbeat",
            auth_handle=auth_handle,
            lease_id=lease.lease_id,
            payload_hash=stable_hash(lease.to_public_dict()),
            now=now,
        )
        return self.dispatcher.renew_lease(self._queue(), transport, lease, now=now)

    def ack(
        self,
        auth_handle: WorkerAuthHandleDTO,
        lease: WorkerQueueLeaseDTO,
        result: InferenceJobResultDTO,
        *,
        now: datetime | None = None,
    ) -> WorkerQueueOperationDTO:
        transport = self._transport(
            action="complete",
            auth_handle=auth_handle,
            lease_id=lease.lease_id,
            payload_hash=stable_hash(result.to_public_dict()),
            now=now,
        )
        return self.dispatcher.complete_lease(
            self._queue(),
            transport,
            lease,
            result,
            now=now,
        )

    def nack(
        self,
        auth_handle: WorkerAuthHandleDTO,
        lease: WorkerQueueLeaseDTO,
        *,
        reason_code: str,
        retry: bool,
        now: datetime | None = None,
    ) -> WorkerQueueOperationDTO:
        validate_public_identifier("reason_code", reason_code)
        transport = self._transport(
            action="retry" if retry else "fail",
            auth_handle=auth_handle,
            lease_id=lease.lease_id,
            payload_hash=stable_hash(
                {
                    "lease_id": lease.lease_id,
                    "reason_code": reason_code,
                    "retry": retry,
                }
            ),
            now=now,
        )
        if retry:
            return self.dispatcher.retry_lease(
                self._queue(),
                transport,
                lease,
                reason_code=reason_code,
                now=now,
            )
        return self.dispatcher.fail_lease(
            self._queue(),
            transport,
            lease,
            reason_code=reason_code,
            now=now,
        )

    def reap_stale_leases(
        self,
        *,
        now: datetime | None = None,
    ) -> tuple[WorkerQueueOperationDTO, ...]:
        return self._queue().reap_stale_leases(now=now)

    def state_snapshot(
        self,
        *,
        now: datetime | None = None,
    ) -> WorkerTransportHarnessStateSnapshotDTO:
        observed_at = _observed_at(now)
        records = tuple(
            WorkerTransportHarnessRecordSnapshotDTO.from_record(record)
            for record in sorted(
                self._queue().records(),
                key=lambda record: (record.queued_at, record.record_id),
            )
        )
        return WorkerTransportHarnessStateSnapshotDTO(
            staging_internal_only=True,
            enabled=self.config.enabled,
            kill_switch_unavailable=self.config.kill_switch_unavailable,
            gateway_enabled=self.dispatcher.enabled,
            gateway_kill_switch_unavailable=self.dispatcher.kill_switch_unavailable,
            storage_available=self.config.storage_available,
            queued_count=self._queue().depth(),
            inflight_count=self._queue().inflight_depth(),
            completed_count=self._queue().completed_count(),
            failed_count=self._queue().failed_count(),
            records=records,
            observed_at=observed_at,
        )

    def health(self, *, now: datetime | None = None) -> dict[str, object]:
        snapshot = self.state_snapshot(now=now).to_public_dict()
        snapshot["ok"] = self._new_work_guard() is None and self.config.storage_available
        snapshot["reason_code"] = _health_reason(
            enabled=self.config.enabled,
            kill_switch_unavailable=self.config.kill_switch_unavailable
            or self.dispatcher.kill_switch_unavailable,
            gateway_enabled=self.dispatcher.enabled,
            storage_available=self.config.storage_available,
        )
        return snapshot

    def _transport(
        self,
        *,
        action: str,
        auth_handle: WorkerAuthHandleDTO,
        job_id: str | None = None,
        lease_id: str | None = None,
        payload_hash: str | None = None,
        now: datetime | None = None,
    ) -> InternalWorkerTransportDTO:
        observed_at = _observed_at(now)
        subject = job_id or lease_id or "queue"
        digest = stable_hash(
            {
                "action": action,
                "worker_id": auth_handle.worker_id,
                "subject": subject,
                "observed_at": observed_at,
            }
        )
        return InternalWorkerTransportDTO(
            transport_id=f"transport-{action}-{digest[:24]}",
            action=action,  # type: ignore[arg-type]
            auth_handle=auth_handle,
            job_id=job_id,
            lease_id=lease_id,
            payload_hash=payload_hash,
            created_at=observed_at,
        )

    def _new_work_guard(self) -> WorkerQueueOperationDTO | None:
        if not self.config.enabled:
            return self._rejected_operation(REASON_WORKER_TRANSPORT_HARNESS_DISABLED)
        if self.config.kill_switch_unavailable or self.dispatcher.kill_switch_unavailable:
            return self._rejected_operation(REASON_WORKER_QUEUE_KILL_SWITCH)
        if not self.dispatcher.enabled:
            return self._rejected_operation(REASON_WORKER_TRANSPORT_HARNESS_GATEWAY_DISABLED)
        return None

    def _rejected_operation(self, reason_code: str) -> WorkerQueueOperationDTO:
        return WorkerQueueOperationDTO(
            status="rejected",
            reason_code=reason_code,
            queue_depth=self._queue().depth(),
            inflight_depth=self._queue().inflight_depth(),
            fail_closed=True,
        )

    def _queue(self) -> InMemoryWorkerDurableQueue:
        if self.queue is None:
            raise RuntimeError("api_chat_worker_transport_harness_queue_missing")
        return self.queue


def _observed_at(now: datetime | None) -> datetime:
    observed_at = now or utc_now()
    validate_aware_timestamp("now", observed_at)
    return observed_at


def _health_reason(
    *,
    enabled: bool,
    kill_switch_unavailable: bool,
    gateway_enabled: bool,
    storage_available: bool,
) -> str:
    if not enabled:
        return REASON_WORKER_TRANSPORT_HARNESS_DISABLED
    if kill_switch_unavailable:
        return REASON_WORKER_QUEUE_KILL_SWITCH
    if not gateway_enabled:
        return REASON_WORKER_TRANSPORT_HARNESS_GATEWAY_DISABLED
    if not storage_available:
        return "api_chat_worker_transport_harness_storage_unavailable"
    return "api_chat_worker_transport_harness_healthy"

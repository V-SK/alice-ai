from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from typing import Literal, Protocol

from alice_acp.api_chat.contracts import stable_hash
from alice_acp.api_chat.types import utc_now, validate_public_identifier
from alice_acp.api_chat.validators import validate_aware_timestamp
from alice_acp.api_chat_gateway.model_routing import (
    ModelRouteDecision,
    ModelRouteRateLimitResult,
)
from alice_acp.api_chat_gateway.worker_bridge import (
    InferenceJobEnvelopeDTO,
    InferenceJobRequestDTO,
    InferenceJobResultDTO,
)
from alice_acp.api_chat_gateway.worker_transport import InternalWorkerTransportDTO

WORKER_QUEUE_CONTRACT_VERSION = "api-chat-worker-durable-queue-contract-v1"

REASON_WORKER_QUEUE_DISABLED = "api_chat_worker_queue_default_off"
REASON_WORKER_QUEUE_KILL_SWITCH = "api_chat_worker_queue_kill_switch_unavailable"
REASON_WORKER_QUEUE_UNAVAILABLE = "api_chat_worker_queue_unavailable"
REASON_WORKER_QUEUE_DISPATCHED = "api_chat_worker_queue_dispatched"
REASON_WORKER_QUEUE_LEASED = "api_chat_worker_queue_leased"
REASON_WORKER_QUEUE_ACKED = "api_chat_worker_queue_acked"
REASON_WORKER_QUEUE_EMPTY = "api_chat_worker_queue_empty"
REASON_WORKER_QUEUE_FULL = "api_chat_worker_queue_full"
REASON_WORKER_QUEUE_ROUTE_NOT_ADMITTED = "api_chat_worker_queue_route_not_admitted"
REASON_WORKER_QUEUE_RETRY_SCHEDULED = "api_chat_worker_queue_retry_scheduled"
REASON_WORKER_QUEUE_FAILED = "api_chat_worker_queue_failed"
REASON_WORKER_QUEUE_STALE_LEASE_REQUEUED = "api_chat_worker_queue_stale_lease_requeued"
REASON_WORKER_QUEUE_STALE_LEASE_FAILED = "api_chat_worker_queue_stale_lease_failed"
REASON_WORKER_QUEUE_LEASE_RENEWED = "api_chat_worker_queue_lease_renewed"
REASON_WORKER_QUEUE_LEASE_EXPIRED = "api_chat_worker_queue_lease_expired"
REASON_WORKER_QUEUE_DUPLICATE_JOB = "api_chat_worker_queue_duplicate_job"
REASON_WORKER_QUEUE_LEASE_NOT_FOUND = "api_chat_worker_queue_lease_not_found"
REASON_WORKER_QUEUE_ACTION_MISMATCH = "api_chat_worker_queue_action_mismatch"
REASON_WORKER_QUEUE_WORKER_MISMATCH = "api_chat_worker_queue_worker_mismatch"

WorkerQueueRecordStatus = Literal["queued", "leased", "completed", "failed"]
WorkerQueueOperationStatus = Literal["accepted", "rejected", "empty"]


class DurableWorkerQueue(Protocol):
    def dispatch(
        self,
        transport: InternalWorkerTransportDTO,
        envelope: InferenceJobEnvelopeDTO,
        *,
        now: datetime | None = None,
    ) -> WorkerQueueOperationDTO: ...

    def lease(
        self,
        transport: InternalWorkerTransportDTO,
        *,
        now: datetime | None = None,
    ) -> WorkerQueueOperationDTO: ...

    def ack(
        self,
        transport: InternalWorkerTransportDTO,
        lease: WorkerQueueLeaseDTO,
        result: InferenceJobResultDTO,
        *,
        now: datetime | None = None,
    ) -> WorkerQueueOperationDTO: ...

    def nack(
        self,
        transport: InternalWorkerTransportDTO,
        lease: WorkerQueueLeaseDTO,
        *,
        reason_code: str,
        retry: bool,
        now: datetime | None = None,
    ) -> WorkerQueueOperationDTO: ...

    def renew(
        self,
        transport: InternalWorkerTransportDTO,
        lease: WorkerQueueLeaseDTO,
        *,
        now: datetime | None = None,
    ) -> WorkerQueueOperationDTO: ...

    def reap_stale_leases(
        self,
        *,
        now: datetime | None = None,
    ) -> tuple[WorkerQueueOperationDTO, ...]: ...

    def depth(self) -> int: ...


@dataclass(frozen=True, slots=True)
class WorkerDurableQueueConfig:
    enabled: bool = False
    kill_switch_unavailable: bool = True
    storage_available: bool = True
    queue_capacity: int = 128
    lease_ttl: timedelta = timedelta(seconds=30)
    max_retries: int = 2

    def __post_init__(self) -> None:
        if self.queue_capacity <= 0:
            raise ValueError("queue_capacity must be positive")
        if self.lease_ttl.total_seconds() <= 0:
            raise ValueError("lease_ttl must be positive")
        if self.max_retries < 0:
            raise ValueError("max_retries must be non-negative")


@dataclass(frozen=True, slots=True)
class WorkerQueueLeaseDTO:
    lease_id: str
    record_id: str
    job_id: str
    worker_id: str
    leased_at: datetime
    expires_at: datetime
    retry_count: int
    stale_lease_count: int

    def __post_init__(self) -> None:
        validate_public_identifier("lease_id", self.lease_id)
        validate_public_identifier("record_id", self.record_id)
        validate_public_identifier("job_id", self.job_id)
        validate_public_identifier("worker_id", self.worker_id)
        validate_aware_timestamp("leased_at", self.leased_at)
        validate_aware_timestamp("expires_at", self.expires_at)
        if self.expires_at <= self.leased_at:
            raise ValueError("lease expires_at must be after leased_at")
        if self.retry_count < 0:
            raise ValueError("lease retry_count must be non-negative")
        if self.stale_lease_count < 0:
            raise ValueError("lease stale_lease_count must be non-negative")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "lease_id": self.lease_id,
            "record_id": self.record_id,
            "job_id": self.job_id,
            "worker_id": self.worker_id,
            "leased_at": self.leased_at.isoformat(),
            "expires_at": self.expires_at.isoformat(),
            "retry_count": self.retry_count,
            "stale_lease_count": self.stale_lease_count,
        }


@dataclass(frozen=True, slots=True)
class WorkerQueueRecordDTO:
    record_id: str
    job: InferenceJobRequestDTO
    route_decision: ModelRouteDecision
    status: WorkerQueueRecordStatus
    reason_code: str
    assigned_worker: str
    queued_at: datetime
    updated_at: datetime
    lease_id: str | None = None
    lease_owner: str | None = None
    lease_expires_at: datetime | None = None
    retry_count: int = 0
    max_retries: int = 2
    stale_lease_count: int = 0
    rate_limit_result: ModelRouteRateLimitResult | None = None
    completed_result: InferenceJobResultDTO | None = None
    model_dispatch_enabled: bool = False
    remote_model_call_performed: bool = False
    public_service_enabled: bool = False
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False

    def __post_init__(self) -> None:
        validate_public_identifier("record_id", self.record_id)
        if self.status not in ("queued", "leased", "completed", "failed"):
            raise ValueError("worker queue status is unsupported")
        validate_public_identifier("reason_code", self.reason_code)
        validate_public_identifier("assigned_worker", self.assigned_worker)
        validate_aware_timestamp("queued_at", self.queued_at)
        validate_aware_timestamp("updated_at", self.updated_at)
        if self.lease_id is not None:
            validate_public_identifier("lease_id", self.lease_id)
        if self.lease_owner is not None:
            validate_public_identifier("lease_owner", self.lease_owner)
        if self.lease_expires_at is not None:
            validate_aware_timestamp("lease_expires_at", self.lease_expires_at)
        if self.status == "leased" and (
            self.lease_id is None
            or self.lease_owner is None
            or self.lease_expires_at is None
        ):
            raise ValueError("leased queue record requires lease metadata")
        if self.status != "leased" and (
            self.lease_id is not None
            or self.lease_owner is not None
            or self.lease_expires_at is not None
        ):
            raise ValueError("non-leased queue record must not carry lease metadata")
        if self.retry_count < 0:
            raise ValueError("retry_count must be non-negative")
        if self.max_retries < 0:
            raise ValueError("max_retries must be non-negative")
        if self.stale_lease_count < 0:
            raise ValueError("stale_lease_count must be non-negative")
        if self.completed_result is not None and self.completed_result.job_id != self.job.job_id:
            raise ValueError("completed_result job_id must match queue job")
        if (
            self.model_dispatch_enabled
            or self.remote_model_call_performed
            or self.public_service_enabled
            or self.live_reward_enabled
            or self.payout_executor_enabled
        ):
            raise ValueError("api_chat_worker_queue_remote_or_public_path_forbidden")

    def to_lease(self, *, leased_at: datetime) -> WorkerQueueLeaseDTO:
        if (
            self.status != "leased"
            or self.lease_id is None
            or self.lease_owner is None
            or self.lease_expires_at is None
        ):
            raise ValueError("queue record is not leased")
        return WorkerQueueLeaseDTO(
            lease_id=self.lease_id,
            record_id=self.record_id,
            job_id=self.job.job_id,
            worker_id=self.lease_owner,
            leased_at=leased_at,
            expires_at=self.lease_expires_at,
            retry_count=self.retry_count,
            stale_lease_count=self.stale_lease_count,
        )

    def to_public_dict(self) -> dict[str, object]:
        return {
            "contract_version": WORKER_QUEUE_CONTRACT_VERSION,
            "record_id": self.record_id,
            "job": self.job.to_public_dict(),
            "route_decision": self.route_decision.to_public_dict(),
            "status": self.status,
            "reason_code": self.reason_code,
            "assigned_worker": self.assigned_worker,
            "queued_at": self.queued_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "lease_id": self.lease_id,
            "lease_owner": self.lease_owner,
            "lease_expires_at": self.lease_expires_at.isoformat()
            if self.lease_expires_at is not None
            else None,
            "retry_count": self.retry_count,
            "max_retries": self.max_retries,
            "stale_lease_count": self.stale_lease_count,
            "rate_limit_result": self.rate_limit_result.to_public_dict()
            if self.rate_limit_result is not None
            else None,
            "completed_result": self.completed_result.to_public_dict()
            if self.completed_result is not None
            else None,
            "model_dispatch_enabled": False,
            "remote_model_call_performed": False,
            "public_service_enabled": False,
            "live_reward_enabled": False,
            "payout_executor_enabled": False,
        }


@dataclass(frozen=True, slots=True)
class WorkerQueueOperationDTO:
    status: WorkerQueueOperationStatus
    reason_code: str
    queue_depth: int
    inflight_depth: int
    fail_closed: bool = False
    record: WorkerQueueRecordDTO | None = None
    lease: WorkerQueueLeaseDTO | None = None

    def __post_init__(self) -> None:
        if self.status not in ("accepted", "rejected", "empty"):
            raise ValueError("worker queue operation status is unsupported")
        validate_public_identifier("reason_code", self.reason_code)
        if self.queue_depth < 0:
            raise ValueError("queue_depth must be non-negative")
        if self.inflight_depth < 0:
            raise ValueError("inflight_depth must be non-negative")
        if self.status == "rejected" and not self.fail_closed:
            raise ValueError("rejected queue operation must fail closed")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "contract_version": WORKER_QUEUE_CONTRACT_VERSION,
            "status": self.status,
            "reason_code": self.reason_code,
            "queue_depth": self.queue_depth,
            "inflight_depth": self.inflight_depth,
            "fail_closed": self.fail_closed,
            "record": self.record.to_public_dict() if self.record is not None else None,
            "lease": self.lease.to_public_dict() if self.lease is not None else None,
            "model_dispatch_enabled": False,
            "remote_model_call_performed": False,
            "public_service_enabled": False,
            "live_reward_enabled": False,
            "payout_executor_enabled": False,
        }


@dataclass(slots=True)
class InMemoryWorkerDurableQueue:
    config: WorkerDurableQueueConfig = field(default_factory=WorkerDurableQueueConfig)
    _records: dict[str, WorkerQueueRecordDTO] = field(default_factory=dict, init=False)
    _record_ids_by_job: dict[str, str] = field(default_factory=dict, init=False)

    def summary(self) -> dict[str, object]:
        return {
            "contract_version": WORKER_QUEUE_CONTRACT_VERSION,
            "enabled": self.config.enabled,
            "kill_switch_unavailable": self.config.kill_switch_unavailable,
            "storage_available": self.config.storage_available,
            "queue_capacity": self.config.queue_capacity,
            "queue_depth": self.depth(),
            "inflight_depth": self.inflight_depth(),
            "completed_count": self.completed_count(),
            "failed_count": self.failed_count(),
            "max_retries": self.config.max_retries,
            "model_dispatch_enabled": False,
            "remote_model_call_performed": False,
            "public_service_enabled": False,
            "live_reward_enabled": False,
            "payout_executor_enabled": False,
        }

    def dispatch(
        self,
        transport: InternalWorkerTransportDTO,
        envelope: InferenceJobEnvelopeDTO,
        *,
        now: datetime | None = None,
    ) -> WorkerQueueOperationDTO:
        observed_at = _observed_at(now)
        guard = self._guard()
        if guard is not None:
            return guard
        _require_action(transport, "dispatch")
        if transport.job_id is not None and transport.job_id != envelope.job.job_id:
            raise ValueError("transport job_id must match queue envelope job_id")
        if envelope.status != "enqueued":
            raise ValueError("worker queue dispatch requires enqueued envelope")
        if (
            envelope.route_decision.status != "admitted"
            or envelope.route_decision.selected_device_id is None
        ):
            return self._operation(
                status="rejected",
                reason_code=REASON_WORKER_QUEUE_ROUTE_NOT_ADMITTED,
                fail_closed=True,
            )
        if transport.auth_handle.worker_id != envelope.route_decision.selected_device_id:
            return self._operation(
                status="rejected",
                reason_code=REASON_WORKER_QUEUE_WORKER_MISMATCH,
                fail_closed=True,
            )
        if self.depth() >= self.config.queue_capacity:
            return self._operation(
                status="rejected",
                reason_code=REASON_WORKER_QUEUE_FULL,
                fail_closed=True,
            )
        if envelope.job.job_id in self._record_ids_by_job:
            return self._operation(
                status="rejected",
                reason_code=REASON_WORKER_QUEUE_DUPLICATE_JOB,
                fail_closed=True,
            )

        record = WorkerQueueRecordDTO(
            record_id=_record_id(envelope.job),
            job=envelope.job,
            route_decision=envelope.route_decision,
            status="queued",
            reason_code=REASON_WORKER_QUEUE_DISPATCHED,
            assigned_worker=envelope.route_decision.selected_device_id,
            queued_at=observed_at,
            updated_at=observed_at,
            max_retries=self.config.max_retries,
            rate_limit_result=envelope.rate_limit_result,
        )
        self._records[record.record_id] = record
        self._record_ids_by_job[record.job.job_id] = record.record_id
        return self._operation(
            status="accepted",
            reason_code=REASON_WORKER_QUEUE_DISPATCHED,
            record=record,
        )

    def lease(
        self,
        transport: InternalWorkerTransportDTO,
        *,
        now: datetime | None = None,
    ) -> WorkerQueueOperationDTO:
        observed_at = _observed_at(now)
        guard = self._guard()
        if guard is not None:
            return guard
        _require_action(transport, "lease")
        candidate = self._next_lease_candidate(transport.auth_handle.worker_id)
        if candidate is None:
            return self._operation(
                status="empty",
                reason_code=REASON_WORKER_QUEUE_EMPTY,
            )
        lease_id = _lease_id(candidate, observed_at)
        leased = replace(
            candidate,
            status="leased",
            reason_code=REASON_WORKER_QUEUE_LEASED,
            lease_id=lease_id,
            lease_owner=transport.auth_handle.worker_id,
            lease_expires_at=observed_at + self.config.lease_ttl,
            updated_at=observed_at,
        )
        self._records[leased.record_id] = leased
        lease = leased.to_lease(leased_at=observed_at)
        return self._operation(
            status="accepted",
            reason_code=REASON_WORKER_QUEUE_LEASED,
            record=leased,
            lease=lease,
        )

    def ack(
        self,
        transport: InternalWorkerTransportDTO,
        lease: WorkerQueueLeaseDTO,
        result: InferenceJobResultDTO,
        *,
        now: datetime | None = None,
    ) -> WorkerQueueOperationDTO:
        observed_at = _observed_at(now)
        guard = self._guard()
        if guard is not None:
            return guard
        _require_action(transport, "complete")
        record = self._record_for_lease(transport, lease)
        if record is None:
            return self._operation(
                status="rejected",
                reason_code=REASON_WORKER_QUEUE_LEASE_NOT_FOUND,
                fail_closed=True,
            )
        if (
            result.job_id != record.job.job_id
            or result.status != "completed"
            or result.assigned_worker != record.assigned_worker
        ):
            raise ValueError("queue ack requires completed result for leased job")
        completed = replace(
            record,
            status="completed",
            reason_code=REASON_WORKER_QUEUE_ACKED,
            lease_id=None,
            lease_owner=None,
            lease_expires_at=None,
            completed_result=result,
            updated_at=observed_at,
        )
        self._records[completed.record_id] = completed
        return self._operation(
            status="accepted",
            reason_code=REASON_WORKER_QUEUE_ACKED,
            record=completed,
        )

    def nack(
        self,
        transport: InternalWorkerTransportDTO,
        lease: WorkerQueueLeaseDTO,
        *,
        reason_code: str,
        retry: bool,
        now: datetime | None = None,
    ) -> WorkerQueueOperationDTO:
        observed_at = _observed_at(now)
        guard = self._guard()
        if guard is not None:
            return guard
        _require_action(transport, "retry" if retry else "fail")
        validate_public_identifier("reason_code", reason_code)
        record = self._record_for_lease(transport, lease)
        if record is None:
            return self._operation(
                status="rejected",
                reason_code=REASON_WORKER_QUEUE_LEASE_NOT_FOUND,
                fail_closed=True,
            )
        retry_count = record.retry_count + 1
        if retry and retry_count <= record.max_retries:
            retried = replace(
                record,
                status="queued",
                reason_code=REASON_WORKER_QUEUE_RETRY_SCHEDULED,
                lease_id=None,
                lease_owner=None,
                lease_expires_at=None,
                retry_count=retry_count,
                updated_at=observed_at,
            )
            self._records[retried.record_id] = retried
            return self._operation(
                status="accepted",
                reason_code=REASON_WORKER_QUEUE_RETRY_SCHEDULED,
                record=retried,
            )

        failed = replace(
            record,
            status="failed",
            reason_code=reason_code or REASON_WORKER_QUEUE_FAILED,
            lease_id=None,
            lease_owner=None,
            lease_expires_at=None,
            retry_count=retry_count,
            updated_at=observed_at,
        )
        self._records[failed.record_id] = failed
        return self._operation(
            status="accepted",
            reason_code=REASON_WORKER_QUEUE_FAILED,
            record=failed,
        )

    def renew(
        self,
        transport: InternalWorkerTransportDTO,
        lease: WorkerQueueLeaseDTO,
        *,
        now: datetime | None = None,
    ) -> WorkerQueueOperationDTO:
        observed_at = _observed_at(now)
        guard = self._guard()
        if guard is not None:
            return guard
        _require_action(transport, "heartbeat")
        record = self._record_for_lease(transport, lease)
        if record is None:
            return self._operation(
                status="rejected",
                reason_code=REASON_WORKER_QUEUE_LEASE_NOT_FOUND,
                fail_closed=True,
            )
        if record.lease_expires_at is None or record.lease_expires_at <= observed_at:
            return self._operation(
                status="rejected",
                reason_code=REASON_WORKER_QUEUE_LEASE_EXPIRED,
                fail_closed=True,
                record=record,
            )

        renewed = replace(
            record,
            reason_code=REASON_WORKER_QUEUE_LEASE_RENEWED,
            lease_expires_at=observed_at + self.config.lease_ttl,
            updated_at=observed_at,
        )
        self._records[renewed.record_id] = renewed
        renewed_lease = renewed.to_lease(leased_at=observed_at)
        return self._operation(
            status="accepted",
            reason_code=REASON_WORKER_QUEUE_LEASE_RENEWED,
            record=renewed,
            lease=renewed_lease,
        )

    def reap_stale_leases(
        self,
        *,
        now: datetime | None = None,
    ) -> tuple[WorkerQueueOperationDTO, ...]:
        observed_at = _observed_at(now)
        guard = self._guard()
        if guard is not None:
            return (guard,)
        operations: list[WorkerQueueOperationDTO] = []
        for record in tuple(self._records.values()):
            if (
                record.status != "leased"
                or record.lease_expires_at is None
                or record.lease_expires_at > observed_at
            ):
                continue
            retry_count = record.retry_count + 1
            stale_count = record.stale_lease_count + 1
            if retry_count <= record.max_retries:
                requeued = replace(
                    record,
                    status="queued",
                    reason_code=REASON_WORKER_QUEUE_STALE_LEASE_REQUEUED,
                    lease_id=None,
                    lease_owner=None,
                    lease_expires_at=None,
                    retry_count=retry_count,
                    stale_lease_count=stale_count,
                    updated_at=observed_at,
                )
                self._records[requeued.record_id] = requeued
                operations.append(
                    self._operation(
                        status="accepted",
                        reason_code=REASON_WORKER_QUEUE_STALE_LEASE_REQUEUED,
                        record=requeued,
                    )
                )
            else:
                failed = replace(
                    record,
                    status="failed",
                    reason_code=REASON_WORKER_QUEUE_STALE_LEASE_FAILED,
                    lease_id=None,
                    lease_owner=None,
                    lease_expires_at=None,
                    retry_count=retry_count,
                    stale_lease_count=stale_count,
                    updated_at=observed_at,
                )
                self._records[failed.record_id] = failed
                operations.append(
                    self._operation(
                        status="accepted",
                        reason_code=REASON_WORKER_QUEUE_STALE_LEASE_FAILED,
                        record=failed,
                    )
                )
        return tuple(operations)

    def depth(self) -> int:
        return sum(1 for record in self._records.values() if record.status == "queued")

    def inflight_depth(self) -> int:
        return sum(1 for record in self._records.values() if record.status == "leased")

    def completed_count(self) -> int:
        return sum(1 for record in self._records.values() if record.status == "completed")

    def failed_count(self) -> int:
        return sum(1 for record in self._records.values() if record.status == "failed")

    def retry_count(self, job_id: str) -> int:
        validate_public_identifier("job_id", job_id)
        record_id = self._record_ids_by_job.get(job_id)
        if record_id is None:
            return 0
        return self._records[record_id].retry_count

    def records(self) -> tuple[WorkerQueueRecordDTO, ...]:
        return tuple(self._records.values())

    def _guard(self) -> WorkerQueueOperationDTO | None:
        if not self.config.enabled:
            return self._operation(
                status="rejected",
                reason_code=REASON_WORKER_QUEUE_DISABLED,
                fail_closed=True,
            )
        if self.config.kill_switch_unavailable:
            return self._operation(
                status="rejected",
                reason_code=REASON_WORKER_QUEUE_KILL_SWITCH,
                fail_closed=True,
            )
        if not self.config.storage_available:
            return self._operation(
                status="rejected",
                reason_code=REASON_WORKER_QUEUE_UNAVAILABLE,
                fail_closed=True,
            )
        return None

    def _operation(
        self,
        *,
        status: WorkerQueueOperationStatus,
        reason_code: str,
        fail_closed: bool = False,
        record: WorkerQueueRecordDTO | None = None,
        lease: WorkerQueueLeaseDTO | None = None,
    ) -> WorkerQueueOperationDTO:
        return WorkerQueueOperationDTO(
            status=status,
            reason_code=reason_code,
            queue_depth=self.depth(),
            inflight_depth=self.inflight_depth(),
            fail_closed=fail_closed,
            record=record,
            lease=lease,
        )

    def _next_lease_candidate(self, worker_id: str) -> WorkerQueueRecordDTO | None:
        validate_public_identifier("worker_id", worker_id)
        candidates = (
            record
            for record in self._records.values()
            if record.status == "queued" and record.assigned_worker == worker_id
        )
        return _first_by_queue_order(candidates)

    def _record_for_lease(
        self,
        transport: InternalWorkerTransportDTO,
        lease: WorkerQueueLeaseDTO,
    ) -> WorkerQueueRecordDTO | None:
        if transport.lease_id is not None and transport.lease_id != lease.lease_id:
            raise ValueError("transport lease_id must match queue lease")
        record = self._records.get(lease.record_id)
        if (
            record is None
            or record.status != "leased"
            or record.lease_id != lease.lease_id
            or record.lease_owner != transport.auth_handle.worker_id
        ):
            return None
        return record


def _require_action(
    transport: InternalWorkerTransportDTO,
    action: Literal["dispatch", "lease", "complete", "retry", "fail", "heartbeat"],
) -> None:
    if transport.action != action:
        raise ValueError(REASON_WORKER_QUEUE_ACTION_MISMATCH)


def _observed_at(now: datetime | None) -> datetime:
    observed_at = now or utc_now()
    validate_aware_timestamp("now", observed_at)
    return observed_at


def _record_id(job: InferenceJobRequestDTO) -> str:
    return f"workerq-{stable_hash({'job_id': job.job_id, 'prompt_hash': job.prompt_hash})[:24]}"


def _lease_id(record: WorkerQueueRecordDTO, leased_at: datetime) -> str:
    digest = stable_hash(
        {
            "record_id": record.record_id,
            "retry_count": record.retry_count,
            "stale_lease_count": record.stale_lease_count,
            "leased_at": leased_at,
        }
    )
    return f"lease-{digest[:24]}"


def _first_by_queue_order(
    records: Iterable[WorkerQueueRecordDTO],
) -> WorkerQueueRecordDTO | None:
    ordered = sorted(records, key=lambda record: (record.queued_at, record.record_id))
    if not ordered:
        return None
    return ordered[0]

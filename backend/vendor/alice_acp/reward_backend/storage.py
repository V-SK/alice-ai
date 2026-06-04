from __future__ import annotations

import json
import os
import threading
from dataclasses import asdict, dataclass, field, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Protocol

from alice_acp.reward_backend.types import (
    FoundationRevenueRecord,
    RewardStageResult,
    RewardWindow,
    StagedRewardRecord,
)

REWARD_BACKEND_EVENTS_FILE = "reward_backend_events.jsonl"
EVENT_WINDOW_REGISTERED = "reward_window_registered"
EVENT_STAGE_RESULT = "reward_stage_result"
EVENT_KILL_SWITCH_UPDATED = "reward_kill_switch_updated"


class RewardBackendStore(Protocol):
    def register_window(self, window: RewardWindow) -> None:
        ...

    def get_window(self, window_id: str) -> RewardWindow | None:
        ...

    def source_seen(self, source_id: str) -> bool:
        ...

    def canonical_hash_owner(self, canonical_proof_hash: str) -> str | None:
        ...

    def record_stage_result(
        self,
        result: RewardStageResult,
        *,
        consume_source_id: bool = True,
    ) -> None:
        ...

    def append_kill_switch_event(
        self,
        *,
        enabled: bool,
        actor: str,
        reason: str,
        recorded_at: datetime,
    ) -> None:
        ...

    def staged_records_for_window(self, window_id: str) -> tuple[StagedRewardRecord, ...]:
        ...

    def foundation_revenue_records(self) -> tuple[FoundationRevenueRecord, ...]:
        ...


@dataclass(slots=True)
class InMemoryRewardBackendStore:
    windows: dict[str, RewardWindow] = field(default_factory=dict)
    staged_records: dict[str, StagedRewardRecord] = field(default_factory=dict)
    foundation_revenue: dict[str, FoundationRevenueRecord] = field(default_factory=dict)
    events: list[dict[str, Any]] = field(default_factory=list)
    _source_ids: set[str] = field(default_factory=set, init=False, repr=False)
    _canonical_hashes: dict[str, str] = field(default_factory=dict, init=False, repr=False)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)

    def register_window(self, window: RewardWindow) -> None:
        with self._lock:
            self.windows[window.window_id] = window
            self.events.append(_event(EVENT_WINDOW_REGISTERED, window))

    def get_window(self, window_id: str) -> RewardWindow | None:
        with self._lock:
            return self.windows.get(window_id)

    def source_seen(self, source_id: str) -> bool:
        with self._lock:
            return source_id in self._source_ids

    def canonical_hash_owner(self, canonical_proof_hash: str) -> str | None:
        with self._lock:
            return self._canonical_hashes.get(canonical_proof_hash)

    def record_stage_result(
        self,
        result: RewardStageResult,
        *,
        consume_source_id: bool = True,
    ) -> None:
        with self._lock:
            self.events.append(
                _event(
                    EVENT_STAGE_RESULT,
                    result,
                    extra={"consume_source_id": consume_source_id},
                )
            )
            self._apply_stage_result(result, consume_source_id=consume_source_id)

    def append_kill_switch_event(
        self,
        *,
        enabled: bool,
        actor: str,
        reason: str,
        recorded_at: datetime,
    ) -> None:
        with self._lock:
            self.events.append(
                _event(
                    EVENT_KILL_SWITCH_UPDATED,
                    {
                        "enabled": enabled,
                        "actor": actor,
                        "reason": reason,
                        "recorded_at": recorded_at,
                    },
                )
            )

    def staged_records_for_window(self, window_id: str) -> tuple[StagedRewardRecord, ...]:
        with self._lock:
            return tuple(
                record
                for record in self.staged_records.values()
                if record.window_id == window_id
            )

    def foundation_revenue_records(self) -> tuple[FoundationRevenueRecord, ...]:
        with self._lock:
            return tuple(self.foundation_revenue.values())

    def _apply_stage_result(
        self,
        result: RewardStageResult,
        *,
        consume_source_id: bool,
    ) -> None:
        if consume_source_id:
            self._source_ids.add(result.source_id)
        if result.staged_record is not None:
            self.staged_records[result.source_id] = result.staged_record
            if result.staged_record.canonical_proof_hash is not None:
                self._canonical_hashes[result.staged_record.canonical_proof_hash] = (
                    result.source_id
                )
        if result.foundation_revenue is not None:
            self.foundation_revenue[result.source_id] = result.foundation_revenue


class JsonlRewardBackendStore:
    def __init__(self, root_or_file: str | Path) -> None:
        root_or_file = Path(root_or_file)
        self.path = (
            root_or_file
            if root_or_file.suffix == ".jsonl"
            else root_or_file / REWARD_BACKEND_EVENTS_FILE
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        self._lock = threading.RLock()
        self._windows: dict[str, RewardWindow] = {}
        self._staged_records: dict[str, StagedRewardRecord] = {}
        self._foundation_revenue: dict[str, FoundationRevenueRecord] = {}
        self._source_ids: set[str] = set()
        self._canonical_hashes: dict[str, str] = {}
        self._load()

    def register_window(self, window: RewardWindow) -> None:
        with self._lock:
            self._append(_event(EVENT_WINDOW_REGISTERED, window))
            self._windows[window.window_id] = window

    def get_window(self, window_id: str) -> RewardWindow | None:
        with self._lock:
            return self._windows.get(window_id)

    def source_seen(self, source_id: str) -> bool:
        with self._lock:
            return source_id in self._source_ids

    def canonical_hash_owner(self, canonical_proof_hash: str) -> str | None:
        with self._lock:
            return self._canonical_hashes.get(canonical_proof_hash)

    def record_stage_result(
        self,
        result: RewardStageResult,
        *,
        consume_source_id: bool = True,
    ) -> None:
        with self._lock:
            event = _event(
                EVENT_STAGE_RESULT,
                result,
                extra={"consume_source_id": consume_source_id},
            )
            self._append(event)
            self._apply_stage_result(result, consume_source_id=consume_source_id)

    def append_kill_switch_event(
        self,
        *,
        enabled: bool,
        actor: str,
        reason: str,
        recorded_at: datetime,
    ) -> None:
        with self._lock:
            self._append(
                _event(
                    EVENT_KILL_SWITCH_UPDATED,
                    {
                        "enabled": enabled,
                        "actor": actor,
                        "reason": reason,
                        "recorded_at": recorded_at,
                    },
                )
            )

    def staged_records_for_window(self, window_id: str) -> tuple[StagedRewardRecord, ...]:
        with self._lock:
            return tuple(
                record
                for record in self._staged_records.values()
                if record.window_id == window_id
            )

    def foundation_revenue_records(self) -> tuple[FoundationRevenueRecord, ...]:
        with self._lock:
            return tuple(self._foundation_revenue.values())

    def _append(self, event: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _load(self) -> None:
        with self.path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"reward_backend_store_corrupt:{self.path}:{line_number}"
                    ) from exc
                if not isinstance(event, dict):
                    raise ValueError(f"reward_backend_store_corrupt:{self.path}:{line_number}")
                self._apply_event(event, line_number=line_number)

    def _apply_event(self, event: dict[str, Any], *, line_number: int) -> None:
        event_type = event.get("event_type")
        payload = event.get("payload")
        if not isinstance(payload, dict):
            raise ValueError(f"reward_backend_store_corrupt:{self.path}:{line_number}")
        if event_type == EVENT_WINDOW_REGISTERED:
            window = _window_from_payload(payload)
            self._windows[window.window_id] = window
            return
        if event_type == EVENT_STAGE_RESULT:
            result = _stage_result_from_payload(payload)
            consume_source_id = bool(event.get("consume_source_id", True))
            self._apply_stage_result(result, consume_source_id=consume_source_id)
            return
        if event_type == EVENT_KILL_SWITCH_UPDATED:
            return
        raise ValueError(f"reward_backend_store_unknown_event:{self.path}:{line_number}")

    def _apply_stage_result(
        self,
        result: RewardStageResult,
        *,
        consume_source_id: bool,
    ) -> None:
        if consume_source_id:
            self._source_ids.add(result.source_id)
        if result.staged_record is not None:
            self._staged_records[result.source_id] = result.staged_record
            if result.staged_record.canonical_proof_hash is not None:
                self._canonical_hashes[result.staged_record.canonical_proof_hash] = (
                    result.source_id
                )
        if result.foundation_revenue is not None:
            self._foundation_revenue[result.source_id] = result.foundation_revenue


def _event(
    event_type: str,
    payload: object,
    *,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    recorded_at = datetime.now(UTC)
    event = {
        "event_type": event_type,
        "recorded_at": recorded_at.isoformat(),
        "payload": _jsonable(payload),
    }
    if extra:
        event.update(_jsonable(extra))
    return event


def _jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple | set):
        return [_jsonable(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return value


def _window_from_payload(payload: dict[str, Any]) -> RewardWindow:
    return RewardWindow(
        window_id=str(payload["window_id"]),
        starts_at=_datetime_from_payload(payload["starts_at"]),
        ends_at=_datetime_from_payload(payload["ends_at"]),
        miner_window_emission_cap=Decimal(str(payload["miner_window_emission_cap"])),
        source_ref=str(payload.get("source_ref", "q15_window_emission_contract")),
        calculation_only=bool(payload.get("calculation_only", True)),
        live_reward_enabled=bool(payload.get("live_reward_enabled", False)),
        payout_executor_enabled=bool(payload.get("payout_executor_enabled", False)),
        chain_transfer_enabled=bool(payload.get("chain_transfer_enabled", False)),
    )


def _stage_result_from_payload(payload: dict[str, Any]) -> RewardStageResult:
    staged_payload = payload.get("staged_record")
    revenue_payload = payload.get("foundation_revenue")
    return RewardStageResult(
        status=str(payload["status"]),  # type: ignore[arg-type]
        reason_code=str(payload["reason_code"]),
        source_id=str(payload["source_id"]),
        window_id=str(payload["window_id"]),
        staged_record=_staged_record_from_payload(staged_payload)
        if isinstance(staged_payload, dict)
        else None,
        foundation_revenue=_foundation_revenue_from_payload(revenue_payload)
        if isinstance(revenue_payload, dict)
        else None,
        paid_acu=Decimal(str(payload.get("paid_acu", "0"))),
        live_reward_enabled=bool(payload.get("live_reward_enabled", False)),
        payout_executor_enabled=bool(payload.get("payout_executor_enabled", False)),
        chain_transfer_enabled=bool(payload.get("chain_transfer_enabled", False)),
    )


def _staged_record_from_payload(payload: dict[str, Any]) -> StagedRewardRecord:
    canonical_hash = payload.get("canonical_proof_hash")
    return StagedRewardRecord(
        source_id=str(payload["source_id"]),
        window_id=str(payload["window_id"]),
        session_id=str(payload["session_id"]),
        proof_id=str(payload["proof_id"]),
        passport_id=str(payload["passport_id"]),
        device_id=str(payload["device_id"]),
        lane=str(payload["lane"]),  # type: ignore[arg-type]
        rewardable_score=Decimal(str(payload["rewardable_score"])),
        authority_status=str(payload["authority_status"]),  # type: ignore[arg-type]
        anti_cheat_status=str(payload["anti_cheat_status"]),  # type: ignore[arg-type]
        recorded_at=_datetime_from_payload(payload["recorded_at"]),
        canonical_proof_hash=str(canonical_hash) if canonical_hash is not None else None,
        paid_acu=Decimal(str(payload.get("paid_acu", "0"))),
        live_reward_enabled=bool(payload.get("live_reward_enabled", False)),
        payout_executor_enabled=bool(payload.get("payout_executor_enabled", False)),
        chain_transfer_enabled=bool(payload.get("chain_transfer_enabled", False)),
    )


def _foundation_revenue_from_payload(payload: dict[str, Any]) -> FoundationRevenueRecord:
    return FoundationRevenueRecord(
        source_id=str(payload["source_id"]),
        window_id=str(payload["window_id"]),
        session_id=str(payload["session_id"]),
        amount=Decimal(str(payload["amount"])),
        recorded_at=_datetime_from_payload(payload["recorded_at"]),
        reason_code=str(payload.get("reason_code", "foundation_revenue_excluded_from_rewards")),
        paid_acu=Decimal(str(payload.get("paid_acu", "0"))),
        live_reward_enabled=bool(payload.get("live_reward_enabled", False)),
        payout_executor_enabled=bool(payload.get("payout_executor_enabled", False)),
        chain_transfer_enabled=bool(payload.get("chain_transfer_enabled", False)),
    )


def _datetime_from_payload(value: object) -> datetime:
    return datetime.fromisoformat(str(value))

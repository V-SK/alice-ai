from __future__ import annotations

import hashlib
import json
import threading
from dataclasses import asdict, is_dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

AUDIT_STREAM_FILES = {
    "events": "events.jsonl",
    "sessions": "sessions.jsonl",
    "proofs": "proofs.jsonl",
    "rejections": "rejections.jsonl",
    "heartbeats": "heartbeats.jsonl",
    "settlement_windows": "settlement_windows.jsonl",
    "balances": "balances.jsonl",
    "foundation_revenue_mock": "foundation_revenue_mock.jsonl",
}


class JsonlAuditStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        for filename in AUDIT_STREAM_FILES.values():
            (self.root / filename).touch(exist_ok=True)
        self._lock = threading.Lock()

    def append(self, stream: str, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        if stream not in AUDIT_STREAM_FILES:
            raise ValueError(f"unknown audit stream: {stream}")
        payload_json = _jsonable(payload)
        payload_hash = _stable_hash(payload_json)
        record = {
            "event_id": _stable_hash(
                {
                    "payload_hash": payload_hash,
                    "recorded_at": datetime.now(UTC).isoformat(),
                    "stream": stream,
                    "event_type": event_type,
                }
            ),
            "stream": stream,
            "event_type": event_type,
            "recorded_at": datetime.now(UTC).isoformat(),
            "payload_hash": payload_hash,
            "payload": payload_json,
        }
        with self._lock:
            self._append_line(self.root / AUDIT_STREAM_FILES[stream], record)
            if stream != "events":
                self._append_line(self.root / AUDIT_STREAM_FILES["events"], record)
        return record

    def count_lines(self, stream: str) -> int:
        path = self.root / AUDIT_STREAM_FILES[stream]
        with path.open(encoding="utf-8") as handle:
            return sum(1 for _ in handle)

    @staticmethod
    def _append_line(path: Path, record: dict[str, Any]) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")


def _jsonable(value: Any) -> Any:
    if is_dataclass(value) and not isinstance(value, type):
        return _jsonable(asdict(value))
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, Path):
        return str(value)
    return value


def _stable_hash(payload: Any) -> str:
    canonical = json.dumps(_jsonable(payload), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

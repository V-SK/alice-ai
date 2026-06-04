from __future__ import annotations

import json
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from alice_acp.shadow_server.storage import AUDIT_STREAM_FILES, _jsonable, _stable_hash


class ShadowBetaAuditStore:
    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        for filename in AUDIT_STREAM_FILES.values():
            (self.root / filename).touch(exist_ok=True)
        self._lock = threading.Lock()

    def append_beta_event(
        self,
        *,
        stream: str,
        event_type: str,
        payload: dict[str, Any],
        reason: str,
        session_id: str | None = None,
        proof_id: str | None = None,
        device_id: str | None = None,
        lane: str | None = None,
        recorded_at: datetime | None = None,
    ) -> dict[str, Any]:
        if stream not in AUDIT_STREAM_FILES:
            raise ValueError(f"unknown audit stream: {stream}")
        if not reason:
            raise ValueError("missing_reason")
        payload_json = _jsonable(payload)
        timestamp = recorded_at or datetime.now(UTC)
        payload_hash = _stable_hash(payload_json)
        record = {
            "event_id": _stable_hash(
                {
                    "event_type": event_type,
                    "payload_hash": payload_hash,
                    "recorded_at": timestamp.isoformat(),
                    "stream": stream,
                }
            ),
            "stream": stream,
            "event_type": event_type,
            "recorded_at": timestamp.isoformat(),
            "payload_hash": payload_hash,
            "session_id": session_id,
            "proof_id": proof_id,
            "device_id": device_id,
            "lane": lane,
            "reason": reason,
            "payload": payload_json,
        }
        with self._lock:
            self._append_line(self.root / AUDIT_STREAM_FILES[stream], record)
            if stream != "events":
                self._append_line(self.root / AUDIT_STREAM_FILES["events"], record)
        return record

    @staticmethod
    def _append_line(path: Path, record: dict[str, Any]) -> None:
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")

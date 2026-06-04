from __future__ import annotations

import json
from datetime import UTC, datetime

from alice_acp.ops_monitor.types import OpsMonitorAuditRecord, OpsMonitorEvaluation


class InMemoryOpsMonitorAuditLog:
    """Local append-only shape for tests and dry-run rehearsals."""

    def __init__(self) -> None:
        self._records: list[OpsMonitorAuditRecord] = []

    @property
    def records(self) -> tuple[OpsMonitorAuditRecord, ...]:
        return tuple(self._records)

    def append(
        self,
        evaluation: OpsMonitorEvaluation,
        *,
        created_at: datetime | None = None,
    ) -> OpsMonitorAuditRecord:
        observed_created_at = created_at or datetime.now(UTC)
        record = OpsMonitorAuditRecord(
            sequence=len(self._records) + 1,
            created_at=observed_created_at,
            record_type="ops_monitor_evaluation",
            payload=evaluation.as_jsonable(),
            contract_version=evaluation.contract_version,
        )
        self._records.append(record)
        return record

    def to_jsonl(self) -> str:
        return "\n".join(
            json.dumps(record.as_jsonable(), sort_keys=True, separators=(",", ":"))
            for record in self._records
        )

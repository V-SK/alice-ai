"""Verified AI-demand admission store for shadow inference sessions.

Phase E (C1): the ``/inference/admit`` route used to hardcode
``admit_inference_demand=True`` (server.py), so *anyone* could obtain an
admitted AI-demand session and then earn AI credit via ``complete_inference``.

This module introduces a ``DemandAdmissionStore`` Protocol seam: an inference
session is only admitted (``ai_demand_admitted=True``) when the supplied
``demand_session_id`` is found in the store.  The default is *fail-closed*: an
unknown demand id is NOT admitted, so ``complete_inference`` rejects with
``REASON_AI_DEMAND_NOT_ADMITTED`` and records no credit.

CREDIT-ONLY: admission only gates whether AI *credit* is recorded.  No
reward/payout/chain flag is touched anywhere here.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from alice_acp.evidence.types import validate_aware_timestamp

DEMAND_ADMISSION_FILE_NAME = "demand_admission.jsonl"

DEMAND_EVENT_ADMITTED = "demand_admitted"
DEMAND_EVENT_REVOKED = "demand_revoked"

DEMAND_STATUS_ADMITTED = "admitted"
DEMAND_STATUS_REVOKED = "revoked"


@dataclass(frozen=True, slots=True)
class DemandAdmissionResult:
    admitted: bool
    demand_session_id: str | None = None


class DemandAdmissionStoreUnavailable(RuntimeError):
    """Raised by a backend that cannot answer — treated as NOT admitted."""


class DemandAdmissionStore(Protocol):
    def is_admitted_demand(
        self,
        *,
        demand_session_id: str,
        passport_id: str,
        device_id: str,
        observed_at: datetime,
    ) -> DemandAdmissionResult:
        ...


@dataclass(frozen=True, slots=True)
class RejectingDemandAdmissionStore:
    """Fail-closed production default: no demand id is ever admitted.

    Until a real verified-demand source (e.g. the API gateway's queued-job /
    paid-demand ledger) is wired in (owner input needed), every inference
    admission request resolves to NOT admitted — the route never auto-passes.
    """

    def is_admitted_demand(
        self,
        *,
        demand_session_id: str,
        passport_id: str,
        device_id: str,
        observed_at: datetime,
    ) -> DemandAdmissionResult:
        validate_aware_timestamp("observed_at", observed_at)
        return DemandAdmissionResult(False)


@dataclass(slots=True)
class FakeDemandAdmissionStore:
    """Deterministic in-memory verified-demand registry for tests/harnesses.

    A demand id is admitted only if it was explicitly registered.  When
    ``require_identity_match`` is True (default), the registered demand must
    also match the requesting ``(passport_id, device_id)`` — modelling that a
    verified demand is bound to the device that produced it.
    """

    admitted: dict[str, tuple[str, str]] = field(default_factory=dict)
    require_identity_match: bool = True

    def register_demand(
        self,
        *,
        demand_session_id: str,
        passport_id: str,
        device_id: str,
    ) -> None:
        self.admitted[demand_session_id] = (passport_id, device_id)

    def is_admitted_demand(
        self,
        *,
        demand_session_id: str,
        passport_id: str,
        device_id: str,
        observed_at: datetime,
    ) -> DemandAdmissionResult:
        validate_aware_timestamp("observed_at", observed_at)
        bound = self.admitted.get(demand_session_id)
        if bound is None:
            return DemandAdmissionResult(False)
        if self.require_identity_match and bound != (passport_id, device_id):
            return DemandAdmissionResult(False)
        return DemandAdmissionResult(True, demand_session_id=demand_session_id)


@dataclass(frozen=True, slots=True)
class DemandAdmissionRecord:
    demand_session_id: str
    passport_id: str
    device_id: str
    status: str = DEMAND_STATUS_ADMITTED


class JsonlDemandAdmissionStore:
    """Durable, file-backed :class:`DemandAdmissionStore` (Phase H_c).

    Phase E left the AI-lane demand-admission seam with only two reference
    implementations — ``FakeDemandAdmissionStore`` (in-memory, tests) and
    ``RejectingDemandAdmissionStore`` (fail-closed production default that admits
    *nothing* until a real verified-demand source is wired). H_c adds the durable
    real backend: an append-only JSONL map

        demand_session_id -> {passport_id, device_id, status}

    so a verified demand registered by the demand source (owner input: the API
    gateway's queued-job / paid-demand ledger) is resolvable across a server
    restart, and a revoked demand STAYS revoked.

    This is the durable sibling of ``FakeDemandAdmissionStore`` and mirrors
    :class:`alice_acp.shadow_server.device_registry_store.JsonlDeviceRegistry`
    exactly (append-only JSONL, fsync per append, replay-on-construction,
    lock-guarded). It implements ``is_admitted_demand`` (the read path the
    ledger's C1 gate consults) plus ``register_demand`` / ``revoke_demand`` (the
    write path the demand source drives).

    CREDIT-ONLY + fail-closed: admission only gates whether AI *credit* is
    recorded; no reward/payout/chain flag is touched. An unknown demand id is
    NOT admitted; a revoked one is NOT admitted. When ``require_identity_match``
    is True (default), an admitted demand must also match the requesting
    ``(passport_id, device_id)`` — modelling that a verified demand is bound to
    the device that produced it. A read/write OSError raises
    :class:`DemandAdmissionStoreUnavailable`, which the ledger maps to a deny —
    never a silent pass. The store path/data_dir is injected, never hardcoded.
    """

    def __init__(
        self,
        root_or_file: str | Path,
        *,
        require_identity_match: bool = True,
    ) -> None:
        root_or_file = Path(root_or_file)
        self.path = (
            root_or_file
            if root_or_file.suffix == ".jsonl"
            else root_or_file / DEMAND_ADMISSION_FILE_NAME
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        self.require_identity_match = require_identity_match
        self._lock = threading.Lock()
        self._records: dict[str, DemandAdmissionRecord] = {}
        self._load()

    def is_admitted_demand(
        self,
        *,
        demand_session_id: str,
        passport_id: str,
        device_id: str,
        observed_at: datetime,
    ) -> DemandAdmissionResult:
        validate_aware_timestamp("observed_at", observed_at)
        with self._lock:
            record = self._records.get(demand_session_id)
        if record is None or record.status != DEMAND_STATUS_ADMITTED:
            return DemandAdmissionResult(False)
        if self.require_identity_match and (record.passport_id, record.device_id) != (
            passport_id,
            device_id,
        ):
            return DemandAdmissionResult(False)
        return DemandAdmissionResult(True, demand_session_id=demand_session_id)

    def register_demand(
        self,
        *,
        demand_session_id: str,
        passport_id: str,
        device_id: str,
        observed_at: datetime,
    ) -> DemandAdmissionRecord:
        """Durably record an admitted ``demand_session_id -> (passport, device)``.

        Idempotent re-registration of the SAME ``(passport, device)`` binding is
        accepted (no new line written). A *different* identity for an existing
        demand id is REFUSED (raises ``ValueError``): a demand id is bound to the
        device that produced it, so silently rebinding it would let one device's
        verified demand be claimed by another.
        """

        validate_aware_timestamp("observed_at", observed_at)
        with self._lock:
            existing = self._records.get(demand_session_id)
            if existing is not None and existing.status == DEMAND_STATUS_ADMITTED:
                if (existing.passport_id, existing.device_id) != (passport_id, device_id):
                    raise ValueError("demand_admission_identity_rebind_forbidden")
                # Already admitted with the same binding — idempotent no-op.
                return existing
            record = DemandAdmissionRecord(
                demand_session_id=demand_session_id,
                passport_id=passport_id,
                device_id=device_id,
                status=DEMAND_STATUS_ADMITTED,
            )
            line = {
                "event_type": DEMAND_EVENT_ADMITTED,
                "demand_session_id": demand_session_id,
                "passport_id": passport_id,
                "device_id": device_id,
                "status": DEMAND_STATUS_ADMITTED,
                "observed_at": observed_at.isoformat(),
            }
            try:
                self._append_line(line)
            except OSError as exc:
                raise DemandAdmissionStoreUnavailable(
                    "demand_admission_store_unavailable"
                ) from exc
            self._records[demand_session_id] = record
            return record

    def revoke_demand(
        self,
        *,
        demand_session_id: str,
        observed_at: datetime,
    ) -> DemandAdmissionRecord | None:
        validate_aware_timestamp("observed_at", observed_at)
        with self._lock:
            existing = self._records.get(demand_session_id)
            if existing is None:
                return None
            record = DemandAdmissionRecord(
                demand_session_id=demand_session_id,
                passport_id=existing.passport_id,
                device_id=existing.device_id,
                status=DEMAND_STATUS_REVOKED,
            )
            line = {
                "event_type": DEMAND_EVENT_REVOKED,
                "demand_session_id": demand_session_id,
                "passport_id": existing.passport_id,
                "device_id": existing.device_id,
                "status": DEMAND_STATUS_REVOKED,
                "observed_at": observed_at.isoformat(),
            }
            try:
                self._append_line(line)
            except OSError as exc:
                raise DemandAdmissionStoreUnavailable(
                    "demand_admission_store_unavailable"
                ) from exc
            self._records[demand_session_id] = record
            return record

    def _append_line(self, record: dict[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    def _load(self) -> None:
        with self.path.open(encoding="utf-8") as handle:
            for line_number, raw in enumerate(handle, start=1):
                line = raw.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as exc:
                    raise ValueError(
                        f"demand_admission_store_corrupt:{self.path}:{line_number}"
                    ) from exc
                if not isinstance(record, dict):
                    raise ValueError(f"demand_admission_store_corrupt:{self.path}:{line_number}")
                self._apply_record(record)

    def _apply_record(self, record: dict[str, Any]) -> None:
        demand_session_id = record.get("demand_session_id")
        passport_id = record.get("passport_id")
        device_id = record.get("device_id")
        status = record.get("status")
        if not (
            isinstance(demand_session_id, str)
            and demand_session_id
            and isinstance(passport_id, str)
            and passport_id
            and isinstance(device_id, str)
            and device_id
            and status in {DEMAND_STATUS_ADMITTED, DEMAND_STATUS_REVOKED}
        ):
            return
        # Last write wins: a later revocation/registration line supersedes.
        self._records[demand_session_id] = DemandAdmissionRecord(
            demand_session_id=demand_session_id,
            passport_id=passport_id,
            device_id=device_id,
            status=status,
        )

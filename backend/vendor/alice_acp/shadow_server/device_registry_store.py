"""Durable DeviceRegistry backend for shadow session issuance (Phase H_a).

Phase E added the :class:`~alice_acp.shadow_server.device_pop.DeviceRegistry`
Protocol seam and two reference implementations — ``FakeDeviceRegistry``
(in-memory, tests) and ``RejectingDeviceRegistry`` (fail-closed production
default that rejects *everything* until a real backend is wired). Phase H_a
wires that real backend: a durable, append-only JSONL map

    (passport_id, device_id) -> {device_public_key_b64, status}

so an external miner that self-enrolls (``POST /device/register``) is resolvable
across a server restart, and a revoked device STAYS revoked.

This is the durable sibling of ``FakeDeviceRegistry`` and mirrors
:class:`alice_acp.shadow_server.dedup_store.JsonlProofDedupStore` exactly
(append-only JSONL, fsync per append, replay-on-construction, lock-guarded).
It implements ``resolve`` (the read path the ledger's C2 device-PoP gate
consults) plus ``register`` / ``revoke`` (the write path the miner roster drives
on enrollment / revocation).

CREDIT-ONLY + fail-closed: only an exactly-registered, *active* identity whose
recorded public key matches resolves; an unknown identity is
``REASON_DEVICE_UNREGISTERED`` and a revoked one is ``REASON_DEVICE_REVOKED``.
Nothing here sets any reward/payout/chain flag. A read/write OSError raises
:class:`~alice_acp.shadow_server.device_pop.DeviceRegistryUnavailable`, which the
PoP verifier maps to a deny — never a silent pass.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from alice_acp.evidence.types import validate_aware_timestamp
from alice_acp.shadow_server.device_pop import (
    DEVICE_STATUS_ACTIVE,
    DEVICE_STATUS_REVOKED,
    REASON_DEVICE_REVOKED,
    REASON_DEVICE_UNREGISTERED,
    DeviceRegistration,
    DeviceRegistryResult,
    DeviceRegistryUnavailable,
)

DEVICE_REGISTRY_FILE_NAME = "device_registry.jsonl"

DEVICE_REGISTRY_EVENT_REGISTERED = "device_registered"
DEVICE_REGISTRY_EVENT_REVOKED = "device_revoked"


@dataclass(frozen=True, slots=True)
class DeviceRegistrationOutcome:
    accepted: bool
    reason_code: str | None = None
    registration: DeviceRegistration | None = None
    rotated: bool = False


REASON_DEVICE_KEY_ROTATION_FORBIDDEN = "device_public_key_rotation_forbidden"


class JsonlDeviceRegistry:
    """Durable, file-backed :class:`DeviceRegistry` (the Phase H_a real backend).

    The latest event per ``(passport_id, device_id)`` wins on replay, so a
    revocation appended after a registration sticks. Fail-closed: corrupt JSONL
    raises on load; a write OSError raises ``DeviceRegistryUnavailable``.
    """

    def __init__(self, root_or_file: str | Path) -> None:
        root_or_file = Path(root_or_file)
        self.path = (
            root_or_file
            if root_or_file.suffix == ".jsonl"
            else root_or_file / DEVICE_REGISTRY_FILE_NAME
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        self._lock = threading.Lock()
        self._registrations: dict[tuple[str, str], DeviceRegistration] = {}
        self._load()

    def resolve(
        self,
        *,
        passport_id: str,
        device_id: str,
        observed_at: datetime,
    ) -> DeviceRegistryResult:
        validate_aware_timestamp("observed_at", observed_at)
        with self._lock:
            registration = self._registrations.get((passport_id, device_id))
        if registration is None:
            return DeviceRegistryResult(False, REASON_DEVICE_UNREGISTERED)
        if registration.status == DEVICE_STATUS_REVOKED:
            return DeviceRegistryResult(False, REASON_DEVICE_REVOKED, registration)
        return DeviceRegistryResult(True, registration=registration)

    def register(
        self,
        *,
        passport_id: str,
        device_id: str,
        device_public_key_b64: str,
        observed_at: datetime,
    ) -> DeviceRegistrationOutcome:
        """Durably record an active ``(passport_id, device_id) -> public_key``.

        Idempotent re-registration of the SAME public key is accepted (no new
        line written) so a miner re-enrolling is not penalised. A *different*
        public key for an existing identity is REFUSED
        (``REASON_DEVICE_KEY_ROTATION_FORBIDDEN``): silent key rotation would
        let a stolen ``(passport, device)`` pair swap in an attacker key. (Key
        rotation is deliberately a privileged, owner-input flow — out of scope
        for open self-enrollment.)
        """

        validate_aware_timestamp("observed_at", observed_at)
        key = (passport_id, device_id)
        with self._lock:
            existing = self._registrations.get(key)
            if existing is not None:
                if existing.device_public_key_b64 != device_public_key_b64:
                    return DeviceRegistrationOutcome(
                        False, REASON_DEVICE_KEY_ROTATION_FORBIDDEN, existing
                    )
                if existing.status == DEVICE_STATUS_ACTIVE:
                    # Already active with the same key — idempotent no-op.
                    return DeviceRegistrationOutcome(True, registration=existing)
            registration = DeviceRegistration(
                passport_id=passport_id,
                device_id=device_id,
                device_public_key_b64=device_public_key_b64,
                status=DEVICE_STATUS_ACTIVE,
            )
            record = {
                "event_type": DEVICE_REGISTRY_EVENT_REGISTERED,
                "passport_id": passport_id,
                "device_id": device_id,
                "device_public_key_b64": device_public_key_b64,
                "status": DEVICE_STATUS_ACTIVE,
                "observed_at": observed_at.isoformat(),
            }
            try:
                self._append_line(record)
            except OSError as exc:
                raise DeviceRegistryUnavailable("device_registry_store_unavailable") from exc
            self._registrations[key] = registration
            return DeviceRegistrationOutcome(
                True, registration=registration, rotated=existing is not None
            )

    def revoke(
        self,
        *,
        passport_id: str,
        device_id: str,
        observed_at: datetime,
    ) -> DeviceRegistrationOutcome:
        validate_aware_timestamp("observed_at", observed_at)
        key = (passport_id, device_id)
        with self._lock:
            existing = self._registrations.get(key)
            if existing is None:
                return DeviceRegistrationOutcome(False, REASON_DEVICE_UNREGISTERED)
            registration = DeviceRegistration(
                passport_id=passport_id,
                device_id=device_id,
                device_public_key_b64=existing.device_public_key_b64,
                status=DEVICE_STATUS_REVOKED,
            )
            record = {
                "event_type": DEVICE_REGISTRY_EVENT_REVOKED,
                "passport_id": passport_id,
                "device_id": device_id,
                "device_public_key_b64": existing.device_public_key_b64,
                "status": DEVICE_STATUS_REVOKED,
                "observed_at": observed_at.isoformat(),
            }
            try:
                self._append_line(record)
            except OSError as exc:
                raise DeviceRegistryUnavailable("device_registry_store_unavailable") from exc
            self._registrations[key] = registration
            return DeviceRegistrationOutcome(True, registration=registration)

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
                        f"device_registry_store_corrupt:{self.path}:{line_number}"
                    ) from exc
                if not isinstance(record, dict):
                    raise ValueError(f"device_registry_store_corrupt:{self.path}:{line_number}")
                self._apply_record(record)

    def _apply_record(self, record: dict[str, Any]) -> None:
        passport_id = record.get("passport_id")
        device_id = record.get("device_id")
        public_key = record.get("device_public_key_b64")
        status = record.get("status")
        if not (
            isinstance(passport_id, str)
            and passport_id
            and isinstance(device_id, str)
            and device_id
            and isinstance(public_key, str)
            and public_key
            and status in {DEVICE_STATUS_ACTIVE, DEVICE_STATUS_REVOKED}
        ):
            return
        # Last write wins: a later revocation/registration line supersedes.
        self._registrations[(passport_id, device_id)] = DeviceRegistration(
            passport_id=passport_id,
            device_id=device_id,
            device_public_key_b64=public_key,
            status=status,
        )

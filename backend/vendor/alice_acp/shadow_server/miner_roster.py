"""Durable miner roster — enrollment + identity + liveness + worker_name (Phase H_a).

The roster is the single durable unification point for an *external* miner's
identity on the shadow server. It composes:

  * a :class:`~alice_acp.shadow_server.device_registry_store.JsonlDeviceRegistry`
    — the authoritative ``(passport_id, device_id) -> device_public_key_b64 +
    active/revoked`` map the ledger's C2 device-PoP gate resolves against, and
  * its OWN durable JSONL of roster-local facts: ``last_seen`` liveness (bumped
    on ``/heartbeat``) and a SERVER-ASSIGNED ``worker_name`` per identity.

It does NOT duplicate the public-key map (that lives in the device registry, the
one source of truth) — it only adds the roster-local columns and orchestrates
enrollment.

Responsibilities
----------------
* :meth:`enroll` — an external miner self-enrolls ``(passport_id, device_id,
  device_public_key_b64)`` and proves key-possession via an enrollment PoP over a
  server-issued single-use nonce. On success the identity is durably registered
  in the device registry AND a stable server-assigned ``worker_name`` is minted.
* :meth:`worker_name_for` — the server-owned worker name echoed in the session
  envelope at ``/session/issue`` (for H_b pool-evidence correlation).
* :meth:`record_liveness` / :meth:`liveness_for` / :meth:`is_live` — ``last_seen``
  liveness, updated on ``/heartbeat``.
* :meth:`revoke` — revoke a device (propagates to the device registry so the C2
  gate then rejects it as ``REASON_DEVICE_REVOKED``).

ENROLLMENT POLICY (credit-only): ``enrollment_open`` defaults ``True`` for the
credit-only phase so external miners can onboard frictionlessly. See the loud
``# TIGHTEN BEFORE REWARD`` marker on :meth:`enroll` — open enrollment is
sybil-able and MUST be gated before any ``paid_acu>0`` lane goes live (Phase J).
``enrollment_open=False`` flips the roster fully fail-closed (every enrollment
rejected).

CREDIT-ONLY + fail-closed: no reward/payout/chain flag is touched anywhere here;
worker_name is an opaque correlation label, not a payout address. Durability
mirrors ``JsonlProofDedupStore``. A write OSError raises ``MinerRosterUnavailable``
so the caller denies rather than silently mutating roster state.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from alice_acp.evidence.types import validate_aware_timestamp
from alice_acp.shadow_server.device_pop import (
    REASON_ENROLLMENT_CLOSED,
    DeviceProofOfPossession,
    verify_device_enrollment_proof_of_possession,
)
from alice_acp.shadow_server.device_registry_store import JsonlDeviceRegistry

MINER_ROSTER_FILE_NAME = "miner_roster.jsonl"

ROSTER_EVENT_ENROLLED = "miner_enrolled"
ROSTER_EVENT_LIVENESS = "miner_liveness"
ROSTER_EVENT_REVOKED = "miner_revoked"

# Default liveness horizon: a miner not seen within this window is "stale".
DEFAULT_LIVENESS_TTL = timedelta(minutes=15)

ROSTER_STATUS_ACTIVE = "active"
ROSTER_STATUS_REVOKED = "revoked"


class MinerRosterUnavailable(RuntimeError):
    """Raised by the durable roster when it cannot write (fail-closed)."""


@dataclass(frozen=True, slots=True)
class MinerRosterEntry:
    passport_id: str
    device_id: str
    worker_name: str
    status: str = ROSTER_STATUS_ACTIVE
    last_seen: datetime | None = None
    enrolled_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class EnrollmentResult:
    accepted: bool
    reason_code: str | None = None
    entry: MinerRosterEntry | None = None


class JsonlMinerRoster:
    """Durable miner roster backed by a device registry + a roster-local JSONL."""

    def __init__(
        self,
        root_or_file: str | Path | None = None,
        *,
        device_registry: JsonlDeviceRegistry | None = None,
        enrollment_open: bool = True,
        liveness_ttl: timedelta = DEFAULT_LIVENESS_TTL,
    ) -> None:
        if root_or_file is None and device_registry is None:
            raise ValueError("miner_roster_requires_root_or_device_registry")
        base = Path(root_or_file) if root_or_file is not None else None
        if base is not None and base.suffix == ".jsonl":
            self.path = base
            registry_root = base.parent
        elif base is not None:
            self.path = base / MINER_ROSTER_FILE_NAME
            registry_root = base
        else:
            # device_registry supplied without a roster root: co-locate the
            # roster JSONL beside the registry file.
            assert device_registry is not None
            self.path = device_registry.path.parent / MINER_ROSTER_FILE_NAME
            registry_root = device_registry.path.parent
        self.device_registry = device_registry or JsonlDeviceRegistry(registry_root)
        self.enrollment_open = enrollment_open
        self.liveness_ttl = liveness_ttl
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        self._lock = threading.Lock()
        self._entries: dict[tuple[str, str], MinerRosterEntry] = {}
        self._load()

    # ----------------------------------------------------------------- enroll
    def enroll(
        self,
        *,
        passport_id: str,
        device_id: str,
        device_public_key_b64: str,
        pop: DeviceProofOfPossession,
        enrollment_nonce: str,
        observed_at: datetime,
    ) -> EnrollmentResult:
        # TIGHTEN BEFORE REWARD: open enrollment is sybil-able; gate before
        # paid_acu>0 (Phase J). For credit-only onboarding ``enrollment_open``
        # defaults True so external miners can register frictionlessly, but this
        # is an UNAUTHENTICATED self-serve write — anyone can mint arbitrarily
        # many (passport, device) identities. Before any paying lane is enabled
        # this MUST be gated (owner-issued enrollment token / passport-ownership
        # proof / invite). ``enrollment_open=False`` flips fully fail-closed.
        validate_aware_timestamp("observed_at", observed_at)
        if not self.enrollment_open:
            return EnrollmentResult(False, REASON_ENROLLMENT_CLOSED)
        # Key-possession proof over the server-issued enrollment nonce (the caller
        # has already verified the nonce was server-issued + single-use).
        if pop.device_public_key_b64 != device_public_key_b64:
            from alice_acp.shadow_server.device_pop import REASON_ENROLLMENT_KEY_MISMATCH

            return EnrollmentResult(False, REASON_ENROLLMENT_KEY_MISMATCH)
        rejection = verify_device_enrollment_proof_of_possession(
            pop,
            passport_id=passport_id,
            device_id=device_id,
            enrollment_nonce=enrollment_nonce,
        )
        if rejection is not None:
            return EnrollmentResult(False, rejection)
        # Durably register the identity in the device registry (the C2 source of
        # truth). Idempotent for the same key; a different key for an existing
        # identity is refused by the registry.
        registration = self.device_registry.register(
            passport_id=passport_id,
            device_id=device_id,
            device_public_key_b64=device_public_key_b64,
            observed_at=observed_at,
        )
        if not registration.accepted:
            return EnrollmentResult(False, registration.reason_code)
        with self._lock:
            key = (passport_id, device_id)
            existing = self._entries.get(key)
            worker_name = (
                existing.worker_name
                if existing is not None
                else _assigned_worker_name(passport_id, device_id)
            )
            entry = MinerRosterEntry(
                passport_id=passport_id,
                device_id=device_id,
                worker_name=worker_name,
                status=ROSTER_STATUS_ACTIVE,
                last_seen=existing.last_seen if existing is not None else None,
                enrolled_at=existing.enrolled_at if existing is not None else observed_at,
            )
            record = {
                "event_type": ROSTER_EVENT_ENROLLED,
                "passport_id": passport_id,
                "device_id": device_id,
                "worker_name": worker_name,
                "status": ROSTER_STATUS_ACTIVE,
                "observed_at": observed_at.isoformat(),
            }
            self._append_locked(record)
            self._entries[key] = entry
        return EnrollmentResult(True, entry=entry)

    # ------------------------------------------------------------ worker_name
    def worker_name_for(self, *, passport_id: str, device_id: str) -> str | None:
        with self._lock:
            entry = self._entries.get((passport_id, device_id))
        if entry is None or entry.status != ROSTER_STATUS_ACTIVE:
            return None
        return entry.worker_name

    # --------------------------------------------------------------- liveness
    def record_liveness(
        self,
        *,
        passport_id: str,
        device_id: str,
        observed_at: datetime,
    ) -> MinerRosterEntry | None:
        """Bump ``last_seen`` for an enrolled identity (called on /heartbeat).

        A heartbeat from an identity NOT on the roster is a no-op (returns
        ``None``) — liveness is only tracked for enrolled miners; it never
        implicitly enrolls.
        """

        validate_aware_timestamp("observed_at", observed_at)
        with self._lock:
            key = (passport_id, device_id)
            existing = self._entries.get(key)
            if existing is None:
                return None
            entry = MinerRosterEntry(
                passport_id=existing.passport_id,
                device_id=existing.device_id,
                worker_name=existing.worker_name,
                status=existing.status,
                last_seen=observed_at,
                enrolled_at=existing.enrolled_at,
            )
            record = {
                "event_type": ROSTER_EVENT_LIVENESS,
                "passport_id": passport_id,
                "device_id": device_id,
                "observed_at": observed_at.isoformat(),
            }
            self._append_locked(record)
            self._entries[key] = entry
            return entry

    def liveness_for(self, *, passport_id: str, device_id: str) -> MinerRosterEntry | None:
        with self._lock:
            return self._entries.get((passport_id, device_id))

    def is_live(
        self,
        *,
        passport_id: str,
        device_id: str,
        observed_at: datetime,
    ) -> bool:
        validate_aware_timestamp("observed_at", observed_at)
        with self._lock:
            entry = self._entries.get((passport_id, device_id))
        if entry is None or entry.status != ROSTER_STATUS_ACTIVE or entry.last_seen is None:
            return False
        return entry.last_seen > observed_at - self.liveness_ttl

    # ----------------------------------------------------------------- revoke
    def revoke(
        self,
        *,
        passport_id: str,
        device_id: str,
        observed_at: datetime,
    ) -> EnrollmentResult:
        validate_aware_timestamp("observed_at", observed_at)
        revocation = self.device_registry.revoke(
            passport_id=passport_id,
            device_id=device_id,
            observed_at=observed_at,
        )
        if not revocation.accepted:
            return EnrollmentResult(False, revocation.reason_code)
        with self._lock:
            key = (passport_id, device_id)
            existing = self._entries.get(key)
            worker_name = (
                existing.worker_name
                if existing is not None
                else _assigned_worker_name(passport_id, device_id)
            )
            entry = MinerRosterEntry(
                passport_id=passport_id,
                device_id=device_id,
                worker_name=worker_name,
                status=ROSTER_STATUS_REVOKED,
                last_seen=existing.last_seen if existing is not None else None,
                enrolled_at=existing.enrolled_at if existing is not None else None,
            )
            record = {
                "event_type": ROSTER_EVENT_REVOKED,
                "passport_id": passport_id,
                "device_id": device_id,
                "worker_name": worker_name,
                "status": ROSTER_STATUS_REVOKED,
                "observed_at": observed_at.isoformat(),
            }
            self._append_locked(record)
            self._entries[key] = entry
            return EnrollmentResult(True, entry=entry)

    # -------------------------------------------------------------- internals
    def _append_locked(self, record: dict[str, Any]) -> None:
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise MinerRosterUnavailable("miner_roster_store_unavailable") from exc

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
                        f"miner_roster_store_corrupt:{self.path}:{line_number}"
                    ) from exc
                if not isinstance(record, dict):
                    raise ValueError(f"miner_roster_store_corrupt:{self.path}:{line_number}")
                self._apply_record(record)

    def _apply_record(self, record: dict[str, Any]) -> None:
        passport_id = record.get("passport_id")
        device_id = record.get("device_id")
        if not (
            isinstance(passport_id, str)
            and passport_id
            and isinstance(device_id, str)
            and device_id
        ):
            return
        key = (passport_id, device_id)
        existing = self._entries.get(key)
        event_type = record.get("event_type")
        observed_at = _parse_dt(record.get("observed_at"))
        if event_type == ROSTER_EVENT_LIVENESS:
            if existing is None:
                return
            self._entries[key] = MinerRosterEntry(
                passport_id=existing.passport_id,
                device_id=existing.device_id,
                worker_name=existing.worker_name,
                status=existing.status,
                last_seen=observed_at or existing.last_seen,
                enrolled_at=existing.enrolled_at,
            )
            return
        worker_name = record.get("worker_name")
        if not isinstance(worker_name, str) or not worker_name:
            if existing is not None:
                worker_name = existing.worker_name
            else:
                worker_name = _assigned_worker_name(passport_id, device_id)
        status = (
            ROSTER_STATUS_REVOKED
            if event_type == ROSTER_EVENT_REVOKED
            else ROSTER_STATUS_ACTIVE
        )
        self._entries[key] = MinerRosterEntry(
            passport_id=passport_id,
            device_id=device_id,
            worker_name=worker_name,
            status=status,
            last_seen=existing.last_seen if existing else None,
            enrolled_at=(existing.enrolled_at if existing else observed_at),
        )


def _assigned_worker_name(passport_id: str, device_id: str) -> str:
    """Deterministic, server-owned worker name for a ``(passport, device)``.

    A short, stable, opaque label derived from the identity (NOT the client's
    self-named worker, which is non-authoritative metrics only). Stable so H_b
    can correlate pool evidence to the same worker across sessions; opaque so it
    leaks nothing and is never a payout address.
    """

    digest = hashlib.sha256(f"alice-acp:worker-name:{passport_id}|{device_id}".encode()).hexdigest()
    return f"alc-w-{digest[:16]}"


def _parse_dt(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None

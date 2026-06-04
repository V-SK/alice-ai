"""Server-issued, single-use issuance-nonce store for shadow session issuance.

Phase H_a (mining lane): before this, ``ShadowSessionIssueRequest.issuance_nonce``
was *client-supplied* — the ledger only checked it had not been reused within the
current process (``ledger.py`` ``_used_session_nonces``). A client could therefore
pick any nonce it liked, sign a device-PoP over it, and issue. That is fine for a
trusted in-process caller but is too weak for an external miner at the untrusted
HTTP edge: it lets a client pre-compute PoPs offline at will.

This module mints a short-TTL, single-use *server* nonce (``POST /session/nonce``)
that the device must sign in its device-PoP at ``/session/issue``. The nonce is:

  * server-minted (random 16 bytes, hex) — a client cannot present a nonce the
    server never issued,
  * short-lived (``DEFAULT_NONCE_TTL``) — a leaked nonce expires quickly,
  * single-use — consumed atomically on a successful issuance (and the ledger's
    existing per-process ``_used_session_nonces`` set still rejects any reuse).

CREDIT-ONLY: nothing here touches any reward/payout/chain flag — it only adds
issuance rigor in front of the (still intact) C2 device-PoP gate.

Durability mirrors :class:`alice_acp.shadow_server.dedup_store.JsonlProofDedupStore`
exactly: an append-only JSONL of ``issued`` / ``consumed`` events, replayed on
construction, fsync'd on every append, guarded by a lock. So a server restart
cannot resurrect an already-consumed nonce, and an issued-but-unconsumed nonce
survives a restart until it expires. Fail-closed: an OSError on read/write
surfaces as :class:`IssuanceNonceStoreUnavailable` so the caller denies rather
than silently minting/admitting.
"""

from __future__ import annotations

import json
import os
import secrets
import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from alice_acp.shadow_server.types import (
    REASON_SESSION_NONCE_NOT_SERVER_ISSUED,
    REASON_SESSION_NONCE_STORE_UNAVAILABLE,
)

ISSUANCE_NONCE_FILE_NAME = "issuance_nonces.jsonl"

# Short TTL: long enough for a miner to mint a nonce, build + sign the PoP, and
# call /session/issue over a normal network round-trip; short enough that a
# leaked/observed nonce is not a durable forgery primitive.
DEFAULT_NONCE_TTL = timedelta(minutes=5)

NONCE_EVENT_ISSUED = "issuance_nonce_issued"
NONCE_EVENT_CONSUMED = "issuance_nonce_consumed"

# Canonical "the server never minted this nonce" reason — shared with the ledger
# edge constant so a single string flows out of /session/issue and /device/register.
REASON_NONCE_UNKNOWN = REASON_SESSION_NONCE_NOT_SERVER_ISSUED
REASON_NONCE_EXPIRED = "issuance_nonce_expired"
REASON_NONCE_ALREADY_CONSUMED = "issuance_nonce_already_consumed"


class IssuanceNonceStoreUnavailable(RuntimeError):
    """Raised by a backend that cannot read/write — treated as fail-closed."""


@dataclass(frozen=True, slots=True)
class IssuedNonce:
    nonce: str
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class NonceConsumeResult:
    accepted: bool
    reason_code: str | None = None


class IssuanceNonceStore(Protocol):
    def issue_nonce(self, *, observed_at: datetime) -> IssuedNonce:
        ...

    def consume_nonce(self, *, nonce: str, observed_at: datetime) -> NonceConsumeResult:
        ...


@dataclass(slots=True)
class InMemoryIssuanceNonceStore:
    """Deterministic in-memory nonce store for tests / in-process harnesses.

    Fail-closed by construction: only a nonce minted by :meth:`issue_nonce`
    consumes; an unknown nonce, an expired nonce, or a second consume of the
    same nonce is rejected.
    """

    ttl: timedelta = DEFAULT_NONCE_TTL
    _issued: dict[str, datetime] = field(default_factory=dict)
    _consumed: set[str] = field(default_factory=set)

    def issue_nonce(self, *, observed_at: datetime) -> IssuedNonce:
        nonce = secrets.token_hex(16)
        expires_at = observed_at + self.ttl
        self._issued[nonce] = expires_at
        return IssuedNonce(nonce=nonce, issued_at=observed_at, expires_at=expires_at)

    def consume_nonce(self, *, nonce: str, observed_at: datetime) -> NonceConsumeResult:
        expires_at = self._issued.get(nonce)
        if expires_at is None:
            return NonceConsumeResult(False, REASON_NONCE_UNKNOWN)
        if nonce in self._consumed:
            return NonceConsumeResult(False, REASON_NONCE_ALREADY_CONSUMED)
        if observed_at >= expires_at:
            return NonceConsumeResult(False, REASON_NONCE_EXPIRED)
        self._consumed.add(nonce)
        return NonceConsumeResult(True)


class JsonlIssuanceNonceStore:
    """Durable, file-backed sibling of :class:`InMemoryIssuanceNonceStore`.

    Append-only JSONL of ``issued`` / ``consumed`` events, replayed on
    construction so neither an issued-but-unconsumed nonce nor an
    already-consumed nonce is lost across a restart. fsync on every append.
    A single-node durable store (mirrors ``JsonlProofDedupStore`` /
    ``JsonlShadowRateLimiter`` — cross-instance sharing needs shared infra and
    stays out of scope, owner-input/follow-up).
    """

    def __init__(self, root_or_file: str | Path, *, ttl: timedelta = DEFAULT_NONCE_TTL) -> None:
        root_or_file = Path(root_or_file)
        self.path = (
            root_or_file
            if root_or_file.suffix == ".jsonl"
            else root_or_file / ISSUANCE_NONCE_FILE_NAME
        )
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        self.ttl = ttl
        self._lock = threading.Lock()
        self._issued: dict[str, datetime] = {}
        self._consumed: set[str] = set()
        self._load()

    def issue_nonce(self, *, observed_at: datetime) -> IssuedNonce:
        nonce = secrets.token_hex(16)
        expires_at = observed_at + self.ttl
        record = {
            "event_type": NONCE_EVENT_ISSUED,
            "nonce": nonce,
            "issued_at": observed_at.isoformat(),
            "expires_at": expires_at.isoformat(),
        }
        with self._lock:
            try:
                self._append_line(record)
            except OSError as exc:
                raise IssuanceNonceStoreUnavailable(
                    REASON_SESSION_NONCE_STORE_UNAVAILABLE
                ) from exc
            self._issued[nonce] = expires_at
        return IssuedNonce(nonce=nonce, issued_at=observed_at, expires_at=expires_at)

    def consume_nonce(self, *, nonce: str, observed_at: datetime) -> NonceConsumeResult:
        with self._lock:
            expires_at = self._issued.get(nonce)
            if expires_at is None:
                return NonceConsumeResult(False, REASON_NONCE_UNKNOWN)
            if nonce in self._consumed:
                return NonceConsumeResult(False, REASON_NONCE_ALREADY_CONSUMED)
            if observed_at >= expires_at:
                return NonceConsumeResult(False, REASON_NONCE_EXPIRED)
            record = {
                "event_type": NONCE_EVENT_CONSUMED,
                "nonce": nonce,
                "consumed_at": observed_at.isoformat(),
            }
            try:
                self._append_line(record)
            except OSError as exc:
                raise IssuanceNonceStoreUnavailable(
                    REASON_SESSION_NONCE_STORE_UNAVAILABLE
                ) from exc
            self._consumed.add(nonce)
        return NonceConsumeResult(True)

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
                        f"issuance_nonce_store_corrupt:{self.path}:{line_number}"
                    ) from exc
                if not isinstance(record, dict):
                    raise ValueError(f"issuance_nonce_store_corrupt:{self.path}:{line_number}")
                self._apply_record(record)

    def _apply_record(self, record: dict[str, Any]) -> None:
        nonce = record.get("nonce")
        if not isinstance(nonce, str) or not nonce:
            return
        event_type = record.get("event_type")
        if event_type == NONCE_EVENT_ISSUED:
            expires_raw = record.get("expires_at")
            if isinstance(expires_raw, str):
                try:
                    self._issued[nonce] = datetime.fromisoformat(expires_raw)
                except ValueError:
                    return
        elif event_type == NONCE_EVENT_CONSUMED:
            self._consumed.add(nonce)

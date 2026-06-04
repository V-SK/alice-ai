"""Durable per-submission dedup store (doc §2.2 — ``dedup(seed‖nonce‖hdr‖xn‖worker)``).

Mirrors the existing durability discipline of
:mod:`alice_acp.shadow_server.dedup_store` and the cursor stores in
:mod:`alice_acp.shadow_server.pool_evidence_providers`: an in-memory spent SET +
an append-only JSONL backing file that is ``0600``, ``fsync``'d on every write, and
REPLAYED on construction so a restart rebuilds the spent set. A write ``OSError``
raises :class:`ShareDedupUnavailable` so the validator FAILS CLOSED (rejects the
submission) rather than risk crediting a share twice.

The dedup key is the submission's intrinsic identity —
``sha256(algorithm‖seed‖header‖nonce‖extranonce‖worker_name)`` — so the SAME nonce
for the SAME work by the SAME worker is credited exactly once, no matter how many
times the rig (or a replay) submits it. This is a SECOND, validator-owned idempotency
layer in front of the M0 ``(pool_id, worker_name, validated_share_id)`` store key: the
validated_share_id is derived from this key (see the validator core), so the two layers
agree and a duplicate never even reaches the store.

CREDIT-ONLY: the file holds only the opaque dedup key (a sha256 hex digest) — no
payout/reward/chain/nonce-plaintext data.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

#: Domain-separation tag folded into the dedup pre-image so this key space can never
#: collide with any other sha256 the system computes.
_DEDUP_DOMAIN = "alice-acp-share-validator-dedup-v1"

#: The unit-separator byte used to join the dedup pre-image fields, so two distinct
#: field tuples can never serialize to the same byte string (e.g. nonce ``b"ab"`` +
#: header ``b"c"`` vs nonce ``b"a"`` + header ``b"bc"``).
_FIELD_SEP = b"\x1f"


def submission_dedup_key(
    *,
    algorithm: str,
    seed: bytes,
    header: bytes,
    nonce: bytes,
    extranonce: bytes,
    worker_name: str,
) -> str:
    """``sha256(domain‖algorithm‖seed‖header‖nonce‖extranonce‖worker_name)`` (hex).

    The doc §2.2 dedup identity. Length-prefixed, separator-joined fields make the
    serialization unambiguous so distinct submissions can never alias to one key.
    """

    hasher = hashlib.sha256()
    parts: tuple[bytes, ...] = (
        _DEDUP_DOMAIN.encode("utf-8"),
        algorithm.encode("utf-8"),
        seed,
        header,
        nonce,
        extranonce,
        worker_name.encode("utf-8"),
    )
    for index, part in enumerate(parts):
        if index:
            hasher.update(_FIELD_SEP)
        # Length-prefix every field so the separator can never be confused with field
        # content (a field that itself contains the separator byte stays unambiguous).
        hasher.update(len(part).to_bytes(8, "big"))
        hasher.update(part)
    return hasher.hexdigest()


@dataclass(frozen=True, slots=True)
class ShareDedupClaim:
    """A claim to spend ONE submission's dedup key for credit."""

    dedup_key: str


@dataclass(frozen=True, slots=True)
class ShareDedupDecision:
    """Outcome of a dedup claim: ``first_seen`` is ``True`` only the first time."""

    first_seen: bool


class ShareDedupUnavailable(RuntimeError):
    """Raised by a durable dedup store when it cannot persist (fail-closed)."""


class ShareDedupStore(Protocol):
    """Tracks the spent SET of submission dedup keys.

    ``claim`` returns ``first_seen=True`` exactly once per never-before-seen
    ``dedup_key`` (adding it to the spent set), and ``first_seen=False`` for any
    repeat. A store-unavailable condition MUST raise :class:`ShareDedupUnavailable`
    so the validator denies rather than risk a double-credit.
    """

    def claim(self, claim: ShareDedupClaim) -> ShareDedupDecision:
        ...


@dataclass(slots=True)
class InMemoryShareDedupStore:
    """In-memory :class:`ShareDedupStore` (tests / single-process default)."""

    _seen: set[str] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def claim(self, claim: ShareDedupClaim) -> ShareDedupDecision:
        if not claim.dedup_key:
            raise ShareDedupUnavailable("share_dedup_key_required")
        with self._lock:
            if claim.dedup_key in self._seen:
                return ShareDedupDecision(first_seen=False)
            self._seen.add(claim.dedup_key)
            return ShareDedupDecision(first_seen=True)


@dataclass(slots=True)
class JsonlShareDedupStore:
    """Durable, append-only :class:`ShareDedupStore` (mirrors the cursor/dedup stores).

    Each first-seen key appends a record ``{"dedup_key": "<sha256>"}`` and adds it to
    the in-memory spent set; replay on construction rebuilds the set (a re-seen key is
    a no-op). A write ``OSError`` raises :class:`ShareDedupUnavailable` so the
    validator denies rather than silently double-crediting. The file is created
    ``0600`` (opaque dedup keys only). CREDIT-ONLY: no payout/reward/chain field is
    ever written.
    """

    path: Path
    _seen: set[str] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        if self.path.suffix != ".jsonl":
            self.path = self.path / "share_dedup.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        # Opaque dedup keys only — keep the file owner-readable only (0600). Best
        # effort: never block construction on a chmod failure.
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass
        self._load()

    def claim(self, claim: ShareDedupClaim) -> ShareDedupDecision:
        if not claim.dedup_key:
            raise ShareDedupUnavailable("share_dedup_key_required")
        with self._lock:
            if claim.dedup_key in self._seen:
                return ShareDedupDecision(first_seen=False)
            self._append_locked({"dedup_key": claim.dedup_key})
            self._seen.add(claim.dedup_key)
            return ShareDedupDecision(first_seen=True)

    def _append_locked(self, record: dict[str, Any]) -> None:
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise ShareDedupUnavailable("share_dedup_store_unavailable") from exc

    def _load(self) -> None:
        with self.path.open(encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                if not isinstance(record, dict):
                    continue
                dedup_key = record.get("dedup_key")
                if isinstance(dedup_key, str) and dedup_key:
                    self._seen.add(dedup_key)

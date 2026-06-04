"""Phase H_b: REAL per-pool evidence providers (anti-cheat) for ALL FOUR lanes.

Phase B shipped the :class:`~alice_acp.shadow_server.mining_authority_bridge.PoolEvidenceProvider`
seam with a fail-closed :class:`NoPoolEvidenceProvider` (every mining proof stays
``under_review`` — no credit) and a deterministic offline test shim. Phase H_b is
the "B2" follow-up promised there: it replaces ``NoPoolEvidenceProvider`` with
providers that let the SERVER *independently* verify each miner's work by polling
the mining POOL's API server-side, so credit is gated on **pool-attested,
server-read per-worker shares — never client claims**.

THE ANTI-CHEAT MODEL (the load-bearing idea)
--------------------------------------------
A real mining pool's public API does NOT expose Alice's internal per-share
``canonical_share_hash`` — it exposes a per-worker *monotonic accepted-share
counter* (and/or hashrate) for a wallet address. So the providers here close the
loop the only sound way:

1. **One request, all workers.** Given the Alice pool address + the
   SERVER-ASSIGNED ``worker_name`` (minted by the H_a miner roster, echoed in the
   session envelope; the client's self-named worker is advisory metrics only),
   each provider queries the pool's PER-ADDRESS ALL-WORKERS endpoint exactly once
   per poll (respecting the pool's rate limit) and caches the parsed snapshot for
   a configurable cadence.
2. **Parse pool-attested per-worker counters**, keyed by worker name.
3. **Credit from the server-read DELTA.** The provider tracks, per
   ``(pool_id, worker_name)``, the last accepted-share count it has already
   *spent* (the cursor, in :class:`PoolShareCursorStore`). When a reconstructed
   proof arrives, the provider attests it as a pool-confirmed accepted share
   **only if** the pool's current count for that worker exceeds the spent cursor
   — i.e. the pool has attested at least one NEW accepted share since the last
   spend. It then advances the cursor by one, so the same pool-counter increment
   can never be spent twice (dedup by ``(pool, worker, cursor)``). Credit is thus
   bounded by what the SERVER read from the pool, not by what the client asserts.
4. **FAIL-CLOSED everywhere.** Pool API unreachable / unconfigured / TLS error /
   worker absent / no un-spent delta → the provider returns ``None``, which the
   authority evaluator treats as ``PROOF_AUTHORITY_EVIDENCE_REQUIRED``
   (``under_review``) — *never* a free pass.

How it plugs into the existing gate (no gate is bypassed or weakened)
---------------------------------------------------------------------
A provider's :meth:`evidence_for` returns a
:class:`~alice_acp.mining_proofs.pool_evidence.PoolEvidenceAuthority` that lists
the proof's ``canonical_share_hash`` in ``accepted_share_hashes`` **iff** the
server-read delta has un-spent budget. That object then flows through the
**unchanged** authority path:
``authority_result_for_proof`` -> ``evaluate_public_beta_proof_authority`` ->
``cross_check_pool_authority`` (which still requires pool/worker/session/
collection-address match + the canonical hash to be in the accepted set) ->
``ingest_mining_proof`` (which still re-applies proof-id dedup, session
validation, observed-window clamp, lane match, ``rewardable_score > 0``,
canonical-hash dedup, and pool-evidence-ref dedup). Phase H_b ADDS the real
evidence source; it removes nothing.

CREDIT-ONLY + boundaries
-------------------------
No reward/payout/chain flag is ever set here; ``paid_acu`` is untouched. The
HTTP client is INJECTABLE so tests run against fixture JSON with NO real network
and NO real token. The F2Pool API secret is read from an injected environment
variable (:data:`F2POOL_API_SECRET_ENV`, ``ALICE_F2POOL_API_SECRET``) and is
NEVER hardcoded in code, fixtures, or tests; absent secret => fail-closed.

OWNER INPUT NEEDED AT DEPLOY: the real Alice pool addresses (PRL ``prl1p2ss…``,
XMR ``46knT…``, RVN ``RWAp…``, LTC ``ltc1qhl39…`` / F2Pool ``tx_acc``), the
F2Pool API secret value (injected via env, never committed), and the per-pool
poll cadence / rate-limit tuning. The PRL chain-explorer cross-check (PoUW
ground truth) is left as a clean, unwired stretch seam (:class:`PrlChainCrossCheck`).
"""

from __future__ import annotations

import json
import os
import threading
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Protocol

from alice_acp.evidence.types import ensure_no_raw_secret
from alice_acp.mining_proofs.canonical import canonical_share_hash
from alice_acp.mining_proofs.pool_evidence import (
    EvidenceSourceType,
    PoolEvidenceAuthority,
    PrlEpochEvidenceAuthority,
    SelfValidatedShareAuthority,
)
from alice_acp.mining_proofs.types import MiningShareProof
from alice_acp.mining_session.types import SignedMiningSession
from alice_acp.shadow_server.types import ShadowSession

# --- HTTP edge (injectable) --------------------------------------------------

#: Default network timeout for a real pool poll. Generous enough for a slow pool
#: but short enough that a hung pool fails closed promptly.
DEFAULT_POOL_HTTP_TIMEOUT = timedelta(seconds=10)

#: Default cache / poll cadence. One snapshot per address is reused for this long
#: so a burst of proofs costs ONE upstream request (respects pool rate limits).
#: OWNER INPUT NEEDED: tune per pool at deploy (e.g. supportxmr caches 1 min and
#: rate-limits 100 req / 15 min / IP).
DEFAULT_POLL_CADENCE = timedelta(seconds=60)


class PoolHttpError(RuntimeError):
    """Raised by a :class:`PoolHttpClient` on any non-success / transport error.

    Providers treat this as fail-closed (no evidence). It deliberately carries no
    response body so a pool error page can never leak into an audit surface.
    """


class PoolHttpClient(Protocol):
    """Minimal injectable HTTP edge for server-side pool polling.

    Production wires :class:`UrllibPoolHttpClient` (stdlib, no third-party dep).
    Tests inject a fake that returns fixture JSON, so NO real network call is ever
    made in the suite. Any transport / non-2xx / TLS failure MUST raise
    :class:`PoolHttpError` so the provider fails closed.
    """

    def get_json(
        self,
        url: str,
        *,
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        ...

    def post_json(
        self,
        url: str,
        *,
        body: Mapping[str, Any],
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        ...


@dataclass(frozen=True, slots=True)
class UrllibPoolHttpClient:
    """Production :class:`PoolHttpClient` over the Python stdlib (``urllib``).

    No third-party HTTP dependency is added. ``https://`` is REQUIRED (a non-TLS
    URL is refused before any socket is opened, so evidence can never be read over
    plaintext). Any URLError / HTTPError / TLS / decode failure becomes a
    :class:`PoolHttpError` (fail-closed). This class makes the only real network
    calls in Phase H_b and is never exercised by the test suite (tests inject a
    fake).
    """

    timeout: timedelta = DEFAULT_POOL_HTTP_TIMEOUT
    #: Identifying User-Agent sent on every request. Some pools' WAFs (notably
    #: supportxmr) answer the bare ``Python-urllib/x.y`` agent with 403, which
    #: would silently starve evidence (no body => zero shares => zero credit). An
    #: honest descriptive agent clears the WAF (verified 200 from the prod egress
    #: IP); a caller-supplied ``User-Agent`` header still overrides this default.
    user_agent: str = "Alice-Protocol-ACP/1.0 (+https://aliceprotocol.org)"

    def get_json(self, url: str, *, headers: Mapping[str, str] | None = None) -> Any:
        return self._request(url, data=None, headers=headers)

    def post_json(
        self,
        url: str,
        *,
        body: Mapping[str, Any],
        headers: Mapping[str, str] | None = None,
    ) -> Any:
        payload = json.dumps(body, separators=(",", ":"), ensure_ascii=True).encode("utf-8")
        merged = {"Content-Type": "application/json", **(dict(headers) if headers else {})}
        return self._request(url, data=payload, headers=merged)

    def _request(
        self,
        url: str,
        *,
        data: bytes | None,
        headers: Mapping[str, str] | None,
    ) -> Any:
        if not url.lower().startswith("https://"):
            raise PoolHttpError("pool_http_requires_https")
        request = urllib.request.Request(url, data=data, method="POST" if data else "GET")
        supplied = {key.lower() for key in (headers or {})}
        if "user-agent" not in supplied:
            request.add_header("User-Agent", self.user_agent)
        for key, value in (headers or {}).items():
            request.add_header(key, value)
        try:
            with urllib.request.urlopen(  # noqa: S310 - https-only enforced above
                request, timeout=self.timeout.total_seconds()
            ) as response:
                raw = response.read()
            return json.loads(raw.decode("utf-8"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            # URLError covers TLS/cert/connection failures; ValueError covers JSON
            # decode failures. Never surface the underlying message (could carry a
            # pool error body); fail closed with a stable reason.
            raise PoolHttpError("pool_http_request_failed") from exc


# --- server-read share cursor (delta + (pool, worker, cursor) dedup) ---------


@dataclass(frozen=True, slots=True)
class PoolShareCursorClaim:
    pool_id: str
    worker_name: str
    #: The pool's current server-read accepted-share count for this worker.
    pool_accepted_count: int


@dataclass(frozen=True, slots=True)
class PoolShareCursorDecision:
    """Outcome of attempting to spend one server-read accepted-share unit."""

    granted: bool
    #: The cursor index this grant consumed (the share number, 1-based). ``None``
    #: when nothing was granted. This index makes the grant idempotent: a replay
    #: of the same proof cannot advance the cursor twice for the same count.
    cursor: int | None = None


class PoolShareCursorStore(Protocol):
    """Tracks the spent accepted-share cursor per ``(pool_id, worker_name)``.

    ``try_spend`` grants exactly one accepted-share unit IFF the pool's current
    count exceeds the already-spent cursor, advancing the cursor by one. This is
    what converts a monotonic pool counter into a non-replayable per-share credit
    budget. A store-unavailable condition MUST fail closed (deny the grant).
    """

    def try_spend(self, claim: PoolShareCursorClaim) -> PoolShareCursorDecision:
        ...


@dataclass(slots=True)
class InMemoryPoolShareCursorStore:
    """In-memory :class:`PoolShareCursorStore` (tests / single-process default)."""

    _spent: dict[tuple[str, str], int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def try_spend(self, claim: PoolShareCursorClaim) -> PoolShareCursorDecision:
        if claim.pool_accepted_count <= 0:
            return PoolShareCursorDecision(False)
        key = (claim.pool_id, claim.worker_name)
        with self._lock:
            spent = self._spent.get(key, 0)
            if claim.pool_accepted_count <= spent:
                # Pool has attested no NEW accepted share since the last spend.
                return PoolShareCursorDecision(False)
            cursor = spent + 1
            self._spent[key] = cursor
            return PoolShareCursorDecision(True, cursor=cursor)


class PoolShareCursorUnavailable(RuntimeError):
    """Raised by the durable cursor store when it cannot persist (fail-closed)."""


@dataclass(slots=True)
class JsonlPoolShareCursorStore:
    """Durable, append-only :class:`PoolShareCursorStore` (mirrors the dedup store).

    Each granted spend appends a record and bumps the in-memory cursor; replay on
    construction rebuilds the spent cursor per ``(pool, worker)``. A write OSError
    raises :class:`PoolShareCursorUnavailable` so the provider denies rather than
    silently double-spending. CREDIT-ONLY: stores only opaque counters, no payout
    data.
    """

    path: Path
    _spent: dict[tuple[str, str], int] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        if self.path.suffix != ".jsonl":
            self.path = self.path / "pool_share_cursors.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        self._load()

    def try_spend(self, claim: PoolShareCursorClaim) -> PoolShareCursorDecision:
        if claim.pool_accepted_count <= 0:
            return PoolShareCursorDecision(False)
        key = (claim.pool_id, claim.worker_name)
        with self._lock:
            spent = self._spent.get(key, 0)
            if claim.pool_accepted_count <= spent:
                return PoolShareCursorDecision(False)
            cursor = spent + 1
            self._append_locked(
                {
                    "pool_id": claim.pool_id,
                    "worker_name": claim.worker_name,
                    "cursor": cursor,
                }
            )
            self._spent[key] = cursor
            return PoolShareCursorDecision(True, cursor=cursor)

    def _append_locked(self, record: dict[str, Any]) -> None:
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise PoolShareCursorUnavailable("pool_share_cursor_store_unavailable") from exc

    def _load(self) -> None:
        with self.path.open(encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line:
                    continue
                record = json.loads(line)
                pool_id = record.get("pool_id")
                worker_name = record.get("worker_name")
                cursor = record.get("cursor")
                if isinstance(pool_id, str) and isinstance(worker_name, str) and isinstance(
                    cursor, int
                ):
                    key = (pool_id, worker_name)
                    if cursor > self._spent.get(key, 0):
                        self._spent[key] = cursor


# --- PRL (pearlhash) epoch cursor (spent-SET by epoch_label, NOT a counter) --
#
# pearlhash exposes NO per-share counter (see docs/PRL-EPOCH-CREDIT-DESIGN.md
# §2.0/§2.2): the only per-account work signal is the hourly EPOCH CREDIT at
# GET /api/account/<addr>. So PRL cannot use the integer-high-water share cursor
# above; epoch labels are not a dense 1..N sequence, and the account API's
# balance_transactions[] is a ROLLING WINDOW, so the oldest labels fall off over
# time. The PRL cursor is therefore a SPENT-SET keyed by
# (pool_id, address, epoch_label): one credit per never-before-seen epoch_label,
# order-independent, and window-eviction-safe (an epoch that has rolled off the
# API but is already in the spent set must NOT re-credit and need not still be
# present upstream). Mirrors the share-cursor store's durability exactly
# (in-memory + append-only JSONL; a write OSError fails closed).


@dataclass(frozen=True, slots=True)
class PrlEpochCursorClaim:
    """A claim to spend ONE credit for a specific pearlhash epoch.

    The credit key is ``(pool_id, address, worker_name, epoch_label)``. pearlhash's
    epoch CREDIT signal is per-ADDRESS (doc §0.2) — but a single address can carry
    MANY registered workers (each a distinct ``--worker`` rig owned by a distinct
    Alice miner), and each worker must be credited its OWN slice of the address's
    epoch (the audit's per-worker-attribution major). So the cursor is keyed per
    ``worker_name`` as WELL as per address: a given ``(worker, epoch)`` is spent at
    most once, and two workers on the SAME address each spend the SAME ``epoch_label``
    INDEPENDENTLY (the first no longer eats the whole epoch). ``epoch_label`` IS the
    cursor; there is no integer count.

    ``worker_name`` defaults to ``""`` for the legacy PER-ADDRESS keying (one credit
    per ``(pool, address, epoch)`` regardless of worker) — this preserves the exact
    pre-per-worker behaviour AND the durable JSONL replay of records written before
    the worker dimension existed (a record with no ``worker_name`` rebuilds as ``""``).
    The provider passes the real ``worker_name`` so each registered worker spends its
    own slice.
    """

    pool_id: str
    address: str
    epoch_label: str
    worker_name: str = ""


@dataclass(frozen=True, slots=True)
class PrlEpochCursorDecision:
    """Outcome of attempting to spend one epoch credit (no integer cursor)."""

    granted: bool


class PrlEpochCursorStore(Protocol):
    """Tracks the spent SET of epoch labels per ``(pool_id, address)``.

    ``try_spend`` grants exactly once per never-before-seen ``epoch_label`` and
    adds it to the spent set; a re-seen label is denied. This converts the
    append-only, window-bounded epoch credits into a non-replayable
    one-credit-per-epoch budget. A store-unavailable condition MUST fail closed
    (deny the grant) so the provider never silently double-credits an epoch.
    """

    def try_spend(self, claim: PrlEpochCursorClaim) -> PrlEpochCursorDecision:
        ...


@dataclass(slots=True)
class InMemoryPrlEpochCursorStore:
    """In-memory :class:`PrlEpochCursorStore` (tests / single-process default).

    Keyed by ``(pool_id, address, worker_name)`` — ``worker_name=""`` is the legacy
    per-address bucket (unchanged behaviour); a real worker name gives each rig on a
    shared address its OWN spent-set so it credits its own per-worker epoch slice.
    """

    _spent: dict[tuple[str, str, str], set[str]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def try_spend(self, claim: PrlEpochCursorClaim) -> PrlEpochCursorDecision:
        if not claim.epoch_label:
            return PrlEpochCursorDecision(False)
        key = (claim.pool_id, claim.address, claim.worker_name)
        with self._lock:
            spent = self._spent.get(key)
            if spent is None:
                spent = set()
                self._spent[key] = spent
            if claim.epoch_label in spent:
                # Already credited this epoch for this (pool, address, worker) — no-op.
                return PrlEpochCursorDecision(False)
            spent.add(claim.epoch_label)
            return PrlEpochCursorDecision(True)


@dataclass(slots=True)
class JsonlPrlEpochCursorStore:
    """Durable, append-only :class:`PrlEpochCursorStore` (mirrors the share store).

    Each granted epoch appends a record
    ``{"pool_id","address","epoch_label","worker_name"}`` and adds the label to the
    in-memory spent set; replay on construction rebuilds the set per
    ``(pool, address, worker_name)`` (idempotent: a re-seen label is a no-op). A
    write OSError raises :class:`PoolShareCursorUnavailable` so the provider denies
    rather than silently double-crediting an epoch. CREDIT-ONLY: stores only the
    opaque routing tuple, NO payout/amount/share data. Window-eviction-safe: once a
    label is in the set it stays spent even after it rolls off the upstream API window.

    BACKWARD-COMPAT: a record written before the per-worker dimension existed has no
    ``worker_name`` and replays into the ``""`` (per-address) bucket — byte-for-byte
    the prior dedup. So an in-place upgrade neither loses nor re-credits any
    already-spent epoch.
    """

    path: Path
    _spent: dict[tuple[str, str, str], set[str]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        if self.path.suffix != ".jsonl":
            self.path = self.path / "prl_epoch_cursors.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        self._load()

    def try_spend(self, claim: PrlEpochCursorClaim) -> PrlEpochCursorDecision:
        if not claim.epoch_label:
            return PrlEpochCursorDecision(False)
        key = (claim.pool_id, claim.address, claim.worker_name)
        with self._lock:
            spent = self._spent.get(key)
            if spent is None:
                spent = set()
                self._spent[key] = spent
            if claim.epoch_label in spent:
                return PrlEpochCursorDecision(False)
            self._append_locked(
                {
                    "pool_id": claim.pool_id,
                    "address": claim.address,
                    "epoch_label": claim.epoch_label,
                    "worker_name": claim.worker_name,
                }
            )
            spent.add(claim.epoch_label)
            return PrlEpochCursorDecision(True)

    def _append_locked(self, record: dict[str, Any]) -> None:
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise PoolShareCursorUnavailable("prl_epoch_cursor_store_unavailable") from exc

    def _load(self) -> None:
        with self.path.open(encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line:
                    continue
                record = json.loads(line)
                pool_id = record.get("pool_id")
                address = record.get("address")
                epoch_label = record.get("epoch_label")
                # Records written before the per-worker dimension have no worker_name
                # → "" (the legacy per-address bucket), so old dedup is preserved.
                worker_name = record.get("worker_name")
                if not isinstance(worker_name, str):
                    worker_name = ""
                if (
                    isinstance(pool_id, str)
                    and isinstance(address, str)
                    and isinstance(epoch_label, str)
                    and epoch_label
                ):
                    self._spent.setdefault((pool_id, address, worker_name), set()).add(
                        epoch_label
                    )


# --- PRL pending-share SNAPSHOT store (work-weight carry across maturity) -----
#
# WHY THIS EXISTS (the measured fidelity bug): a PRL epoch's fractional-work
# ``share`` lives ONLY in ``pending_rewards.epochs[]`` while the epoch is still
# PENDING (only the newest ~3 hours). By the time the epoch MATURES (appears as a
# positive ``"Epoch … credit"`` row in ``balance_transactions`` days later) its
# ``share`` is GONE from the API — so a matured epoch joined against the *live*
# pending map finds nothing and falls back to the flat unit (every epoch credits
# equally regardless of work). To credit WORK-WEIGHTED, the share must be
# SNAPSHOTTED while the epoch is still pending, then looked up when it matures.
#
# This store mirrors :class:`PrlEpochCursorStore`'s shape EXACTLY (same
# ``(pool_id, address, epoch_label)`` keying, in-memory + append-only JSONL,
# replay-on-construct, fsync) but carries two extra fields — the ``share`` (a
# Decimal, the fractional work-weight) and ``observed_at`` (when it was last seen
# pending, for DURABLE TTL eviction that survives a restart). It is SEPARATE from the
# spent-set cursor: the cursor gates "credit this epoch at most once", this store
# answers "what was this epoch's work-share when it was pending?". CREDIT-ONLY: it
# holds the routing triple + the opaque ``share`` fraction + a timestamp; NO
# amount/payout/reward/chain field. The store stays BOUNDED two ways: the provider
# :meth:`prune`s a snapshot once its epoch has matured + been spent, and
# :meth:`prune_stale` TTL-evicts a pending epoch that NEVER matured (a pre-maturity
# rollback) — both keyed off the durable ``observed_at`` so a restart still bounds it.


@dataclass(frozen=True, slots=True)
class PrlPendingShareClaim:
    """A pending epoch's work-``share`` to snapshot, keyed like the cursor.

    Same ``(pool_id, address, epoch_label)`` key as :class:`PrlEpochCursorClaim`
    (the label is already canonicalized via :func:`_normalize_epoch_label`, so a
    pending snapshot and the later matured credit collide on the SAME key). ``share``
    is the ``pending_rewards.epochs[].share`` fraction — the opaque work-weight,
    never a payout. ``observed_at`` is the poll instant this pending share was seen
    (drives durable TTL eviction); ``None`` lets the store treat it as un-aged
    (never TTL-evicted, only prune-after-spend).
    """

    pool_id: str
    address: str
    epoch_label: str
    share: Decimal
    observed_at: datetime | None = None


class PrlPendingShareSnapshotStore(Protocol):
    """Durable map ``(pool_id, address, epoch_label) -> share`` for PRL epochs.

    ``record`` snapshots a still-PENDING epoch's ``share`` (last write wins — the
    share refines as the epoch accrues work before maturing). ``share_for`` returns
    the snapshotted share for an epoch that has since matured (``None`` when never
    snapshotted — e.g. the epoch matured before we first polled, → graceful flat
    fallback). ``prune`` drops named keys (matured+spent); ``prune_stale`` drops
    every key last seen before ``now - ttl`` (a pending epoch that never matured) —
    both keep the store bounded. Every mutating call MUST be swallowed by the caller
    as a non-fatal degradation (the credit path NEVER blocks on the work-weight:
    missing share simply means flat fallback, never a crash, never a zero-credit of
    a real matured epoch).
    """

    def record(self, claim: PrlPendingShareClaim) -> None:
        ...

    def share_for(self, *, pool_id: str, address: str, epoch_label: str) -> Decimal | None:
        ...

    def prune(self, *, pool_id: str, address: str, epoch_labels: tuple[str, ...]) -> None:
        ...

    def prune_stale(self, *, now: datetime, ttl: timedelta) -> None:
        ...


@dataclass(frozen=True, slots=True)
class _PendingShareEntry:
    """A snapshotted share + the instant it was last seen pending (for TTL)."""

    share: Decimal
    last_seen_at: datetime | None


@dataclass(slots=True)
class InMemoryPrlPendingShareSnapshotStore:
    """In-memory :class:`PrlPendingShareSnapshotStore` (tests / single-process)."""

    _shares: dict[tuple[str, str], dict[str, _PendingShareEntry]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, claim: PrlPendingShareClaim) -> None:
        if not claim.epoch_label or claim.share < Decimal("0"):
            return
        key = (claim.pool_id, claim.address)
        with self._lock:
            self._shares.setdefault(key, {})[claim.epoch_label] = _PendingShareEntry(
                share=claim.share, last_seen_at=claim.observed_at
            )

    def share_for(self, *, pool_id: str, address: str, epoch_label: str) -> Decimal | None:
        if not epoch_label:
            return None
        with self._lock:
            entry = self._shares.get((pool_id, address), {}).get(epoch_label)
            return entry.share if entry is not None else None

    def prune(self, *, pool_id: str, address: str, epoch_labels: tuple[str, ...]) -> None:
        if not epoch_labels:
            return
        key = (pool_id, address)
        with self._lock:
            bucket = self._shares.get(key)
            if bucket is None:
                return
            for label in epoch_labels:
                bucket.pop(label, None)
            if not bucket:
                self._shares.pop(key, None)

    def prune_stale(self, *, now: datetime, ttl: timedelta) -> None:
        with self._lock:
            for key in list(self._shares):
                bucket = self._shares[key]
                for label in [
                    label
                    for label, entry in bucket.items()
                    if entry.last_seen_at is not None and now - entry.last_seen_at > ttl
                ]:
                    bucket.pop(label, None)
                if not bucket:
                    self._shares.pop(key, None)

    def size(self) -> int:
        """Total snapshotted (pool, address, epoch) entries — for bound assertions."""

        with self._lock:
            return sum(len(bucket) for bucket in self._shares.values())


@dataclass(slots=True)
class JsonlPrlPendingShareSnapshotStore:
    """Durable, append-only :class:`PrlPendingShareSnapshotStore` (mirrors the cursor).

    Each ``record`` appends ``{"pool_id","address","epoch_label","share",
    "observed_at"}``; each ``prune``/``prune_stale`` appends a tombstone
    (``{...,"share":null}``) so a restart forgets the dropped key. Replay on
    construction folds the log in order — LAST record per ``(pool, address,
    epoch_label)`` wins (a refined share, or a tombstone), exactly mirroring the
    in-memory last-write semantics. A write OSError is SWALLOWED (the work-weight is
    a fidelity nicety, never the credit gate — a failed snapshot just degrades that
    epoch to the flat fallback). CREDIT-ONLY: the routing triple + the opaque
    ``share`` fraction + a timestamp only, NO amount/payout/reward/chain. Bounded by
    the provider's prune-after-spend + DURABLE TTL eviction (``observed_at`` persists,
    so a restart still bounds the never-mature case); the log is COMPACTED on
    construction (replayed live state is rewritten, dropping tombstones + history).
    """

    path: Path
    _shares: dict[tuple[str, str], dict[str, _PendingShareEntry]] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        if self.path.suffix != ".jsonl":
            self.path = self.path / "prl_pending_shares.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        self._load()
        self._compact()

    def record(self, claim: PrlPendingShareClaim) -> None:
        if not claim.epoch_label or claim.share < Decimal("0"):
            return
        key = (claim.pool_id, claim.address)
        entry = _PendingShareEntry(share=claim.share, last_seen_at=claim.observed_at)
        with self._lock:
            bucket = self._shares.setdefault(key, {})
            if bucket.get(claim.epoch_label) == entry:
                # Identical share + observed_at already snapshotted — skip the append
                # (keeps the log from growing on an idempotent re-poll at the same
                # instant). One live record per pending epoch is the bound.
                return
            record: dict[str, Any] = {
                "pool_id": claim.pool_id,
                "address": claim.address,
                "epoch_label": claim.epoch_label,
                "share": str(claim.share),
            }
            if claim.observed_at is not None:
                record["observed_at"] = claim.observed_at.isoformat()
            self._append_locked(record)
            bucket[claim.epoch_label] = entry

    def share_for(self, *, pool_id: str, address: str, epoch_label: str) -> Decimal | None:
        if not epoch_label:
            return None
        with self._lock:
            entry = self._shares.get((pool_id, address), {}).get(epoch_label)
            return entry.share if entry is not None else None

    def prune(self, *, pool_id: str, address: str, epoch_labels: tuple[str, ...]) -> None:
        if not epoch_labels:
            return
        key = (pool_id, address)
        with self._lock:
            bucket = self._shares.get(key)
            if bucket is None:
                return
            for label in epoch_labels:
                if label not in bucket:
                    continue
                self._append_tombstone_locked(pool_id, address, label)
                bucket.pop(label, None)
            if not bucket:
                self._shares.pop(key, None)

    def prune_stale(self, *, now: datetime, ttl: timedelta) -> None:
        with self._lock:
            for key in list(self._shares):
                pool_id, address = key
                bucket = self._shares[key]
                stale = [
                    label
                    for label, entry in bucket.items()
                    if entry.last_seen_at is not None and now - entry.last_seen_at > ttl
                ]
                for label in stale:
                    self._append_tombstone_locked(pool_id, address, label)
                    bucket.pop(label, None)
                if not bucket:
                    self._shares.pop(key, None)

    def size(self) -> int:
        with self._lock:
            return sum(len(bucket) for bucket in self._shares.values())

    def _append_tombstone_locked(self, pool_id: str, address: str, epoch_label: str) -> None:
        self._append_locked(
            {
                "pool_id": pool_id,
                "address": address,
                "epoch_label": epoch_label,
                "share": None,
            }
        )

    def _append_locked(self, record: dict[str, Any]) -> None:
        # Fail-soft: a failed snapshot write only costs this epoch its work-weight
        # (it degrades to the flat fallback downstream), so DO NOT raise into the
        # credit path the way the cursor store does. The in-memory state is still
        # updated by the caller, so a same-process lookup still sees the share.
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError:
            return

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
                if not isinstance(record, Mapping):
                    continue
                pool_id = record.get("pool_id")
                address = record.get("address")
                epoch_label = record.get("epoch_label")
                if not (
                    isinstance(pool_id, str)
                    and isinstance(address, str)
                    and isinstance(epoch_label, str)
                    and epoch_label
                ):
                    continue
                bucket = self._shares.setdefault((pool_id, address), {})
                raw_share = record.get("share")
                if raw_share is None:
                    # Tombstone (prune) — the LAST word for this key forgets it.
                    bucket.pop(epoch_label, None)
                    continue
                share = _coerce_decimal(raw_share)
                if share is None or share < Decimal("0"):
                    continue
                last_seen_at: datetime | None = None
                raw_seen = record.get("observed_at")
                if isinstance(raw_seen, str) and raw_seen:
                    try:
                        last_seen_at = datetime.fromisoformat(raw_seen)
                    except ValueError:
                        last_seen_at = None
                bucket[epoch_label] = _PendingShareEntry(share=share, last_seen_at=last_seen_at)
        # Drop any now-empty buckets created purely by tombstones.
        self._shares = {key: bucket for key, bucket in self._shares.items() if bucket}

    def _compact(self) -> None:
        """Rewrite the log to the replayed LIVE state (drop tombstones + history).

        Keeps the file bounded by the number of LIVE pending snapshots rather than
        the lifetime append count. Fail-soft: an OSError leaves the (correct,
        already-replayed) in-memory state intact and the un-compacted log in place.
        """

        with self._lock:
            tmp = self.path.with_suffix(self.path.suffix + ".compact")
            try:
                with tmp.open("w", encoding="utf-8") as handle:
                    for (pool_id, address), bucket in self._shares.items():
                        for epoch_label, entry in bucket.items():
                            record: dict[str, Any] = {
                                "pool_id": pool_id,
                                "address": address,
                                "epoch_label": epoch_label,
                                "share": str(entry.share),
                            }
                            if entry.last_seen_at is not None:
                                record["observed_at"] = entry.last_seen_at.isoformat()
                            handle.write(
                                json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n"
                            )
                    handle.flush()
                    os.fsync(handle.fileno())
                os.replace(tmp, self.path)
            except OSError:
                try:
                    tmp.unlink()
                except OSError:
                    pass


# --- self-validated share intake (Milestone 0 / doc §2.4) --------------------
#
# The proxy pool's share VALIDATOR (Milestone 1, out of scope here) re-hashes each
# miner nonce and writes one ``ValidatedShare`` per re-hash-confirmed pool-diff
# share into a durable ``ValidatedShareStore``. The ``ProxyPoolEvidenceProvider``
# below DRAINS that store for a ``(pool_id, worker_name)`` and emits a
# ``SelfValidatedShareAuthority`` carrying Alice's OWN canonical_share_hash + the
# validated ``share_difficulty`` — so credit is gated on Alice's re-hash, not an
# upstream count (doc §2.2/§2.4/§3 Q1). The store mirrors the cursor stores
# structurally (InMemory + append-only JSONL; replay-on-construct; fsync; a write
# OSError fails closed). Idempotency key = ``(pool_id, worker_name,
# validated_share_id)``: one credit per never-before-seen validated share, so a
# replay or restart never double-credits. CREDIT-ONLY: it holds ONLY the opaque
# share facts in §2.4 — no payout/reward/chain symbol, ``paid_acu`` untouched.


@dataclass(frozen=True, slots=True)
class ValidatedShare:
    """One Alice-re-hash-confirmed pool-diff share (doc §2.4 ``ValidatedShare``).

    The validator emits this for a share whose nonce Alice re-hashed and confirmed
    at/above the pool difficulty. ``canonical_share_hash`` is the value Alice's own
    canonical hashing computed for the share (NOT an upstream hash); the
    ``ProxyPoolEvidenceProvider`` carries it into the
    :class:`SelfValidatedShareAuthority` so the self-validated gate binds THIS
    credit unit to THIS share. ``share_difficulty`` is the real validated
    difficulty (the credit magnitude). CREDIT-ONLY: only opaque facts; no payout
    address, reward, chain, or ``paid_acu`` field exists here.

    CROSS-PROCESS SESSION CARRY (doc §2.8): ``carried_session`` is the SIGNED
    :class:`ShadowSession` the TRANSPORT minted (in its OWN ledger) for the
    connection that produced this share. The SEPARATE credit-server process never
    saw that issuance, so the session rides the SAME shared store the share rides;
    the credit server RE-VERIFIES its HMAC signature against the shared auth-secret
    before crediting (``ledger.admit_cross_process_session``) — a forged/tampered
    session fails closed. It is OPTIONAL (``None``) so the in-process single-process
    path (and pre-existing callers that hand-feed shares for a session already in
    the credit ledger) are unchanged. It carries ONLY the already-public session
    envelope fields (incl. the public anti-replay ``session_nonce``); the secret is
    NEVER carried.
    """

    pool_id: str
    worker_name: str
    alice_collection_address: str
    session_id: str
    algorithm: str
    validated_share_id: str
    share_difficulty: Decimal
    canonical_share_hash: str
    header_hash: str
    share_nonce: str
    validated_at: datetime
    carried_session: ShadowSession | None = None

    def __post_init__(self) -> None:
        required = (
            self.pool_id,
            self.worker_name,
            self.alice_collection_address,
            self.session_id,
            self.algorithm,
            self.validated_share_id,
            self.canonical_share_hash,
            self.header_hash,
            self.share_nonce,
        )
        if any(not value for value in required):
            raise ValueError("validated share fields must be non-empty")
        for field_name, value in (
            ("pool_id", self.pool_id),
            ("worker_name", self.worker_name),
            ("alice_collection_address", self.alice_collection_address),
            ("session_id", self.session_id),
            ("algorithm", self.algorithm),
            ("validated_share_id", self.validated_share_id),
            ("share_nonce", self.share_nonce),
        ):
            ensure_no_raw_secret(value, field_name=field_name)
        if not isinstance(self.share_difficulty, Decimal):
            raise TypeError("share_difficulty must be Decimal")
        if self.share_difficulty <= Decimal("0"):
            raise ValueError("share_difficulty must be positive")
        if self.carried_session is not None:
            # The carried session must be FOR this share (same session_id), so the
            # signature the credit server re-verifies binds the worker/lane/expiry the
            # share was produced under. A mismatch is a construction bug → fail-closed.
            if self.carried_session.session_id != self.session_id:
                raise ValueError("carried_session.session_id must equal session_id")

    @property
    def intake_key(self) -> tuple[str, str, str]:
        """Idempotency key: ``(pool_id, worker_name, validated_share_id)``."""

        return (self.pool_id, self.worker_name, self.validated_share_id)


class ValidatedShareStore(Protocol):
    """Durable intake buffer of un-spent :class:`ValidatedShare`s (doc §2.4).

    ``record`` appends a validated share (idempotent on the
    ``(pool_id, worker_name, validated_share_id)`` key — a re-recorded share is a
    no-op). ``drain_one`` returns and marks-spent the oldest un-spent share for a
    ``(pool_id, worker_name)``, or ``None`` when none remain (the clean
    ``credit_attested_shares`` loop terminator). A store-unavailable condition MUST
    fail closed (raise :class:`PoolShareCursorUnavailable`) so the provider denies
    rather than double-crediting.
    """

    def record(self, share: ValidatedShare) -> bool:
        ...

    def drain_one(self, *, pool_id: str, worker_name: str) -> ValidatedShare | None:
        ...

    def peek_next(self, *, pool_id: str, worker_name: str) -> ValidatedShare | None:
        """The OLDEST un-spent share for ``(pool_id, worker_name)`` WITHOUT spending it.

        Returns the EXACT share :meth:`drain_one` would next return (FIFO, oldest
        un-spent) but leaves it un-spent, so a peek immediately followed by a drain
        sees the same share. The credit server uses this to read the share's
        ``validated_at`` and reconstruct the credited proof at the SAME ``observed_at``
        the validator folded into the carried ``canonical_share_hash`` (so the
        recompute-and-compare in ``cross_check_self_validated_share`` matches
        cross-process). Returns ``None`` when no un-spent share remains. A
        store-unavailable condition MUST fail closed (raise
        :class:`PoolShareCursorUnavailable`).
        """

        ...

    def pending_sessions(self) -> tuple[ShadowSession, ...]:
        """The carried sessions of every UN-SPENT share, NON-consuming (doc §2.8).

        Lets the credit server's proof-authority scheduler DISCOVER the sessions the
        transport minted (its own ``ledger.sessions`` is empty cross-process) WITHOUT
        draining a share: one entry per distinct ``session_id`` that still has at
        least one un-spent share carrying a session. Read-only — it never marks a
        share spent. Stores with no carried sessions return ``()`` (the in-process
        path is unaffected).
        """

        ...


@dataclass(slots=True)
class InMemoryValidatedShareStore:
    """In-memory :class:`ValidatedShareStore` (tests / single-process default)."""

    _shares: dict[tuple[str, str], list[ValidatedShare]] = field(default_factory=dict)
    _seen: set[tuple[str, str, str]] = field(default_factory=set)
    _spent: set[tuple[str, str, str]] = field(default_factory=set)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, share: ValidatedShare) -> bool:
        key = share.intake_key
        with self._lock:
            if key in self._seen:
                # Idempotent: a re-recorded validated share is a no-op.
                return False
            self._seen.add(key)
            self._shares.setdefault((share.pool_id, share.worker_name), []).append(share)
            return True

    def drain_one(self, *, pool_id: str, worker_name: str) -> ValidatedShare | None:
        with self._lock:
            queue = self._shares.get((pool_id, worker_name))
            if not queue:
                return None
            for share in queue:
                if share.intake_key not in self._spent:
                    self._spent.add(share.intake_key)
                    return share
            return None

    def peek_next(self, *, pool_id: str, worker_name: str) -> ValidatedShare | None:
        # Same FIFO oldest-un-spent scan as drain_one, but WITHOUT marking spent, so
        # a peek+drain pair returns the same share (the credit server peeks the
        # validated_at to align the reconstruction observed_at, then drains it).
        with self._lock:
            queue = self._shares.get((pool_id, worker_name))
            if not queue:
                return None
            for share in queue:
                if share.intake_key not in self._spent:
                    return share
            return None

    def pending_sessions(self) -> tuple[ShadowSession, ...]:
        with self._lock:
            return _distinct_pending_sessions(
                share
                for queue in self._shares.values()
                for share in queue
                if share.intake_key not in self._spent
            )


@dataclass(slots=True)
class JsonlValidatedShareStore:
    """Durable, append-only :class:`ValidatedShareStore` (mirrors the cursor stores).

    Two append-only record kinds share one JSONL file: a ``"share"`` row carries the
    full validated-share fields (so the un-spent queue rebuilds on construction), and
    a ``"spent"`` row marks an idempotency key consumed. Replay on construction
    rebuilds the un-spent queue per ``(pool, worker)`` (a re-seen ``record`` is a
    no-op; a ``spent`` key is skipped on drain). A write OSError raises
    :class:`PoolShareCursorUnavailable` so the provider denies rather than silently
    double-crediting. The file is created 0600 (opaque share facts only). CREDIT-ONLY:
    no payout/reward/chain field is ever written.
    """

    path: Path
    _queue: dict[tuple[str, str], list[ValidatedShare]] = field(default_factory=dict)
    _seen: set[tuple[str, str, str]] = field(default_factory=set)
    _spent: set[tuple[str, str, str]] = field(default_factory=set)
    #: Byte offset of the last COMPLETE line ingested. The DRAINER (credit server) and
    #: the APPENDER (transport) are SEPARATE processes writing one file, so every read
    #: tails from here to pick up the other process's appends (see _ingest_new_lines).
    _read_offset: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        if self.path.suffix != ".jsonl":
            self.path = self.path / "validated_shares.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        # Opaque share facts only — keep the intake buffer owner-readable only (0600).
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            # Best-effort hardening; never block construction on a chmod failure.
            pass
        self._ingest_new_lines_locked()

    def record(self, share: ValidatedShare) -> bool:
        key = share.intake_key
        with self._lock:
            if key in self._seen:
                return False
            record: dict[str, Any] = {
                "kind": "share",
                "pool_id": share.pool_id,
                "worker_name": share.worker_name,
                "alice_collection_address": share.alice_collection_address,
                "session_id": share.session_id,
                "algorithm": share.algorithm,
                "validated_share_id": share.validated_share_id,
                "share_difficulty": format(share.share_difficulty.normalize(), "f"),
                "canonical_share_hash": share.canonical_share_hash,
                "header_hash": share.header_hash,
                "share_nonce": share.share_nonce,
                "validated_at": share.validated_at.isoformat(),
            }
            if share.carried_session is not None:
                # Cross-process credit plane: persist the SIGNED session envelope (the
                # already-public fields only — the secret is never written) so a
                # restarted/separate credit server still discovers + re-verifies it.
                record["carried_session"] = _session_to_record(share.carried_session)
            self._append_locked(record)
            self._seen.add(key)
            self._queue.setdefault((share.pool_id, share.worker_name), []).append(share)
            return True

    def drain_one(self, *, pool_id: str, worker_name: str) -> ValidatedShare | None:
        with self._lock:
            self._ingest_new_lines_locked()  # tail cross-process "share" appends first
            queue = self._queue.get((pool_id, worker_name))
            if not queue:
                return None
            for share in queue:
                if share.intake_key in self._spent:
                    continue
                self._append_locked(
                    {
                        "kind": "spent",
                        "pool_id": share.pool_id,
                        "worker_name": share.worker_name,
                        "validated_share_id": share.validated_share_id,
                    }
                )
                self._spent.add(share.intake_key)
                return share
            return None

    def peek_next(self, *, pool_id: str, worker_name: str) -> ValidatedShare | None:
        # Mirror drain_one's read EXACTLY (tail cross-process "share" appends first, then
        # scan FIFO for the oldest un-spent) but DO NOT append a "spent" row or mutate
        # _spent — so the very next drain_one returns this same share. The credit server
        # peeks the share's validated_at to reconstruct the proof at the matching
        # observed_at before draining it.
        with self._lock:
            self._ingest_new_lines_locked()  # tail cross-process "share" appends first
            queue = self._queue.get((pool_id, worker_name))
            if not queue:
                return None
            for share in queue:
                if share.intake_key in self._spent:
                    continue
                return share
            return None

    def pending_sessions(self) -> tuple[ShadowSession, ...]:
        with self._lock:
            self._ingest_new_lines_locked()  # tail cross-process "share" appends first
            return _distinct_pending_sessions(
                share
                for queue in self._queue.values()
                for share in queue
                if share.intake_key not in self._spent
            )

    def _append_locked(self, record: dict[str, Any]) -> None:
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise PoolShareCursorUnavailable("validated_share_store_unavailable") from exc

    def _ingest_new_lines_locked(self) -> None:
        """Tail lines appended since the last read — INCLUDING by ANOTHER process.

        The credit server (DRAINER) constructs this store once at startup, but the
        transport (APPENDER, a SEPARATE process) keeps appending ``"share"`` rows. A
        one-shot load at construction would leave the drainer blind to them, so every
        read tails the file from the last byte offset to the last COMPLETE line (a
        trailing partial/mid-append line is left for the next read). Re-applying a row
        is idempotent (``_seen`` / ``_spent`` dedup), so re-reading our own ``"spent"``
        appends is harmless. The caller holds ``self._lock`` (or runs single-threaded
        at construction). A read OSError is swallowed (the next read retries).
        """

        try:
            with self.path.open("rb") as handle:
                handle.seek(self._read_offset)
                data = handle.read()
        except OSError:
            return
        if not data:
            return
        last_newline = data.rfind(b"\n")
        if last_newline < 0:
            return  # only a partial (mid-append) line so far; retry on the next read
        self._read_offset += last_newline + 1
        for raw in data[: last_newline + 1].split(b"\n"):
            line = raw.strip()
            if not line:
                continue
            try:
                record = json.loads(line.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                continue
            if isinstance(record, Mapping):
                self._apply_record_locked(record)

    def _apply_record_locked(self, record: Mapping[str, Any]) -> None:
        kind = record.get("kind")
        if kind == "spent":
            key = self._key_from(record)
            if key is not None:
                self._spent.add(key)
            return
        if kind != "share":
            return
        share = self._share_from(record)
        if share is None:
            return
        key = share.intake_key
        if key in self._seen:
            return
        self._seen.add(key)
        self._queue.setdefault((share.pool_id, share.worker_name), []).append(share)

    @staticmethod
    def _key_from(record: Mapping[str, Any]) -> tuple[str, str, str] | None:
        pool_id = record.get("pool_id")
        worker_name = record.get("worker_name")
        validated_share_id = record.get("validated_share_id")
        if (
            isinstance(pool_id, str)
            and isinstance(worker_name, str)
            and isinstance(validated_share_id, str)
        ):
            return (pool_id, worker_name, validated_share_id)
        return None

    @staticmethod
    def _share_from(record: Mapping[str, Any]) -> ValidatedShare | None:
        difficulty = _coerce_decimal(record.get("share_difficulty"))
        validated_at = record.get("validated_at")
        if difficulty is None or not isinstance(validated_at, str):
            return None
        try:
            parsed_at = datetime.fromisoformat(validated_at)
        except ValueError:
            return None
        carried_session = _session_from_record(record.get("carried_session"))
        try:
            return ValidatedShare(
                pool_id=str(record.get("pool_id")),
                worker_name=str(record.get("worker_name")),
                alice_collection_address=str(record.get("alice_collection_address")),
                session_id=str(record.get("session_id")),
                algorithm=str(record.get("algorithm")),
                validated_share_id=str(record.get("validated_share_id")),
                share_difficulty=difficulty,
                canonical_share_hash=str(record.get("canonical_share_hash")),
                header_hash=str(record.get("header_hash")),
                share_nonce=str(record.get("share_nonce")),
                validated_at=parsed_at,
                carried_session=carried_session,
            )
        except (ValueError, TypeError):
            return None


# --- parsed pool snapshot ----------------------------------------------------


@dataclass(frozen=True, slots=True)
class WorkerPoolStats:
    """Pool-attested, server-read stats for ONE worker on ONE pool address."""

    worker_name: str
    accepted_shares: int
    hashrate: Decimal | None = None


@dataclass(frozen=True, slots=True)
class PoolAddressSnapshot:
    """A parsed, server-read snapshot of every worker on one pool address."""

    pool_id: str
    fetched_at: datetime
    workers: tuple[WorkerPoolStats, ...]

    def worker(self, worker_name: str) -> WorkerPoolStats | None:
        for entry in self.workers:
            if entry.worker_name == worker_name:
                return entry
        return None


# --- PRL (pearlhash) per-address epoch snapshot ------------------------------


@dataclass(frozen=True, slots=True)
class EpochCredit:
    """One pearlhash epoch's credit signal, keyed by ``epoch_label``.

    ``epoch_label`` is the idempotency key (unique per credited hour; the row key
    the pearlhash UI itself uses). ``matured`` is ``True`` when the epoch appears
    as a positive ``"Epoch … credit"`` row in ``balance_transactions`` (the pool
    has FINALIZED that credit) — only matured epochs are credited (doc §2.5).
    ``share`` is the per-epoch fractional-work signal from
    ``pending_rewards.epochs[]`` (``None`` when absent) and is the magnitude
    source; ``amount`` is the PRL-denominated payout (carried for audit only,
    NEVER the credit magnitude — doc §2.4).
    """

    epoch_label: str
    amount: Decimal
    share: Decimal | None
    matured: bool


@dataclass(frozen=True, slots=True)
class PrlConnectedWorker:
    """One live worker on a pearlhash account, from ``connected_workers[]``.

    pearlhash's ``/api/account`` lists each connected rig with a ``worker_name`` (the
    ``--worker`` token the miner passed — EMPTY when none was passed), an opaque
    ``worker_id``, and a ``gpu_info[]`` whose hashrates sum to ``hashrate``. This is
    the ONLY per-worker signal the API exposes (confirmed live: there is NO per-worker
    accepted-share or per-worker epoch field — those are per-ADDRESS only). The
    provider uses ``worker_name`` to MATCH a registered Alice worker and ``hashrate``
    to compute that worker's PROPORTION of the address's total work, which DERIVES the
    per-worker split of the per-address epoch credit (the audit's per-worker
    attribution major).
    """

    worker_name: str
    hashrate: Decimal


@dataclass(frozen=True, slots=True)
class PrlAccountSnapshot:
    """A parsed, server-read snapshot of one pearlhash account (``/api/account``).

    The pool CREDIT signal is the per-ADDRESS epoch list (pearlhash exposes NO
    per-worker epoch/share): ``epochs`` carries every parsed epoch keyed by
    ``epoch_label``; the provider credits the matured ones that the durable epoch
    cursor has not yet spent (doc §2.1/§2.3).

    Per-WORKER attribution is DERIVED, not read: ``workers`` is the parsed
    ``connected_workers[]`` (each a :class:`PrlConnectedWorker` with its
    ``worker_name`` + ``hashrate``). The provider splits a matured epoch's
    per-address credit across the registered workers by HASHRATE PROPORTION, so two
    rigs on the SAME address each credit their own share instead of the first one
    eating the whole epoch. ``connected_workers`` is the worker COUNT (kept for
    backward-compatible callers / advisory liveness).

    ``pending_shares`` is the canonical-label -> ``share`` map parsed from
    ``pending_rewards.epochs[]`` (the still-immature epochs). The provider
    SNAPSHOTS these each poll so an epoch's work-``share`` survives into the future
    poll where the epoch matures (its share is gone from the live API by then) — the
    work-weight carry that makes credit fidelity work (doc §2.4/§2.5).
    """

    pool_id: str
    fetched_at: datetime
    address: str
    connected_workers: int
    epochs: tuple[EpochCredit, ...]
    pending_shares: tuple[tuple[str, Decimal], ...] = ()
    workers: tuple[PrlConnectedWorker, ...] = ()

    def matured_epochs(self) -> tuple[EpochCredit, ...]:
        return tuple(epoch for epoch in self.epochs if epoch.matured)

    def total_worker_hashrate(self) -> Decimal:
        """Sum of every connected worker's hashrate (the proportion denominator)."""

        total = Decimal("0")
        for worker in self.workers:
            if worker.hashrate > Decimal("0"):
                total += worker.hashrate
        return total

    def has_connected_worker(self, worker_name: str) -> bool:
        """Whether a NAMED worker is present in ``connected_workers[]`` this poll.

        This is the PRL-PARTICIPATION test (the attribution authority): pearlhash's
        ``connected_workers[]`` is the ONLY signal that a given ``--worker`` was
        actually mining PRL at poll time. A worker MISSING from this list did NOT mine
        PRL — so it must collect ZERO PRL credit (NOT a flat unit), which is exactly
        how a non-PRL worker (an LTC ASIC / the XMR worker) is kept out of the PRL
        epoch credit (the audit's per-worker attribution leak).

        Presence is independent of hashrate: a connected rig reporting a momentary
        ``hashrate=0`` is still PRESENT (it DID mine PRL this session) and degrades to
        the flat per-worker unit via :meth:`worker_hashrate_proportion`, never to zero.
        An empty ``worker_name`` (a rig that joined with no ``--worker``) is never a
        match here — there is no named PRL participant to attribute to, so the credit
        path treats it as absent (zero), not the legacy flat bucket.
        """

        if not worker_name:
            return False
        return any(worker.worker_name == worker_name for worker in self.workers)

    def worker_hashrate_proportion(self, worker_name: str) -> Decimal | None:
        """This worker's share of the address's total LIVE hashrate, or ``None``.

        Returns ``None`` (→ the caller falls back to the per-worker flat unit for a
        worker that IS connected, never a crash) when: the address's total hashrate is
        zero/unknown, or this worker's own hashrate is non-positive (a connected rig at
        a momentary ``hashrate=0``). Otherwise returns ``worker_hashrate /
        total_hashrate`` in ``(0, 1]``.

        ABSENCE (the worker is not in ``connected_workers[]`` at all) is NOT
        distinguishable from a connected-but-zero-hashrate worker by this method's
        return alone — both yield ``None`` here. The caller MUST first gate on
        :meth:`has_connected_worker`: an ABSENT worker collects ZERO (it did not mine
        PRL); only a PRESENT worker reaches this proportion (work-weighted, else the
        flat per-worker unit). Only named workers are matchable; an empty
        ``worker_name`` is never matched.
        """

        if not worker_name:
            return None
        total = self.total_worker_hashrate()
        if total <= Decimal("0"):
            return None
        mine = Decimal("0")
        for worker in self.workers:
            if worker.worker_name == worker_name and worker.hashrate > Decimal("0"):
                mine += worker.hashrate
        if mine <= Decimal("0"):
            return None
        return mine / total


# --- PRL (tw-pool.com) per-(worker, time-bucket) share snapshot --------------
#
# tw-pool.com ("小幣礦池", owner-chosen, run by egg5233) is the deployed PRL pool.
# Unlike pearlhash (per-ADDRESS hourly epoch credits, no per-worker counter), tw-pool's
# GET /api/worker_stats breaks the work out PER WORKER natively:
#   history["<addr>.<worker>"] = [ {time, hashrate, shares, invalidshares}, ... ]
# So the credit signal is the per-(worker, bucket_time) ``shares`` (valid shares in
# that time bucket); ``invalidshares`` flags rejects. This is a ROLLING WINDOW of
# time-bucketed points, NOT a lifetime-monotonic counter (the window slides), so a
# naive sum/delta of ``shares`` would double-count. The dedup keys on the bucket TIME
# (doc twpool-prl-integration.md §3-A): each (pool, worker, bucket_time) credited
# EXACTLY ONCE, reusing the PRL epoch-cursor spent-SET with ``epoch_label =
# str(bucket_time)`` and ``worker_name = <alc-w-id>``. Hash-free authority gate
# (tw-pool exposes no per-share hash) — the SAME PrlEpochEvidenceAuthority +
# cross_check_prl_epoch_authority the pearlhash path uses.

#: SHARE_SCALE for the tw-pool PRL lane: the multiplier applied to a bucket's valid
#: ``shares`` to produce the ``epoch_share`` carried on the authority. The downstream
#: :func:`prl_epoch_rewardable_score` then multiplies by
#: :data:`alice_acp.mining_proofs.pool_evidence.PRL_EPOCH_SHARE_SCALE` (= 1000), so the
#: NET rewardable score for a bucket is ``shares * TWPOOL_PRL_SHARE_SCALE * 1000``. The
#: default ``0.001`` makes that net score == ``shares`` (one score unit per valid
#: share) — a defensible, work-proportional, credit-only default that mirrors the
#: other lanes' "validated work 1:1" footing without coupling to PRL token value
#: (``shares`` is a count, not a payout). OWNER INPUT NEEDED (V): confirm / retune this
#: magnitude constant at deploy so a typical tw-pool bucket's score lands in the same
#: band as a per-share score on the XMR/RVN/LTC lanes.
TWPOOL_PRL_SHARE_SCALE = Decimal("0.001")

#: tw-pool ``pool`` query param (Pearl chain; on-pool algo label ``ZkPoUW``).
TWPOOL_PRL_POOL_PARAM = "pearl"


@dataclass(frozen=True, slots=True)
class TwPoolBucket:
    """One ``history[<addr>.<worker>]`` time-bucket point from tw-pool worker_stats.

    ``bucket_time`` is the bucket's unix ``time`` (the idempotency key — the cursor's
    ``epoch_label`` is ``str(bucket_time)``). ``shares`` is the valid-share magnitude
    for the bucket; ``invalidshares`` is the rejected count (a bucket with
    ``invalidshares > 0`` is SKIPPED — not credited — per the doc's down-weight/skip
    rule). ``hashrate`` is advisory liveness only.
    """

    bucket_time: int
    shares: Decimal
    invalidshares: Decimal
    hashrate: Decimal | None = None

    @property
    def epoch_label(self) -> str:
        # The cursor / authority idempotency key for this worker-bucket. Stable string
        # form of the unix bucket time so the spent-SET + JSONL replay key on it.
        return str(self.bucket_time)

    @property
    def creditable(self) -> bool:
        # Credit a bucket only when it carries strictly-positive valid shares AND no
        # rejects (fail-closed / down-weight-to-skip on any reject — doc §3-A).
        return self.shares > Decimal("0") and self.invalidshares <= Decimal("0")


@dataclass(frozen=True, slots=True)
class TwPoolWorkerStatsSnapshot:
    """Parsed tw-pool ``/api/worker_stats`` snapshot for one Alice PRL address.

    ``buckets_by_worker`` maps a tw-pool worker name (the ``<worker>`` half of the
    ``<addr>.<worker>`` history key — i.e. the Alice-minted ``alc-w-<id>``) to its
    per-time-bucket points. The provider credits each un-spent creditable bucket once
    per ``(pool, worker, bucket_time)``. Defensive/fail-soft: a missing/garbled
    ``history`` yields an empty map (→ no buckets → fail-closed, no credit).
    """

    pool_id: str
    fetched_at: datetime
    address: str
    buckets_by_worker: Mapping[str, tuple[TwPoolBucket, ...]]

    def buckets_for(self, worker_name: str) -> tuple[TwPoolBucket, ...]:
        return tuple(self.buckets_by_worker.get(worker_name, ()))


# --- base delta provider (shared anti-cheat flow) ----------------------------


@dataclass(slots=True)
class _DeltaPoolEvidenceProvider:
    """Base for every real lane provider: poll -> parse -> server-read delta.

    Subclasses supply the lane's :meth:`_fetch_snapshot` (endpoint + parse +
    worker correlation). This base owns the uniform, lane-agnostic anti-cheat
    flow shared by ALL FOUR lanes so a cheater cannot pick a weak lane:

    * a per-address snapshot cache keyed by ``alice_address`` with a poll cadence,
    * worker correlation by the SERVER-ASSIGNED ``worker_name`` (falling back to
      the session's server-owned ``worker_id`` only for the trusted in-process
      caller; the client's self-named worker is never trusted),
    * delta accounting + ``(pool, worker, cursor)`` dedup via the cursor store,
    * fail-closed on EVERY error path (returns ``None`` => under_review).
    """

    pool_id: str
    #: The address/account the provider POLLS the pool with. For PRL/XMR/RVN this
    #: is the Alice wallet (== the proof's collection address); for F2Pool it is the
    #: ``mining_user_name`` (``tx_acc``), which is NOT the on-chain wallet — so
    #: ``expected_collection_address`` is set separately there.
    alice_address: str
    http_client: PoolHttpClient
    clock: object  # callable returning an aware datetime
    cursor_store: PoolShareCursorStore = field(default_factory=InMemoryPoolShareCursorStore)
    poll_cadence: timedelta = DEFAULT_POLL_CADENCE
    source_type: EvidenceSourceType = "manual"
    #: The collection address the proof must carry for this provider to attest. When
    #: ``None`` it defaults to ``alice_address`` (PRL/XMR/RVN). F2Pool sets it to the
    #: LTC wallet so the provider still binds attestation to the on-chain address it
    #: is configured for, even though it polls by account name.
    expected_collection_address: str | None = None
    _cache: dict[str, PoolAddressSnapshot] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        if not self.pool_id or not self.alice_address:
            raise ValueError("pool provider requires pool_id and alice_address")
        ensure_no_raw_secret(self.pool_id, field_name="pool_id")
        ensure_no_raw_secret(self.alice_address, field_name="alice_address")
        if self.expected_collection_address is not None:
            ensure_no_raw_secret(
                self.expected_collection_address, field_name="expected_collection_address"
            )

    @property
    def _bound_collection_address(self) -> str:
        return self.expected_collection_address or self.alice_address

    # -- lane hook ---------------------------------------------------------
    def _fetch_snapshot(self, *, now: datetime) -> PoolAddressSnapshot:
        """Fetch + parse the pool's per-address all-workers snapshot.

        Subclasses implement this. It MUST raise :class:`PoolHttpError` (or any
        exception) on failure; the base catches everything and fails closed.
        """

        raise NotImplementedError

    # -- cached poll -------------------------------------------------------
    def _snapshot(self, *, now: datetime) -> PoolAddressSnapshot | None:
        with self._lock:
            cached = self._cache.get(self.alice_address)
            if cached is not None and now - cached.fetched_at < self.poll_cadence:
                return cached
        # Poll outside the lock (network I/O); a concurrent poll only wastes one
        # request, never corrupts state.
        try:
            snapshot = self._fetch_snapshot(now=now)
        except Exception:
            # Fail-closed: unreachable / TLS / unconfigured / malformed all map to
            # "no evidence". Never raises into the credit gate.
            return None
        with self._lock:
            self._cache[self.alice_address] = snapshot
        return snapshot

    # -- correlation -------------------------------------------------------
    @staticmethod
    def _worker_name_for(session: SignedMiningSession, proof: MiningShareProof) -> str:
        # The proof's ``pool_worker_name`` is the value cross_check binds against
        # and is, on the trusted server path, set to the SERVER-ASSIGNED worker
        # name (H_a) carried on the reconstructed proof. We correlate the pool
        # snapshot against that exact value.
        return proof.pool_worker_name

    # -- evidence ----------------------------------------------------------
    def evidence_for(
        self,
        session: SignedMiningSession,
        proof: MiningShareProof,
    ) -> PoolEvidenceAuthority | None:
        # Address binding: the provider only attests for the collection address it
        # is configured to watch. A proof for a different address gets no evidence
        # (fail-closed) — the provider never vouches for an address it is not
        # configured for. (For F2Pool the bound collection address is the LTC
        # wallet, distinct from the account name it polls with.)
        if proof.alice_collection_address != self._bound_collection_address:
            return None
        if proof.pool_id != self.pool_id:
            return None
        now = _aware_now(self.clock)
        if now is None:
            return None
        snapshot = self._snapshot(now=now)
        if snapshot is None:
            return None
        worker_name = self._worker_name_for(session, proof)
        stats = snapshot.worker(worker_name)
        if stats is None or stats.accepted_shares <= 0:
            # Worker absent from the pool, or zero pool-attested accepted shares.
            return None
        try:
            decision = self.cursor_store.try_spend(
                PoolShareCursorClaim(
                    pool_id=self.pool_id,
                    worker_name=worker_name,
                    pool_accepted_count=stats.accepted_shares,
                )
            )
        except PoolShareCursorUnavailable:
            return None
        if not decision.granted:
            return None
        # The server read a NEW accepted share for this worker from the pool, so it
        # attests THIS proof's canonical hash as accepted. cross_check then matches
        # it against the proof and the rest of the authority/credit gate runs
        # unchanged.
        share_hash = canonical_share_hash(proof)
        try:
            return PoolEvidenceAuthority(
                pool_id=proof.pool_id,
                worker_name=worker_name,
                session_id=proof.session_id,
                accepted_share_hashes=(share_hash,),
                rejected_share_hashes=(),
                rejected_share_results=(),
                generated_at=snapshot.fetched_at,
                source_type=self.source_type,
                alice_collection_address=proof.alice_collection_address,
            )
        except ValueError:
            return None

    # -- credit-time reconstruction hint -----------------------------------
    def next_reconstruction_observed_at(
        self, *, pool_id: str, worker_name: str
    ) -> datetime | None:
        """No reconstruction hint for the upstream-poll lanes (XMR/RVN/LTC API).

        These lanes credit a poll-counter DELTA: the scheduler reconstructs one proof
        per cursor index at the poll's ``observed_at`` and there is no externally-fixed
        per-share clock to align to. Returning ``None`` keeps ``credit_attested_shares``
        on the EXISTING ``cursor_index=<loop index>`` + ``observed_at=<poll time>`` path
        (byte-for-byte unchanged). The self-validated :class:`ProxyPoolEvidenceProvider`
        overrides this to return the peeked share's ``validated_at``.
        """

        return None


# --- per-lane providers ------------------------------------------------------


@dataclass(slots=True)
class PearlhashPoolEvidenceProvider(_DeltaPoolEvidenceProvider):
    """PRL (pearlhash) — EPOCH-CREDIT evidence via ``GET {base}/api/account/<addr>``.

    pearlhash has NO per-share counter and NO canonical per-share hash, so the PRL
    lane CANNOT use the share-hash delta flow the other three lanes use (see
    docs/PRL-EPOCH-CREDIT-DESIGN.md §2.0). Its only per-account work signal is the
    hourly EPOCH CREDIT at ``GET /api/account/<addr>`` (the shipped
    ``/api/wallet?addr=`` path does NOT exist — it returns the SPA HTML shell, a
    404, so the old share-counting provider always credited nothing). This provider
    therefore:

    * polls ``GET {base_url}/api/account/{alice_address}`` and parses the account
      JSON into a :class:`PrlAccountSnapshot` of :class:`EpochCredit` rows (keeping
      ONLY positive ``"Epoch … credit"`` rows from ``balance_transactions[]`` —
      dropping negative ``Payment`` / ``Auto Payment`` — plus the
      ``pending_rewards.epochs[]`` ``share``/``amount`` for magnitude);
    * on each :meth:`evidence_for` call SPENDS the oldest un-spent MATURED epoch via
      the durable :class:`PrlEpochCursorStore` (keyed by
      ``(pool_id, address, epoch_label)``), emitting a
      :class:`PrlEpochEvidenceAuthority` for that one new epoch (one grant per call,
      mirroring the base's one-share-per-call semantics so the scheduler loop
      terminates when the un-spent epoch set is exhausted);
    * FAILS CLOSED on every error path (404 / transport error / missing keys /
      non-JSON → empty snapshot → no epoch → ``None`` → ``under_review``).

    It satisfies the SAME ``evidence_for`` Protocol as the share-hash lanes
    (``LaneRoutingPoolEvidenceProvider`` / the scheduler call it identically), but
    overrides ``evidence_for`` to use the epoch path and does NOT use the inherited
    share ``cursor_store`` (it uses ``epoch_cursor`` instead). PRL is PoUW, so the
    chain is ground truth; the optional chain cross-check is a clean unwired seam
    (:class:`PrlChainCrossCheck`).
    """

    #: ``base_url`` STAYS ``www.pearlhash.xyz`` (the apex/SPA host); only the PATH
    #: moves to the real ``/api/account/<addr>`` endpoint. HTTPS is enforced by the
    #: urllib client.
    base_url: str = "https://www.pearlhash.xyz"
    #: The PRL epoch cursor (spent-SET keyed by ``(pool, address, epoch_label)``).
    #: Defaults to in-memory; the deploy wires the durable JSONL store. This is
    #: SEPARATE from the inherited share ``cursor_store`` (which PRL never uses).
    epoch_cursor: PrlEpochCursorStore = field(default_factory=InMemoryPrlEpochCursorStore)
    #: The PRL pending-share SNAPSHOT store (work-weight carry across maturity). On
    #: each poll the provider records every still-PENDING epoch's ``share`` here, so
    #: when the epoch MATURES days later (its live share gone from the API) the
    #: snapshotted share is looked up and credit is WORK-WEIGHTED instead of flat.
    #: Defaults to in-memory; the deploy wires the durable JSONL store. SEPARATE from
    #: the cursor (the cursor gates dedup; this carries the magnitude).
    pending_share_store: PrlPendingShareSnapshotStore = field(
        default_factory=InMemoryPrlPendingShareSnapshotStore
    )
    #: TTL after which a snapshotted pending share is evicted even if its epoch never
    #: matured (e.g. the pool rolled it back pre-maturity). Generously past the
    #: ~12-24h ``balance_transactions`` window so a real epoch always matures within
    #: it; keeps the store bounded against pending epochs that never graduate.
    pending_share_ttl: timedelta = timedelta(days=2)
    _account_cache: dict[str, PrlAccountSnapshot] = field(default_factory=dict)

    # -- the share-hash base hook is unused on the PRL epoch path --------------
    def _fetch_snapshot(self, *, now: datetime) -> PoolAddressSnapshot:
        # PRL does not use the per-worker share snapshot; evidence_for is overridden
        # to use the epoch path. Guard against accidental use of the base flow.
        raise NotImplementedError("PRL uses the epoch-credit path, not _fetch_snapshot")

    # -- account fetch + parse (fail-soft to empty) ----------------------------
    def _fetch_account_snapshot(self, *, now: datetime) -> PrlAccountSnapshot:
        """Fetch + parse ``GET {base}/api/account/{addr}`` into epoch credits.

        Defensive at every level: any missing / wrong-shaped key yields an empty
        ``epochs`` tuple (the provider then finds no epoch and fails closed). A 404
        (never mined) raises :class:`PoolHttpError` from the client, which the
        cached-poll wrapper catches → ``None`` → ``under_review``.
        """

        url = f"{self.base_url}/api/account/{self.alice_address}"
        payload = self.http_client.get_json(url)
        if not isinstance(payload, Mapping):
            # Non-JSON / SPA-HTML / list → no signal → fail-closed (empty).
            return PrlAccountSnapshot(
                pool_id=self.pool_id,
                fetched_at=now,
                address=self.alice_address,
                connected_workers=0,
                epochs=(),
            )
        epochs = _parse_prl_account_epochs(payload)
        workers = _parse_prl_connected_workers(payload)
        return PrlAccountSnapshot(
            pool_id=self.pool_id,
            fetched_at=now,
            address=self.alice_address,
            connected_workers=len(workers),
            epochs=epochs,
            pending_shares=_parse_prl_pending_share_snapshot(payload),
            workers=workers,
        )

    def _account_snapshot(self, *, now: datetime) -> PrlAccountSnapshot | None:
        with self._lock:
            cached = self._account_cache.get(self.alice_address)
            if cached is not None and now - cached.fetched_at < self.poll_cadence:
                return cached
        try:
            snapshot = self._fetch_account_snapshot(now=now)
        except Exception:
            # Fail-closed: unreachable / TLS / 404 / malformed all map to None.
            return None
        with self._lock:
            self._account_cache[self.alice_address] = snapshot
        # Snapshot every still-PENDING epoch's work-share so it survives to the poll
        # where the epoch matures (its live share is gone by then). Fail-soft: a
        # store error here is a non-fatal fidelity miss (the epoch later degrades to
        # the flat fallback), NEVER a credit-path failure — so it must not raise.
        self._snapshot_pending_shares(snapshot, now=now)
        return snapshot

    # -- work-weight carry: snapshot pending shares + TTL-evict ----------------
    def _snapshot_pending_shares(self, snapshot: PrlAccountSnapshot, *, now: datetime) -> None:
        """Persist each PENDING epoch's share (with ``now``) + TTL-evict stale ones.

        Records the freshly-observed pending shares (last-write-wins as the share
        refines), stamping each with ``now`` so the store can DURABLY TTL-evict a
        snapshot whose epoch has not been re-seen pending within
        :attr:`pending_share_ttl` (a pending epoch that never matured — e.g. a
        pre-maturity rollback). Driving TTL off the store's own persisted timestamp
        (not a per-provider dict) keeps the bound across process restarts. The
        matured+spent prune happens in :meth:`evidence_for`; this handles the
        never-mature case. Every step is best-effort: a store error degrades
        fidelity, never the credit gate.
        """

        try:
            for label, share in snapshot.pending_shares:
                self.pending_share_store.record(
                    PrlPendingShareClaim(
                        pool_id=self.pool_id,
                        address=self.alice_address,
                        epoch_label=label,
                        share=share,
                        observed_at=now,
                    )
                )
            self.pending_share_store.prune_stale(now=now, ttl=self.pending_share_ttl)
        except Exception:
            # Best-effort fidelity only — never let snapshotting break the poll.
            return

    def _resolve_epoch_share(self, epoch: EpochCredit) -> Decimal | None:
        """Resolve a matured epoch's work-share for the credit magnitude.

        Precedence: (1) the LIVE-joined ``epoch.share`` (present only on the rare
        poll where the epoch is matured AND still pending — kept as the freshest
        source); else (2) the SNAPSHOTTED share captured while the epoch was pending
        (the normal case — by maturity the live share is gone); else (3) ``None``,
        which downstream falls back to the flat unit (graceful degradation for an
        epoch that matured before we ever saw it pending). Best-effort: a store read
        error yields ``None`` (flat fallback), never a crash.
        """

        if epoch.share is not None and epoch.share > Decimal("0"):
            return epoch.share
        try:
            return self.pending_share_store.share_for(
                pool_id=self.pool_id,
                address=self.alice_address,
                epoch_label=epoch.epoch_label,
            )
        except Exception:
            return None

    def _resolve_worker_epoch_share(
        self, epoch: EpochCredit, *, worker_name: str, snapshot: PrlAccountSnapshot
    ) -> Decimal | None:
        """The PER-WORKER work-share for a matured epoch (the per-worker split).

        pearlhash's epoch ``share`` is per-ADDRESS, so a worker's slice is DERIVED:
        ``address_epoch_share * worker_hashrate_proportion``. The proportion comes
        from the LIVE ``connected_workers[]`` hashrates at credit time (the API
        exposes no historical per-worker hashrate — this is the best available
        signal, an approximation of the work split during the epoch).

        PRECONDITION (the leak fix): the caller has ALREADY confirmed the worker is
        PRESENT in ``connected_workers[]`` via :meth:`has_connected_worker`. An ABSENT
        worker never reaches here — it collects ZERO (it did not mine PRL), gated in
        :meth:`evidence_for` BEFORE the epoch cursor is spent, so no flat unit leaks to
        a non-PRL worker (the LTC ASICs / the XMR worker).

        For a PRESENT worker this returns ``None`` (→ the FLAT per-worker unit
        downstream, never a crash) when EITHER magnitude input is missing: the address
        epoch-share is absent/zero (epoch matured before we snapshotted its share) OR
        the proportion is underivable (a connected rig at a momentary ``hashrate=0``).
        Flat PER WORKER keeps a genuine PRL worker's matured epoch strictly positive
        (the ledger's ``rewardable_score > 0`` guard passes) without over-crediting it
        the WHOLE address epoch. Otherwise returns the work-weighted slice.
        """

        address_share = self._resolve_epoch_share(epoch)
        if address_share is None or address_share <= Decimal("0"):
            return None
        proportion = snapshot.worker_hashrate_proportion(worker_name)
        if proportion is None or proportion <= Decimal("0"):
            return None
        worker_share = address_share * proportion
        if worker_share <= Decimal("0"):
            return None
        return worker_share

    # -- epoch evidence (one un-spent matured epoch per call) ------------------
    def evidence_for(
        self,
        session: SignedMiningSession,
        proof: MiningShareProof,
    ) -> PrlEpochEvidenceAuthority | None:
        # SAME address/pool binding as the base (fail-closed otherwise).
        if proof.alice_collection_address != self._bound_collection_address:
            return None
        if proof.pool_id != self.pool_id:
            return None
        now = _aware_now(self.clock)
        if now is None:
            return None
        snapshot = self._account_snapshot(now=now)
        if snapshot is None:
            return None
        worker_name = self._worker_name_for(session, proof)
        # PRL-PARTICIPATION GATE (the audit's attribution leak fix): a worker ABSENT
        # from pearlhash's live ``connected_workers[]`` did NOT mine PRL this poll, so
        # it collects ZERO PRL credit — NOT the flat per-worker unit the magnitude
        # fallback would otherwise grant. This is what keeps a NON-PRL worker (an LTC
        # ASIC or the XMR worker, whose session the proof-authority scheduler pairs
        # with EVERY configured provider including this one) out of the PRL epoch
        # credit: such a worker is never in pearlhash's connected_workers, so we emit
        # NO evidence AND never spend its epoch cursor (the cursor is preserved, so a
        # later poll where a genuinely-PRL worker of that exact name connects is
        # unaffected). Fail-closed: absence => no evidence => under_review => no credit.
        if not snapshot.has_connected_worker(worker_name):
            return None
        # Credit only MATURED epochs (doc §2.5). Order-independent: the spent-set
        # cursor handles dedup, so iteration order only affects which un-spent epoch
        # is granted first on this call — the scheduler loops until all are spent.
        for epoch in snapshot.matured_epochs():
            try:
                # PER-WORKER dedup (the audit's attribution major): the cursor is keyed
                # by ``(pool, address, worker_name, epoch_label)``, so each registered
                # worker on a SHARED address spends the SAME epoch INDEPENDENTLY — the
                # first worker no longer eats the whole per-address epoch. A given
                # (worker, epoch) is still spent at most once (no double-credit).
                decision = self.epoch_cursor.try_spend(
                    PrlEpochCursorClaim(
                        pool_id=self.pool_id,
                        address=self.alice_address,
                        epoch_label=epoch.epoch_label,
                        worker_name=worker_name,
                    )
                )
            except PoolShareCursorUnavailable:
                # Durable store write failed → deny (fail-closed; never double-credit).
                return None
            if not decision.granted:
                # This worker already credited this epoch — try the next un-spent one.
                continue
            # WORK-WEIGHTED, PER-WORKER magnitude for a PRESENT worker (absence was
            # already gated to ZERO above, before this spend): the address epoch-share
            # (live, else the share we snapshotted while pending) split by THIS worker's
            # hashrate proportion. ``None`` → flat per-worker unit (epoch matured before
            # we saw its share, or the connected rig momentarily reports hashrate=0 so
            # its proportion is underivable) — a graceful-degradation magnitude for a
            # GENUINE PRL worker, never a leak. The spent-set already granted, so this
            # only sets the magnitude — never gates.
            epoch_share = self._resolve_worker_epoch_share(
                epoch, worker_name=worker_name, snapshot=snapshot
            )
            try:
                authority = PrlEpochEvidenceAuthority(
                    pool_id=proof.pool_id,
                    worker_name=worker_name,
                    session_id=proof.session_id,
                    alice_collection_address=proof.alice_collection_address,
                    epoch_label=epoch.epoch_label,
                    epoch_share=epoch_share,
                    epoch_amount=epoch.amount,
                    generated_at=snapshot.fetched_at,
                    source_type=self.source_type,
                )
            except (ValueError, TypeError):
                return None
            # NOTE (per-worker fix): we do NOT eagerly prune the pending-share snapshot
            # here. The snapshot is keyed PER-ADDRESS (the per-address epoch share) and
            # is SHARED by every registered worker on this address — pruning it on the
            # FIRST worker's spend would starve the LATER workers of the work-weight
            # (they would all fall back to the flat unit, re-introducing an unfair
            # split). The store stays bounded by DURABLE TTL eviction instead: once an
            # epoch matures it stops appearing in ``pending_rewards`` and so stops being
            # re-recorded, its ``observed_at`` stops advancing, and
            # :meth:`PrlPendingShareSnapshotStore.prune_stale` evicts it after
            # :attr:`pending_share_ttl` (generously past the ~12-24h maturation window).
            return authority
        # No un-spent matured epoch → no new credit → under_review (fail-closed).
        return None


@dataclass(slots=True)
class TwPoolPearlEvidenceProvider(PearlhashPoolEvidenceProvider):
    """PRL (tw-pool.com) — per-(worker, time-bucket) SHARE evidence via worker_stats.

    The DEPLOYED PRL evidence provider (owner chose tw-pool.com over the interim
    pearlhash design; see docs/twpool-prl-integration.md). tw-pool is a STANDARD
    multi-algo pool whose ``GET /api/worker_stats`` breaks work out PER WORKER
    natively, so — unlike the per-ADDRESS pearlhash epoch model this subclasses — the
    credit signal is the per-(worker, time-bucket) valid ``shares``. This provider:

    * polls ``GET {base}/api/worker_stats?address=<addr>&pool=pearl&mode=hour&
      excludeWorker=false`` and parses ``history["<addr>.<worker>"]`` into per-worker
      :class:`TwPoolBucket` rows (``{time, hashrate, shares, invalidshares}``);
    * on each :meth:`evidence_for` call spends the oldest un-spent CREDITABLE bucket
      for the proof's worker via the durable PRL epoch cursor, keyed
      ``(pool_id, address, worker_name, epoch_label=str(bucket_time))`` — so each
      (worker, bucket_time) is credited EXACTLY ONCE (rolling-window-safe: re-polling
      the same window re-sees spent labels and grants nothing; a new bucket grants
      once). A naive sum/delta of ``shares`` is NOT used (the window slides);
    * carries the bucket's ``shares * TWPOOL_PRL_SHARE_SCALE`` as the authority's
      ``epoch_share`` (the magnitude), SKIPS buckets with ``invalidshares > 0``, and
      emits the SAME :class:`PrlEpochEvidenceAuthority` the pearlhash path emits — so
      :func:`cross_check_prl_epoch_authority` (hash-free) + the authority evaluator's
      PRL branch + ``prl_epoch_rewardable_score`` carry it through UNCHANGED;
    * FAILS CLOSED on every error path: ``{"result":"error"}`` / missing ``history``
      / HTTP error / 404 / non-JSON → empty snapshot → no bucket → ``None`` →
      ``under_review``.

    It is a SIBLING provider/cursor for PRL only: it does NOT touch the XMR/RVN/LTC
    share-hash paths, and (being a :class:`PearlhashPoolEvidenceProvider` subclass) it
    still satisfies the ``isinstance(provider, PearlhashPoolEvidenceProvider)`` PRL
    lane-binding check in the http_app target builder and the ``_LANE_PROVIDER_CLASSES``
    PRL branch — no routing/scheduler change. The inherited pearlhash account-poll
    fields (``pending_share_store`` etc.) are UNUSED on this path (tw-pool's ``shares``
    is inline per bucket — there is no pending/matured split to carry across). CREDIT-
    ONLY: sets no reward/payout/chain symbol; ``paid_acu`` untouched; the cursor JSONL
    stores only the opaque ``(pool, address, worker, bucket_time)`` tuple.
    """

    #: tw-pool's JSON API lives on the ``api.`` subdomain (the ``www`` host serves the
    #: Vite SPA shell, not JSON). HTTPS is enforced by the urllib client.
    base_url: str = "https://api.tw-pool.com"
    #: SHARE_SCALE for this lane (see :data:`TWPOOL_PRL_SHARE_SCALE`). Carried as a
    #: field so a deploy / test can retune the magnitude without touching the module
    #: constant. OWNER INPUT NEEDED (V): confirm the magnitude at deploy.
    share_scale: Decimal = TWPOOL_PRL_SHARE_SCALE
    #: tw-pool's own per-address snapshot cache (a different shape than the inherited
    #: pearlhash ``_account_cache``); keyed by ``alice_address`` with the poll cadence.
    _twpool_cache: dict[str, TwPoolWorkerStatsSnapshot] = field(default_factory=dict)

    # -- worker_stats fetch + parse (fail-soft to empty) -----------------------
    def _fetch_worker_stats_snapshot(self, *, now: datetime) -> TwPoolWorkerStatsSnapshot:
        """Fetch + parse ``GET {base}/api/worker_stats`` into per-worker buckets.

        Defensive at every level: ``{"result":"error"}`` / a missing or wrong-shaped
        ``history`` yields an empty ``buckets_by_worker`` (the provider then finds no
        bucket and fails closed). An HTTP error / 404 raises :class:`PoolHttpError`
        from the client, which the cached-poll wrapper catches → ``None`` →
        ``under_review``.
        """

        params = urllib.parse.urlencode(
            {
                "address": self.alice_address,
                "pool": TWPOOL_PRL_POOL_PARAM,
                "mode": "hour",
                "excludeWorker": "false",
            }
        )
        url = f"{self.base_url}/api/worker_stats?{params}"
        payload = self.http_client.get_json(url)
        buckets = _parse_twpool_worker_stats(payload, address=self.alice_address)
        return TwPoolWorkerStatsSnapshot(
            pool_id=self.pool_id,
            fetched_at=now,
            address=self.alice_address,
            buckets_by_worker=buckets,
        )

    def _worker_stats_snapshot(self, *, now: datetime) -> TwPoolWorkerStatsSnapshot | None:
        with self._lock:
            cached = self._twpool_cache.get(self.alice_address)
            if cached is not None and now - cached.fetched_at < self.poll_cadence:
                return cached
        try:
            snapshot = self._fetch_worker_stats_snapshot(now=now)
        except Exception:
            # Fail-closed: unreachable / TLS / 404 / {"result":"error"} / malformed
            # all map to None (no evidence). Never raises into the credit gate.
            return None
        with self._lock:
            self._twpool_cache[self.alice_address] = snapshot
        return snapshot

    # -- advisory pool-agrees health signal (OFF the per-worker credit hot path) --
    def fetch_balance_health(self, *, now: datetime | None = None) -> bool | None:
        """ADVISORY ONLY: does tw-pool's ``getBalance`` agree the address has earned?

        Polls ``GET {base}/api/getBalance?address=<addr>&pool=pearl`` → ``{totalHeld,
        totalPaid}`` and returns ``True`` when their sum is strictly positive (the pool
        agrees the Alice address has earned PRL), ``False`` when zero, ``None`` on any
        error / garbled body. This is a corroborating HEALTH signal for the audit
        surface; it is DELIBERATELY NOT ANDed into the per-(worker, bucket) credit
        grant (that stays gated on worker_stats alone), so getBalance latency/outage
        can never starve the hot path. Never raises.
        """

        try:
            params = urllib.parse.urlencode(
                {"address": self.alice_address, "pool": TWPOOL_PRL_POOL_PARAM}
            )
            payload = self.http_client.get_json(f"{self.base_url}/api/getBalance?{params}")
        except Exception:
            return None
        if not isinstance(payload, Mapping):
            return None
        held = _coerce_decimal(payload.get("totalHeld"))
        paid = _coerce_decimal(payload.get("totalPaid"))
        if held is None and paid is None:
            return None
        total = (held or Decimal("0")) + (paid or Decimal("0"))
        return total > Decimal("0")

    # -- the inherited pearlhash account/share hooks are unused on this path ------
    def _fetch_account_snapshot(self, *, now: datetime) -> PrlAccountSnapshot:
        # tw-pool uses the worker_stats per-bucket path, not pearlhash's /api/account.
        raise NotImplementedError("tw-pool uses worker_stats, not /api/account")

    # -- per-(worker, bucket) evidence (one un-spent bucket per call) ------------
    def evidence_for(
        self,
        session: SignedMiningSession,
        proof: MiningShareProof,
    ) -> PrlEpochEvidenceAuthority | None:
        # SAME address/pool binding as the base (fail-closed otherwise).
        if proof.alice_collection_address != self._bound_collection_address:
            return None
        if proof.pool_id != self.pool_id:
            return None
        now = _aware_now(self.clock)
        if now is None:
            return None
        snapshot = self._worker_stats_snapshot(now=now)
        if snapshot is None:
            return None
        worker_name = self._worker_name_for(session, proof)
        if not worker_name:
            return None
        # tw-pool breaks out per worker natively, so participation == "this worker has a
        # history series in worker_stats". A worker with NO buckets did not mine PRL on
        # tw-pool → ZERO credit (no evidence, no cursor spend) — the same fail-closed
        # attribution posture as the pearlhash connected_workers gate, and what keeps a
        # non-PRL worker (an LTC/XMR worker mis-paired by the scheduler) out of PRL
        # credit (it is never a key in tw-pool's history).
        buckets = snapshot.buckets_for(worker_name)
        if not buckets:
            return None
        # Credit the oldest un-spent CREDITABLE bucket first (skip invalidshares>0 and
        # non-positive-shares buckets). Order-independent: the spent-SET cursor handles
        # dedup, so the scheduler loops until every un-spent bucket for this worker is
        # granted. A NAIVE sum/delta is intentionally avoided — the window slides.
        for bucket in sorted(buckets, key=lambda b: b.bucket_time):
            if not bucket.creditable:
                continue
            try:
                # Per-(pool, address, worker, bucket_time) dedup via the SAME PRL epoch
                # cursor (epoch_label == the bucket's stable time string). Each
                # (worker, bucket_time) is spent at most once; re-polling the rolling
                # window never double-credits a bucket already in the spent set.
                decision = self.epoch_cursor.try_spend(
                    PrlEpochCursorClaim(
                        pool_id=self.pool_id,
                        address=self.alice_address,
                        epoch_label=bucket.epoch_label,
                        worker_name=worker_name,
                    )
                )
            except PoolShareCursorUnavailable:
                # Durable store write failed → deny (fail-closed; never double-credit).
                return None
            if not decision.granted:
                # This (worker, bucket) already credited — try the next un-spent one.
                continue
            # Magnitude = the bucket's valid shares, scaled. prl_epoch_rewardable_score
            # then multiplies by PRL_EPOCH_SHARE_SCALE; with the default share_scale the
            # net score == ``shares`` (one unit per valid share). epoch_share is always
            # strictly positive here (creditable ⇒ shares > 0), so the score never falls
            # back to the flat unit.
            epoch_share = bucket.shares * self.share_scale
            try:
                authority = PrlEpochEvidenceAuthority(
                    pool_id=proof.pool_id,
                    worker_name=worker_name,
                    session_id=proof.session_id,
                    alice_collection_address=proof.alice_collection_address,
                    epoch_label=bucket.epoch_label,
                    epoch_share=epoch_share,
                    # epoch_amount is audit-only and never the magnitude; tw-pool's
                    # per-bucket signal carries no PRL token amount, so report 0.
                    epoch_amount=Decimal("0"),
                    generated_at=snapshot.fetched_at,
                    source_type=self.source_type,
                )
            except (ValueError, TypeError):
                return None
            return authority
        # No un-spent creditable bucket for this worker → no new credit → under_review.
        return None


@dataclass(slots=True)
class ProxyPoolEvidenceProvider(_DeltaPoolEvidenceProvider):
    """Self-validated share-hash lane — Alice's OWN re-hash is the authority (doc §2.4).

    The proxy-pool credit model for the share-hash legs (XMR/RVN/LTC): instead of
    polling an upstream pool's per-worker accepted-COUNT (the
    :class:`_DeltaPoolEvidenceProvider` subclasses), this provider DRAINS a durable
    :class:`ValidatedShareStore` for the proof's ``(pool_id, worker_name)`` and emits
    a :class:`SelfValidatedShareAuthority` carrying Alice's OWN
    ``canonical_share_hash`` + the validated ``share_difficulty``. Credit is thus
    gated on Alice's re-hash, never an upstream count (doc §2.2/§3 Q1). It satisfies
    the SAME ``evidence_for`` Protocol as the other lanes (the
    :class:`LaneRoutingPoolEvidenceProvider` / scheduler call it identically) and is
    registered per share-hash lane in :func:`build_lane_routing_provider`.

    The TRANSPORT/validator that POPULATES the store is Milestone 1 (out of scope
    here) — this is the seam. FAIL-CLOSED: no un-spent validated share for
    ``(pool, worker)`` → ``evidence_for`` returns ``None`` → ``under_review`` (the
    clean loop terminator in ``credit_attested_shares``). Store-unavailable → deny.
    CREDIT-ONLY: sets no reward/payout/chain symbol; ``paid_acu`` untouched; the
    store holds only the opaque §2.4 fields.

    It does NOT use the inherited upstream ``cursor_store`` / ``_fetch_snapshot``
    poll path (it has no upstream poll); ``http_client`` is required by the base but
    is never called (a no-op fake is fine).
    """

    validated_share_store: ValidatedShareStore = field(
        default_factory=InMemoryValidatedShareStore
    )

    # The upstream poll hook is unused on the self-validated path; guard against
    # accidental use of the base poll flow.
    def _fetch_snapshot(self, *, now: datetime) -> PoolAddressSnapshot:
        raise NotImplementedError("ProxyPoolEvidenceProvider drains the ValidatedShareStore")

    def evidence_for(
        self,
        session: SignedMiningSession,
        proof: MiningShareProof,
    ) -> SelfValidatedShareAuthority | None:
        # SAME address/pool binding as the base (fail-closed otherwise): the provider
        # only attests for the collection address + pool it is configured to watch.
        if proof.alice_collection_address != self._bound_collection_address:
            return None
        if proof.pool_id != self.pool_id:
            return None
        worker_name = self._worker_name_for(session, proof)
        try:
            share = self.validated_share_store.drain_one(
                pool_id=self.pool_id, worker_name=worker_name
            )
        except PoolShareCursorUnavailable:
            # Durable store read/write failed → deny (fail-closed; never double-credit).
            return None
        if share is None:
            # No un-spent validated share for this (pool, worker) → fail-closed.
            return None
        try:
            return SelfValidatedShareAuthority(
                pool_id=proof.pool_id,
                worker_name=worker_name,
                session_id=proof.session_id,
                alice_collection_address=proof.alice_collection_address,
                # Alice's OWN re-hashed hash for THIS share (the validator's value),
                # which the self-validated gate binds against the proof's recomputed
                # canonical_share_hash.
                canonical_share_hash=share.canonical_share_hash,
                share_difficulty=share.share_difficulty,
                generated_at=share.validated_at,
                source_type=self.source_type,
            )
        except (ValueError, TypeError):
            return None

    # -- credit-time reconstruction hint -----------------------------------
    def next_reconstruction_observed_at(
        self, *, pool_id: str, worker_name: str
    ) -> datetime | None:
        """The ``observed_at`` the NEXT un-spent share's canonical hash was bound at.

        The validator carries each share's reconstruction ``observed_at`` AS its
        ``validated_at`` (it reconstructed the carried ``canonical_share_hash`` at
        ``cursor_index=0`` + that instant). The credit server reads this hint BEFORE
        draining and reconstructs the proof at exactly this ``observed_at`` (cursor 0),
        so ``cross_check_self_validated_share``'s recompute-and-compare matches even
        though the credit server's own tick clock differs (cross-process). Uses
        :meth:`ValidatedShareStore.peek_next` (NON-consuming) so the share the hint
        describes is the SAME one the subsequent ``evidence_for`` drain spends. Returns
        ``None`` (→ the unchanged poll path) when no un-spent share remains or the
        store is unavailable (fail-closed; ``evidence_for`` then also yields ``None``).
        """

        try:
            share = self.validated_share_store.peek_next(
                pool_id=self.pool_id, worker_name=worker_name
            )
        except PoolShareCursorUnavailable:
            return None
        if share is None:
            return None
        return share.validated_at


@dataclass(slots=True)
class SupportXmrPoolEvidenceProvider(_DeltaPoolEvidenceProvider):
    """XMR (supportxmr) — per-worker accepted shares via the REAL cryptonote-pool API.

    The real www.supportxmr.com (cryptonote-pool) API does NOT expose per-worker
    accepted-share counts on a single all-workers call. Confirmed against live
    responses:

    * ``GET /api/miner/<addr>/stats/allWorkers`` -> ``{"global": {"lts","identifer",
      "hash","totalHash"}, ...}`` — only the ADDRESS aggregate; NO ``validShares``,
      so it is USELESS for per-worker accepted shares (the previous implementation
      parsed a non-existent ``perWorkerStats`` list, always got nothing, and
      credited zero — fail-closed but silently wrong).
    * ``GET /api/miner/<addr>/identifiers`` -> ``["alc-w-...", ...]`` — the list of
      this address's worker identifiers.
    * ``GET /api/miner/<addr>/stats/<identifier>`` -> ``{"lts","identifer","hash",
      "totalHash","validShares":N,"invalidShares":M}`` — the PER-WORKER stats, where
      ``validShares`` is the cumulative accepted-share count (and ``hash`` is the
      current hashrate). Only share counts are read.

    So the snapshot is built by fanning out: one ``/identifiers`` call, then one
    ``/stats/<identifier>`` call per worker, correlating by the server-assigned
    worker identifier (the H_a ``worker_name``, e.g. ``alc-w-...``).

    RATE-LIMIT-AT-SCALE CAVEAT: this costs ``1 + N`` upstream requests per poll
    cadence per address (``N`` = number of workers on the address). supportxmr
    rate-limits ~100 requests / 15 min / IP (with a ~1-min server cache). The
    snapshot cache means a BURST of proofs within one cadence still costs only
    ``1 + N`` requests, but at scale (many workers and/or many addresses) the poll
    cadence must be RAISED (or the address set SHARDED across cadence windows / IPs)
    so the fan-out stays under the rate limit. This is NOT solved here — it is a
    deploy-time tuning knob (``poll_cadence`` / target sharding).
    """

    # NOTE: the canonical host is www.supportxmr.com. The apex (supportxmr.com)
    # answers /api/... with a 301 to www that drops the path, so polling the apex
    # yields no JSON (the H_b poll then reads zero shares and credits nothing).
    # Point directly at www so the per-worker endpoints return 200 + JSON.
    base_url: str = "https://www.supportxmr.com"

    def _fetch_snapshot(self, *, now: datetime) -> PoolAddressSnapshot:
        identifiers = self.http_client.get_json(
            f"{self.base_url}/api/miner/{self.alice_address}/identifiers"
        )
        if not isinstance(identifiers, list):
            # /identifiers is documented (and observed) to be a JSON array. Any other
            # shape (error object, null, ...) => no workers => fail-closed.
            return PoolAddressSnapshot(pool_id=self.pool_id, fetched_at=now, workers=())
        # Build a name->stats map keyed by worker identifier, then hand it to the
        # shared parser so the produced WorkerPoolStats objects match every other
        # lane's (and so snapshot.worker(name) / evidence_for read them identically).
        stats_by_identifier: dict[str, Mapping[str, Any]] = {}
        for identifier in identifiers:
            # Skip the address aggregate and any non-string / empty entry; "global"
            # is the address total, not a real worker.
            if not isinstance(identifier, str) or not identifier or identifier == "global":
                continue
            try:
                worker_payload = self.http_client.get_json(
                    f"{self.base_url}/api/miner/{self.alice_address}/stats/{identifier}"
                )
            except Exception:
                # A single per-worker fetch that fails/garbles must NOT wedge the
                # others: skip this worker (fail-closed for it alone) and continue.
                continue
            if isinstance(worker_payload, Mapping):
                stats_by_identifier[identifier] = worker_payload
        workers = _parse_named_workers(
            stats_by_identifier,
            accepted_keys=("validShares", "valid_shares", "accepted_shares", "shares"),
            hashrate_keys=("hash", "hashrate", "hash_rate"),
        )
        return PoolAddressSnapshot(pool_id=self.pool_id, fetched_at=now, workers=workers)


@dataclass(slots=True)
class RavenminerPoolEvidenceProvider(_DeltaPoolEvidenceProvider):
    """RVN (ravenminer) — per-worker accepted shares via the REAL public tRPC API.

    ravenminer's real, public, UNAUTHENTICATED data source is a tRPC endpoint on
    the ``www`` host. Confirmed against the live API:

    * ``GET https://www.ravenminer.com/api/trpc/getWorkers?input=<url-encoded JSON>``
      where the input is the superjson-wrapped request
      ``{"json": {"coinId": <coinId>, "address": <addr>}}`` (``coinId`` = 1 for
      RVN/ravencoin, a constant; ``address`` is the Alice pool wallet).
    * The response is a superjson/tRPC envelope; the worker list is at
      ``result.data.json`` — a ``list[dict]`` like
      ``[{"id":"1","name":"<worker>","shares":"<N>","hr":<float>,"online":1}, ...]``.
      ``shares`` is a superjson bigint serialized as a numeric STRING (the shared
      int-coercion handles numeric strings); it is the cumulative MONOTONIC
      accepted-share counter that drives credit. ``hr`` is the current hashrate
      (advisory only). Workers are name-keyed and PERSIST while a rig is offline,
      so the server-read counter keeps correlating after a disconnect.

    The snapshot is built by unwrapping the envelope defensively (any missing /
    wrong-shaped level => empty snapshot => fail-closed) and handing the worker
    list to the shared parser, correlating by the server-assigned ``worker_name``.

    NOTE: the canonical host is ``www.ravenminer.com``. The apex
    (``ravenminer.com``) answers from the prod egress IP with a 522 (origin
    unreachable) — the SAME trap as supportxmr — so polling the apex yields no
    JSON (the H_b poll then reads zero shares and credits nothing). Point directly
    at ``www`` so the tRPC endpoint returns 200 + the superjson envelope.
    """

    base_url: str = "https://www.ravenminer.com"
    #: RVN / ravencoin coin id on the ravenminer tRPC API (a constant).
    coin_id: int = 1

    def _fetch_snapshot(self, *, now: datetime) -> PoolAddressSnapshot:
        inp = json.dumps(
            {"json": {"coinId": self.coin_id, "address": self.alice_address}},
            separators=(",", ":"),
        )
        url = f"{self.base_url}/api/trpc/getWorkers?input={urllib.parse.quote(inp)}"
        payload = self.http_client.get_json(url)
        # Unwrap the superjson/tRPC envelope: result.data.json -> list[dict]. Walk
        # it defensively; any missing / non-Mapping / non-list level fails closed
        # to an empty snapshot (mirrors the supportxmr shape guards).
        node: Any = payload
        for key in ("result", "data", "json"):
            if not isinstance(node, Mapping):
                return PoolAddressSnapshot(pool_id=self.pool_id, fetched_at=now, workers=())
            node = node.get(key)
        if not isinstance(node, list):
            return PoolAddressSnapshot(pool_id=self.pool_id, fetched_at=now, workers=())
        workers = _parse_named_workers(
            node,
            name_keys=("name",),
            accepted_keys=("shares",),
            hashrate_keys=("hr",),
        )
        return PoolAddressSnapshot(pool_id=self.pool_id, fetched_at=now, workers=workers)


#: Environment variable the F2Pool API secret is injected through. The secret is
#: NEVER hardcoded in code, fixtures, or tests; absent => fail-closed.
F2POOL_API_SECRET_ENV = "ALICE_F2POOL_API_SECRET"

#: F2Pool's secret HTTP header name.
F2POOL_API_SECRET_HEADER = "F2P-API-SECRET"


@dataclass(slots=True)
class F2PoolEvidenceProvider(_DeltaPoolEvidenceProvider):
    """LTC (F2Pool) — ``POST https://api.f2pool.com/v2/hash_rate/worker/list``.

    Body ``{"mining_user_name": "<tx_acc>", "currency": "litecoin"}``; the API
    secret is sent in the ``F2P-API-SECRET`` header. The secret is read from the
    injected env var :data:`F2POOL_API_SECRET_ENV` (``ALICE_F2POOL_API_SECRET``)
    at call time — it is NEVER stored on the instance, hardcoded, or written to
    any audit surface. If the env var is absent/blank, :meth:`_fetch_snapshot`
    raises before any request, so the lane fails closed (under_review).

    Here ``alice_address`` is the F2Pool ``mining_user_name`` (``tx_acc``). The
    response lists workers with a cumulative accepted-share count; we correlate by
    worker name.
    """

    base_url: str = "https://api.f2pool.com"
    currency: str = "litecoin"
    #: Injected for tests so the secret source is overridable WITHOUT a real token
    #: ever appearing in code/tests; defaults to reading the process environment.
    secret_provider: object = os.environ.get

    def _api_secret(self) -> str | None:
        getter = self.secret_provider
        value = getter(F2POOL_API_SECRET_ENV) if callable(getter) else None
        if not value or not str(value).strip():
            return None
        return str(value)

    def _fetch_snapshot(self, *, now: datetime) -> PoolAddressSnapshot:
        secret = self._api_secret()
        if secret is None:
            # Unconfigured secret => fail closed; never poll without the token.
            raise PoolHttpError("f2pool_api_secret_missing")
        url = f"{self.base_url}/v2/hash_rate/worker/list"
        payload = self.http_client.post_json(
            url,
            body={"mining_user_name": self.alice_address, "currency": self.currency},
            headers={F2POOL_API_SECRET_HEADER: secret},
        )
        # F2Pool v2 returns {"workers": [{"hash_rate_info": {"name": "..."}, ...}]}
        # or a flatter list; tolerate common shapes via the shared parser.
        workers = _parse_named_workers(
            _extract(payload, "workers", "worker_list", "hash_rate"),
            name_keys=("name", "worker_name", "hash_rate_info.name", "worker"),
            accepted_keys=(
                "accepted_shares",
                "shares",
                "share_count",
                "validShares",
                "valid_shares",
            ),
            hashrate_keys=("hash_rate", "hashrate", "hash_rate_info.hash_rate"),
        )
        return PoolAddressSnapshot(pool_id=self.pool_id, fetched_at=now, workers=workers)


# --- lane routing + config factory -------------------------------------------


@dataclass(slots=True)
class LaneRoutingPoolEvidenceProvider:
    """Route a proof to the configured provider for its lane (anti-cheat parity).

    A proof's lane is implied by its ``pool_id`` (each lane has a distinct Alice
    pool address + ``pool_id``). This router holds one real provider per
    configured lane and dispatches by ``pool_id``; a proof whose ``pool_id`` has
    no configured provider gets NO evidence (fail-closed). Because the SAME
    server-read-delta provider backs every lane, a cheater cannot pick a weak
    lane: an unconfigured lane is fail-closed, and a configured one is gated on
    the pool's own per-worker attestation.
    """

    providers_by_pool_id: dict[str, _DeltaPoolEvidenceProvider] = field(default_factory=dict)

    def register(self, provider: _DeltaPoolEvidenceProvider) -> None:
        self.providers_by_pool_id[provider.pool_id] = provider

    def evidence_for(
        self,
        session: SignedMiningSession,
        proof: MiningShareProof,
    ) -> PoolEvidenceAuthority | PrlEpochEvidenceAuthority | SelfValidatedShareAuthority | None:
        provider = self.providers_by_pool_id.get(proof.pool_id)
        if provider is None:
            # No real provider configured for this lane => fail-closed.
            return None
        return provider.evidence_for(session, proof)

    def next_reconstruction_observed_at(
        self, *, pool_id: str, worker_name: str
    ) -> datetime | None:
        """Route the credit-time reconstruction hint to the per-pool provider.

        ``credit_attested_shares`` calls this (by ``pool_id`` / ``worker_name``) BEFORE
        reconstructing a proof. We dispatch to the same per-lane provider ``evidence_for``
        routes to: the self-validated :class:`ProxyPoolEvidenceProvider` returns the next
        un-spent share's ``validated_at`` (so the credit server reconstructs at the
        validator's exact ``observed_at``); the upstream-poll / PRL providers return
        ``None`` (the unchanged cursor/poll path). An unconfigured lane or a routed
        provider lacking the method returns ``None`` (fail-soft → the existing path).
        """

        provider = self.providers_by_pool_id.get(pool_id)
        if provider is None:
            return None
        hint = getattr(provider, "next_reconstruction_observed_at", None)
        if hint is None:
            return None
        return hint(pool_id=pool_id, worker_name=worker_name)


@dataclass(frozen=True, slots=True)
class LaneProviderConfig:
    """Per-lane wiring for a real provider (one of the four)."""

    #: One of "prl" / "xmr" / "rvn" / "ltc".
    lane_kind: str
    #: The ``pool_id`` carried on this lane's sessions/proofs (used for routing).
    pool_id: str
    #: The address/account the provider POLLS with (PRL/XMR/RVN wallet, or F2Pool
    #: mining_user_name / ``tx_acc``).
    alice_address: str
    #: The collection address the proof must carry to be attested. Leave ``None``
    #: for PRL/XMR/RVN (defaults to ``alice_address``); set the LTC wallet for the
    #: F2Pool lane, where the poll account differs from the on-chain address.
    expected_collection_address: str | None = None


_LANE_PROVIDER_CLASSES: dict[str, type[_DeltaPoolEvidenceProvider]] = {
    # PRL deploys to tw-pool.com (the owner-chosen pool). TwPoolPearlEvidenceProvider
    # subclasses PearlhashPoolEvidenceProvider, so the PRL lane-binding isinstance
    # check + the PRL build branch below still recognize it as the PRL provider.
    "prl": TwPoolPearlEvidenceProvider,
    "xmr": SupportXmrPoolEvidenceProvider,
    "rvn": RavenminerPoolEvidenceProvider,
    # The Quai lane is KawPoW like RVN. On the proxy-pool path (the Quai deploy) the
    # registered provider is the self-validated ``ProxyPoolEvidenceProvider`` (keyed by
    # pool_id), so this poll class is NOT instantiated; it is the non-proxy upstream-poll
    # fallback only (the Quai deploy always wires the validated-share store, so it never
    # reaches the 2Miners API — a dedicated 2Miners poll client is out of scope here).
    "quai": RavenminerPoolEvidenceProvider,
    "ltc": F2PoolEvidenceProvider,
}


def build_lane_routing_provider(
    lane_configs: tuple[LaneProviderConfig, ...],
    *,
    http_client: PoolHttpClient,
    clock: object,
    cursor_store: PoolShareCursorStore | None = None,
    poll_cadence: timedelta = DEFAULT_POLL_CADENCE,
    prl_epoch_cursor: PrlEpochCursorStore | None = None,
    prl_pending_share_store: PrlPendingShareSnapshotStore | None = None,
    validated_share_store: ValidatedShareStore | None = None,
) -> LaneRoutingPoolEvidenceProvider:
    """Build a :class:`LaneRoutingPoolEvidenceProvider` from per-lane config.

    An empty ``lane_configs`` yields a router that always fails closed (no lane
    configured), preserving the production default of "no evidence => under_review"
    until an operator wires real addresses. The share ``cursor_store`` is SHARED
    across the share-hash lanes (XMR/RVN/LTC) so the ``(pool, worker, cursor)``
    budget is global and durable. The PRL lane instead uses ``prl_epoch_cursor`` (a
    spent-SET keyed by ``(pool, address, epoch_label)``) PLUS the
    ``prl_pending_share_store`` (the work-weight snapshot carried across maturity);
    all default to in-memory and the deploy passes durable JSONL stores.

    Milestone 0 (doc §2.4): when ``validated_share_store`` is supplied, the
    share-hash lanes (XMR/RVN/LTC) are wired with the SELF-VALIDATED
    :class:`ProxyPoolEvidenceProvider` (Alice's own re-hash is the authority,
    draining that store) INSTEAD of the upstream-poll providers — the proxy-pool
    credit model. PRL always rides the epoch-credit provider regardless. With no
    ``validated_share_store`` the historical upstream-poll providers are wired
    (unchanged), so this is purely additive.
    """

    store = cursor_store or InMemoryPoolShareCursorStore()
    epoch_store = prl_epoch_cursor or InMemoryPrlEpochCursorStore()
    pending_share_store = prl_pending_share_store or InMemoryPrlPendingShareSnapshotStore()
    router = LaneRoutingPoolEvidenceProvider()
    for cfg in lane_configs:
        provider_cls = _LANE_PROVIDER_CLASSES.get(cfg.lane_kind)
        if provider_cls is None:
            raise ValueError(f"unknown lane_kind {cfg.lane_kind!r}")
        if issubclass(provider_cls, PearlhashPoolEvidenceProvider):
            # PRL is the epoch-credit lane (tw-pool deploy = TwPoolPearlEvidenceProvider,
            # a PearlhashPoolEvidenceProvider subclass): wire the epoch cursor, not the
            # share cursor (which it never uses). It binds collection == alice_address.
            # The inherited pending_share_store is harmless on the tw-pool path (unused).
            router.register(
                provider_cls(
                    pool_id=cfg.pool_id,
                    alice_address=cfg.alice_address,
                    http_client=http_client,
                    clock=clock,
                    poll_cadence=poll_cadence,
                    expected_collection_address=cfg.expected_collection_address,
                    epoch_cursor=epoch_store,
                    pending_share_store=pending_share_store,
                )
            )
            continue
        if validated_share_store is not None:
            # Proxy-pool self-validated lane (doc §2.4): credit is gated on Alice's
            # OWN re-hash drained from the shared validated-share store, not an
            # upstream count. Same (pool/worker/session/collection) binding.
            router.register(
                ProxyPoolEvidenceProvider(
                    pool_id=cfg.pool_id,
                    alice_address=cfg.alice_address,
                    http_client=http_client,
                    clock=clock,
                    poll_cadence=poll_cadence,
                    expected_collection_address=cfg.expected_collection_address,
                    validated_share_store=validated_share_store,
                )
            )
            continue
        router.register(
            provider_cls(
                pool_id=cfg.pool_id,
                alice_address=cfg.alice_address,
                http_client=http_client,
                clock=clock,
                cursor_store=store,
                poll_cadence=poll_cadence,
                expected_collection_address=cfg.expected_collection_address,
            )
        )
    return router


# --- env-driven deploy wiring ------------------------------------------------

#: Per-lane env vars carrying the Alice pool ADDRESS + the lane's ``pool_id``. The
#: deployed edge sets these to the owner-known addresses; absent => that lane stays
#: fail-closed (no provider configured). NONE of these is a secret — they are
#: public pool addresses / account names. The F2Pool API SECRET is read separately
#: at poll time from :data:`F2POOL_API_SECRET_ENV` and is NEVER in config.
LANE_ENV_SPECS: tuple[tuple[str, str, str, str | None], ...] = (
    # (lane_kind, address_env, pool_id_env, expected_collection_address_env)
    ("prl", "ALICE_PRL_POOL_ADDRESS", "ALICE_PRL_POOL_ID", None),
    ("xmr", "ALICE_XMR_POOL_ADDRESS", "ALICE_XMR_POOL_ID", None),
    ("rvn", "ALICE_RVN_POOL_ADDRESS", "ALICE_RVN_POOL_ID", None),
    # The Quai (KawPoW) lane mirrors the RVN lane: its OWN pool address + pool_id
    # (``quai`` by deploy convention) so Quai-lane shares credit SEPARATELY from RVN's
    # ``ravenminer``. On the proxy-pool path (the Quai deploy) the lane is the
    # self-validated ``ProxyPoolEvidenceProvider`` draining the shared store by THIS
    # pool_id; the upstream-poll fallback class is only used when no proxy store is wired.
    ("quai", "ALICE_QUAI_POOL_ADDRESS", "ALICE_QUAI_POOL_ID", None),
    # F2Pool polls by mining_user_name (tx_acc) but binds attestation to the LTC
    # wallet (the on-chain collection address).
    ("ltc", "ALICE_F2POOL_MINING_USER_NAME", "ALICE_LTC_POOL_ID", "ALICE_LTC_COLLECTION_ADDRESS"),
)


#: NEW env (M0/M1 proxy-pool credit plane): the path of the SHARED validated-share
#: JSONL the TRANSPORT SERVICE appends to and the credit server DRAINS. When set, the
#: share-hash lanes (XMR/RVN/LTC) are wired with the SELF-VALIDATED
#: :class:`ProxyPoolEvidenceProvider` (Alice's OWN re-hash is the authority) draining
#: that store, INSTEAD of the upstream-poll providers (doc §2.4). MUST be the SAME
#: path the transport service's ``ALICE_PROXY_VALIDATED_SHARE_STORE`` points at. Unset
#: => the historical upstream-poll providers are wired (purely additive). NOT a secret
#: (it is a filesystem path to opaque share facts; the file itself is created 0600).
PROXY_VALIDATED_SHARE_STORE_ENV = "ALICE_PROXY_VALIDATED_SHARE_STORE"


def build_pool_evidence_provider_from_env(
    *,
    http_client: PoolHttpClient,
    clock: object,
    cursor_store: PoolShareCursorStore | None = None,
    poll_cadence: timedelta = DEFAULT_POLL_CADENCE,
    env: Mapping[str, str] | None = None,
    prl_epoch_cursor: PrlEpochCursorStore | None = None,
    prl_pending_share_store: PrlPendingShareSnapshotStore | None = None,
    validated_share_store: ValidatedShareStore | None = None,
) -> LaneRoutingPoolEvidenceProvider:
    """Build the deployed :class:`LaneRoutingPoolEvidenceProvider` from env vars.

    Only lanes whose address + pool_id env vars are BOTH present are wired; every
    other lane stays fail-closed (no provider => under_review). The F2Pool API
    secret is NOT read here (it is read at poll time from
    :data:`F2POOL_API_SECRET_ENV`), so no secret ever touches config. With no lane
    env vars set this returns an all-fail-closed router (the production default).

    PROXY-POOL CREDIT PLANE (doc §2.4/§2.8): when ``validated_share_store`` is supplied
    OR :data:`PROXY_VALIDATED_SHARE_STORE_ENV` names a path, the share-hash lanes are
    wired with the SELF-VALIDATED :class:`ProxyPoolEvidenceProvider` draining that
    durable store (credit gated on Alice's OWN re-hash, written by the SEPARATE
    transport-service process — the credit server is the SOLE DRAINER). An explicit
    ``validated_share_store`` arg wins over the env path (for tests / callers that
    already hold a store). With neither set, the historical upstream-poll providers
    are wired (unchanged); this is purely additive.
    """

    source = env if env is not None else os.environ
    # Resolve the shared validated-share store: explicit arg wins, else the env path
    # (the SAME path the transport service appends to — the cross-process hand-off).
    store = validated_share_store
    if store is None:
        store_path = (source.get(PROXY_VALIDATED_SHARE_STORE_ENV) or "").strip()
        if store_path:
            store = JsonlValidatedShareStore(path=Path(store_path))
    configs: list[LaneProviderConfig] = []
    for lane_kind, address_env, pool_id_env, collection_env in LANE_ENV_SPECS:
        address = (source.get(address_env) or "").strip()
        pool_id = (source.get(pool_id_env) or "").strip()
        if not address or not pool_id:
            continue
        expected = None
        if collection_env is not None:
            expected = (source.get(collection_env) or "").strip() or None
        configs.append(
            LaneProviderConfig(
                lane_kind=lane_kind,
                pool_id=pool_id,
                alice_address=address,
                expected_collection_address=expected,
            )
        )
    return build_lane_routing_provider(
        tuple(configs),
        http_client=http_client,
        clock=clock,
        cursor_store=cursor_store,
        poll_cadence=poll_cadence,
        prl_epoch_cursor=prl_epoch_cursor,
        prl_pending_share_store=prl_pending_share_store,
        validated_share_store=store,
    )


# --- optional PRL chain cross-check (clean stretch seam; UNWIRED) ------------


class PrlChainCrossCheck(Protocol):
    """OPTIONAL second source for PRL (PoUW => chain is ground truth). UNWIRED.

    A clean seam for a future Pearl chain-explorer cross-check (prlscan.com /
    explorer.pearlresearch.ai) that confirms the address's on-chain rewards as an
    independent corroboration of the pool API. It is intentionally NOT wired into
    the credit path in Phase H_b (do not block on it); a provider could AND this
    against the pool delta to require two independent sources before crediting.
    """

    def confirms_address_rewards(self, *, alice_address: str, at: datetime) -> bool:
        ...


# --- shared JSON parsing helpers (defensive, fail-soft to empty) -------------


# --- PRL (pearlhash) /api/account parsing ------------------------------------
#
# The account JSON shape (ground truth read from pearlhash's own frontend, doc
# §0.3):
#   {
#     "connected_workers": [ {..., "gpu_info":[{"hashrate":N,...}]}, ... ],
#     "balance_transactions": [ {"reason": "<label>", "amount": N, "timestamp": ts}, ... ],
#     "pending_rewards": {"epochs": [ {"epoch_label","share","amount","immature_tx"}, ... ]}
#   }
# balance_transactions[] is an append-only ledger; positive rows carry an
# "Epoch <start> UTC .. <end> UTC credit"-style reason (the MATURED work signal);
# negative rows are Payment / Auto Payment (dropped). pending_rewards.epochs[]
# carries the per-epoch fractional ``share`` used for the credit magnitude.

#: Marker that identifies a positive epoch-credit row in ``balance_transactions``.
#: The pearlhash reason is "Epoch <start> UTC .. <end> UTC credit"; we keep rows
#: whose reason contains "epoch" AND "credit" (case-insensitive) AND whose amount
#: is strictly positive (negative Payment / Auto Payment rows are dropped).
_PRL_EPOCH_REASON_TOKEN = "epoch"
_PRL_CREDIT_REASON_TOKEN = "credit"
#: Trailing word stripped from a credit ``reason`` to derive the canonical
#: ``epoch_label`` that matches ``pending_rewards.epochs[].epoch_label`` (which has
#: no trailing " credit"). Normalization is whitespace-collapsed + case-preserving.
_PRL_CREDIT_REASON_SUFFIX = "credit"
#: Leading word stripped from a MATURED ``balance_transactions`` reason. Confirmed
#: against the live pearlhash API: the matured reason is
#: ``"Epoch <start> UTC .. <end> UTC credit"`` (KEEPS a leading "Epoch ") while the
#: ``pending_rewards.epochs[].epoch_label`` is ``"<start> UTC .. <end> UTC"`` (NO
#: prefix). Stripping the prefix (case-insensitive) is what joins the two
#: label-spaces so a pending epoch's ``share`` can be carried to its matured credit.
_PRL_EPOCH_REASON_PREFIX = "epoch"


def _normalize_epoch_label(value: str) -> str:
    """Canonicalize an epoch label so a matured reason and a pending label match.

    Collapses internal whitespace, strips a leading ``Epoch`` word and a trailing
    ``credit`` word (both case-insensitive). The matured ``balance_transactions``
    reason is ``"Epoch … credit"`` while the ``pending_rewards`` ``epoch_label`` is
    bare ``"<start> UTC .. <end> UTC"`` — so BOTH affixes must be removed for the
    two label-spaces to join (confirmed against the live pearlhash API). Case of the
    inner label is preserved.
    """

    collapsed = " ".join(value.split())
    if collapsed.lower().startswith(_PRL_EPOCH_REASON_PREFIX):
        candidate = collapsed[len(_PRL_EPOCH_REASON_PREFIX) :]
        # Only strip when "Epoch" is a standalone leading WORD (followed by
        # whitespace), never a prefix of a larger token, so a bare label is left
        # untouched and re-normalizing an already-canonical label is idempotent.
        if candidate[:1].isspace():
            collapsed = candidate.lstrip()
    if collapsed.lower().endswith(_PRL_CREDIT_REASON_SUFFIX):
        collapsed = collapsed[: -len(_PRL_CREDIT_REASON_SUFFIX)].rstrip()
    return collapsed


def _coerce_decimal(value: Any) -> Decimal | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        try:
            return Decimal(str(value))
        except (InvalidOperation, ValueError):
            return None
    if isinstance(value, str) and value.strip():
        try:
            return Decimal(value.strip())
        except (InvalidOperation, ValueError):
            return None
    return None


def _count_connected_workers(payload: Mapping[str, Any]) -> int:
    workers = payload.get("connected_workers")
    if isinstance(workers, list):
        return sum(1 for entry in workers if isinstance(entry, Mapping))
    return 0


def _parse_prl_connected_workers(
    payload: Mapping[str, Any],
) -> tuple[PrlConnectedWorker, ...]:
    """Parse ``connected_workers[]`` into per-worker ``(worker_name, hashrate)``.

    The live shape (confirmed against ``/api/account``) is::

        "connected_workers": [
          {"worker_name": "<--worker or ''>", "worker_id": <int>,
           "gpu_info": [{"hashrate": <number>, ...}, ...]}, ...]

    Each worker's hashrate is the SUM of its ``gpu_info[].hashrate`` (a multi-GPU rig
    reports one entry per card). Defensive at every level: a non-list / non-mapping /
    missing-key entry contributes nothing; a worker with no positive hashrate is kept
    with ``hashrate=0`` (so it is COUNTED as connected but contributes 0 to the
    proportion split, never a divide-by-zero). Two entries can share a ``worker_name``
    (a miner running the same ``--worker`` on two rigs); the proportion helper sums
    them, so they are treated as one logical worker's combined hashrate. Fail-soft:
    garbage yields an empty tuple (→ per-address flat fallback downstream).
    """

    raw = payload.get("connected_workers")
    if not isinstance(raw, list):
        return ()
    out: list[PrlConnectedWorker] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        name = entry.get("worker_name")
        worker_name = name if isinstance(name, str) else ""
        hashrate = Decimal("0")
        gpu_info = entry.get("gpu_info")
        if isinstance(gpu_info, list):
            for gpu in gpu_info:
                if not isinstance(gpu, Mapping):
                    continue
                value = _coerce_decimal(gpu.get("hashrate"))
                if value is not None and value > Decimal("0"):
                    hashrate += value
        out.append(PrlConnectedWorker(worker_name=worker_name, hashrate=hashrate))
    return tuple(out)


def _parse_prl_pending_shares(
    payload: Mapping[str, Any],
) -> dict[str, tuple[Decimal | None, Decimal | None]]:
    """Map canonical ``epoch_label`` -> ``(share, amount)`` from pending_rewards.

    Defensive: any missing/garbled level yields an empty map. ``share`` and
    ``amount`` are coerced to ``Decimal`` (``None`` when absent/non-numeric).
    """

    pending = payload.get("pending_rewards")
    if not isinstance(pending, Mapping):
        return {}
    epochs = pending.get("epochs")
    if not isinstance(epochs, list):
        return {}
    out: dict[str, tuple[Decimal | None, Decimal | None]] = {}
    for entry in epochs:
        if not isinstance(entry, Mapping):
            continue
        raw_label = entry.get("epoch_label")
        if not isinstance(raw_label, str) or not raw_label.strip():
            continue
        label = _normalize_epoch_label(raw_label)
        if not label:
            continue
        share = _coerce_decimal(entry.get("share"))
        if share is not None and share < Decimal("0"):
            share = None
        amount = _coerce_decimal(entry.get("amount"))
        out[label] = (share, amount)
    return out


def _parse_prl_pending_share_snapshot(
    payload: Mapping[str, Any],
) -> tuple[tuple[str, Decimal], ...]:
    """Pending ``(canonical_label, share)`` pairs to SNAPSHOT, from pending_rewards.

    Drops any pending epoch with an absent / non-positive share (nothing to carry).
    The provider records these each poll so the share survives to the later poll
    where the epoch matures (its live share is gone by then). Fail-soft: a
    missing/garbled ``pending_rewards`` yields no pairs (no snapshot — graceful
    flat fallback later). Reuses :func:`_parse_prl_pending_shares` so the canonical
    label is IDENTICAL to the matured-epoch key it later joins against.
    """

    out: list[tuple[str, Decimal]] = []
    for label, (share, _amount) in _parse_prl_pending_shares(payload).items():
        if share is not None and share > Decimal("0"):
            out.append((label, share))
    return tuple(out)


def _parse_prl_account_epochs(payload: Mapping[str, Any]) -> tuple[EpochCredit, ...]:
    """Parse the account JSON into MATURED epoch credits keyed by ``epoch_label``.

    Keeps ONLY positive ``"Epoch … credit"`` rows from ``balance_transactions[]``
    (dropping negative ``Payment`` / ``Auto Payment``); each becomes a matured
    :class:`EpochCredit`. The per-epoch ``share`` (the credit magnitude source) is
    joined from ``pending_rewards.epochs[]`` by canonical ``epoch_label``; ``amount``
    prefers the pending value and falls back to the matured row's amount. Fail-soft:
    a malformed/absent ``balance_transactions`` yields no matured epochs (the
    provider then credits nothing — fail-closed).
    """

    pending_by_label = _parse_prl_pending_shares(payload)
    txns = payload.get("balance_transactions")
    if not isinstance(txns, list):
        return ()
    epochs: list[EpochCredit] = []
    seen: set[str] = set()
    for row in txns:
        if not isinstance(row, Mapping):
            continue
        reason = row.get("reason")
        if not isinstance(reason, str):
            continue
        low = reason.lower()
        if _PRL_EPOCH_REASON_TOKEN not in low or _PRL_CREDIT_REASON_TOKEN not in low:
            # Not an epoch-credit row (e.g. Payment / Auto Payment) — drop.
            continue
        amount = _coerce_decimal(row.get("amount"))
        if amount is None or amount <= Decimal("0"):
            # Drop negative / zero / non-numeric rows (payments are negative).
            continue
        label = _normalize_epoch_label(reason)
        if not label or label in seen:
            continue
        seen.add(label)
        pending_share, pending_amount = pending_by_label.get(label, (None, None))
        epochs.append(
            EpochCredit(
                epoch_label=label,
                # Magnitude uses ``share`` downstream; ``amount`` is audit-only, so
                # prefer the (immature) pending amount when present, else the
                # matured credit-row amount.
                amount=pending_amount if pending_amount is not None else amount,
                share=pending_share,
                matured=True,
            )
        )
    return tuple(epochs)


# --- PRL (tw-pool.com) /api/worker_stats parsing -----------------------------
#
# The worker_stats JSON shape (ground truth from the live API + the pool frontend's
# own consumer code; doc twpool-prl-integration.md §2.1):
#   {
#     "hashrate": <addr-aggregate H/s>, "balance": <held PRL>, "paid": <PRL>, ...,
#     "history": {
#       "<addr>.<worker>": [ {"time": <unix>, "hashrate": N, "shares": N,
#                             "invalidshares": N}, ... ],
#       ...
#     }
#   }
# The per-(worker, time-bucket) ``shares`` is the credit signal; the dedup keys on
# the bucket ``time``. The history key is "<addr>.<worker>"; we split off the
# leading "<addr>." so the map is keyed by the bare worker name (the Alice-minted
# ``alc-w-<id>`` the proof carries). FAIL-CLOSED: {"result":"error"} / missing
# ``history`` / wrong shapes → empty map → no credit.


def _parse_twpool_worker_stats(
    payload: Any, *, address: str
) -> dict[str, tuple[TwPoolBucket, ...]]:
    """Parse tw-pool ``worker_stats`` into ``{worker_name: (TwPoolBucket, ...)}``.

    Splits each ``history`` key ``"<address>.<worker>"`` on the FIRST ``"<address>."``
    prefix to recover the bare worker name (the Pearl address itself contains no
    ``.``; a defensive fallback splits on the last ``.`` if the prefix does not match,
    so a differently-formatted key still yields a usable worker name). Each bucket is
    coerced defensively: a point missing ``time``/``shares``, or with a non-numeric /
    negative value, is dropped; ``invalidshares`` defaults to 0 when absent.
    Fail-soft: ``{"result":"error"}`` or any missing/garbled level yields an empty map
    (the provider then finds no bucket and fails closed). Duplicate bucket ``time``
    values within one worker collapse to the last seen (the cursor would dedup them
    anyway).
    """

    if not isinstance(payload, Mapping):
        return {}
    # Explicit pool error sentinel → fail-closed (doc §2 / FAIL-CLOSED rule).
    if payload.get("result") == "error":
        return {}
    history = payload.get("history")
    if not isinstance(history, Mapping):
        return {}
    prefix = f"{address}."
    out: dict[str, tuple[TwPoolBucket, ...]] = {}
    for raw_key, series in history.items():
        if not isinstance(raw_key, str) or not isinstance(series, list):
            continue
        if raw_key.startswith(prefix):
            worker_name = raw_key[len(prefix) :]
        elif "." in raw_key:
            # Defensive fallback: a key not matching the bound address still splits to
            # its trailing worker segment (e.g. a proxied/secondary-address key).
            worker_name = raw_key.rsplit(".", 1)[1]
        else:
            worker_name = raw_key
        if not worker_name:
            continue
        by_time: dict[int, TwPoolBucket] = {}
        for point in series:
            if not isinstance(point, Mapping):
                continue
            bucket_time = _coerce_int(point.get("time"))
            shares = _coerce_decimal(point.get("shares"))
            if bucket_time is None or bucket_time < 0 or shares is None or shares < Decimal("0"):
                continue
            invalid = _coerce_decimal(point.get("invalidshares"))
            if invalid is None or invalid < Decimal("0"):
                invalid = Decimal("0")
            hashrate = _coerce_decimal(point.get("hashrate"))
            by_time[bucket_time] = TwPoolBucket(
                bucket_time=bucket_time,
                shares=shares,
                invalidshares=invalid,
                hashrate=hashrate,
            )
        if by_time:
            out[worker_name] = tuple(by_time[t] for t in sorted(by_time))
    return out


def _extract(payload: Any, *keys: str) -> Any:
    """Return the first present key from a dict payload, else the payload itself.

    Pool APIs differ in whether they nest the worker list under ``data`` etc.; we
    unwrap one ``data`` level then look for the requested keys. Returns ``None``
    when nothing matches a dict shape (the caller's parser then yields no workers,
    i.e. fail-closed).
    """

    if isinstance(payload, dict) and "data" in payload and isinstance(payload["data"], dict):
        payload = payload["data"]
    if isinstance(payload, dict):
        for key in keys:
            if key in payload:
                return payload[key]
        return None
    return payload


def _parse_named_workers(
    container: Any,
    *,
    name_keys: tuple[str, ...] = ("name", "worker", "worker_name", "identifier"),
    accepted_keys: tuple[str, ...] = ("accepted_shares", "accepted", "shares"),
    hashrate_keys: tuple[str, ...] = ("hashrate", "hash_rate"),
) -> tuple[WorkerPoolStats, ...]:
    """Parse a workers container (list of dicts OR name->stats map) defensively.

    Any worker without a usable name or a non-negative integer accepted-share
    count is dropped (it can never grant credit). Malformed input yields an empty
    tuple — the provider then finds no worker and fails closed.
    """

    rows: list[tuple[str | None, Mapping[str, Any]]] = []
    if isinstance(container, list):
        for item in container:
            if isinstance(item, Mapping):
                rows.append((None, item))
    elif isinstance(container, Mapping):
        for name, stats in container.items():
            if isinstance(stats, Mapping):
                rows.append((str(name), stats))
            else:
                # A bare ``name: accepted_count`` map.
                count = _coerce_int(stats)
                if count is not None and isinstance(name, str) and name:
                    rows.append((str(name), {"accepted_shares": count}))
    else:
        return ()

    workers: list[WorkerPoolStats] = []
    seen: set[str] = set()
    for fallback_name, stats in rows:
        name = fallback_name
        if name is None:
            name = _first_str(stats, name_keys)
        if not name or name in seen:
            continue
        accepted = _first_int(stats, accepted_keys)
        if accepted is None or accepted < 0:
            continue
        seen.add(name)
        workers.append(
            WorkerPoolStats(
                worker_name=name,
                accepted_shares=accepted,
                hashrate=_first_decimal(stats, hashrate_keys),
            )
        )
    return tuple(workers)


def _nested_get(stats: Mapping[str, Any], dotted: str) -> Any:
    cur: Any = stats
    for part in dotted.split("."):
        if isinstance(cur, Mapping) and part in cur:
            cur = cur[part]
        else:
            return None
    return cur


def _first_str(stats: Mapping[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = _nested_get(stats, key) if "." in key else stats.get(key)
        if isinstance(value, str) and value:
            return value
    return None


def _first_int(stats: Mapping[str, Any], keys: tuple[str, ...]) -> int | None:
    for key in keys:
        value = _nested_get(stats, key) if "." in key else stats.get(key)
        coerced = _coerce_int(value)
        if coerced is not None:
            return coerced
    return None


def _first_decimal(stats: Mapping[str, Any], keys: tuple[str, ...]) -> Decimal | None:
    for key in keys:
        value = _nested_get(stats, key) if "." in key else stats.get(key)
        if isinstance(value, bool):
            continue
        if isinstance(value, (int, float, str)):
            try:
                return Decimal(str(value))
            except (InvalidOperation, ValueError):
                continue
    return None


def _coerce_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError:
            try:
                f = float(value)
            except ValueError:
                return None
            if f.is_integer():
                return int(f)
    return None


def _aware_now(clock: object) -> datetime | None:
    if not callable(clock):
        return None
    try:
        value = clock()
    except Exception:
        return None
    if isinstance(value, datetime) and value.tzinfo is not None:
        return value
    return None


# --- carried-session (cross-process credit plane, doc §2.8) ------------------
#
# The TRANSPORT mints a ShadowSession in its OWN ledger; the SEPARATE credit
# server re-verifies its signature against the shared auth-secret before crediting
# (ledger.admit_cross_process_session). The session rides the shared validated-share
# store on ValidatedShare.carried_session. These helpers (de)serialize ONLY the
# already-public session envelope fields to/from the JSONL "share" row — the
# auth-secret is NEVER part of the record. ``session_nonce`` is the public
# anti-replay value the signature mixes in (so the credit server can recompute the
# HMAC); it is not secret material.

#: The ShadowSession fields persisted on a carried-session record. Exactly the
#: public envelope the credit server needs to recompute + compare the signature and
#: build a ProofAuthorityTarget. No secret, no payout/reward/chain field.
_CARRIED_SESSION_FIELDS: tuple[str, ...] = (
    "session_id",
    "passport_id",
    "device_id",
    "lane",
    "session_kind",
    "issued_at",
    "expires_at",
    "signature",
    "worker_id",
    "worker_name",
    "session_nonce",
)


def _session_to_record(session: ShadowSession) -> dict[str, Any]:
    """Serialize the public ShadowSession envelope for the JSONL share row."""

    return {
        "session_id": session.session_id,
        "passport_id": session.passport_id,
        "device_id": session.device_id,
        "lane": session.lane,
        "session_kind": session.session_kind,
        "issued_at": session.issued_at.isoformat(),
        "expires_at": session.expires_at.isoformat(),
        "signature": session.signature,
        "worker_id": session.worker_id,
        "worker_name": session.worker_name,
        "session_nonce": session.session_nonce,
    }


def _session_from_record(value: Any) -> ShadowSession | None:
    """Rebuild a ShadowSession from a carried-session record (fail-soft to None).

    Any missing/wrong-shaped field yields ``None`` (the share simply carries no
    session — it then credits only if the session is already in the credit ledger).
    The credit server STILL re-verifies the signature before crediting, so a
    malformed/forged record never grants credit on its own.
    """

    if not isinstance(value, Mapping):
        return None
    try:
        issued_at = datetime.fromisoformat(str(value["issued_at"]))
        expires_at = datetime.fromisoformat(str(value["expires_at"]))
    except (KeyError, ValueError, TypeError):
        return None
    required = ("session_id", "passport_id", "device_id", "lane", "session_kind", "signature")
    if any(not isinstance(value.get(key), str) or not value.get(key) for key in required):
        return None
    try:
        return ShadowSession(
            session_id=str(value["session_id"]),
            passport_id=str(value["passport_id"]),
            device_id=str(value["device_id"]),
            lane=str(value["lane"]),  # type: ignore[arg-type]
            session_kind=str(value["session_kind"]),  # type: ignore[arg-type]
            issued_at=issued_at,
            expires_at=expires_at,
            signature=str(value["signature"]),
            worker_id=_opt_str(value.get("worker_id")),
            worker_name=_opt_str(value.get("worker_name")),
            session_nonce=_opt_str(value.get("session_nonce")),
        )
    except (ValueError, TypeError):
        return None


def _opt_str(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _distinct_pending_sessions(shares: Any) -> tuple[ShadowSession, ...]:
    """One carried session per distinct session_id over the given un-spent shares."""

    seen: set[str] = set()
    out: list[ShadowSession] = []
    for share in shares:
        session = share.carried_session
        if session is None or session.session_id in seen:
            continue
        seen.add(session.session_id)
        out.append(session)
    return tuple(out)

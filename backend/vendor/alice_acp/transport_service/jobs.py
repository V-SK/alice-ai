"""The internal job model + the server<->relay seam (doc §2.1 + §2.3 R2).

This module is PURE (no socket, no asyncio): it holds the value objects and the
in-process protocols that wire the miner-facing :mod:`stratum_server` to the
upstream-facing :mod:`dispatcher` relay. Keeping them here keeps the wire layer
and the relay independently testable and lets the relay own the
``internal_job_id <-> (lane, upstream_job_id, extranonce)`` map (R2) without the
server knowing anything about the upstream protocol.

THE TWO HALVES OF THE SEAM
--------------------------
* :class:`JobSource` — the relay side. The server's per-connection lifecycle
  pulls the lane's CURRENT internal job from here to push ``mining.notify`` down
  to a freshly-logged-in miner, and subscribes for the lane's job stream so a new
  upstream job fans out to every connected miner on that lane. The relay (R1/R2)
  is the only implementer.
* :class:`SolutionSink` — the relay side again. When the server's validator
  confirms a submission is ``is_solution`` (cleared the REAL upstream net target),
  the connection hands it here so R3 forwards it upstream IMMEDIATELY. The server
  never batches/queues/withholds — it calls :meth:`SolutionSink.submit_solution`
  synchronously from the accept path (the hard anti-selfish-mining rule).

THE NET-DIFFICULTY HANDOFF (replaces the transport_front placeholder)
---------------------------------------------------------------------
``connection.py`` carried a ``DEFAULT_NET_TARGET_FACTOR`` placeholder net target
(pool * 1e6) so a submission was classifiable as a SHARE without being mislabelled
a SOLUTION before any relay existed. With the relay wired, the REAL upstream net
difficulty rides on :attr:`InternalJob.net_difficulty` (fed from R1/R2), and the
server stamps it onto each :class:`RawSubmission` it builds — so ``is_solution``
now means "cleared the real chain target", which is exactly what R3 forwards.

CREDIT-ONLY: a job carries no reward/payout/chain symbol; the credited unit is
still the validator's ValidatedShareStore write (unchanged). The relay's upstream
submit is the foundation's revenue coin, SEPARATE from credit; ``paid_acu`` is
untouched here. ``ensure_no_raw_secret`` guards every advisory-provenance string.
"""

from __future__ import annotations

import itertools
import threading
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Any, Protocol

from alice_acp.evidence.types import ensure_no_raw_secret
from alice_acp.shadow_server.types import (
    MAIN_POOL_GPU_QUAI,
    MAIN_POOL_GPU_RVN,
    SCRYPT_POOL,
    XMR_POOL,
    Lane,
)
from alice_acp.transport_front.stratum_messages import (
    build_job_notification,
    build_kawpow_job_notification,
    build_monero_job_object,
    build_scrypt_job_notification,
    build_xmr_job_notification,
)

#: Reason codes for a refused/dropped solution forward (advisory telemetry only —
#: a refusal NEVER affects credit, which already happened in the validator).
SOLUTION_FORWARD_NO_MAPPING = "solution_forward_no_internal_job"
SOLUTION_FORWARD_UPSTREAM_DOWN = "solution_forward_upstream_unavailable"


@dataclass(frozen=True, slots=True)
class InternalJob:
    """One Alice-internal job, translated by R2 from an upstream job (doc §2.3).

    The relay's R2 role turns each upstream stratum job into one of these with
    Alice's own pool target (the per-connection vardiff value is layered on top by
    the server when it pushes) and the REAL upstream ``net_difficulty`` stamped on.
    ``internal_job_id`` is the opaque id Alice hands miners (so the upstream job id
    is never leaked downward and the relay can re-key on its own terms); the relay
    owns the ``internal_job_id -> (lane, upstream_job_id, extranonce)`` reverse map
    in :class:`JobTranslationMap`.

    ``payload`` is the algo-specific ``mining.notify`` body the miner needs
    (header/seed/target fields the upstream supplied) — this module does not invent
    those; R2 assembles them. CREDIT-ONLY: no reward/payout/chain symbol here.
    """

    lane: Lane
    internal_job_id: str
    upstream_job_id: str
    #: The REAL network (solution) difficulty from the upstream job. The server
    #: stamps this as the RawSubmission ``net_target_difficulty`` so ``is_solution``
    #: classification means "cleared the real chain target" (R3-forwardable).
    net_difficulty: Decimal
    #: The upstream-supplied pool/share difficulty for this job (the floor the
    #: per-connection vardiff is clamped to start from). Advisory; the server's
    #: vardiff owns the per-connection pool target it actually sends.
    pool_difficulty: Decimal
    #: The algo-specific ``mining.notify`` params body (header/seed/target/...),
    #: assembled by R2 from the upstream job. Opaque to this module.
    payload: dict[str, Any] = field(default_factory=dict)
    #: The per-connection extranonce context bound to this job (advisory provenance
    #: for the reverse map). Empty when the lane assigns extranonce per-connection.
    extranonce: str = ""
    #: Whether miners should drop in-flight work for prior jobs (the stratum
    #: ``clean_jobs`` flag). The server forwards it on the ``mining.notify``.
    clean_jobs: bool = True
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.internal_job_id or not self.upstream_job_id:
            raise ValueError("internal/upstream job id must be non-empty")
        for value in (self.net_difficulty, self.pool_difficulty):
            if not isinstance(value, Decimal):
                raise TypeError("job difficulties must be Decimal")
            if value <= Decimal("0"):
                raise ValueError("job difficulties must be positive")
        if self.net_difficulty < self.pool_difficulty:
            # A solution is necessarily also a share: net >= pool always.
            raise ValueError("net_difficulty must be >= pool_difficulty")
        # Advisory-provenance strings must never carry raw secret material.
        ensure_no_raw_secret(self.internal_job_id, field_name="internal_job_id")
        ensure_no_raw_secret(self.upstream_job_id, field_name="upstream_job_id")
        if self.extranonce:
            ensure_no_raw_secret(self.extranonce, field_name="extranonce")

    def to_notification(self, *, target: str | None = None) -> dict[str, Any]:
        """The job-push dict the server pushes down to a miner (LANE-AWARE wire shape).

        Four on-wire dialects, routed STRICTLY by lane (NEVER a client-supplied algo
        field — the PORT is the lane authority):

        * SCRYPT/LTC (Bitcoin-family) — the standard POSITIONAL ``mining.notify`` array
          (:func:`build_scrypt_job_notification`): the 9-element ``[job_id, prevhash,
          coinb1, coinb2, merkle_branch, version, nbits, ntime, clean_jobs]`` a stock
          Litecoin rig reads by index. UNCHANGED.
        * RandomX/XMR (cryptonote/xmrig) — the ``job``-method OBJECT push
          (:func:`build_xmr_job_notification` wrapping :func:`build_monero_job_object`):
          ``{"jsonrpc":"2.0","method":"job","params":{blob, job_id, target, seed_hash,
          height, algo}}``. NOT a positional array, NOT ``mining.notify`` (a Monero rig
          ignores both). The ``target`` is THIS CONNECTION's per-connection difficulty
          (passed by the server as the xmrig compact target); when omitted it falls back
          to the upstream job's ``target`` so the push is always well-formed. There is NO
          ``mining.set_difficulty`` on this lane — a retarget pushes a fresh ``job``.
        * KawPoW/RVN (Ethereum/Ravencoin dialect) — the POSITIONAL ``mining.notify`` array
          (:func:`build_kawpow_job_notification`): the 7-element ``[job_id, headerHash,
          seedHash, target, clean_jobs, height, bits]`` a stock KawPoW rig (T-Rex /
          kawpowminer) reads by index. The ``target`` is THIS CONNECTION's per-connection
          target (passed by the server as the FULL 32-byte target); absent one the upstream
          job's ``target`` is used (always well-formed). KawPoW has NO
          ``mining.set_difficulty``; the per-connection difficulty rides ``mining.notify``'s
          target AND ``mining.set_target`` (a retarget pushes ``set_target``, NOT a fresh
          job, NOT ``set_difficulty``).
        * any OTHER lane — the GENERIC object-payload :func:`build_job_notification`
          (UNCHANGED): a hypothetical future lane keeps the prior generic framing.

        Either way the upstream job id is NOT included — miners only see the internal id.
        """

        if self.lane == SCRYPT_POOL:
            return build_scrypt_job_notification(
                job_id=self.internal_job_id,
                payload=self.payload,
                clean_jobs=self.clean_jobs,
            )
        if self.lane == XMR_POOL:
            # The cryptonote ``job``-method OBJECT push. The per-connection ``target`` (the
            # server's vardiff, as an xmrig compact target) overrides the upstream job
            # target; absent one, the upstream job target is used (always well-formed).
            effective_target = target if target is not None else self.payload["target"]
            job_object = build_monero_job_object(
                job_id=self.internal_job_id,
                payload=self.payload,
                target=effective_target,
            )
            return build_xmr_job_notification(job=job_object)
        if self.lane in (MAIN_POOL_GPU_RVN, MAIN_POOL_GPU_QUAI):
            # The KawPoW POSITIONAL ``mining.notify`` (RVN AND Quai — the KawPoW wire is
            # identical). The per-connection ``target`` (the server's vardiff, as a FULL
            # 32-byte target) overrides the upstream job target; absent one, the upstream
            # job target is used (always well-formed). The payload carries the exact
            # ``headerHash``/``seedHash`` keys.
            effective_target = target if target is not None else self.payload["target"]
            payload = {**self.payload, "target": effective_target}
            return build_kawpow_job_notification(
                job_id=self.internal_job_id,
                payload=payload,
                clean_jobs=self.clean_jobs,
            )
        # Any other lane: the generic object-payload notify, UNCHANGED.
        body = dict(self.payload)
        body.setdefault("clean_jobs", self.clean_jobs)
        return build_job_notification(job_id=self.internal_job_id, payload=body)


@dataclass(frozen=True, slots=True)
class ConfirmedSolution:
    """A validator-confirmed SOLUTION the server hands to R3 for IMMEDIATE forward.

    Built ONLY for a submission the merged :class:`ShareValidator` classified as
    ``is_solution`` (cleared the REAL upstream net target). It carries the opaque
    facts R3 needs to assemble the upstream ``mining.submit`` — the bound identity
    (server-owned worker), the internal job id (R3 maps it back to the upstream job
    id + extranonce via :class:`JobTranslationMap`), the rig's nonce/extranonce, and
    Alice's recomputed ``result_difficulty`` (telemetry only — NEVER credit).

    NOTE (doc §2.3 R3): forwarding is advisory for the FOUNDATION'S upstream coin,
    SEPARATE from credit. The credited unit already landed in the ValidatedShareStore
    inside the validator; an upstream ACK or refusal changes nothing about credit.
    """

    lane: Lane
    internal_job_id: str
    worker_name: str
    nonce_hex: str
    result_difficulty: Decimal
    extranonce2_hex: str = ""
    ntime_hex: str = ""
    mix_hash_hex: str = ""
    #: The KawPoW ``headerHash`` (32-byte hex) the rig answered — the RVN lane's upstream
    #: ``mining.submit`` carries it as the 4th positional field (``[worker, job_id, nonce,
    #: headerHash, mixHash]``; the pool re-checks the header+nonce+mix against the same
    #: job). The rig submits it verbatim and the server carries it through. Empty for the
    #: Scrypt/XMR lanes. Advisory only — NEVER credit (the credited unit is the
    #: validator's ValidatedShareStore write).
    header_hash_hex: str = ""
    #: The RandomX ``result`` hash (32-byte hex) the rig submitted — the XMR lane's
    #: upstream ``mining.submit`` carries it as ``result`` (the pool re-checks it
    #: against the same blob/seed). Empty for the Scrypt/KawPoW lanes (whose upstream
    #: submit needs no separate result hash). Advisory only — NEVER credit (the credited
    #: unit is the validator's ValidatedShareStore write).
    result_hash_hex: str = ""
    canonical_share_hash: str | None = None


class JobSource(Protocol):
    """The relay side of the seam the server PULLS jobs from (doc §2.3 R1/R2).

    The server uses :meth:`current_job` to push the first ``mining.notify`` to a
    freshly-logged-in miner and :meth:`subscribe` to receive every subsequent
    translated job for a lane (so a new upstream job fans out to all connected
    miners). The relay (R1 the upstream connection + R2 translation) implements it;
    a test injects a FAKE. ``current_job`` returns ``None`` until the relay has
    received + translated the lane's first upstream job (the server then pushes no
    job yet — fail-soft, never an error).
    """

    def current_job(self, lane: Lane) -> InternalJob | None: ...

    def subscribe(self, lane: Lane, callback: Callable[[InternalJob], None]) -> Callable[[], None]:
        """Register ``callback`` for each new job on ``lane``; return an unsubscribe."""
        ...


class SolutionSink(Protocol):
    """The relay side of the seam the server PUSHES solutions to (doc §2.3 R3).

    The server calls :meth:`submit_solution` SYNCHRONOUSLY from the validator-accept
    path the instant a submission is classified ``is_solution`` — never batched,
    queued-with-delay, or withheld (the hard anti-selfish-mining rule). The relay's
    R3 role forwards it upstream immediately and records the upstream ACK as advisory
    telemetry only. Returns ``True`` when the forward was dispatched (NOT when it was
    credited — forwarding is not credit).
    """

    def submit_solution(self, solution: ConfirmedSolution) -> bool: ...


@dataclass(slots=True)
class JobTranslationMap:
    """R2's authoritative ``internal_job_id <-> (lane, upstream_job_id, extranonce)`` map.

    The relay owns ONE of these. On each translated upstream job R2 calls
    :meth:`register` to mint/record the internal job id; when R3 must forward a
    confirmed solution it calls :meth:`resolve` to recover the upstream job id +
    extranonce to put on the upstream ``mining.submit``. Bounded by ``max_entries``
    (oldest evicted) so a long-running relay never grows unboundedly; an evicted
    job's solution simply fails to resolve (R3 then records
    :data:`SOLUTION_FORWARD_NO_MAPPING` and drops — a stale solution is worthless
    upstream anyway). Thread-safe (the relay's tasks share one instance).
    """

    max_entries: int = 4096
    _forward: dict[str, tuple[Lane, str, str]] = field(default_factory=dict)
    _order: list[str] = field(default_factory=list)
    _counter: itertools.count = field(default_factory=lambda: itertools.count(1))
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def mint_internal_id(self, lane: Lane) -> str:
        """Mint a fresh, opaque internal job id (never derived from the upstream id)."""

        return f"alc-job-{lane}-{next(self._counter):x}"

    def register(
        self, *, internal_job_id: str, lane: Lane, upstream_job_id: str, extranonce: str = ""
    ) -> None:
        with self._lock:
            if internal_job_id not in self._forward:
                self._order.append(internal_job_id)
            self._forward[internal_job_id] = (lane, upstream_job_id, extranonce)
            while len(self._order) > self.max_entries:
                evicted = self._order.pop(0)
                self._forward.pop(evicted, None)

    def resolve(self, internal_job_id: str) -> tuple[Lane, str, str] | None:
        """Recover ``(lane, upstream_job_id, extranonce)`` for R3, or ``None``."""

        with self._lock:
            return self._forward.get(internal_job_id)

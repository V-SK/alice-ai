"""OPEN self-serve enrollment for the public LTC (Scrypt) lane (review #3/#4).

This is the registration-less ``<Alice_address>.<worker_label>`` model (Qubic-DOGE /
F2Pool style) — but the identity is the miner's **Alice address** (the SS58 format-300
Alice-token destination), NOT the mined coin's address. This is the V cross-lane
directive: a miner earns Alice tokens, so their mining-login username is the Alice
address those tokens go to (THIS LTC lane now and EVERY future lane: XMR/RVN). It is
enabled PER LANE behind an env flag (default OFF); when OFF the
:class:`~alice_acp.transport_front.identity.StratumIdentityResolver` behaves byte-for-
byte as the roster-gated path. When ON for the lane the ALICE ADDRESS validation
(:mod:`alice_acp.transport_front.alice_address`) is the new admission gate and this
module supplies the rest of the open-mode pieces:

* :func:`sanitize_worker_label` — bound + restrict the client's worker label (#4).
* :func:`open_worker_name` — a DETERMINISTIC, SERVER-derived ``worker_name`` from the
  (validated Alice address, sanitized label). Not client-controlled beyond those two
  validated inputs; it is the credited worker + the dedup/cross-check binding.
* :class:`OpenEnrollmentLimiter` — the anti-spam budget (#3): a per-address+per-IP
  session-issuance/connection rate limit, a GLOBAL new-address admission budget per
  rolling window, and a cap on distinct credit keys (worker labels) per address. All
  configurable from env with safe defaults; fail-closed (over-limit ⇒ reject).

CREDIT-ONLY: nothing here sets a reward/payout/chain symbol or reads/writes a payout
address. The Alice address is a CREDIT identity (the Alice-token destination) only —
this module never performs a payout/transfer.
"""

from __future__ import annotations

import hashlib
import os
import threading
from collections import OrderedDict, deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from alice_acp.evidence.types import ensure_no_raw_secret

# --- env flags / knobs -------------------------------------------------------

#: PER-LANE master switch for open enrollment on the LTC/Scrypt lane (default OFF).
#: With it OFF the resolver is the roster-gated path, unchanged. Mirrors the
#: default-OFF posture of the stratum gate + the public-miner gate.
LTC_OPEN_ENROLLMENT_ENV = "ALICE_LTC_OPEN_ENROLLMENT"

#: PER-LANE master switch for open enrollment on the XMR/RandomX lane (default OFF).
#: Distinct from the LTC flag so a stray ``ALICE_LTC_OPEN_ENROLLMENT`` can NEVER flip the
#: XMR lane open (and vice-versa). Same default-OFF, registration-less ``<Alice_address>.
#: <worker>`` model — the miner's CREDIT identity is their Alice SS58-300 address (the V
#: cross-lane directive), the SAME identity as the LTC lane.
XMR_OPEN_ENROLLMENT_ENV = "ALICE_XMR_OPEN_ENROLLMENT"

#: PER-LANE master switch for open enrollment on the RVN/KawPoW lane (default OFF).
#: Distinct from the LTC/XMR flags so a stray flag on either can NEVER flip the RVN lane
#: open (and vice-versa). Same default-OFF, registration-less ``<Alice_address>.<worker>``
#: model — the miner's CREDIT identity is their Alice SS58-300 address (the V cross-lane
#: directive), the SAME identity as the LTC/XMR lanes (NOT a Ravencoin address).
RVN_OPEN_ENROLLMENT_ENV = "ALICE_RVN_OPEN_ENROLLMENT"

#: PER-LANE master switch for open enrollment on the Quai/KawPoW lane (default OFF).
#: Mirror of :data:`RVN_OPEN_ENROLLMENT_ENV`; distinct from every other lane's flag so a
#: stray flag elsewhere can NEVER flip the Quai lane open (and vice-versa). Same default-OFF,
#: registration-less ``<Alice_address>.<worker>`` model — the miner's CREDIT identity is
#: their Alice SS58-300 address (the V cross-lane directive), the SAME identity as the
#: LTC/XMR/RVN lanes (NOT a Quai 0x address).
QUAI_OPEN_ENROLLMENT_ENV = "ALICE_QUAI_OPEN_ENROLLMENT"

#: Max sessions/connections a single (address) OR a single (peer IP) may have issued
#: within :data:`OPEN_RATE_WINDOW_ENV`. A 0 disables that particular cap (NOT
#: recommended on the public edge). Default 600/window (= 1/s sustained over the 600 s
#: window). A SINGLE legitimate miner RE-AUTHS on every reconnect (e.g. T-Rex force-
#: reconnects after a burst of rejected shares), and each re-auth is one issuance against
#: BOTH its per-address and its per-IP counter — so a too-low cap throttles a normal
#: reconnecting miner into failure (a 60/window cap trips after just 60 reconnects in 10
#: min). 600 comfortably tolerates any sane reconnect cadence while still bounding a
#: single key; the anti-spam defense against a NOVEL-ADDRESS flood is the GLOBAL
#: new-address budget (:data:`OPEN_MAX_NEW_ADDRESSES_ENV`, 500/window) — a returning
#: address is not a novel-flood vector. Per-IP at 600 still bounds one source, and the
#: global new-address budget caps how many distinct NEW addresses one IP can introduce.
OPEN_MAX_PER_ADDRESS_ENV = "ALICE_LTC_OPEN_MAX_SESSIONS_PER_ADDRESS"
OPEN_MAX_PER_IP_ENV = "ALICE_LTC_OPEN_MAX_SESSIONS_PER_IP"

#: GLOBAL new-address admission budget per window: at most this many DISTINCT
#: never-before-seen addresses may be admitted within one rolling window. This is a RATE
#: limit (it throttles fresh-address admissions per window), NOT a hard cap on the number
#: of distinct credit keys retained — so the in-memory stores are bounded SEPARATELY by an
#: LRU (:data:`OPEN_MAX_TRACKED_ADDRESSES_ENV`). Default 500/window.
OPEN_MAX_NEW_ADDRESSES_ENV = "ALICE_LTC_OPEN_MAX_NEW_ADDRESSES"

#: The rolling window (seconds) the three rate counters above are measured over.
#: Default 600s (10 min).
OPEN_RATE_WINDOW_ENV = "ALICE_LTC_OPEN_RATE_WINDOW_S"

#: Cap on DISTINCT credit keys (sanitized worker labels) one address may ever spawn.
#: This bounds the per-address fan-out of (address|worker) credit keys / queued
#: validated shares so one address cannot grow the credit + share stores unbounded by
#: cycling worker labels. Default 64.
OPEN_MAX_WORKERS_PER_ADDRESS_ENV = "ALICE_LTC_OPEN_MAX_WORKERS_PER_ADDRESS"

#: Hard LRU cap on the number of DISTINCT addresses kept in the in-memory seen-set +
#: per-address worker store. The new-address budget above is a per-window RATE, not a
#: lifetime cap on distinct keys, so WITHOUT this the store would grow for the life of the
#: process (~72k entries/day at the default budget — a slow unbounded leak). When the
#: store is full the LEAST-recently-active address is evicted; a re-admitted evicted
#: address is treated as new again (re-charges the new-address budget) — fail-closed
#: (over-count), never a free pass. Default 100_000 (~1-2 days of distinct addresses at
#: the default budget).
OPEN_MAX_TRACKED_ADDRESSES_ENV = "ALICE_LTC_OPEN_MAX_TRACKED_ADDRESSES"

# 600/window (= 1/s over the 600 s window): tolerates a normal/aggressive reconnect
# cadence from one legit miner (each reconnect re-auths = one issuance) while still
# bounding a single address/IP. The novel-address-flood defense is the GLOBAL
# new-address budget below (a returning address never charges it).
DEFAULT_OPEN_MAX_PER_ADDRESS = 600
DEFAULT_OPEN_MAX_PER_IP = 600
DEFAULT_OPEN_MAX_NEW_ADDRESSES = 500
DEFAULT_OPEN_RATE_WINDOW = timedelta(seconds=600)
DEFAULT_OPEN_MAX_WORKERS_PER_ADDRESS = 64
DEFAULT_OPEN_MAX_TRACKED_ADDRESSES = 100_000

#: The DEFAULT worker label when the login supplies none / an empty one. A stable,
#: charset-safe label so every address has at least one credit key.
DEFAULT_WORKER_LABEL = "default"

#: Max worker-label length (chars) after sanitization (#4). A bounded label keeps the
#: credit key + the displayed worker small and prevents an unbounded-string store-bloat.
MAX_WORKER_LABEL_LENGTH = 32

#: The restrictive worker-label charset (#4): ASCII alphanumerics + a few benign
#: separators. Everything else (control chars, non-ASCII, shell/markup metacharacters)
#: is dropped during sanitization, so a label can never carry a control char or a
#: secret-shaped blob into the store.
_WORKER_LABEL_ALLOWED = frozenset(
    "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.@"
)


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def open_enrollment_enabled(env: dict[str, str] | None = None) -> bool:
    """Read :data:`LTC_OPEN_ENROLLMENT_ENV` (default OFF). Mirrors the gate readers.

    This is the LTC/Scrypt-lane reader (kept for the credit-edge + deploy callers that
    are LTC-specific). For a lane-aware check use :func:`open_enrollment_enabled_for_lane`.
    """

    source = env if env is not None else os.environ
    return _truthy(source.get(LTC_OPEN_ENROLLMENT_ENV))


#: PER-LANE open-enrollment env flag map. Only the open-enrollable lanes appear; a lane
#: absent here is NEVER open-enrollable regardless of any env. Imported lazily by the
#: resolver via :func:`open_enrollment_enabled_for_lane` to avoid a Lane import cycle.
_OPEN_ENROLLMENT_ENV_BY_LANE: dict[str, str] = {
    "scrypt_pool": LTC_OPEN_ENROLLMENT_ENV,
    "xmr_pool": XMR_OPEN_ENROLLMENT_ENV,
    "main_pool_gpu_rvn": RVN_OPEN_ENROLLMENT_ENV,
    "main_pool_gpu_quai": QUAI_OPEN_ENROLLMENT_ENV,
}


def open_enrollment_enabled_for_lane(lane: str, env: dict[str, str] | None = None) -> bool:
    """Whether open enrollment is enabled for ``lane`` (per-lane flag; default OFF).

    Maps the lane to its OWN env flag (``scrypt_pool`` -> :data:`LTC_OPEN_ENROLLMENT_ENV`,
    ``xmr_pool`` -> :data:`XMR_OPEN_ENROLLMENT_ENV`, ``main_pool_gpu_rvn`` ->
    :data:`RVN_OPEN_ENROLLMENT_ENV`, ``main_pool_gpu_quai`` ->
    :data:`QUAI_OPEN_ENROLLMENT_ENV`). A lane with no mapping is NEVER open-enrollable. So
    each lane is independently gated (default OFF): no lane's flag can open another.
    """

    flag = _OPEN_ENROLLMENT_ENV_BY_LANE.get(lane)
    if flag is None:
        return False
    source = env if env is not None else os.environ
    return _truthy(source.get(flag))


# --- worker label (#4) -------------------------------------------------------


def sanitize_worker_label(raw: str | None) -> str:
    """Return a bounded, charset-restricted worker label (#4); never raises.

    Drops every character outside :data:`_WORKER_LABEL_ALLOWED` (so control chars /
    non-ASCII / metacharacters cannot survive), truncates to
    :data:`MAX_WORKER_LABEL_LENGTH`, and falls back to :data:`DEFAULT_WORKER_LABEL`
    when the input is absent or sanitizes to empty. The result is ALWAYS a non-empty,
    store-safe label so an open miner always has a valid credit key.
    """

    if not raw:
        return DEFAULT_WORKER_LABEL
    cleaned = "".join(ch for ch in raw if ch in _WORKER_LABEL_ALLOWED)
    cleaned = cleaned[:MAX_WORKER_LABEL_LENGTH]
    if not cleaned:
        return DEFAULT_WORKER_LABEL
    # Defense-in-depth: the restrictive charset still permits ``sk-…`` / ``ghp_…``
    # SHAPES (the secret-detector's prefixes use the same alnum+``-``/``_`` charset). A
    # label that matches a raw-secret pattern is NEVER persisted as a credit key — fall
    # back to the default so a pasted token can't become a (address|label) store key.
    try:
        ensure_no_raw_secret(cleaned, field_name="worker_label")
    except ValueError:
        return DEFAULT_WORKER_LABEL
    return cleaned


#: Hex width of the sha256 truncation in :func:`open_worker_name`. 32 hex = 128 bits
#: (review #F-B): widened from the roster's 16-hex/64-bit to remove the only practical
#: collision-griefing surface. At 64 bits a ~2**32 birthday effort could find two
#: ``(address|label)`` pairs colliding to ONE credit key (two distinct miners' shares
#: pooling under one worker_name, or a griefer steering credit); at 128 bits that
#: effort is ~2**64 — infeasible. Address+label remain the SIGNED trust anchor on the
#: credit side regardless; this only removes the accidental/forced-collision risk. The
#: ``alc-w-`` prefix + lowercase-hex body keep the opaque shape the credit spine and
#: the pool worker-correlation already accept (no downstream length/shape constraint
#: pins it to 16 hex — the roster name is itself free-form opaque).
OPEN_WORKER_NAME_HEX_WIDTH = 32


def open_worker_name(*, address: str, worker_label: str) -> str:
    """Deterministic SERVER-derived worker_name for an open ``(Alice address, label)``.

    Mirrors the roster's opaque ``alc-w-<hex>`` shape (so the credit spine treats it
    identically) but derives it from the VALIDATED Alice address + the SANITIZED label —
    the only client-influenced inputs, both already gated. It is the credited worker AND
    the value the canonical-share-hash / pool cross-check binds to, so it is NEVER the
    client's raw self-named worker. Stable across reconnects (same address+label ⇒ same
    worker_name ⇒ the share dedup cursor + credit key are stable).

    The sha256 is truncated to :data:`OPEN_WORKER_NAME_HEX_WIDTH` (32 hex = 128 bits,
    #F-B) — wide enough that two ``(address, label)`` pairs cannot be driven to one
    credit key by a feasible birthday search. BOTH the transport (at mint) and the
    credit-side re-derivation (in ``http_app._build_proof_authority_targets``) call
    THIS function, so they always agree on the name.
    """

    digest = hashlib.sha256(
        f"alice-acp:open-worker-name:{address}|{worker_label}".encode()
    ).hexdigest()
    return f"alc-w-{digest[:OPEN_WORKER_NAME_HEX_WIDTH]}"


# --- anti-spam budget (#3) ---------------------------------------------------


@dataclass(frozen=True, slots=True)
class OpenEnrollmentLimits:
    """The resolved, env-driven open-enrollment rate-limit config (all per-lane)."""

    max_sessions_per_address: int = DEFAULT_OPEN_MAX_PER_ADDRESS
    max_sessions_per_ip: int = DEFAULT_OPEN_MAX_PER_IP
    max_new_addresses_per_window: int = DEFAULT_OPEN_MAX_NEW_ADDRESSES
    rate_window: timedelta = DEFAULT_OPEN_RATE_WINDOW
    max_workers_per_address: int = DEFAULT_OPEN_MAX_WORKERS_PER_ADDRESS
    max_tracked_addresses: int = DEFAULT_OPEN_MAX_TRACKED_ADDRESSES

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> OpenEnrollmentLimits:
        source = env if env is not None else os.environ
        return cls(
            max_sessions_per_address=_nonneg_int(
                source.get(OPEN_MAX_PER_ADDRESS_ENV), DEFAULT_OPEN_MAX_PER_ADDRESS
            ),
            max_sessions_per_ip=_nonneg_int(
                source.get(OPEN_MAX_PER_IP_ENV), DEFAULT_OPEN_MAX_PER_IP
            ),
            max_new_addresses_per_window=_nonneg_int(
                source.get(OPEN_MAX_NEW_ADDRESSES_ENV), DEFAULT_OPEN_MAX_NEW_ADDRESSES
            ),
            rate_window=_positive_seconds(
                source.get(OPEN_RATE_WINDOW_ENV), DEFAULT_OPEN_RATE_WINDOW
            ),
            max_workers_per_address=_positive_int(
                source.get(OPEN_MAX_WORKERS_PER_ADDRESS_ENV),
                DEFAULT_OPEN_MAX_WORKERS_PER_ADDRESS,
            ),
            max_tracked_addresses=_positive_int(
                source.get(OPEN_MAX_TRACKED_ADDRESSES_ENV),
                DEFAULT_OPEN_MAX_TRACKED_ADDRESSES,
            ),
        )


#: Stable, secret-free rejection reasons (returned by :meth:`OpenEnrollmentLimiter.admit`).
OPEN_RATE_LIMITED_ADDRESS = "open_enrollment_rate_limited_address"
OPEN_RATE_LIMITED_IP = "open_enrollment_rate_limited_ip"
OPEN_NEW_ADDRESS_BUDGET_EXCEEDED = "open_enrollment_new_address_budget_exceeded"
OPEN_WORKER_FANOUT_EXCEEDED = "open_enrollment_worker_fanout_exceeded"


@dataclass(slots=True)
class OpenEnrollmentLimiter:
    """In-memory, thread-safe anti-spam budget for open enrollment (#3), fail-closed.

    Enforces three independent caps on a candidate ``(address, worker_label, peer_ip)``
    admission, all over a rolling :attr:`OpenEnrollmentLimits.rate_window`:

    1. **Per-address + per-IP session rate** — at most N issuances per address and per
       IP per window (a single key — address or source — cannot mint sessions
       unbounded).
    2. **Global new-address budget** — at most M never-before-seen addresses admitted
       per window. This is a RATE limit that throttles fresh-address admissions; it is
       NOT a hard cap on the number of distinct keys retained (see Memory below).
    3. **Per-address worker fan-out** — a cap on the count of DISTINCT
       ``(address|worker_label)`` credit keys one (currently-tracked) address may spawn
       (bounds the per-address share-store / credit-key fan-out).

    :meth:`admit` returns ``None`` to ADMIT (and records the admission), or a stable
    reason string to REJECT (recording nothing — a rejected attempt does not consume
    budget, so a flood that is being rejected cannot also starve a legitimate miner via
    side effects). It is purely a transport-front admission throttle; the durable
    credit invariants downstream are unchanged.

    **Memory.** Every backing structure is bounded so the long-running process cannot
    leak: the merged seen-set + per-address worker store
    (:attr:`_workers_by_address`) is LRU-capped at
    :attr:`OpenEnrollmentLimits.max_tracked_addresses` (evicting the least-recently-active
    address — see :meth:`_evict_tracked_overflow`), and the per-address / per-IP rate
    deques are compacted of fully-expired keys once per window (see :meth:`_compact`).
    Both stay fail-closed: LRU eviction only re-charges budgets on a returning address
    (over-count), and compaction only drops EMPTY (expired) windows, never a live count.

    Note this is a SINGLE-PROCESS limiter (the transport service is one process); it is
    not a cross-process global. That matches the deploy: one transport listener owns the
    public LTC lane. ``ledger.max_issuances_per_identity`` provides the complementary
    durable per-identity issuance cap inside the ledger.
    """

    limits: OpenEnrollmentLimits = field(default_factory=OpenEnrollmentLimits)
    #: Injectable monotonic-ish clock (aware datetime) for deterministic tests.
    clock: object = None
    _address_hits: dict[str, deque[datetime]] = field(default_factory=dict)
    _ip_hits: dict[str, deque[datetime]] = field(default_factory=dict)
    _new_address_window: deque[datetime] = field(default_factory=deque)
    #: LRU of admitted address -> its distinct sanitized worker labels. Key PRESENCE is
    #: the "address already seen" signal (so a returning miner is not re-charged the
    #: new-address budget); the value set bounds the per-address worker fan-out. This one
    #: structure is both the seen-set and the worker store, LRU-bounded by
    #: ``limits.max_tracked_addresses`` so neither can grow for the life of the process.
    #: Most-recently-active address is at the END (right); evictions pop from the front.
    _workers_by_address: OrderedDict[str, set[str]] = field(default_factory=OrderedDict)
    #: Timestamp of the last hit-map compaction; gates :meth:`_maybe_compact` to once per
    #: window. ``None`` until the first admission.
    _last_compaction: datetime | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def admit(
        self,
        *,
        address: str,
        worker_label: str,
        peer_ip: str,
        now: datetime,
    ) -> str | None:
        """Admit (record + return ``None``) or reject (return a stable reason).

        Checks are ordered cheapest-first and the durable per-key state is mutated ONLY
        on a full admit, so a rejected attempt records nothing. ``peer_ip`` may be empty
        (the platform did not expose a peername) — the per-IP cap is then skipped for
        that admission (the per-address + global budgets still apply), so a missing
        peername can never be a free pass past the OTHER caps.
        """

        window = self.limits.rate_window
        with self._lock:
            # Memory hygiene (at most once per window): drop fully-expired hit deques so
            # the per-address / per-IP maps track only recent activity, never the life of
            # the process. Only EMPTY (expired) windows are dropped, so this can never
            # lower a live count — see :meth:`_compact`.
            self._maybe_compact(now)

            # 1a. per-address session rate. Read via .get() (an absent key == 0 hits) so a
            # fresh miss / a later reject never leaves an empty deque behind to leak.
            if self.limits.max_sessions_per_address > 0:
                hits = self._address_hits.get(address)
                if hits is not None and len(self._prune(hits, now, window)) >= (
                    self.limits.max_sessions_per_address
                ):
                    return OPEN_RATE_LIMITED_ADDRESS
            # 1b. per-IP session rate (skipped when the peer IP is unknown).
            if peer_ip and self.limits.max_sessions_per_ip > 0:
                ip_hits = self._ip_hits.get(peer_ip)
                if ip_hits is not None and len(self._prune(ip_hits, now, window)) >= (
                    self.limits.max_sessions_per_ip
                ):
                    return OPEN_RATE_LIMITED_IP
            # 3. per-address worker fan-out (distinct credit keys per address). Key
            # presence in `_workers_by_address` is ALSO the "already seen" signal below.
            workers = self._workers_by_address.get(address)
            is_new_address = workers is None
            if is_new_address:
                is_new_worker = True
            else:
                is_new_worker = worker_label not in workers
                if is_new_worker and len(workers) >= self.limits.max_workers_per_address:
                    return OPEN_WORKER_FANOUT_EXCEEDED
            # 2. global new-address admission budget (only charged for a NEW address).
            if is_new_address and self.limits.max_new_addresses_per_window > 0:
                new_window = self._prune(self._new_address_window, now, window)
                if len(new_window) >= self.limits.max_new_addresses_per_window:
                    return OPEN_NEW_ADDRESS_BUDGET_EXCEEDED

            # All caps passed — COMMIT the admission state.
            if self.limits.max_sessions_per_address > 0:
                self._address_hits.setdefault(address, deque()).append(now)
            if peer_ip and self.limits.max_sessions_per_ip > 0:
                self._ip_hits.setdefault(peer_ip, deque()).append(now)
            if is_new_address:
                # New address: create its worker store (most-recent ⇒ END of the LRU),
                # charge the new-address budget, then evict any LRU overflow so the store
                # stays bounded over the process lifetime.
                self._workers_by_address[address] = {worker_label}
                if self.limits.max_new_addresses_per_window > 0:
                    self._new_address_window.append(now)
                self._evict_tracked_overflow()
            else:
                # Seen address: keep it hot in the LRU and record any new credit key.
                self._workers_by_address.move_to_end(address)
                if is_new_worker:
                    workers.add(worker_label)
            return None

    @staticmethod
    def _prune(window: deque[datetime], now: datetime, horizon: timedelta) -> deque[datetime]:
        """Drop timestamps older than ``now - horizon`` from the left of ``window``."""

        cutoff = now - horizon
        while window and window[0] < cutoff:
            window.popleft()
        return window

    def _maybe_compact(self, now: datetime) -> None:
        """Compact the per-key hit maps at most once per rate window (amortized cheap).

        The hit maps accrue one key per distinct address / IP. A key touched once (a
        one-shot address or IP) would otherwise keep its now-stale deque forever, since
        :meth:`_prune` only runs on a key that is touched again. Sweeping once per window
        bounds both maps to keys active within roughly the last window. Must be called
        under :attr:`_lock`.
        """

        last = self._last_compaction
        if last is None or now - last >= self.limits.rate_window:
            self._compact(now)

    def _compact(self, now: datetime) -> None:
        """Prune every hit deque and drop the keys whose deque is left empty (expired).

        Only removes EMPTY (fully expired) windows — it never drops a deque that still
        holds an in-window timestamp — so it can never lower a live count: the limiter
        stays fail-closed (over-count, never under-count). Must be called under the lock.
        """

        window = self.limits.rate_window
        for hits_map in (self._address_hits, self._ip_hits):
            stale = [key for key, dq in hits_map.items() if not self._prune(dq, now, window)]
            for key in stale:
                del hits_map[key]
        self._last_compaction = now

    def _evict_tracked_overflow(self) -> None:
        """Evict least-recently-active addresses until the tracked store is within cap.

        Bounds :attr:`_workers_by_address` (the merged seen-set + per-address worker
        store). The just-admitted address was inserted/moved to the END, so it is never
        the one evicted. Evicting an address only forgets that it was seen and its set of
        worker labels: on return it is treated as new again (re-charging the new-address
        budget) — fail-closed (over-count), never a free pass. Must be called under the
        lock, after the just-admitted address has been placed at the END.
        """

        cap = self.limits.max_tracked_addresses
        while len(self._workers_by_address) > cap:
            self._workers_by_address.popitem(last=False)


# --- env coercion (fail-soft: garbage -> default) ----------------------------


def _nonneg_int(raw: str | None, default: int) -> int:
    """A non-negative int from env, else ``default`` (fail-soft). ``0`` is honored.

    ``0`` is a VALID explicit value (disable that cap) and is kept as-is. A negative /
    non-numeric value falls back to the safe default (a typo can never silently disable
    a cap by going negative).
    """

    text = (raw or "").strip()
    if not text:
        return default
    try:
        value = int(text)
    except ValueError:
        return default
    return value if value >= 0 else default


def _positive_int(raw: str | None, default: int) -> int:
    """A strictly-positive int from env, else ``default`` (fail-soft)."""

    text = (raw or "").strip()
    if not text:
        return default
    try:
        value = int(text)
    except ValueError:
        return default
    return value if value > 0 else default


def _positive_seconds(raw: str | None, default: timedelta) -> timedelta:
    """A strictly-positive seconds window from env, else ``default`` (fail-soft)."""

    text = (raw or "").strip()
    if not text:
        return default
    try:
        value = float(text)
    except ValueError:
        return default
    return timedelta(seconds=value) if value > 0 else default

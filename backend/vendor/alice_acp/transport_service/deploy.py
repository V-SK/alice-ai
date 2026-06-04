"""DEPLOY WIRING — the runnable LTC proxy-pool transport service (doc §2.1/§2.3/§2.8).

This is the single, env-driven entry point that turns the already-merged components
into a deployable asyncio service: it reads env, wires the miner-facing
:class:`~alice_acp.transport_service.stratum_server.StratumServer` to the
upstream-facing :class:`~alice_acp.transport_service.dispatcher.DispatcherRelay`
(built by :func:`~alice_acp.transport_service.proxy_pool_wiring.build_ltc_relay`
with the REAL F2Pool upstream + REAL Scrypt translator), and runs them on one
event loop. ``python -m alice_acp.transport_service`` calls :func:`build_and_run`.

WHAT IT WIRES (all from env; see :func:`load_transport_config`)
--------------------------------------------------------------
* A :class:`StratumIdentityResolver` (the §2.5 gate+registry+roster+session chain)
  backed by its OWN :class:`ShadowRewardLedger` configured ``require_device_pop=False``
  (the stratum path — roster membership is the proof) but SHARING the SAME durable
  roster / device-registry JSONL files the credit server uses (so both processes
  agree on identity), with the roster's ``worker_name_resolver`` wired (so the issued
  session carries the server-assigned ``worker_name``).
* A :class:`ShareValidator` (LTC = :class:`LocalScryptVerifier`) writing the SHARED
  durable :class:`JsonlValidatedShareStore` — the cross-process credit hand-off.
* A :class:`StratumServer` binding the LTC lane port.
* A :class:`DispatcherRelay` via :func:`build_ltc_relay` (the real
  ``ltc.f2pool.com:5200`` upstream + the real :class:`ScryptJobTranslator`), wired as
  the server's job source + solution sink.

THE CROSS-PROCESS STORE HAND-OFF (§2.8 active-passive credit plane)
-------------------------------------------------------------------
The transport service (this process) is the SOLE APPENDER to the shared
:class:`JsonlValidatedShareStore`: ``ShareValidator.validate`` calls ``store.record``
(a ``"share"`` row) for every Alice-re-hash-confirmed share. The credit server (a
SEPARATE process) is the SOLE DRAINER: its ``ProxyPoolEvidenceProvider.evidence_for``
calls ``store.drain_one`` (which appends a ``"spent"`` row) and credits through the
unchanged spine. The JSONL store is append-only with replay-on-construction, so the
two roles compose: the appender only ever adds ``"share"`` rows; the drainer only
ever adds ``"spent"`` rows and reads the un-spent queue. SINGLE-WRITER-PER-ROW-KIND
is the topology — there is no concurrent mutation of the same logical record, only
disjoint append streams to one file, each fsync-durable. (A drain IS a write — the
``"spent"`` marker — so the two processes are "single appender + single drainer",
NOT "one writer, one read-only reader"; the drainer is the credit-plane authority
for spend, the appender for record. They never both write the same kind of row.)

require_device_pop PER PATH (no shared-instance weakening)
----------------------------------------------------------
This service's ledger is a DISTINCT instance with ``require_device_pop=False`` (the
stratum path: there is no interactive PoP on a stock ASIC; the roster gate IS the
proof). The HTTP-edge credit server keeps its OWN ledger with
``require_device_pop=True``. They share the durable roster / registry / validated-
share FILES, never a ledger instance — so the stratum relaxation never weakens the
HTTP edge.

CREDIT-ONLY + secrets: nothing here sets a reward/payout/chain symbol; ``paid_acu``
is untouched (the credited unit is the validator's store write). The upstream login
password and the auth secret are read at use-time / from a file and handed straight
to the consumer; they are NEVER stored on a logged object or printed. Logs carry
only stable codes (the server/relay structured event lists already enforce this).
"""

from __future__ import annotations

import asyncio
import os
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from pathlib import Path

from alice_acp.shadow_server.device_registry_store import JsonlDeviceRegistry
from alice_acp.shadow_server.ledger import ShadowRewardLedger
from alice_acp.shadow_server.miner_roster import JsonlMinerRoster
from alice_acp.shadow_server.pool_evidence_providers import JsonlValidatedShareStore
from alice_acp.shadow_server.server import RECONSTRUCTED_SHARE_DIFFICULTY
from alice_acp.shadow_server.types import (
    MAIN_POOL_GPU_QUAI,
    MAIN_POOL_GPU_RVN,
    SCRYPT_POOL,
    XMR_POOL,
)
from alice_acp.share_validator.dedup_store import JsonlShareDedupStore
from alice_acp.share_validator.validator import ShareValidator
from alice_acp.share_validator.verifiers.kawpow import LocalKawPowVerifier
from alice_acp.share_validator.verifiers.randomx import LocalRandomXVerifier
from alice_acp.share_validator.verifiers.scrypt import LocalScryptVerifier
from alice_acp.transport_front.identity import (
    DEFAULT_PORT_LTC,
    DEFAULT_PORT_QUAI,
    DEFAULT_PORT_RVN,
    DEFAULT_PORT_XMR,
    PORT_LTC_ENV,
    PORT_QUAI_ENV,
    PORT_RVN_ENV,
    PORT_XMR_ENV,
    STRATUM_GATE_ENABLED_ENV,
    StratumIdentityResolver,
    stratum_gate_enabled,
)
from alice_acp.transport_front.open_enrollment import (
    OpenEnrollmentLimiter,
    OpenEnrollmentLimits,
    open_enrollment_enabled,
    open_enrollment_enabled_for_lane,
)

#: RVN OPEN-ENROLLMENT readiness flag (its OWN per-lane flag ``ALICE_RVN_OPEN_ENROLLMENT``,
#: default OFF). Imported here so the deploy config can surface whether the RVN lane is
#: open-enrollable; mirrors the XMR/LTC flags. Independent of the others — neither flips
#: another lane open.
from alice_acp.transport_service.dispatcher import (
    UPSTREAM_LTC_LOGIN_ENV,
    UPSTREAM_QUAI_LOGIN_ENV,
    UPSTREAM_RVN_LOGIN_ENV,
    UPSTREAM_XMR_LOGIN_ENV,
    ltc_credentials_from_env,
    quai_credentials_from_env,
    rvn_credentials_from_env,
    xmr_credentials_from_env,
)
from alice_acp.transport_service.proxy_pool_wiring import build_proxy_relay
from alice_acp.transport_service.stratum_server import (
    DEFAULT_PRE_LOGIN_TIMEOUT_S,
    DEFAULT_SUBMIT_FLOOD_BAN_S,
    LaneListenerConfig,
    StratumServer,
    build_connection_factory,
)

#: HARD-MINIMUM vardiff floor for the PUBLIC Scrypt/LTC lane (ASIC-only by design).
#: This is BOTH the no-``d=`` default AND a hard minimum: it stops a rig from running
#: Alice's re-hash validator at ``d=1`` (a flood of hundreds-of-thousands of sub-target
#: shares per second = CPU-burn DoS, since every submit runs a full
#: ``hashlib.scrypt(N=1024)`` re-hash BEFORE the difficulty/dedup checks). A login's own
#: ``d=`` may RAISE the floor above this (the right practice for a faster rig —
#: ``d ≈ hashrate_H/s × 15 / 65536``) but can NEVER lower it below: a hostile ``d=1`` is
#: CLAMPED UP to this minimum. An ASIC at this floor submits at a rate Alice can
#: comfortably re-hash (e.g. a ~9.5 GH/s rig → <10 shares/s).
LTC_ASIC_DEFAULT_VARDIFF_FLOOR = Decimal("16384")

#: TEST-RIG ESCAPE HATCH (env override of the LTC hard-minimum floor above). The PUBLIC
#: default stays :data:`LTC_ASIC_DEFAULT_VARDIFF_FLOOR` (16384) and a public deploy MUST
#: NOT set this. An INTERNAL / tailnet TEST deployment may lower it so a CPU rig is
#: usable on the lane: narissa (a CPU rig) equilibrates near a vardiff of ~86 and is
#: unusable at a 16384 floor (it would submit a share roughly every ~190× the target
#: interval). Setting e.g. ``ALICE_LTC_MIN_VARDIFF_FLOOR=1`` on the test box lets such a
#: rig set its own ``d=`` (or run at the lowered minimum) WITHOUT weakening the public
#: lane — the public process simply never sets this env var. A garbage / non-positive
#: value is IGNORED (fail-soft) and the 16384 default is kept. Reading it here (not in a
#: client field) keeps the floor SERVER-SIDE authoritative.
LTC_MIN_VARDIFF_FLOOR_ENV = "ALICE_LTC_MIN_VARDIFF_FLOOR"

#: HARD-MINIMUM vardiff floor for the PUBLIC XMR/RandomX lane (a CPU lane — NOT the
#: 16384 ASIC clamp). RandomX is the ONLY scale-asymmetric leg: EVERY submit runs ONE
#: full RandomX hash on Alice's side (~ms each, ~256 MiB cache per seed), so the floor's
#: job is to BOUND THE WORST-CASE RE-HASH RATE, not to gate a fast ASIC. We pick a modest
#: ``1000``: a single CPU rig hashes ~1-5 kH/s, so ``d ~= hashrate * 15s`` puts a sane rig
#: well above this; the floor only clamps a hostile/buggy ``d=1`` that would otherwise
#: flood Alice with one full RandomX hash per sub-target share. At ``d=1000`` a 5 kH/s
#: rig submits ~one share every ~0.2s — comfortably re-hashable — and a flood is bounded
#: to << the librandomx throughput. Like the LTC floor this is BOTH the no-``d=`` default
#: AND a hard minimum a login's ``d=`` may RAISE but never lower; env-overridable for a
#: faster/internal rig via :data:`XMR_MIN_VARDIFF_FLOOR_ENV`.
XMR_DEFAULT_VARDIFF_FLOOR = Decimal("1000")

#: ENV OVERRIDE of the XMR hard-minimum floor above (the RandomX mirror of
#: :data:`LTC_MIN_VARDIFF_FLOOR_ENV`). A faster CPU farm may RAISE it; an internal test
#: box with a slow single core may LOWER it (e.g. ``1``). A garbage / non-positive value
#: is IGNORED (fail-soft) and the :data:`XMR_DEFAULT_VARDIFF_FLOOR` default is kept.
#: Server-side authoritative (read here, never a client field).
XMR_MIN_VARDIFF_FLOOR_ENV = "ALICE_XMR_MIN_VARDIFF_FLOOR"

#: STARTING/MINIMUM vardiff floor for the PUBLIC RVN/KawPoW lane (a GPU lane — NOT the
#: 16384 ASIC clamp, NOT a per-share full-hash CPU bound like XMR). Its job here is the
#: GPU analog of LTC's ASIC floor / XMR's CPU floor: start a rig WITHIN A FEW retarget
#: steps of its equilibrium so the vardiff does not have to climb a long 4x-per-step ramp
#: (each step is a ``mining.set_target`` the rig must track, and during each step the
#: rig's in-flight shares against the not-yet-applied lower target re-hash BELOW the new
#: pool target and reject as SHARE_VALIDATOR_LOW_DIFFICULTY — the straddle that produced
#: the 62.7%-acceptance / reconnect-storm smoke).
#:
#: THE MATH (2**256 scale, ``d = 2**256 / H_target``; share rate = hashrate / d). A
#: KawPoW GPU equilibrates at ``d_eq ~= hashrate * target_interval``:
#:   * a weak ~0.5 MH/s GPU -> d_eq ~= 0.5e6 * 15 s ~= 7.5e6
#:   * a 3070Ti ~15-25 MH/s -> d_eq ~= 2.3e8 .. 3.8e8
#:   * a 200 MH/s small rig -> d_eq ~= 3.0e9
#: From a floor of 1 the 4x-per-step clamp needs ``log4(d_eq)`` ~= 14 steps for a 3070Ti
#: (measured: 1->4->16->...->2.25e8 = 14 set_target pushes). We pick ``262144`` (= 2**18
#: ~= 2.6e5, the same power-of-two shape as LTC's 16384 = 2**14): it sits an order of
#: magnitude BELOW even the weakest GPU's equilibrium (7.5e6 / 2.6e5 ~= 29x, so the
#: weakest GPU still ramps UP -- never starts ABOVE equilibrium and submits too slowly,
#: since the floor is a hard MINIMUM d= cannot lower), yet high enough that a 3070Ti
#: reaches equilibrium in ~5 steps (measured) instead of ~14, and a 200 MH/s rig in ~7.
#: ``order ~1e5-1e6`` as sized for the GPU lane. (Difficulty is on the 2^256 "expected-hash
#: count" scale -- ``result_difficulty = 2^256 / final_hash``, == ravenminer's conventional
#: wire difficulty x 2^32; MEASURED. The transient 84-shares/s at the floor for a fast GPU
#: lasts ~0.2 s before the first retarget -- harmless -- and is the unavoidable cost of a
#: single hard floor that must also sit below a weak GPU's equilibrium.)
#:
#: Like the LTC/XMR floors this is BOTH the no-``d=`` default AND a hard minimum a login's
#: ``d=`` may RAISE but never lower; env-overridable via :data:`RVN_MIN_VARDIFF_FLOOR_ENV`
#: (a slow internal test GPU may lower it; a fast farm may raise it). (Alice's KawPoW
#: re-hash is one DAG lookup per share — cheap relative to RandomX — so unlike XMR this
#: floor is sized for sane vardiff RAMP behaviour, not a worst-case-rehash bound.)
RVN_DEFAULT_VARDIFF_FLOOR = Decimal("262144")

#: ENV OVERRIDE of the RVN hard-minimum floor above (the KawPoW mirror of
#: :data:`XMR_MIN_VARDIFF_FLOOR_ENV`). A farm may RAISE it to bound its per-share re-hash
#: rate; an internal test box may keep the default ``1``. A garbage / non-positive value is
#: IGNORED (fail-soft) and the :data:`RVN_DEFAULT_VARDIFF_FLOOR` default is kept. Server-side
#: authoritative (read here, never a client field).
RVN_MIN_VARDIFF_FLOOR_ENV = "ALICE_RVN_MIN_VARDIFF_FLOOR"

#: STARTING/MINIMUM vardiff floor for the PUBLIC Quai/KawPoW lane. Quai's PoW IS KawPoW, so
#: the lane's vardiff sizing is IDENTICAL to the RVN lane: 262144 (= 2**18) — a GPU-sized
#: starting floor so a rig converges in a few retarget steps instead of climbing the long
#: 4x-per-step ramp from 1 (NOT the 16384 ASIC clamp). Mirror of
#: :data:`RVN_DEFAULT_VARDIFF_FLOOR`. Like the other lanes' floors this is BOTH the no-``d=``
#: default AND a hard minimum a login's ``d=`` may RAISE but never lower; env-overridable via
#: :data:`QUAI_MIN_VARDIFF_FLOOR_ENV`.
QUAI_DEFAULT_VARDIFF_FLOOR = Decimal("262144")

#: ENV OVERRIDE of the Quai hard-minimum floor above (the Quai mirror of
#: :data:`RVN_MIN_VARDIFF_FLOOR_ENV`). A farm may RAISE it; a slow internal test GPU may
#: LOWER it. A garbage / non-positive value is IGNORED (fail-soft) and the
#: :data:`QUAI_DEFAULT_VARDIFF_FLOOR` default is kept. Server-side authoritative (read here,
#: never a client field).
QUAI_MIN_VARDIFF_FLOOR_ENV = "ALICE_QUAI_MIN_VARDIFF_FLOOR"

#: NEW env: the SHARED validated-share JSONL the transport service APPENDS to and the
#: credit server DRAINS (the §2.4/§2.8 cross-process credit hand-off). Both processes
#: MUST point at the SAME path. A directory is accepted (the store appends
#: ``validated_shares.jsonl`` inside it); a ``.jsonl`` file path is used as-is.
PROXY_VALIDATED_SHARE_STORE_ENV = "ALICE_PROXY_VALIDATED_SHARE_STORE"

#: OPEN ENROLLMENT (review #3/#4): a DURABLE per-identity issuance cap wired onto the
#: transport ledger's ``max_issuances_per_identity`` when open mode is on. This is the
#: ledger-side complement to the transport-front :class:`OpenEnrollmentLimiter` — it
#: hard-bounds how many sessions ONE ``(address, worker_label)`` may ever be issued (a
#: cap on un-spent queued shares / credit keys per identity). ``0`` (the default) leaves
#: the ledger cap disabled (the in-memory limiter still applies). A garbage / negative
#: value is ignored (fail-soft -> 0). NOTE: this is process-lifetime in-ledger state.
OPEN_MAX_ISSUANCES_PER_IDENTITY_ENV = "ALICE_LTC_OPEN_MAX_ISSUANCES_PER_IDENTITY"
DEFAULT_OPEN_MAX_ISSUANCES_PER_IDENTITY = 0

#: REUSED from the credit server (http_app.main): the durable data dir rooting the
#: roster + device-registry JSONL. Pointing the transport service at the SAME dir is
#: what makes the two processes share one roster/registry (identity parity).
SHADOW_DATA_DIR_ENV = "ALICE_ACP_SHADOW_DATA_DIR"
#: REUSED from the credit server: the file holding the session-signing HMAC secret.
#: Read at startup, handed to the ledger, never logged. The SAME secret as the credit
#: server so the issued session signatures verify across processes.
SHADOW_AUTH_FILE_ENV = "ALICE_ACP_SHADOW_AUTH_FILE"

#: The interface the LTC stratum listener binds. Defaults to loopback; the deploy sets
#: the real interface (behind the same trusted reverse proxy as the credit edge).
STRATUM_BIND_HOST_ENV = "ALICE_ACP_STRATUM_BIND_HOST"
DEFAULT_STRATUM_BIND_HOST = "0.0.0.0"

#: DoS-HARDENING env knobs for the public stratum accept path. The slowloris deadline
#: is generous; the connection caps now carry NON-ZERO PRODUCTION DEFAULTS (#F-A op
#: hardening) so the DoS caps are LIVE out of the box on this public-edge deploy — the
#: library defaults (:data:`stratum_server.DEFAULT_MAX_CONNECTIONS_PER_IP` /
#: ``_GLOBAL`` = ``0`` = unlimited) left the caps inert. All three stay env-overridable.
#:
#: * ``ALICE_STRATUM_PRELOGIN_TIMEOUT_S`` — seconds a connection has to complete
#:   subscribe+authorize before it is dropped (slowloris defense). Default 30s.
#: * ``ALICE_STRATUM_MAX_CONN_PER_IP`` — max simultaneous connections from one peer IP
#:   (``0`` = unlimited). DEFAULT :data:`PROD_MAX_CONNECTIONS_PER_IP` (64) — generous
#:   for a small farm of ASICs behind ONE NAT/router yet far below a single-IP flood.
#:   Raise it for a large single-IP farm; set ``0`` only to disable the per-IP cap.
#: * ``ALICE_STRATUM_MAX_CONN_GLOBAL`` — max simultaneous connections across all peers
#:   (``0`` = unlimited). DEFAULT :data:`PROD_MAX_CONNECTIONS_GLOBAL` (2048) — a
#:   capacity-shaped ceiling in the low thousands that bounds total accepted sockets
#:   (and thus per-connection memory / the slowloris fan-out) on a single listener.
#: A garbage / negative value for any of these is IGNORED (fail-soft -> the default).
STRATUM_PRELOGIN_TIMEOUT_ENV = "ALICE_STRATUM_PRELOGIN_TIMEOUT_S"
STRATUM_MAX_CONN_PER_IP_ENV = "ALICE_STRATUM_MAX_CONN_PER_IP"
STRATUM_MAX_CONN_GLOBAL_ENV = "ALICE_STRATUM_MAX_CONN_GLOBAL"

#: SUBMIT-FLOOD env knobs (the audit BLOCKER fix). The per-connection submit-flood limiter
#: stops a line-rate flood of well-formed SUB-TARGET / invalid submits from ever reaching
#: the expensive PoW re-hash (which vardiff cannot throttle — it reacts only to ACCEPTED
#: shares). All carry NON-ZERO production defaults so the limiter is LIVE out of the box on
#: this public edge; all stay env-overridable (a garbage / non-positive value is IGNORED,
#: fail-soft → the default; ``0`` is the explicit operator opt-out where noted).
#:
#: * ``ALICE_STRATUM_SUBMIT_RATE_HEADROOM`` — the GENEROUS multiple of the at-target share
#:   rate the per-window rate cap is sized to. The vardiff aims for ~1 accepted share per
#:   ~15s, so the at-target rate is tiny; a headroom of :data:`PROD_SUBMIT_RATE_HEADROOM`
#:   (1000x) puts the cap orders of magnitude above any legit rig's submit rate while still
#:   cutting a thousands/sec flood. ``0`` / garbage → the 1000x default (NOT off — a public
#:   edge always wants the rate cap); to truly disable the rate cap, build the factory
#:   directly (tests do). The cap itself is computed lane-aware by ``for_lane_rate``.
#: * ``ALICE_STRATUM_SUBMIT_MAX_CONSEC_REJECTS`` — max CONSECUTIVE below-target / invalid
#:   submits before the connection is dropped + briefly banned (the ckpool reject-run
#:   shape; the half vardiff cannot do). DEFAULT :data:`PROD_SUBMIT_MAX_CONSEC_REJECTS`
#:   (512) tolerates a transient retarget-straddle burst (the bounded grace already absorbs
#:   the common case) yet stops an unbounded reject flood. ``0`` disables the run cap.
#: * ``ALICE_STRATUM_SUBMIT_FLOOD_BAN_S`` — seconds a peer that tripped the limiter is
#:   refused at admission (so it cannot instantly reconnect + re-flood). DEFAULT
#:   :data:`stratum_server.DEFAULT_SUBMIT_FLOOD_BAN_S` (30s). ``0`` disables the
#:   reconnect-refusal (the per-connection drop still fires).
#: * ``ALICE_STRATUM_VERIFY_CONCURRENCY`` — max PoW re-hashes running CONCURRENTLY in the
#:   off-loop verify pool (the Semaphore size). ``0`` / unset → auto-sized to ≈ CPU cores
#:   (the library default), which bounds total re-hash CPU while keeping the loop responsive.
STRATUM_SUBMIT_RATE_HEADROOM_ENV = "ALICE_STRATUM_SUBMIT_RATE_HEADROOM"
STRATUM_SUBMIT_MAX_CONSEC_REJECTS_ENV = "ALICE_STRATUM_SUBMIT_MAX_CONSEC_REJECTS"
STRATUM_SUBMIT_FLOOD_BAN_ENV = "ALICE_STRATUM_SUBMIT_FLOOD_BAN_S"
STRATUM_VERIFY_CONCURRENCY_ENV = "ALICE_STRATUM_VERIFY_CONCURRENCY"

#: PRODUCTION defaults for the submit-flood limiter (the BLOCKER fix), LIVE out of the box.
#: 1000x headroom: a generous multiple of the ~1-share/15s at-target rate, so the per-window
#: rate cap lands orders of magnitude above any legit rig yet far below a flood. 512
#: consecutive rejects: tolerant of a transient retarget straddle, decisive on a sustained
#: reject flood. Both env-overridable; sized GENEROUSLY so a legit fast rig is never harmed.
PROD_SUBMIT_RATE_HEADROOM = Decimal("1000")
PROD_SUBMIT_MAX_CONSEC_REJECTS = 512

#: PRODUCTION defaults for the two connection caps (#F-A op hardening). Distinct from
#: the library ``DEFAULT_MAX_CONNECTIONS_*`` (0 = unlimited): the deployed public edge
#: opts INTO sane finite caps by default while a bare ``StratumServer`` (tests, internal
#: callers) keeps the unlimited library default. Both are env-overridable; ``0`` via env
#: still means "unlimited" (the explicit operator opt-out).
#:
#: 64 per IP: a single NAT/router fronting a small ASIC farm can hold many simultaneous
#: rig connections; 64 is generous for that yet blocks a single source opening thousands
#: of sockets. 2048 global: a low-thousands ceiling on total concurrent sockets for one
#: listener — comfortably above expected legitimate fan-in, well under a resource-
#: exhausting flood. An operator sizes both to real capacity at deploy.
PROD_MAX_CONNECTIONS_PER_IP = 64
PROD_MAX_CONNECTIONS_GLOBAL = 2048

#: Per-connection session TTL for the minted stratum session (mirrors the resolver
#: default; overridable for long-lived ASIC connections).
DEFAULT_SESSION_TTL = timedelta(minutes=30)

#: Stable, secret-free startup/lifecycle codes for the service's own structured log.
SERVICE_STARTING = "transport_service_starting"
SERVICE_GATE_DISABLED = "transport_service_gate_disabled"
SERVICE_LTC_LOGIN_ABSENT = "transport_service_ltc_login_absent"
SERVICE_LISTENING = "transport_service_listening"
SERVICE_UPSTREAM_UNCONFIGURED = "transport_service_upstream_unconfigured"
SERVICE_STOPPED = "transport_service_stopped"


class TransportServiceConfigError(RuntimeError):
    """A fail-soft startup refusal: a clear, secret-free reason, no traceback/crash."""


@dataclass(frozen=True, slots=True)
class TransportServiceConfig:
    """The env-resolved, secret-free wiring config for the LTC transport service.

    Carries only NON-secret facts (paths, the listen port, the gate flag). The auth
    secret and the upstream password are NOT held here — they are read at use-time
    (the secret into the ledger at build, the password by the relay's cred factory
    at connect) so no secret lands on a config object that could be logged.
    """

    data_dir: Path
    validated_share_store_path: Path
    auth_secret_file: Path | None
    ltc_port: int
    bind_host: str
    gate_enabled: bool
    #: Whether the LTC upstream login env is present (drives the fail-soft note).
    ltc_login_present: bool
    #: The XMR/RandomX lane listener port (default 3333) + whether its upstream login
    #: (Alice's Monero address) is present (drives the fail-soft note, like the LTC one).
    xmr_port: int = DEFAULT_PORT_XMR
    xmr_login_present: bool = False
    #: The RVN/KawPoW lane listener port (default 4444) + whether its upstream login
    #: (Alice's Ravencoin address) is present (drives the fail-soft note, like the others).
    rvn_port: int = DEFAULT_PORT_RVN
    rvn_login_present: bool = False
    #: The Quai/KawPoW lane listener port (default 7777) + whether its upstream login
    #: (Alice's Quai 0x address) is present (drives the fail-soft note, like the others).
    quai_port: int = DEFAULT_PORT_QUAI
    quai_login_present: bool = False
    #: The resolved HARD-MINIMUM vardiff floor for the LTC lane (the public default is
    #: :data:`LTC_ASIC_DEFAULT_VARDIFF_FLOOR`; an internal TEST deploy may lower it via
    #: :data:`LTC_MIN_VARDIFF_FLOOR_ENV`). A login's ``d=`` may raise but never lower it.
    ltc_min_vardiff_floor: Decimal = LTC_ASIC_DEFAULT_VARDIFF_FLOOR
    #: The resolved HARD-MINIMUM vardiff floor for the XMR lane (the public default is
    #: :data:`XMR_DEFAULT_VARDIFF_FLOOR` = 1000; env-overridable via
    #: :data:`XMR_MIN_VARDIFF_FLOOR_ENV`). A login's ``d=`` may raise but never lower it.
    xmr_min_vardiff_floor: Decimal = XMR_DEFAULT_VARDIFF_FLOOR
    #: The resolved HARD-MINIMUM vardiff floor for the RVN lane (the public default is
    #: :data:`RVN_DEFAULT_VARDIFF_FLOOR` = 262144 = 2**18, a GPU-sized starting floor so a
    #: rig converges in a few retarget steps — NOT the 16384 ASIC clamp; env-overridable
    #: via :data:`RVN_MIN_VARDIFF_FLOOR_ENV`). A login's ``d=`` may raise but never lower it.
    rvn_min_vardiff_floor: Decimal = RVN_DEFAULT_VARDIFF_FLOOR
    #: The resolved HARD-MINIMUM vardiff floor for the Quai lane (the public default is
    #: :data:`QUAI_DEFAULT_VARDIFF_FLOOR` = 262144 = 2**18, the SAME GPU-sized floor as RVN
    #: since Quai is KawPoW; env-overridable via :data:`QUAI_MIN_VARDIFF_FLOOR_ENV`). A
    #: login's ``d=`` may raise but never lower it.
    quai_min_vardiff_floor: Decimal = QUAI_DEFAULT_VARDIFF_FLOOR
    #: OPEN ENROLLMENT (review #3/#4): whether the registration-less
    #: ``<Alice_address>.<worker>`` model is enabled for the LTC lane (default OFF —
    #: byte-for-byte the roster path when off). When ON, the validated Alice address is
    #: the admission gate and the :class:`OpenEnrollmentLimiter` budgets new miners.
    open_enrollment: bool = False
    #: OPEN ENROLLMENT on the XMR lane (its OWN per-lane flag ``ALICE_XMR_OPEN_ENROLLMENT``,
    #: default OFF). Independent of the LTC flag — neither can flip the other lane open.
    xmr_open_enrollment: bool = False
    #: OPEN ENROLLMENT on the RVN lane (its OWN per-lane flag ``ALICE_RVN_OPEN_ENROLLMENT``,
    #: default OFF). Independent of the LTC/XMR flags — none can flip another lane open.
    rvn_open_enrollment: bool = False
    #: OPEN ENROLLMENT on the Quai lane (its OWN per-lane flag ``ALICE_QUAI_OPEN_ENROLLMENT``,
    #: default OFF). Independent of every other lane's flag — none can flip another lane open.
    quai_open_enrollment: bool = False
    #: DURABLE per-(address,worker) ledger issuance cap when open mode is on (0=off).
    open_max_issuances_per_identity: int = DEFAULT_OPEN_MAX_ISSUANCES_PER_IDENTITY
    #: DoS-hardening accept-path knobs (resolved from env). The slowloris deadline is
    #: generous; the connection caps default to the NON-ZERO production values (#F-A op
    #: hardening) so a config built directly (not via :meth:`from_env`) is still capped.
    pre_login_timeout_s: float = DEFAULT_PRE_LOGIN_TIMEOUT_S
    max_connections_per_ip: int = PROD_MAX_CONNECTIONS_PER_IP
    max_connections_global: int = PROD_MAX_CONNECTIONS_GLOBAL
    #: SUBMIT-FLOOD limiter knobs (the BLOCKER fix), resolved from env with NON-ZERO
    #: production defaults so the per-connection flood gate is LIVE out of the box. The rate
    #: headroom sizes the per-window submit-rate cap (a generous multiple of the at-target
    #: rate); the consecutive-reject cap bounds a sustained reject run; the flood-ban seconds
    #: refuse a tripped peer at admission; the verify concurrency bounds off-loop re-hashes.
    submit_rate_headroom: Decimal = PROD_SUBMIT_RATE_HEADROOM
    submit_max_consecutive_rejects: int = PROD_SUBMIT_MAX_CONSEC_REJECTS
    submit_flood_ban_s: float = DEFAULT_SUBMIT_FLOOD_BAN_S
    #: Off-loop verify concurrency (the Semaphore size). ``0`` → auto-size to ≈ CPU cores.
    verify_concurrency: int = 0
    session_ttl: timedelta = DEFAULT_SESSION_TTL

    def public_summary(self) -> dict[str, object]:
        """A secret-free dict for the startup log (paths + flags; NO secret)."""

        return {
            "data_dir": str(self.data_dir),
            "validated_share_store": str(self.validated_share_store_path),
            "auth_secret_file_configured": self.auth_secret_file is not None,
            "ltc_port": self.ltc_port,
            "xmr_port": self.xmr_port,
            "rvn_port": self.rvn_port,
            "quai_port": self.quai_port,
            "bind_host": self.bind_host,
            "gate_enabled": self.gate_enabled,
            "ltc_login_present": self.ltc_login_present,
            "xmr_login_present": self.xmr_login_present,
            "rvn_login_present": self.rvn_login_present,
            "quai_login_present": self.quai_login_present,
            "ltc_min_vardiff_floor": format(self.ltc_min_vardiff_floor.normalize(), "f"),
            "xmr_min_vardiff_floor": format(self.xmr_min_vardiff_floor.normalize(), "f"),
            "rvn_min_vardiff_floor": format(self.rvn_min_vardiff_floor.normalize(), "f"),
            "quai_min_vardiff_floor": format(self.quai_min_vardiff_floor.normalize(), "f"),
            "open_enrollment": self.open_enrollment,
            "xmr_open_enrollment": self.xmr_open_enrollment,
            "rvn_open_enrollment": self.rvn_open_enrollment,
            "quai_open_enrollment": self.quai_open_enrollment,
            "open_max_issuances_per_identity": self.open_max_issuances_per_identity,
            "pre_login_timeout_s": self.pre_login_timeout_s,
            "max_connections_per_ip": self.max_connections_per_ip,
            "max_connections_global": self.max_connections_global,
            "submit_rate_headroom": format(self.submit_rate_headroom.normalize(), "f"),
            "submit_max_consecutive_rejects": self.submit_max_consecutive_rejects,
            "submit_flood_ban_s": self.submit_flood_ban_s,
            "verify_concurrency": self.verify_concurrency,
        }


def _env(env: dict[str, str] | None) -> dict[str, str]:
    return env if env is not None else dict(os.environ)


def load_transport_config(env: dict[str, str] | None = None) -> TransportServiceConfig:
    """Resolve the LTC transport-service config from env (fail-soft, secret-free).

    Raises :class:`TransportServiceConfigError` with a CLEAR, secret-free message for
    a misconfiguration the service cannot run with (no data dir, no validated-share
    store path). The ABSENCE of the LTC upstream login is NOT an error here — the
    service still starts and serves the miner edge; the relay's lane simply stays
    unconnected (fail-soft) until the login env is supplied. The gate flag is read
    but a disabled gate is also not an error (the server rejects logins fail-closed).
    """

    source = _env(env)

    data_dir_raw = (source.get(SHADOW_DATA_DIR_ENV) or "").strip()
    if not data_dir_raw:
        raise TransportServiceConfigError(
            f"{SHADOW_DATA_DIR_ENV} is required (the durable roster/registry dir, "
            "shared with the credit server)"
        )
    data_dir = Path(data_dir_raw)

    store_raw = (source.get(PROXY_VALIDATED_SHARE_STORE_ENV) or "").strip()
    if not store_raw:
        raise TransportServiceConfigError(
            f"{PROXY_VALIDATED_SHARE_STORE_ENV} is required (the shared validated-share "
            "JSONL the credit server drains)"
        )
    validated_share_store_path = Path(store_raw)

    auth_file_raw = (source.get(SHADOW_AUTH_FILE_ENV) or "").strip()
    auth_secret_file = Path(auth_file_raw) if auth_file_raw else None

    ltc_port = _read_port(source.get(PORT_LTC_ENV), DEFAULT_PORT_LTC, env_name=PORT_LTC_ENV)
    xmr_port = _read_port(source.get(PORT_XMR_ENV), DEFAULT_PORT_XMR, env_name=PORT_XMR_ENV)
    rvn_port = _read_port(source.get(PORT_RVN_ENV), DEFAULT_PORT_RVN, env_name=PORT_RVN_ENV)
    quai_port = _read_port(source.get(PORT_QUAI_ENV), DEFAULT_PORT_QUAI, env_name=PORT_QUAI_ENV)
    bind_host = (source.get(STRATUM_BIND_HOST_ENV) or DEFAULT_STRATUM_BIND_HOST).strip()
    gate_enabled = stratum_gate_enabled(source)
    ltc_login_present = bool((source.get(UPSTREAM_LTC_LOGIN_ENV) or "").strip())
    xmr_login_present = bool((source.get(UPSTREAM_XMR_LOGIN_ENV) or "").strip())
    rvn_login_present = bool((source.get(UPSTREAM_RVN_LOGIN_ENV) or "").strip())
    quai_login_present = bool((source.get(UPSTREAM_QUAI_LOGIN_ENV) or "").strip())
    ltc_min_vardiff_floor = _read_min_vardiff_floor(source.get(LTC_MIN_VARDIFF_FLOOR_ENV))
    xmr_min_vardiff_floor = _read_min_vardiff_floor(
        source.get(XMR_MIN_VARDIFF_FLOOR_ENV), default=XMR_DEFAULT_VARDIFF_FLOOR
    )
    rvn_min_vardiff_floor = _read_min_vardiff_floor(
        source.get(RVN_MIN_VARDIFF_FLOOR_ENV), default=RVN_DEFAULT_VARDIFF_FLOOR
    )
    quai_min_vardiff_floor = _read_min_vardiff_floor(
        source.get(QUAI_MIN_VARDIFF_FLOOR_ENV), default=QUAI_DEFAULT_VARDIFF_FLOOR
    )
    open_enrollment = open_enrollment_enabled(source)
    xmr_open_enrollment = open_enrollment_enabled_for_lane(XMR_POOL, source)
    rvn_open_enrollment = open_enrollment_enabled_for_lane(MAIN_POOL_GPU_RVN, source)
    quai_open_enrollment = open_enrollment_enabled_for_lane(MAIN_POOL_GPU_QUAI, source)
    open_max_issuances_per_identity = _read_nonneg_int(
        source.get(OPEN_MAX_ISSUANCES_PER_IDENTITY_ENV),
        DEFAULT_OPEN_MAX_ISSUANCES_PER_IDENTITY,
    )
    pre_login_timeout_s = _read_positive_float(
        source.get(STRATUM_PRELOGIN_TIMEOUT_ENV), DEFAULT_PRE_LOGIN_TIMEOUT_S
    )
    # #F-A op hardening: the public-edge deploy defaults the connection caps to the
    # NON-ZERO production values (the library default is 0=unlimited, which left the DoS
    # caps inert). Still env-overridable; an explicit env ``0`` re-opts-out (unlimited).
    max_connections_per_ip = _read_nonneg_int(
        source.get(STRATUM_MAX_CONN_PER_IP_ENV), PROD_MAX_CONNECTIONS_PER_IP
    )
    max_connections_global = _read_nonneg_int(
        source.get(STRATUM_MAX_CONN_GLOBAL_ENV), PROD_MAX_CONNECTIONS_GLOBAL
    )
    # SUBMIT-FLOOD limiter (the BLOCKER fix): the rate headroom + consecutive-reject cap +
    # the brief reconnect-ban + the off-loop verify concurrency. All default NON-ZERO (LIVE
    # by default); a garbage / non-positive value is IGNORED (fail-soft → the default), so a
    # typo can never silently disable the flood protection.
    submit_rate_headroom = _read_positive_decimal(
        source.get(STRATUM_SUBMIT_RATE_HEADROOM_ENV), PROD_SUBMIT_RATE_HEADROOM
    )
    submit_max_consecutive_rejects = _read_nonneg_int(
        source.get(STRATUM_SUBMIT_MAX_CONSEC_REJECTS_ENV), PROD_SUBMIT_MAX_CONSEC_REJECTS
    )
    submit_flood_ban_s = _read_nonneg_float(
        source.get(STRATUM_SUBMIT_FLOOD_BAN_ENV), DEFAULT_SUBMIT_FLOOD_BAN_S
    )
    # Verify concurrency: 0 = auto-size to ≈ cores (the library default). A negative /
    # garbage value falls back to 0 (auto), so the off-loop bound can never be disabled.
    verify_concurrency = _read_nonneg_int(source.get(STRATUM_VERIFY_CONCURRENCY_ENV), 0)

    return TransportServiceConfig(
        data_dir=data_dir,
        validated_share_store_path=validated_share_store_path,
        auth_secret_file=auth_secret_file,
        ltc_port=ltc_port,
        bind_host=bind_host,
        gate_enabled=gate_enabled,
        ltc_login_present=ltc_login_present,
        xmr_port=xmr_port,
        xmr_login_present=xmr_login_present,
        rvn_port=rvn_port,
        rvn_login_present=rvn_login_present,
        quai_port=quai_port,
        quai_login_present=quai_login_present,
        ltc_min_vardiff_floor=ltc_min_vardiff_floor,
        xmr_min_vardiff_floor=xmr_min_vardiff_floor,
        rvn_min_vardiff_floor=rvn_min_vardiff_floor,
        quai_min_vardiff_floor=quai_min_vardiff_floor,
        open_enrollment=open_enrollment,
        xmr_open_enrollment=xmr_open_enrollment,
        rvn_open_enrollment=rvn_open_enrollment,
        quai_open_enrollment=quai_open_enrollment,
        open_max_issuances_per_identity=open_max_issuances_per_identity,
        pre_login_timeout_s=pre_login_timeout_s,
        max_connections_per_ip=max_connections_per_ip,
        max_connections_global=max_connections_global,
        submit_rate_headroom=submit_rate_headroom,
        submit_max_consecutive_rejects=submit_max_consecutive_rejects,
        submit_flood_ban_s=submit_flood_ban_s,
        verify_concurrency=verify_concurrency,
    )


def _read_port(raw: str | None, default: int, *, env_name: str = PORT_LTC_ENV) -> int:
    text = (raw or "").strip()
    if not text:
        return default
    try:
        port = int(text)
    except ValueError as exc:
        raise TransportServiceConfigError(f"{env_name} must be an integer") from exc
    if port < 0 or port > 65535:
        raise TransportServiceConfigError(f"{env_name} must be a valid TCP port")
    return port


def _read_min_vardiff_floor(
    raw: str | None, *, default: Decimal = LTC_ASIC_DEFAULT_VARDIFF_FLOOR
) -> Decimal:
    """Resolve a lane's hard-minimum vardiff floor from env (fail-soft, never a crash).

    ``default`` is the PUBLIC floor for the lane (LTC = :data:`LTC_ASIC_DEFAULT_VARDIFF_FLOOR`
    16384; XMR = :data:`XMR_DEFAULT_VARDIFF_FLOOR` 1000). An internal TEST deploy may set
    the lane's env override to a LOWER positive value (LTC: a CPU rig near ~86; XMR: a
    slow single core). A missing / garbage / non-positive value is IGNORED and the
    ``default`` is kept (fail-soft — never let a typo silently disable a lane's flood
    protection by crashing, and never let it silently take effect either). This is NOT a
    startup-fatal misconfiguration.
    """

    text = (raw or "").strip()
    if not text:
        return default
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        return default
    if value <= 0:
        return default
    return value


def _read_positive_float(raw: str | None, default: float) -> float:
    """A positive float from env, else ``default`` (fail-soft; garbage/<=0 -> default).

    Used for the pre-login deadline. A non-numeric or non-positive value keeps the safe
    default (a typo can never silently DISABLE the slowloris deadline).
    """

    text = (raw or "").strip()
    if not text:
        return default
    try:
        value = float(text)
    except ValueError:
        return default
    return value if value > 0 else default


def _read_nonneg_int(raw: str | None, default: int) -> int:
    """A non-negative int from env, else ``default`` (fail-soft). ``0`` means unlimited.

    Used for the per-IP / global connection caps. A non-numeric or negative value keeps
    the default. ``0`` is a VALID explicit value (unlimited) and is honored as given.
    """

    text = (raw or "").strip()
    if not text:
        return default
    try:
        value = int(text)
    except ValueError:
        return default
    return value if value >= 0 else default


def _read_nonneg_float(raw: str | None, default: float) -> float:
    """A non-negative float from env, else ``default`` (fail-soft). ``0`` is honored.

    Used for the brief submit-flood ban seconds. A non-numeric or negative value keeps the
    default; ``0`` is a VALID explicit value (disable the reconnect-refusal) honored as given.
    """

    text = (raw or "").strip()
    if not text:
        return default
    try:
        value = float(text)
    except ValueError:
        return default
    return value if value >= 0 else default


def _read_positive_decimal(raw: str | None, default: Decimal) -> Decimal:
    """A positive Decimal from env, else ``default`` (fail-soft; garbage / <=0 → default).

    Used for the submit-rate headroom (a generous multiple of the at-target rate). A
    non-numeric or non-positive value keeps the default — a typo can never silently disable
    the rate cap by zeroing the headroom.
    """

    text = (raw or "").strip()
    if not text:
        return default
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        return default
    return value if value > 0 else default


def _read_auth_secret(path: Path | None) -> bytes | None:
    """Read the auth-secret file at use-time (never logged). ``None`` when unset.

    A configured-but-unreadable secret file is a fail-soft startup refusal (clear
    error), not a crash — and the reason carries the PATH only, never the contents.
    """

    if path is None:
        return None
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        raise TransportServiceConfigError(
            f"auth secret file unreadable: {path} ({exc.strerror or 'os error'})"
        ) from exc
    if not text:
        raise TransportServiceConfigError(f"auth secret file is empty: {path}")
    return text.encode("utf-8")


@dataclass(slots=True)
class TransportService:
    """The assembled, runnable LTC transport service (server + relay + credit spine).

    Built by :func:`build_transport_service`. Holds the wired components so a caller
    (or a test) can :meth:`start` / :meth:`serve_forever` / :meth:`stop` them, or
    inspect the structured ``events`` log. The components themselves are the merged,
    independently-tested classes; this object only owns their lifecycle + a
    secret-free startup log.
    """

    config: TransportServiceConfig
    resolver: StratumIdentityResolver
    validator: ShareValidator
    server: StratumServer
    relay: object  # DispatcherRelay (typed loosely to avoid a hard import cycle)
    validated_share_store: JsonlValidatedShareStore
    roster: JsonlMinerRoster
    events: list[tuple[str, dict[str, object]]]

    async def start(self) -> None:
        """Open the upstream relay (R1) then bind the miner-facing listener.

        Order matters: the relay starts first so a job can be cached before a miner
        logs in (the server pushes the current job on login). The relay is fail-soft
        when the LTC login env is absent — it constructs but leaves the lane
        unconnected; the server still binds and accepts logins (which the validator
        credits independently of the upstream forward).
        """

        await self.relay.start()  # type: ignore[attr-defined]
        if not self.config.ltc_login_present:
            self._emit(SERVICE_UPSTREAM_UNCONFIGURED, lane="scrypt_pool")
        if not self.config.xmr_login_present:
            self._emit(SERVICE_UPSTREAM_UNCONFIGURED, lane="xmr_pool")
        if not self.config.rvn_login_present:
            self._emit(SERVICE_UPSTREAM_UNCONFIGURED, lane="main_pool_gpu_rvn")
        if not self.config.quai_login_present:
            self._emit(SERVICE_UPSTREAM_UNCONFIGURED, lane="main_pool_gpu_quai")
        await self.server.start()
        self._emit(
            SERVICE_LISTENING,
            lane="scrypt_pool",
            bound_port=self.server.bound_port(SCRYPT_POOL),
        )
        self._emit(
            SERVICE_LISTENING,
            lane="xmr_pool",
            bound_port=self.server.bound_port(XMR_POOL),
        )
        self._emit(
            SERVICE_LISTENING,
            lane="main_pool_gpu_rvn",
            bound_port=self.server.bound_port(MAIN_POOL_GPU_RVN),
        )
        self._emit(
            SERVICE_LISTENING,
            lane="main_pool_gpu_quai",
            bound_port=self.server.bound_port(MAIN_POOL_GPU_QUAI),
        )

    async def serve_forever(self) -> None:
        await self.server.serve_forever()

    async def stop(self) -> None:
        await self.server.stop()
        await self.relay.stop()  # type: ignore[attr-defined]
        self._emit(SERVICE_STOPPED)

    def event_codes(self) -> list[str]:
        return [code for code, _ in self.events]

    def _emit(self, code: str, **detail: object) -> None:
        self.events.append((code, detail))


def build_transport_service(
    config: TransportServiceConfig,
    *,
    clock: object = None,
    upstreams: object = None,
    cred_factory: object = None,
) -> TransportService:
    """Wire the runnable LTC transport service from a resolved config.

    Pure wiring (no I/O beyond opening the durable JSONL files + reading the auth
    secret); the network sockets open only at :meth:`TransportService.start`. The
    optional ``upstreams`` / ``cred_factory`` seams let a test inject a FAKE upstream
    (and a fake cred factory) so the smoke test drives the whole stack with NO real
    network; production passes neither, and :func:`build_ltc_relay` builds the real
    F2Pool upstream + the real Scrypt translator reading env at use-time.

    The ledger is a DISTINCT instance with ``require_device_pop=False`` (the stratum
    path) sharing the SAME durable roster/registry FILES as the credit server's ledger
    (which keeps ``require_device_pop=True``). No ledger instance is shared.
    """

    server_clock = clock if clock is not None else (lambda: datetime.now(UTC))

    # Durable roster + its C2 device registry, rooted at the SHARED data dir so the
    # transport service and the credit server resolve the same enrolled identities.
    registry = JsonlDeviceRegistry(config.data_dir)
    roster = JsonlMinerRoster(config.data_dir, device_registry=registry)

    # This service's OWN ledger: require_device_pop=False (stratum path), sharing the
    # roster's worker_name_resolver (so the minted session carries the server-assigned
    # worker_name) + the SAME server secret as the credit server (read at use-time).
    auth_secret = _read_auth_secret(config.auth_secret_file)
    ledger = ShadowRewardLedger(
        server_secret=auth_secret,
        require_server_secret=auth_secret is not None,
        device_registry=registry,
        require_device_pop=False,  # stratum path: the roster gate IS the proof.
        worker_name_resolver=(
            lambda passport_id, device_id: roster.worker_name_for(
                passport_id=passport_id, device_id=device_id
            )
        ),
        # OPEN ENROLLMENT (#3): the DURABLE per-(address,worker) issuance cap. Only set
        # when open mode is on for EITHER open-enrollable lane (so the roster-only deploy
        # keeps the existing behaviour: 0 = disabled). It hard-bounds how many sessions one
        # open identity may be issued this process, the ledger-side complement to the
        # in-memory OpenEnrollmentLimiter.
        max_issuances_per_identity=(
            config.open_max_issuances_per_identity
            if (
                config.open_enrollment
                or config.xmr_open_enrollment
                or config.rvn_open_enrollment
                or config.quai_open_enrollment
            )
            else 0
        ),
        server_clock=server_clock,  # type: ignore[arg-type]
    )

    # OPEN ENROLLMENT (#3): the SHARED, process-global anti-spam budget (per-address +
    # per-IP rate, global new-address budget, per-address worker fan-out). Built from env
    # with the SAME clock the service uses, so the rolling windows track the service
    # clock deterministically. None when open mode is off (the resolver never builds one).
    open_limiter = (
        OpenEnrollmentLimiter(
            limits=OpenEnrollmentLimits.from_env(os.environ),
            clock=server_clock,
        )
        if (
            config.open_enrollment
            or config.xmr_open_enrollment
            or config.rvn_open_enrollment
            or config.quai_open_enrollment
        )
        else None
    )

    resolver = StratumIdentityResolver(
        device_registry=registry,
        roster=roster,
        ledger=ledger,
        session_ttl=config.session_ttl,
        # The resolver reads the gate + lane-port + lane-pool env from os.environ in
        # production (env=None). Tests inject env via build_and_run's env arg.
        env=None,
        open_limiter=open_limiter,
    )

    # The SHARED durable validated-share store: this process is the SOLE APPENDER. The
    # validator is wired with ALL THREE share-hash legs that run in-process: the LTC Scrypt
    # verifier (hashlib.scrypt), the XMR RandomX verifier (FFI over librandomx, BSD-3), AND
    # the RVN KawPoW verifier (FFI over a permissive KawPoW reference). Each FFI verifier
    # FAILS CLOSED when its native lib is absent (this sandbox) — that lane's submit then
    # NACKs and credits nothing (never a fabricated accept); a deploy shipping the lib
    # re-hashes for real. KawPoW is now a first-class lane (the RVN proxy leg).
    store = JsonlValidatedShareStore(path=config.validated_share_store_path)
    validator = ShareValidator(
        verifiers={
            LocalScryptVerifier().algorithm: LocalScryptVerifier(),
            LocalRandomXVerifier().algorithm: LocalRandomXVerifier(),
            LocalKawPowVerifier().algorithm: LocalKawPowVerifier(),
        },
        validated_share_store=store,
        # Durable dedup beside the share store (its own JSONL) so a replayed nonce
        # never double-records across a restart.
        dedup_store=JsonlShareDedupStore(path=_dedup_path(config.validated_share_store_path)),
        clock=server_clock,  # type: ignore[arg-type]
    )

    # The upstream relay: ONE multi-lane relay (real F2Pool LTC upstream + real Scrypt
    # translator AND real supportxmr XMR upstream + real Monero translator) via
    # build_proxy_relay in production; the test injects fake upstream(s) + a cred factory.
    if upstreams is not None:
        relay = _build_relay_with_injected_upstream(
            upstreams=upstreams, cred_factory=cred_factory, clock=server_clock
        )
    else:
        relay = build_proxy_relay()

    factory = build_connection_factory(
        resolver=resolver,
        validator=validator,
        credit_observed_at=server_clock,  # type: ignore[arg-type]
        # The flat default the scheduler reconstructs the canonical hash with; the REAL
        # validated difficulty rides the SelfValidatedShareAuthority override.
        hash_difficulty=RECONSTRUCTED_SHARE_DIFFICULTY,
        # HARD-MINIMUM floors per lane: the mapped value is BOTH the no-d= default AND a
        # hard minimum a login's d= can never go below — so a hostile d=1 is CLAMPED UP to
        # it and cannot flood the re-hash validator. SCRYPT (ASIC): 16384 (CPU test deploy
        # may lower via ALICE_LTC_MIN_VARDIFF_FLOOR). XMR (RandomX CPU): a MODEST 1000 that
        # bounds the worst-case re-hash rate (each share = one full RandomX hash), NOT the
        # ASIC clamp — env-overridable via ALICE_XMR_MIN_VARDIFF_FLOOR. RVN (KawPoW GPU): a
        # GPU-sized 262144 (= 2**18) so a rig starts within ~5 retarget steps of its
        # equilibrium (~1e8) instead of climbing ~14 4x-steps from 1 -- the GPU analog of
        # the ASIC/CPU floors; env-overridable via ALICE_RVN_MIN_VARDIFF_FLOOR; NOT the
        # 16384 ASIC clamp.
        vardiff_floors={
            SCRYPT_POOL: config.ltc_min_vardiff_floor,
            XMR_POOL: config.xmr_min_vardiff_floor,
            MAIN_POOL_GPU_RVN: config.rvn_min_vardiff_floor,
            # The Quai (KawPoW) lane uses the SAME GPU-sized floor as RVN (262144).
            MAIN_POOL_GPU_QUAI: config.quai_min_vardiff_floor,
        },
        # SUBMIT-FLOOD limiter (the BLOCKER fix), LIVE by default: each connection gets a
        # per-connection rate cap sized to a GENEROUS multiple (config.submit_rate_headroom,
        # 1000x by default) of the ~1-share/15s at-target rate — orders of magnitude above
        # any legit rig yet far below a line-rate flood — PLUS a consecutive-reject run cap
        # (config.submit_max_consecutive_rejects). The gate is checked BEFORE the re-hash, so
        # a flood of cheap sub-target garbage never reaches the validator.
        submit_rate_headroom=config.submit_rate_headroom,
        submit_max_consecutive_rejects=config.submit_max_consecutive_rejects,
    )
    server = StratumServer(
        resolver=resolver,
        connection_factory=factory,
        job_source=relay,
        solution_sink=relay,
        lanes=(
            LaneListenerConfig(
                lane=SCRYPT_POOL,
                lane_port=config.ltc_port,
                bind_port=config.ltc_port,
                host=config.bind_host,
            ),
            LaneListenerConfig(
                lane=XMR_POOL,
                lane_port=config.xmr_port,
                bind_port=config.xmr_port,
                host=config.bind_host,
            ),
            LaneListenerConfig(
                lane=MAIN_POOL_GPU_RVN,
                lane_port=config.rvn_port,
                bind_port=config.rvn_port,
                host=config.bind_host,
            ),
            LaneListenerConfig(
                lane=MAIN_POOL_GPU_QUAI,
                lane_port=config.quai_port,
                bind_port=config.quai_port,
                host=config.bind_host,
            ),
        ),
        clock=server_clock,  # type: ignore[arg-type]
        # DoS HARDENING: the accept-path admission + slowloris deadline (from env). The
        # R4 lane hook is the relay's admission controller (PERMISSIVE/OFF by default);
        # the per-IP + global caps are the server's own counters and now default NON-ZERO
        # (#F-A: 64 / 2048, env-overridable) so they are LIVE on the public edge; the
        # pre-login deadline drops a half-open handshake.
        pre_login_timeout_s=config.pre_login_timeout_s,
        max_connections_per_ip=config.max_connections_per_ip,
        max_connections_global=config.max_connections_global,
        admit_connection=relay.admit_connection,  # type: ignore[attr-defined]
        # OFF-LOOP re-hash (the BLOCKER fix): the per-submit PoW re-hash runs in a bounded
        # thread pool gated by a Semaphore sized to ``verify_concurrency`` (0 → ≈ cores), so
        # one rig's re-hash never blocks the loop for the others and total re-hash CPU stays
        # bounded. The brief flood-ban refuses a tripped peer at admission for this long.
        submit_flood_ban_s=config.submit_flood_ban_s,
        verify_concurrency=config.verify_concurrency or None,
    )
    return TransportService(
        config=config,
        resolver=resolver,
        validator=validator,
        server=server,
        relay=relay,
        validated_share_store=store,
        roster=roster,
        events=[(SERVICE_STARTING, config.public_summary())],
    )


def _build_relay_with_injected_upstream(*, upstreams, cred_factory, clock):
    """Build a DispatcherRelay over injected (fake) upstream(s) — the test seam.

    Mirrors :func:`build_proxy_relay` but takes caller-supplied upstream(s) + a cred
    factory and the REAL per-lane translators (so the smoke test exercises the real
    Scrypt header assembly / Monero blob path / net-difficulty against a fake socket).
    Lane-aware: wires whichever of ``SCRYPT_POOL`` / ``XMR_POOL`` the caller injected
    (the existing tests inject only LTC; the XMR test injects only XMR; a future test may
    inject both). All translators share the relay's ONE :class:`JobTranslationMap` so
    minted internal job ids are unique across lanes and R3's reverse map stays coherent.
    """

    from alice_acp.transport_service.dispatcher import DispatcherRelay
    from alice_acp.transport_service.jobs import JobTranslationMap
    from alice_acp.transport_service.kawpow_translator import KawPoWJobTranslator
    from alice_acp.transport_service.monero_translator import MoneroJobTranslator
    from alice_acp.transport_service.scrypt_translator import ScryptJobTranslator

    job_map = JobTranslationMap()
    translators: dict[str, object] = {}
    if SCRYPT_POOL in upstreams:
        ltc_up = upstreams[SCRYPT_POOL]
        translators[SCRYPT_POOL] = ScryptJobTranslator(
            job_map=job_map,
            subscription_provider=ltc_up.current_subscription,
            pool_difficulty_provider=ltc_up.current_pool_difficulty,
            clock=clock,
        )
    if XMR_POOL in upstreams:
        translators[XMR_POOL] = MoneroJobTranslator(job_map=job_map, clock=clock)
    if MAIN_POOL_GPU_RVN in upstreams:
        translators[MAIN_POOL_GPU_RVN] = KawPoWJobTranslator(job_map=job_map, clock=clock)
    if MAIN_POOL_GPU_QUAI in upstreams:
        # The Quai lane is KawPoW — the SAME translator class as RVN (the wire is identical).
        translators[MAIN_POOL_GPU_QUAI] = KawPoWJobTranslator(job_map=job_map, clock=clock)

    def dispatch_translate(lane, upstream_job):
        translator = translators.get(lane)
        if translator is None:
            return None
        return translator(lane, upstream_job)

    # The injected cred factory (when given) is applied to EVERY injected lane; otherwise
    # each lane reads its own env at use-time (LTC via ltc_credentials_from_env, XMR via
    # xmr_credentials_from_env, RVN via rvn_credentials_from_env) — the fail-soft default
    # the absent-login test relies on.
    cred_factories: dict[str, object] = {}
    for lane in upstreams:
        if cred_factory is not None:
            cred_factories[lane] = cred_factory
        elif lane == XMR_POOL:
            cred_factories[lane] = lambda: xmr_credentials_from_env()
        elif lane == MAIN_POOL_GPU_RVN:
            cred_factories[lane] = lambda: rvn_credentials_from_env()
        elif lane == MAIN_POOL_GPU_QUAI:
            cred_factories[lane] = lambda: quai_credentials_from_env()
        else:
            cred_factories[lane] = lambda: ltc_credentials_from_env()

    return DispatcherRelay(
        upstreams=dict(upstreams),
        cred_factories=cred_factories,
        job_translator=dispatch_translate,
        job_map=job_map,
        clock=clock,
    )


def _dedup_path(store_path: Path) -> Path:
    """The durable share-dedup JSONL co-located with the validated-share store.

    Keeps the two transport-side durable files together; a directory store path puts
    both files in that directory, a ``.jsonl`` store path puts the dedup file beside
    it as ``share_dedup.jsonl``.
    """

    store_path = Path(store_path)
    parent = store_path.parent if store_path.suffix == ".jsonl" else store_path
    return parent / "share_dedup.jsonl"


def build_and_run(env: dict[str, str] | None = None) -> int:
    """Read env, wire the LTC transport service, and RUN it on asyncio.

    The ``python -m alice_acp.transport_service`` entry point. Returns a process exit
    code (0 clean). Fail-soft: a configuration error (no data dir / no store path /
    unreadable secret) prints a CLEAR, secret-free message to stderr and returns 2
    WITHOUT a traceback or crash. The ABSENT LTC upstream login is NOT fatal (the
    service serves the miner edge with the lane unconnected). A disabled gate is NOT
    fatal (the server rejects logins fail-closed).

    NOTE: in production ``env`` is ``None`` (the process env is authoritative for both
    the config AND the resolver's gate/lane lookup). The ``env`` arg exists so a test
    can drive the wiring deterministically; when supplied it is also installed into
    ``os.environ`` for the duration of the run so the resolver (which reads
    ``os.environ``) and the relay's at-use-time cred factory see the same values.
    """

    try:
        if env is not None:
            return _run_with_injected_env(env)
        config = load_transport_config(None)
        service = build_transport_service(config)
        return _run_service(service)
    except TransportServiceConfigError as exc:
        # Fail-soft: a clear, secret-free message; no traceback, no crash.
        print(f"transport_service: refusing to start: {exc}", file=sys.stderr)
        return 2


def _run_with_injected_env(env: dict[str, str]) -> int:
    """Run with an injected env installed into os.environ for the run's duration.

    The resolver + the at-use-time cred factory both read ``os.environ``; installing
    the injected env makes a test's env authoritative end-to-end, then restores the
    prior environment. Used by tests; production calls :func:`build_and_run` with no
    env so the real process environment is used directly.
    """

    saved = dict(os.environ)
    try:
        os.environ.update(env)
        config = load_transport_config(env)
        service = build_transport_service(config)
        return _run_service(service)
    finally:
        os.environ.clear()
        os.environ.update(saved)


def _run_service(service: TransportService) -> int:
    if not service.config.gate_enabled:
        # Not fatal — the server still binds and rejects every login fail-closed
        # (STRATUM_GATE_DISABLED). Surface a clear note so an operator knows why no
        # miner is being admitted (the one-line fix is setting the gate env).
        service._emit(SERVICE_GATE_DISABLED)
        print(
            f"transport_service: NOTE {STRATUM_GATE_ENABLED_ENV} is not enabled; "
            "all stratum logins will be rejected fail-closed until it is set to 1.",
            file=sys.stderr,
        )
    if not service.config.ltc_login_present:
        service._emit(SERVICE_LTC_LOGIN_ABSENT)
        print(
            f"transport_service: NOTE {UPSTREAM_LTC_LOGIN_ENV} is absent; the LTC "
            "upstream lane stays unconnected (fail-soft) — miners can still log in "
            "and shares still credit, but no job source / solution forward exists "
            "until the login env is supplied.",
            file=sys.stderr,
        )

    async def _main() -> None:
        await service.start()
        try:
            await service.serve_forever()
        finally:
            await service.stop()

    try:
        asyncio.run(_main())
    except KeyboardInterrupt:
        # Clean operator shutdown (SIGINT): asyncio.run already ran the stop() in the
        # finally above; treat as a clean exit.
        return 0
    return 0


__all__ = [
    "DEFAULT_SESSION_TTL",
    "DEFAULT_STRATUM_BIND_HOST",
    "LTC_ASIC_DEFAULT_VARDIFF_FLOOR",
    "LTC_MIN_VARDIFF_FLOOR_ENV",
    "OPEN_MAX_ISSUANCES_PER_IDENTITY_ENV",
    "PROXY_VALIDATED_SHARE_STORE_ENV",
    "QUAI_DEFAULT_VARDIFF_FLOOR",
    "QUAI_MIN_VARDIFF_FLOOR_ENV",
    "RVN_DEFAULT_VARDIFF_FLOOR",
    "RVN_MIN_VARDIFF_FLOOR_ENV",
    "SERVICE_GATE_DISABLED",
    "SERVICE_LISTENING",
    "SERVICE_LTC_LOGIN_ABSENT",
    "SERVICE_STARTING",
    "SERVICE_STOPPED",
    "SERVICE_UPSTREAM_UNCONFIGURED",
    "SHADOW_AUTH_FILE_ENV",
    "SHADOW_DATA_DIR_ENV",
    "STRATUM_BIND_HOST_ENV",
    "STRATUM_MAX_CONN_GLOBAL_ENV",
    "STRATUM_MAX_CONN_PER_IP_ENV",
    "STRATUM_PRELOGIN_TIMEOUT_ENV",
    "STRATUM_SUBMIT_FLOOD_BAN_ENV",
    "STRATUM_SUBMIT_MAX_CONSEC_REJECTS_ENV",
    "STRATUM_SUBMIT_RATE_HEADROOM_ENV",
    "STRATUM_VERIFY_CONCURRENCY_ENV",
    "XMR_DEFAULT_VARDIFF_FLOOR",
    "XMR_MIN_VARDIFF_FLOOR_ENV",
    "TransportService",
    "TransportServiceConfig",
    "TransportServiceConfigError",
    "build_and_run",
    "build_transport_service",
    "load_transport_config",
]

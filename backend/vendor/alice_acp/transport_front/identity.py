"""The transport-front IDENTITY HOOK (doc §2.5) — login → roster-bound identity.

This is the Alice-specific, fail-closed glue that turns a stratum login username
into a server-owned, per-connection identity + a minted shadow session. It is the
exact §2.5 contract:

1. PARSE the username ``<passport_id>.<device_id>[.<worker_label>]`` (the
   client's ``worker_label`` is non-authoritative metrics only — it is NEVER the
   credited worker).
2. RESOLVE ``(passport_id, device_id)`` against the
   :class:`~alice_acp.shadow_server.device_registry_store.JsonlDeviceRegistry`
   (``resolve`` — the C2 authority source). Require ``accepted`` (registered AND
   active); an unregistered identity (``REASON_DEVICE_UNREGISTERED``) or a revoked
   one (``REASON_DEVICE_REVOKED``) FAILS CLOSED — no session, no jobs.
3. Look up the SERVER-ASSIGNED opaque ``worker_name``
   (``alc-w-<16hex>``) via :meth:`JsonlMinerRoster.worker_name_for` (roster
   membership; a worker_name only exists for an enrolled+active identity, so this
   is the second fail-closed gate).
4. Map the connection's PORT → lane via :data:`PORT_LANE_MAP`. The PORT
   determines the lane SERVER-SIDE — a client NEVER supplies the lane; an unknown
   port fails closed.
5. MINT a per-connection :class:`~alice_acp.shadow_server.types.ShadowSession`
   via a ROSTER-GATED internal issuance with ``require_device_pop=False`` (no
   interactive PoP on stratum — roster membership is already proven by steps 2-3;
   the accepted D-STRATUM-SESSION decision). The issuance reuses the EXISTING
   :meth:`ShadowRewardLedger.issue_session` (so every credit-only guard —
   ``live_reward``/``payout``/``paid_acu`` — is inherited) with the roster's
   ``worker_name_resolver`` wired, and is itself ROSTER-GATED here (we never call
   ``issue_session`` for an identity that failed steps 2-3).
6. Build a :class:`StratumConnectionIdentity` (passport_id, device_id,
   worker_name, lane, session, + the lane's pool_id / collection address).

THE GATE (doc §2.5, mirror of ``hardening.py``'s public-miner gate):
``ALICE_ACP_STRATUM_GATE_ENABLED`` (default OFF). With the gate OFF, NO stratum
login is accepted — :meth:`StratumIdentityResolver.resolve_login` returns a
fail-closed :class:`LoginRejected` with ``STRATUM_GATE_DISABLED`` before any
registry/roster lookup. This is the same default-OFF posture as
``policy.public_miner_gate_enabled``.

CREDIT-ONLY: the front sets no reward/payout/chain symbol; the
``require_device_pop=False`` issuance is roster-gated AND flag-gated;
``ensure_no_raw_secret`` guards every identity string; NO payout address is ever
read from or written by this hook.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, replace
from datetime import datetime, timedelta

from alice_acp.evidence.types import ensure_no_raw_secret
from alice_acp.shadow_server.device_registry_store import JsonlDeviceRegistry
from alice_acp.shadow_server.ledger import ShadowRewardLedger
from alice_acp.shadow_server.miner_roster import JsonlMinerRoster
from alice_acp.shadow_server.types import (
    MAIN_POOL_GPU_QUAI,
    MAIN_POOL_GPU_RVN,
    SCRYPT_POOL,
    SESSION_KIND_MINING,
    XMR_POOL,
    Lane,
    ShadowSession,
    ShadowSessionIssueRequest,
    utc_now,
)
from alice_acp.transport_front.alice_address import validate_alice_address
from alice_acp.transport_front.open_enrollment import (
    OpenEnrollmentLimiter,
    open_enrollment_enabled_for_lane,
    open_worker_name,
    sanitize_worker_label,
)

#: The env flag mirroring ``ALICE_ACP_PUBLIC_MINER_GATE_ENABLED`` (hardening.py).
#: DEFAULT OFF: with the gate off, NO stratum login is accepted. Wired the same
#: way the existing scheduler/public-miner flags are (``_env_truthy`` in main()).
STRATUM_GATE_ENABLED_ENV = "ALICE_ACP_STRATUM_GATE_ENABLED"

#: Canonical lane port env vars (the PORT determines the lane server-side, never a
#: client field — doc §2.1 / §2.5). The deploy binds one listener per algo and
#: passes the bound port; absent => that lane is not served. These are NOT secrets.
PORT_XMR_ENV = "ALICE_ACP_STRATUM_PORT_XMR"
PORT_RVN_ENV = "ALICE_ACP_STRATUM_PORT_RVN"
PORT_LTC_ENV = "ALICE_ACP_STRATUM_PORT_LTC"
#: The Quai (KawPoW) lane listener port env (mirror of :data:`PORT_RVN_ENV`). A
#: SECOND KawPoW lane needs its OWN downstream port so a Quai rig and an RVN rig
#: never share a listener (RVN 4444 is taken; XMR 3333 / LTC 5555 too) — Quai = 7777.
PORT_QUAI_ENV = "ALICE_ACP_STRATUM_PORT_QUAI"

#: Default per-algo stratum ports (overridable by the env vars above). Chosen to
#: match the conventional stock-miner defaults: RandomX 3333, KawPoW 4444, Scrypt
#: 5555. The mapping is PORT -> lane; a connection on an unmapped port fails closed.
DEFAULT_PORT_XMR = 3333
DEFAULT_PORT_RVN = 4444
DEFAULT_PORT_LTC = 5555
#: The Quai (KawPoW) lane's default downstream port (mirror of :data:`DEFAULT_PORT_RVN`).
#: 7777 — distinct from the taken RVN/XMR/LTC ports so the SECOND KawPoW lane has its
#: own listener; env-overridable via :data:`PORT_QUAI_ENV`.
DEFAULT_PORT_QUAI = 7777


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def stratum_gate_enabled(env: dict[str, str] | None = None) -> bool:
    """Read :data:`STRATUM_GATE_ENABLED_ENV` (default OFF).

    Mirrors ``http_app._env_truthy`` exactly so the stratum gate is wired
    identically to the public-miner gate.
    """

    source = env if env is not None else os.environ
    return _truthy(source.get(STRATUM_GATE_ENABLED_ENV))


def port_lane_map(env: dict[str, str] | None = None) -> dict[int, Lane]:
    """Build the PORT → lane map from env (or the conventional defaults).

    ``PORT_XMR -> xmr_pool``, ``PORT_RVN -> main_pool_gpu_rvn``,
    ``PORT_LTC -> scrypt_pool`` (doc §2.5). The PORT is the ONLY lane authority —
    a client never supplies the lane. A connection arriving on a port not in this
    map is fail-closed (no lane => :meth:`lane_for_port` returns ``None``).
    """

    source = env if env is not None else os.environ
    return {
        _port(source, PORT_XMR_ENV, DEFAULT_PORT_XMR): XMR_POOL,
        _port(source, PORT_RVN_ENV, DEFAULT_PORT_RVN): MAIN_POOL_GPU_RVN,
        _port(source, PORT_LTC_ENV, DEFAULT_PORT_LTC): SCRYPT_POOL,
        # The Quai (KawPoW) lane on its own port (mirror of the RVN entry). A Quai
        # rig connects here; the PORT is the only lane authority (never a client field).
        _port(source, PORT_QUAI_ENV, DEFAULT_PORT_QUAI): MAIN_POOL_GPU_QUAI,
    }


def _port(source: dict[str, str], name: str, default: int) -> int:
    raw = (source.get(name) or "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def lane_for_port(port: int, *, env: dict[str, str] | None = None) -> Lane | None:
    """Resolve a connection's bound PORT to its lane, or ``None`` (fail-closed)."""

    return port_lane_map(env).get(port)


#: Per-lane pool routing config (pool_id + Alice collection address) the front
#: stamps onto the submission identity + the session. These are server-owned, set
#: from the SAME env names the credit-side provider reads (``LANE_ENV_SPECS`` in
#: pool_evidence_providers.py) so the front and the provider agree on identity.
#: NONE is a secret (public pool addresses / account names).
_LANE_POOL_ENV: dict[Lane, tuple[str, str]] = {
    XMR_POOL: ("ALICE_XMR_POOL_ID", "ALICE_XMR_POOL_ADDRESS"),
    MAIN_POOL_GPU_RVN: ("ALICE_RVN_POOL_ID", "ALICE_RVN_POOL_ADDRESS"),
    # The Quai (KawPoW) lane reads its OWN pool_id + pool address (mirror of the RVN
    # entry). The pool_id (``quai``) is what keeps Quai-lane credit SEPARATE from RVN's
    # ``ravenminer`` — the front stamps it on the session/proof and the credit-side
    # provider drains by the SAME pool_id (so front and provider agree on identity).
    MAIN_POOL_GPU_QUAI: ("ALICE_QUAI_POOL_ID", "ALICE_QUAI_POOL_ADDRESS"),
    # F2Pool polls by mining_user_name but binds attestation to the LTC wallet
    # (the on-chain collection address) — the credit side does the same split.
    SCRYPT_POOL: ("ALICE_LTC_POOL_ID", "ALICE_LTC_COLLECTION_ADDRESS"),
}


# --- reason codes ------------------------------------------------------------

STRATUM_GATE_DISABLED = "stratum_gate_disabled"
STRATUM_LOGIN_BAD_USERNAME = "stratum_login_bad_username"
STRATUM_LOGIN_UNKNOWN_PORT = "stratum_login_unknown_port"
STRATUM_LOGIN_DEVICE_NOT_RESOLVED = "stratum_login_device_not_resolved"
STRATUM_LOGIN_NOT_ON_ROSTER = "stratum_login_not_on_roster"
STRATUM_LOGIN_SESSION_NOT_ISSUED = "stratum_login_session_not_issued"
STRATUM_LOGIN_POOL_UNCONFIGURED = "stratum_login_pool_unconfigured"
STRATUM_LOGIN_ACCEPTED = "stratum_login_accepted"
#: OPEN-MODE fail-closed reasons (review #3/#4). A bad/non-Alice address is the new
#: admission gate's reject (#4): the open-enrollment identity is the miner's ALICE
#: ADDRESS (the SS58 format-300 Alice-token destination — V directive), so a string that
#: is not a checksum-valid Alice address is rejected here. The rate-limit reason is
#: surfaced verbatim from the :class:`OpenEnrollmentLimiter` (#3).
STRATUM_LOGIN_OPEN_BAD_ADDRESS = "stratum_login_open_bad_address"

#: Lanes for which open self-serve enrollment MAY be enabled: the public LTC/Scrypt lane,
#: the public XMR/RandomX lane, AND the public RVN/KawPoW lane. Each lane is additionally
#: gated by its OWN per-lane env flag via :func:`open_enrollment_enabled_for_lane` (default
#: OFF) — so no lane's flag can ever flip another lane open. (RVN was a deliberate hard
#: exclusion before the RVN proxy leg landed; it is now an open-enrollable lane on the SAME
#: Alice-address-is-the-identity model as LTC/XMR, still default OFF.)
_OPEN_ENROLLMENT_LANES: frozenset[Lane] = frozenset(
    {SCRYPT_POOL, XMR_POOL, MAIN_POOL_GPU_RVN, MAIN_POOL_GPU_QUAI}
)


@dataclass(frozen=True, slots=True)
class ParsedUsername:
    """The decomposed ``<passport_id>.<device_id>[.<worker_label>]`` login user.

    ``worker_label`` is the client's self-named worker — NON-AUTHORITATIVE; the
    credited worker is the server-assigned ``worker_name`` resolved from the
    roster. ``ensure_no_raw_secret`` is enforced on each component so a secret
    pasted into the username is refused, never echoed.
    """

    passport_id: str
    device_id: str
    worker_label: str | None = None


def parse_username(username: str) -> ParsedUsername | None:
    """Parse ``<passport_id>.<device_id>[.<worker_label>]`` (doc §2.5).

    Splits on ``.`` into 2 or 3 parts. Returns ``None`` (fail-closed) for an empty
    username, a username with fewer than 2 components, any empty component, or a
    component carrying raw secret material. A 3rd component is the optional,
    non-authoritative worker label; any extra ``.``-segments are folded into the
    label (a worker label may legitimately contain dots).
    """

    if not isinstance(username, str) or not username:
        return None
    parts = username.split(".")
    if len(parts) < 2:
        return None
    passport_id, device_id = parts[0], parts[1]
    worker_label = ".".join(parts[2:]) if len(parts) > 2 else None
    if not passport_id or not device_id:
        return None
    if worker_label is not None and not worker_label:
        worker_label = None
    try:
        ensure_no_raw_secret(passport_id, field_name="passport_id")
        ensure_no_raw_secret(device_id, field_name="device_id")
        if worker_label is not None:
            ensure_no_raw_secret(worker_label, field_name="worker_label")
    except ValueError:
        return None
    return ParsedUsername(passport_id=passport_id, device_id=device_id, worker_label=worker_label)


@dataclass(frozen=True, slots=True)
class StratumConnectionIdentity:
    """The resolved, server-owned per-connection identity (doc §2.5).

    Built ONLY for a login that passed the gate + registry + roster + session
    gates. Carries the server-owned ``worker_name`` (the credited worker), the
    PORT-derived ``lane``, the minted :class:`ShadowSession`, and the lane's
    pool routing/binding (``pool_id`` + ``alice_collection_address``). The
    client's ``worker_label`` is carried for display/correlation only. CREDIT-ONLY:
    no payout address anywhere.
    """

    passport_id: str
    device_id: str
    worker_name: str
    lane: Lane
    pool_id: str
    alice_collection_address: str
    session: ShadowSession
    worker_label: str | None = None

    @property
    def accepted(self) -> bool:
        return True


@dataclass(frozen=True, slots=True)
class LoginRejected:
    """A fail-closed login rejection (doc §2.5). Carries a stable reason only."""

    reason: str

    @property
    def accepted(self) -> bool:
        return False


@dataclass(slots=True)
class StratumIdentityResolver:
    """Resolve a stratum login to a roster-bound identity + minted session (§2.5).

    Inject the durable :class:`JsonlDeviceRegistry` (the C2 authority), the
    :class:`JsonlMinerRoster` (worker_name + membership), and the
    :class:`ShadowRewardLedger` used to mint the per-connection session. The
    ledger MUST be the SAME instance the credit spine reads (so the issued session
    is in ``ledger.sessions`` for the scheduler) and SHOULD be configured with the
    roster's ``worker_name_resolver`` (so the issued session carries the same
    server-assigned ``worker_name`` the front binds to). ``require_device_pop`` is
    left at its default ``False`` on that ledger for the stratum path — roster
    membership is the proof (the accepted D-STRATUM-SESSION decision); this
    resolver is itself the roster gate.

    The whole resolve is gated behind :data:`STRATUM_GATE_ENABLED_ENV` (default
    OFF). ``env`` is injectable for tests; it defaults to ``os.environ``.

    OPEN MODE (review #3/#4; registration-less ``<Alice_address>.<worker>``). When
    :func:`~alice_acp.transport_front.open_enrollment.open_enrollment_enabled_for_lane` is
    set for an open-enrollable lane (:data:`SCRYPT_POOL` / :data:`XMR_POOL` /
    :data:`MAIN_POOL_GPU_RVN`), the resolver SKIPS the registry + roster gates and instead
    admits on a VALIDATED ALICE ADDRESS — the SS58
    format-300 Alice-token destination (the new gate, #4) — under a rate-limit budget
    (#3, :class:`OpenEnrollmentLimiter`). The Alice address (NOT the mined coin's
    address) is the miner's identity per the V cross-lane directive: a miner earns Alice
    tokens, so their mining-login username is the Alice address those tokens go to. The
    Alice address becomes the credit ``passport_id``, the sanitized worker label the
    ``device_id``, and a deterministic SERVER-derived name the ``worker_name``. The gate
    flag + the pool config + the credit-only session-mint are ALL still applied. With
    open mode OFF (the default) this resolver is byte-for-byte the roster path.
    """

    device_registry: JsonlDeviceRegistry
    roster: JsonlMinerRoster
    ledger: ShadowRewardLedger
    session_ttl: timedelta = timedelta(minutes=30)
    env: dict[str, str] | None = None
    #: Per-lane open-enrollment anti-spam budget (#3). Lazily built from env on first
    #: open-mode use when ``None`` (so the roster-only deploy never constructs one); a
    #: caller/test may inject one (e.g. a pinned clock / tuned limits). SHARED across
    #: connections (the limiter is the cross-connection budget), so the transport wires
    #: ONE resolver (it already does) and the limiter state is process-global.
    open_limiter: OpenEnrollmentLimiter | None = None

    def resolve_login(
        self,
        *,
        username: str,
        port: int,
        observed_at: datetime | None = None,
        peer_ip: str = "",
    ) -> StratumConnectionIdentity | LoginRejected:
        """Run the full §2.5 chain; return identity or a fail-closed rejection.

        ``port`` is the connection's SERVER-bound listener port (the lane
        authority). ``observed_at`` defaults to ``utc_now()`` (an aware UTC
        timestamp; the device registry + issuance require timezone-aware times).
        ``peer_ip`` is the connection's source IP (server-observed, never a client
        field); it is used ONLY by the open-mode per-IP rate limit and is ignored on
        the roster path. An empty/absent peer IP skips the per-IP cap (the per-address
        + global budgets still apply), so a missing peername is never a free pass.
        """

        now = observed_at or utc_now()

        # GATE (mirror of the public-miner gate): default OFF => no login accepted.
        # The gate applies ON TOP OF open mode — open enrollment never bypasses it.
        if not stratum_gate_enabled(self.env):
            return LoginRejected(STRATUM_GATE_DISABLED)

        # PORT -> lane (server-side authority). Unknown port => fail closed.
        lane = lane_for_port(port, env=self.env)
        if lane is None:
            return LoginRejected(STRATUM_LOGIN_UNKNOWN_PORT)

        # OPEN MODE (per-lane, default OFF): the registration-less address-gated path.
        # ONLY for an open-enrollable lane (SCRYPT_POOL / XMR_POOL) AND only when THAT
        # lane's own per-lane flag is set. Everything else (KawPoW, or an open-enrollable
        # lane with its flag off) falls through to the UNCHANGED roster path below. The
        # per-lane flag means the LTC flag can never open XMR and vice-versa.
        if lane in _OPEN_ENROLLMENT_LANES and open_enrollment_enabled_for_lane(lane, self.env):
            return self._resolve_open_login(
                username=username, lane=lane, now=now, peer_ip=peer_ip
            )

        # Parse the username (fail-closed on a malformed/secret-bearing user).
        parsed = parse_username(username)
        if parsed is None:
            return LoginRejected(STRATUM_LOGIN_BAD_USERNAME)

        # RESOLVE the device against the C2 registry authority: require
        # accepted (registered AND active). Unregistered/revoked => fail closed.
        resolution = self.device_registry.resolve(
            passport_id=parsed.passport_id,
            device_id=parsed.device_id,
            observed_at=now,
        )
        if not resolution.accepted:
            return LoginRejected(STRATUM_LOGIN_DEVICE_NOT_RESOLVED)

        # ROSTER worker_name: only an enrolled+active identity has one (second
        # fail-closed gate; the credited worker is this server-assigned name).
        worker_name = self.roster.worker_name_for(
            passport_id=parsed.passport_id, device_id=parsed.device_id
        )
        if not worker_name:
            return LoginRejected(STRATUM_LOGIN_NOT_ON_ROSTER)

        # Lane pool routing/binding (server-owned; same env names the credit
        # provider reads). Unconfigured lane => fail closed.
        pool = self._lane_pool(lane)
        if pool is None:
            return LoginRejected(STRATUM_LOGIN_POOL_UNCONFIGURED)
        pool_id, collection_address = pool

        # MINT the per-connection session via the roster-gated internal issuance
        # (require_device_pop=False on the ledger for the stratum path). We only
        # reach here for a roster-proven identity, so this issuance is itself
        # roster-gated. The issued session lands in ledger.sessions for the
        # credit scheduler and carries the roster worker_name.
        issue = self.ledger.issue_session(
            ShadowSessionIssueRequest(
                passport_id=parsed.passport_id,
                device_id=parsed.device_id,
                lane=lane,
                session_kind=SESSION_KIND_MINING,
                worker_id=worker_name,
                requested_at=now,
                ttl=self.session_ttl,
            )
        )
        if not issue.accepted or issue.session is None:
            return LoginRejected(STRATUM_LOGIN_SESSION_NOT_ISSUED)

        return StratumConnectionIdentity(
            passport_id=parsed.passport_id,
            device_id=parsed.device_id,
            worker_name=worker_name,
            lane=lane,
            pool_id=pool_id,
            alice_collection_address=collection_address,
            session=issue.session,
            worker_label=parsed.worker_label,
        )

    def _resolve_open_login(
        self,
        *,
        username: str,
        lane: Lane,
        now: datetime,
        peer_ip: str,
    ) -> StratumConnectionIdentity | LoginRejected:
        """OPEN self-serve resolve: ALICE ADDRESS is the gate (#4) + rate-limited (#3).

        The ROSTER + REGISTRY are bypassed (they do not apply to a registration-less
        miner); the new admission gate is a VALIDATED ALICE ADDRESS — the SS58 format-300
        Alice-token destination (V cross-lane directive: a miner earns Alice tokens, so
        their mining-login identity is the Alice address those tokens go to, NOT the
        mined coin's address). The username is ``<Alice_address>[.<worker_label>]``: the
        FIRST dot-segment is the Alice address, the REST (folded with ``.``) is the
        optional worker label. Fail-closed at every step (bad address / over-limit /
        unconfigured pool / session-not-issued).

        IDENTITY MAPPING (so credit keys per ``alice_address|worker``):
          * ``passport_id`` = the validated, NORMALIZED Alice address (the credit
            identity = the Alice-token destination),
          * ``device_id`` = the sanitized worker label (or ``"default"``),
          * ``worker_name`` = a deterministic SERVER-derived name from (address, label)
            — NOT client-controlled beyond those two validated inputs.

        CREDIT-ONLY: ``collection_address`` is Alice's POOL collection address (from
        :meth:`_lane_pool`), NEVER the miner's Alice address; the miner's Alice address
        is their CREDIT identity only — this resolver never performs a payout/transfer.
        The session is minted through the SAME
        :meth:`ShadowRewardLedger.issue_session` as the roster path, so every credit-only
        guard (``live_reward`` / ``payout`` / ``miner_payout_address`` / HMAC) is
        inherited and no payout destination is ever stored.
        """

        # PARSE: first dot-segment = Alice address; the rest = the optional worker label.
        if not isinstance(username, str) or not username:
            return LoginRejected(STRATUM_LOGIN_BAD_USERNAME)
        head, _, tail = username.partition(".")

        # GATE #4: the identity must be a checksum-valid ALICE address (SS58 format 300 —
        # the alice-wallet's address; the miner's Alice-token destination). NOT the mined
        # coin's address. `validate_alice_address` returns the normalized canonical form
        # or None (REJECT: wrong network / bad checksum / wrong length / junk).
        address = validate_alice_address(head)
        if address is None:
            return LoginRejected(STRATUM_LOGIN_OPEN_BAD_ADDRESS)

        # SANITIZE #4: bound + restrict the worker label; empty/absent => "default".
        worker_label = sanitize_worker_label(tail if tail else None)
        # SERVER-derived worker_name (the credited worker + dedup/cross-check binding).
        worker_name = open_worker_name(address=address, worker_label=worker_label)

        # Lane pool routing/binding (server-owned; Alice's POOL collection address —
        # NEVER the miner's Alice address). Unconfigured lane => fail closed.
        pool = self._lane_pool(lane)
        if pool is None:
            return LoginRejected(STRATUM_LOGIN_POOL_UNCONFIGURED)
        pool_id, collection_address = pool

        # RATE LIMIT #3 (per-address + per-IP + global new-address budget + per-address
        # worker fan-out). Over-limit => fail closed. Checked BEFORE minting so a flood
        # never grows ledger.sessions / the share store unbounded.
        rejection = self._open_limiter().admit(
            address=address, worker_label=worker_label, peer_ip=peer_ip, now=now
        )
        if rejection is not None:
            return LoginRejected(rejection)

        # MINT via the SAME issue_session (inherits live_reward/payout/HMAC guards). The
        # identity is (address, worker_label); session_kind is mining; credit-only flags
        # stay at their safe defaults (the request never sets a payout address / reward).
        issue = self.ledger.issue_session(
            ShadowSessionIssueRequest(
                passport_id=address,
                device_id=worker_label,
                lane=lane,
                session_kind=SESSION_KIND_MINING,
                worker_id=worker_name,
                requested_at=now,
                ttl=self.session_ttl,
            )
        )
        if not issue.accepted or issue.session is None:
            return LoginRejected(STRATUM_LOGIN_SESSION_NOT_ISSUED)

        # The ledger's worker_name_resolver is the ROSTER (which has no entry for an open
        # address), so the minted session carries worker_name=None. Stamp the
        # SERVER-derived open worker_name onto the envelope. worker_name is NOT part of
        # the HMAC payload (see ledger._session_signature), so this replace preserves the
        # signature — the credit server still re-verifies it cross-process. The credit
        # spine (server._reconstruct_session) REQUIRES session.worker_name, so this is
        # what lets an open share reconstruct + credit.
        session = issue.session
        if session.worker_name != worker_name:
            session = replace(session, worker_name=worker_name)
            # Keep ledger.sessions consistent with the carried copy (the in-process
            # proof-authority scheduler iterates ledger.sessions; the cross-process path
            # uses the worker_name-stamped copy carried on the ValidatedShare). Both must
            # carry the server-derived worker_name the credit spine reconstructs against.
            self.ledger.sessions[session.session_id] = session

        return StratumConnectionIdentity(
            passport_id=address,
            device_id=worker_label,
            worker_name=worker_name,
            lane=lane,
            pool_id=pool_id,
            alice_collection_address=collection_address,
            session=session,
            worker_label=worker_label,
        )

    def _open_limiter(self) -> OpenEnrollmentLimiter:
        """The shared open-enrollment limiter, lazily built from env on first use."""

        if self.open_limiter is None:
            from alice_acp.transport_front.open_enrollment import OpenEnrollmentLimits

            self.open_limiter = OpenEnrollmentLimiter(
                limits=OpenEnrollmentLimits.from_env(self.env)
            )
        return self.open_limiter

    def _lane_pool(self, lane: Lane) -> tuple[str, str] | None:
        spec = _LANE_POOL_ENV.get(lane)
        if spec is None:
            return None
        source = self.env if self.env is not None else os.environ
        pool_id_env, address_env = spec
        pool_id = (source.get(pool_id_env) or "").strip()
        address = (source.get(address_env) or "").strip()
        if not pool_id or not address:
            return None
        return pool_id, address

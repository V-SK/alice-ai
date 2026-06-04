"""Account-poll-lane self-serve enrollment (PRL/pearlhash + any non-stratum lane).

THE GAP THIS CLOSES (measured, live)
------------------------------------
The deployed PRL provider (:class:`PearlhashPoolEvidenceProvider`) fetches + parses
Alice's pearlhash account fine, but credit never AUTO-FLOWS because the
:class:`~alice_acp.shadow_server.proof_authority_scheduler.ProofAuthorityScheduler`
only polls a provider when it has a TARGET for a worker, and a target needs an Alice
SESSION for that worker (``_build_proof_authority_targets`` joins ``ledger.sessions``
UNION the proxy store's ``pending_sessions()``). The STRATUM lanes (LTC/XMR/RVN) mint
that session at login through the transport front. **PRL has NO stratum front** —
``pearl-miner`` mines pearlhash DIRECTLY (raw ``--host host:port --user <addr>
--worker <name>``), so no Alice session is ever created → the scheduler never polls
the PRL provider → ``prl_pending_shares.jsonl`` stays empty.

THE ACCOUNT-POLL ENROLLMENT (this module)
-----------------------------------------
A registration-less, self-serve enrollment path for an ACCOUNT-POLL (non-stratum)
lane. A PRL miner registers ONCE with their **Alice address** (the SS58 format-300
Alice-token destination — the V cross-lane directive) + an optional worker label.
The server, fail-closed at every step:

1. VALIDATES the Alice address (:func:`validate_alice_address` — full SS58 +
   blake2b checksum, NOT a regex; the SAME gate the stratum open lane uses);
2. SANITIZES the worker label + applies the SAME anti-spam budget
   (:class:`OpenEnrollmentLimiter`) the stratum open lane uses;
3. DERIVES the SERVER-OWNED ``worker_name`` = :func:`open_worker_name`\\ ``(address,
   label)`` — the SAME deterministic derivation the stratum open lane + the credit
   re-derivation (``_credit_worker_name``) use, so the three always agree;
4. MINTS an HMAC-SIGNED :class:`ShadowSession` for ``(passport_id=address,
   device_id=label, worker_name)`` via the SAME
   :meth:`ShadowRewardLedger.issue_session` the roster + stratum-open paths use
   (inheriting EVERY credit-only guard: live_reward / payout / miner_payout_address
   / the keyed HMAC), and stores it in a :class:`PrlSessionRegistrationStore`;
5. RETURNS the derived ``worker_name`` so the miner passes it as
   ``pearl-miner --worker <worker_name>`` (so pearlhash reports THAT exact name in
   ``connected_workers[].worker_name``, which the provider matches).

The scheduler's target source then unions the registration store's
:meth:`pending_sessions` (exactly like it unions the proxy store's), so the PRL
provider is polled per registered worker and the worker's epoch-share credits to the
registered worker's Alice address — closing the loop with NO stratum session.

SECURITY (load-bearing): the worker_name↔Alice-address binding is AUTHENTICATED by
the SESSION HMAC, identical to the stratum open lane. The ``worker_name`` is a pure
deterministic function of the Alice address (``open_worker_name(address, label)``);
the session signature is a keyed HMAC over ``(session_id, passport_id=address,
device_id=label, lane, expires_at, session_nonce)`` (``ledger._session_signature``).
On the credit side the worker_name is RE-DERIVED from the SIGNED ``passport_id`` /
``device_id`` and the session signature is RE-VERIFIED against the shared auth-secret
(``ledger.verify_session_signature`` / ``admit_cross_process_session``) BEFORE any
epoch drains. So NO ONE can claim another miner's worker_name to steal their PRL
credit: to be credited under ``W = open_worker_name(victim_addr, label)`` an attacker
would need a signature-valid session whose SIGNED ``passport_id`` is ``victim_addr``
— which they cannot forge without the server secret, and which would in any case
credit ``victim_addr`` (the passport_id), not the attacker. A forged registration
(wrong/absent signature) fails ``verify_session_signature`` and credits nothing.

CREDIT-ONLY: nothing here sets a reward/payout/chain symbol or reads/writes a payout
address. The Alice address is a CREDIT identity (the Alice-token destination) only;
the registration store holds only the public session envelope (no secret material).
This is the registration counterpart of ``transport_front/open_enrollment.py``; it
adds NO new trust primitive — it reuses the address gate, the worker derivation, the
anti-spam budget, and the keyed-HMAC session, wired for a non-stratum lane.
"""

from __future__ import annotations

import json
import os
import threading
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from alice_acp.shadow_server.types import (
    SESSION_KIND_MINING,
    Lane,
    ShadowSession,
    ShadowSessionIssueRequest,
)
from alice_acp.transport_front.alice_address import validate_alice_address
from alice_acp.transport_front.open_enrollment import (
    OpenEnrollmentLimiter,
    open_worker_name,
    sanitize_worker_label,
)

# --- env flag ----------------------------------------------------------------

#: PER-LANE master switch for ACCOUNT-POLL enrollment on the PRL lane (default OFF).
#: Distinct from every stratum lane's ``ALICE_*_OPEN_ENROLLMENT`` flag so a stray
#: stratum flag can never flip PRL account-poll enrollment on (and vice-versa). With
#: it OFF :func:`enroll_account_poll_worker` refuses every registration (fail-closed),
#: so the PRL credit loop stays exactly as today until an operator turns it on.
PRL_ACCOUNT_POLL_ENROLLMENT_ENV = "ALICE_PRL_ACCOUNT_POLL_ENROLLMENT"

#: Default session TTL for a registered account-poll worker. PRL epochs mature over
#: ~12-24h and a rig mines for days, so the session must live long enough that the
#: scheduler keeps a live target across many epochs without re-enrollment. A miner
#: re-enrolls (idempotently) to refresh it; the TTL is a generous default an operator
#: can pin via env. NOT a security boundary (the HMAC is), just a liveness window.
DEFAULT_ACCOUNT_POLL_SESSION_TTL = timedelta(days=7)
ACCOUNT_POLL_SESSION_TTL_ENV = "ALICE_PRL_ACCOUNT_POLL_SESSION_TTL_SECONDS"


def _truthy(value: str | None) -> bool:
    return (value or "").strip().lower() in {"1", "true", "yes", "on"}


def account_poll_enrollment_enabled(env: dict[str, str] | None = None) -> bool:
    """Whether PRL account-poll enrollment is enabled (default OFF). Fail-closed."""

    source = env if env is not None else os.environ
    return _truthy(source.get(PRL_ACCOUNT_POLL_ENROLLMENT_ENV))


def account_poll_session_ttl(env: dict[str, str] | None = None) -> timedelta:
    """The configured account-poll session TTL, else the safe default (fail-soft)."""

    source = env if env is not None else os.environ
    raw = (source.get(ACCOUNT_POLL_SESSION_TTL_ENV) or "").strip()
    if not raw:
        return DEFAULT_ACCOUNT_POLL_SESSION_TTL
    try:
        seconds = float(raw)
    except ValueError:
        return DEFAULT_ACCOUNT_POLL_SESSION_TTL
    return timedelta(seconds=seconds) if seconds > 0 else DEFAULT_ACCOUNT_POLL_SESSION_TTL


# --- the registration store (the scheduler-discoverable seam) ----------------


class PrlSessionRegistrationStore(Protocol):
    """Durable store of SIGNED account-poll sessions, keyed by ``worker_name``.

    The credit-flow seam: :func:`enroll_account_poll_worker` records each minted,
    HMAC-signed :class:`ShadowSession` here; the proof-authority scheduler's target
    source unions :meth:`pending_sessions` (NON-consuming) into its candidate
    sessions, exactly as it unions the proxy store's. So a registered worker becomes
    a poll TARGET and its epoch-share credits — with NO stratum session.

    Keyed by ``worker_name`` (one live registration per server-derived worker name):
    a re-enrollment of the SAME ``(address, label)`` derives the SAME worker_name and
    REPLACES the prior session (idempotent refresh), so the store never grows for a
    returning miner. The store holds ONLY the public session envelope (no secret); the
    credit server STILL re-verifies each session's signature before crediting, so a
    record discovered here grants nothing on its own.
    """

    def record(self, session: ShadowSession) -> None:
        ...

    def pending_sessions(self) -> tuple[ShadowSession, ...]:
        ...


@dataclass(slots=True)
class InMemoryPrlSessionRegistrationStore:
    """In-memory :class:`PrlSessionRegistrationStore` (tests / single-process default).

    Keyed by the server-derived ``worker_name`` (last-write-wins, so a re-enrollment
    refreshes the session in place — bounded by the distinct-worker count, which the
    enrollment limiter's per-address worker fan-out already caps).
    """

    _by_worker: dict[str, ShadowSession] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def record(self, session: ShadowSession) -> None:
        key = session.worker_name or session.worker_id
        if not key:
            return
        with self._lock:
            self._by_worker[key] = session

    def pending_sessions(self) -> tuple[ShadowSession, ...]:
        with self._lock:
            return tuple(self._by_worker.values())

    def size(self) -> int:
        with self._lock:
            return len(self._by_worker)


@dataclass(slots=True)
class JsonlPrlSessionRegistrationStore:
    """Durable, append-only :class:`PrlSessionRegistrationStore` (mirrors the cursor).

    Each :meth:`record` appends the PUBLIC session envelope (NO secret material — the
    HMAC ``signature`` + the public ``session_nonce`` only, both already on the
    envelope); replay on construction rebuilds the live map per ``worker_name``
    (LAST record wins — a refreshed session). A write OSError is raised as
    :class:`PrlSessionRegistrationUnavailable` so the caller fails the enrollment
    closed (never silently drops a registration). The credit server re-verifies the
    signature before crediting, so a malformed/forged record never grants credit.
    """

    path: Path
    _by_worker: dict[str, ShadowSession] = field(default_factory=dict)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        self.path = Path(self.path)
        if self.path.suffix != ".jsonl":
            self.path = self.path / "prl_account_poll_sessions.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        self._load()

    def record(self, session: ShadowSession) -> None:
        key = session.worker_name or session.worker_id
        if not key:
            return
        with self._lock:
            self._append_locked(_session_to_record(session))
            self._by_worker[key] = session

    def pending_sessions(self) -> tuple[ShadowSession, ...]:
        with self._lock:
            return tuple(self._by_worker.values())

    def size(self) -> int:
        with self._lock:
            return len(self._by_worker)

    def _append_locked(self, record: dict[str, Any]) -> None:
        try:
            with self.path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, sort_keys=True, separators=(",", ":")) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        except OSError as exc:
            raise PrlSessionRegistrationUnavailable(
                "prl_account_poll_session_store_unavailable"
            ) from exc

    def _load(self) -> None:
        with self.path.open(encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                session = _session_from_record(record)
                if session is None:
                    continue
                key = session.worker_name or session.worker_id
                if key:
                    self._by_worker[key] = session


class PrlSessionRegistrationUnavailable(RuntimeError):
    """The durable registration store could not be written — fail the enrollment."""


# --- the enrollment request / result ----------------------------------------


@dataclass(frozen=True, slots=True)
class AccountPollEnrollmentRequest:
    """A self-serve account-poll registration: an Alice address + optional label.

    ``alice_address`` is the miner's Alice SS58 format-300 token DESTINATION (their
    CREDIT identity — the V cross-lane directive), NOT the mined coin's address.
    ``worker_label`` is the optional human label (sanitized server-side); the
    SERVER-derived ``worker_name`` (NOT this label) is what the miner passes to
    ``pearl-miner --worker``. ``peer_ip`` is the server-observed source IP (used only
    by the per-IP anti-spam cap; empty skips that cap).
    """

    alice_address: str
    #: PRL is the GPU-PRIMARY lane and now has its OWN credit lane
    #: (``main_pool_gpu_prl``) instead of overloading the stratum RVN lane id
    #: ``main_pool_gpu_rvn``. It stays in the shared-GPU-budget family (it shares the
    #: GPU sub-budget pro-rata with RVN + Quai — the intended design, since PRL/RVN are
    #: the same GPU capacity), but its credit now carries a PRL-specific lane so PRL work
    #: is distinguishable from RVN work in the ledger/reward statements and the
    #: account-poll credit gate no longer collides with the stratum RVN open lane.
    lane: Lane = "main_pool_gpu_prl"
    worker_label: str | None = None
    peer_ip: str = ""


@dataclass(frozen=True, slots=True)
class AccountPollEnrollmentResult:
    """The outcome of an account-poll enrollment (credit-only; no payout/reward).

    On ACCEPT, ``session`` is the minted HMAC-signed :class:`ShadowSession` and
    ``worker_name`` is the SERVER-derived name the miner MUST pass to
    ``pearl-miner --worker <worker_name>`` (so pearlhash reports it in
    ``connected_workers[].worker_name`` and the provider matches it). On REJECT, both
    are ``None`` and ``reason_code`` is a stable, secret-free reason.
    """

    accepted: bool
    reason_code: str
    worker_name: str | None = None
    session: ShadowSession | None = None
    alice_address: str | None = None


# Stable, secret-free reason codes (mirror the stratum-open rejection vocabulary).
ACCOUNT_POLL_ENROLLED = "account_poll_enrolled"
ACCOUNT_POLL_DISABLED = "account_poll_enrollment_disabled"
ACCOUNT_POLL_BAD_ADDRESS = "account_poll_bad_alice_address"
ACCOUNT_POLL_SESSION_NOT_ISSUED = "account_poll_session_not_issued"
ACCOUNT_POLL_STORE_UNAVAILABLE = "account_poll_store_unavailable"


#: A ledger able to mint a signed session. Structural so this module never imports the
#: heavy ledger; the harness/edge passes its real :class:`ShadowRewardLedger`.
class _SessionIssuer(Protocol):
    def issue_session(self, request: ShadowSessionIssueRequest) -> Any:
        ...


def enroll_account_poll_worker(
    request: AccountPollEnrollmentRequest,
    *,
    ledger: _SessionIssuer,
    store: PrlSessionRegistrationStore,
    now: datetime,
    limiter: OpenEnrollmentLimiter | None = None,
    env: dict[str, str] | None = None,
) -> AccountPollEnrollmentResult:
    """Validate + mint + record an account-poll worker registration, fail-closed.

    The full chain (every step fail-closed — a failure REJECTS, never a free pass):

    1. GATE: account-poll enrollment must be enabled for this process (env, default
       OFF). Disabled => reject (the PRL credit loop stays exactly as today).
    2. ADDRESS: ``validate_alice_address`` (SS58 format-300 + blake2b checksum). A
       bad/typo'd/wrong-network address => reject (never persisted under an un-ownable
       key).
    3. LABEL + ANTI-SPAM: sanitize the worker label and apply the SAME
       :class:`OpenEnrollmentLimiter` budget the stratum open lane uses (per-address +
       per-IP rate, global new-address budget, per-address worker fan-out). Over-limit
       => reject (so a registration flood never grows the store / ledger unbounded).
    4. DERIVE: ``worker_name = open_worker_name(address, label)`` — the SAME server
       derivation the stratum open lane + the credit re-derivation use.
    5. MINT: an HMAC-signed session via ``ledger.issue_session`` (inherits every
       credit-only guard + the keyed HMAC). A non-accepted issue (e.g. missing signing
       secret, rate cap, kill-switch) => reject with that reason.
    6. RECORD: the signed session into ``store`` (the scheduler-discoverable seam). A
       durable-store write failure => reject (the registration is not silently lost).

    Returns the derived ``worker_name`` + the session on accept; the miner runs
    ``pearl-miner --user <Alice PRL collection address> --worker <worker_name>``.
    """

    # 1. GATE (default OFF).
    if not account_poll_enrollment_enabled(env):
        return AccountPollEnrollmentResult(False, ACCOUNT_POLL_DISABLED)

    # 2. ADDRESS gate (SS58 format-300 + checksum; NOT a regex).
    address = validate_alice_address(request.alice_address)
    if address is None:
        return AccountPollEnrollmentResult(False, ACCOUNT_POLL_BAD_ADDRESS)

    # 3. LABEL + anti-spam budget (the SAME limiter the stratum open lane uses).
    worker_label = sanitize_worker_label(request.worker_label)
    if limiter is not None:
        rejection = limiter.admit(
            address=address,
            worker_label=worker_label,
            peer_ip=request.peer_ip,
            now=now,
        )
        if rejection is not None:
            return AccountPollEnrollmentResult(False, rejection)

    # 4. DERIVE the server-owned worker_name (the credited worker + cross-check key).
    worker_name = open_worker_name(address=address, worker_label=worker_label)

    # 5. MINT the HMAC-signed session (inherits the credit-only + keyed-HMAC guards).
    issue = ledger.issue_session(
        ShadowSessionIssueRequest(
            passport_id=address,
            device_id=worker_label,
            lane=request.lane,
            session_kind=SESSION_KIND_MINING,
            worker_id=worker_name,
            requested_at=now,
            ttl=account_poll_session_ttl(env),
        )
    )
    session = getattr(issue, "session", None)
    if not getattr(issue, "accepted", False) or session is None:
        reason = getattr(issue, "reason_code", ACCOUNT_POLL_SESSION_NOT_ISSUED)
        return AccountPollEnrollmentResult(False, reason or ACCOUNT_POLL_SESSION_NOT_ISSUED)

    # STAMP the derived worker_name onto the session envelope. ``issue_session`` only
    # sets ``worker_name`` from a roster ``worker_name_resolver`` (the registration-less
    # path has none → it would be ``None``), but ``credit_attested_shares`` requires the
    # session to carry the server-assigned ``worker_name``. SIGNATURE-SAFE: the keyed
    # HMAC covers (session_id, passport_id, device_id, lane, expires_at, session_nonce)
    # — NOT ``worker_name`` (``ledger._session_signature``), so re-stamping it does not
    # invalidate the signature, and the credit side independently RE-DERIVES the SAME
    # name from the signed passport_id/device_id (the trust anchor), using this carried
    # value only as a defensive equality check. So stamping it here is purely a carry
    # convenience, never a trust grant.
    if session.worker_name != worker_name:
        session = replace(session, worker_name=worker_name)

    # 6. RECORD into the scheduler-discoverable store (fail-closed on a write error).
    try:
        store.record(session)
    except PrlSessionRegistrationUnavailable:
        return AccountPollEnrollmentResult(False, ACCOUNT_POLL_STORE_UNAVAILABLE)

    return AccountPollEnrollmentResult(
        accepted=True,
        reason_code=ACCOUNT_POLL_ENROLLED,
        worker_name=worker_name,
        session=session,
        alice_address=address,
    )


# --- public session (de)serialization (no secret material) -------------------
#
# Mirrors pool_evidence_providers._session_to_record / _session_from_record EXACTLY
# (the same public envelope the cross-process credit plane persists). Duplicated here
# rather than imported to avoid a transport_front -> shadow_server import edge for a
# 10-line helper; the field set is identical so the two stores are interchangeable.

_REGISTRATION_REQUIRED = (
    "session_id",
    "passport_id",
    "device_id",
    "lane",
    "session_kind",
    "signature",
)


def _session_to_record(session: ShadowSession) -> dict[str, Any]:
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
    if not isinstance(value, dict):
        return None
    try:
        issued_at = datetime.fromisoformat(str(value["issued_at"]))
        expires_at = datetime.fromisoformat(str(value["expires_at"]))
    except (KeyError, ValueError, TypeError):
        return None
    if any(
        not isinstance(value.get(key), str) or not value.get(key)
        for key in _REGISTRATION_REQUIRED
    ):
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

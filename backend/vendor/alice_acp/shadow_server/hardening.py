from __future__ import annotations

import hashlib
import json
import math
import os
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from ipaddress import ip_address, ip_network
from pathlib import Path
from typing import Literal

SHADOW_SERVER_MODE = "staging_shadow_only"
RATE_LIMIT_STORE_UNAVAILABLE_REASON = "rate_limit_store_unavailable"
AUTH_MISSING_REASON = "missing_auth"
AUTH_INVALID_REASON = "invalid_auth"
NON_INTERNAL_REASON = "non_internal_shadow_access"
# B1: an external (public) client that arrived via the trusted public proxy and is
# requesting a mining-flow route while the public-miner gate is ON is admitted to
# the DISTINCT ``public_miner`` scope (NOT ``internal``). A public client that is
# admitted to the scope but targets a NON-mining route is rejected with this
# reason at the single chokepoint (fail-closed: the scope only ever reaches the
# mining flow; identity is still proven downstream by the unchanged PoP/nonce/
# roster ledger gates).
PUBLIC_MINER_NON_MINING_REASON = "public_miner_scope_non_mining_route"
RATE_LIMIT_REASON = "rate_limited"
RATE_LIMIT_BURST_REASON = "rate_limited_burst"
RATE_LIMIT_HOURLY_REASON = "rate_limited_hourly"
RATE_LIMIT_IDENTITY_MISSING_REASON = "missing_rate_limit_identity"

# B1: ``public_miner`` is a DISTINCT authorized scope an EXTERNAL client enters
# ONLY behind the trusted public proxy, on a mining-flow route, with the gate ON
# (and proving identity downstream). It is deliberately NOT ``internal`` — nothing
# that keys off the trusted ``internal`` classification is widened by B1.
ClientScope = Literal["local", "internal", "external", "public_miner"]
RateLimitIdentityKind = Literal[
    "api_key",
    "auth_token",
    "passport_device",
    "cookie",
    "client_ip",
]


@dataclass(frozen=True, slots=True)
class ShadowModeInvariants:
    shadow_mode: str = SHADOW_SERVER_MODE
    staging_only: bool = True
    staging_internal_only: bool = True
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False
    can_start_live_rewards: bool = False
    can_start_payout_executor: bool = False
    production_ready: bool = False

    def as_payload(self) -> dict[str, object]:
        return {
            "shadow_mode": self.shadow_mode,
            "mode": self.shadow_mode,
            "staging_only": self.staging_only,
            "staging_internal_only": self.staging_internal_only,
            "live_reward_enabled": self.live_reward_enabled,
            "payout_executor_enabled": self.payout_executor_enabled,
            "can_start_live_rewards": self.can_start_live_rewards,
            "can_start_payout_executor": self.can_start_payout_executor,
            "production_ready": self.production_ready,
        }


@dataclass(frozen=True, slots=True)
class ShadowAccessPolicy:
    local_hosts: tuple[str, ...] = ("127.0.0.1", "::1", "localhost")
    internal_cidrs: tuple[str, ...] = (
        "100.64.0.0/10",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "fc00::/7",
    )
    allowed_external_hosts: tuple[str, ...] = ()
    allow_localhost_without_auth: bool = True
    require_auth_for_internal: bool = True
    # Phase H ("public-XFF"): trusted reverse-proxy allowlist. The chosen launch
    # topology is PUBLIC (external miners -> nginx -> internal ACP), so behind
    # nginx every miner appears as the nginx socket IP. ``X-Forwarded-For`` is
    # honored ONLY when the raw connection peer is one of these IPs (a known
    # reverse proxy); the real client IP is then the RIGHTMOST-untrusted hop of
    # the forwarded chain. Default EMPTY => never trust a client-supplied
    # forwarded header; the socket peer is the authoritative client host
    # (fail-closed). This is the shadow-edge port of the F-phase H1 fix
    # (api_chat_gateway/public_gateway._resolve_network_anchor / trusted_proxy_ips).
    trusted_proxy_ips: tuple[str, ...] = ()
    # B1 ("public-miner gate"): DEFAULT OFF. When False (the default, and the
    # behavior identical to today), an external/public client is rejected exactly
    # as before — ``evaluate_shadow_access`` never enters the public-miner branch.
    # When True, an EXTERNAL client that (a) arrived via the trusted public proxy
    # (``resolved.forwarded`` — its real IP recovered through ``trusted_proxy_ips``)
    # and (b) is requesting a MINING-FLOW route is admitted to the DISTINCT
    # ``public_miner`` scope WITHOUT the internal auth secret; identity (valid
    # Ed25519 PoP + fresh server-issued issuance nonce + on-roster) is then proven
    # by the UNCHANGED downstream ledger gates (C2 device-PoP / H_a nonce / roster),
    # and the mining-only restriction is enforced at the chokepoint. Wired from
    # ``ALICE_ACP_PUBLIC_MINER_GATE_ENABLED`` (default 0) in ``main()``, exactly
    # like the existing scheduler flags. CREDIT-ONLY: the admitted path funnels
    # into the same session-issue / proof-ingest handlers under the same ledger
    # guards (paid_acu "0"; reward/payout/chain stay OFF).
    public_miner_gate_enabled: bool = False

    def __post_init__(self) -> None:
        for proxy_ip in self.trusted_proxy_ips:
            try:
                ip_address(proxy_ip.strip())
            except ValueError as exc:
                raise ValueError(
                    "trusted_proxy_ips must be valid IP addresses"
                ) from exc

    @property
    def normalized_trusted_proxy_ips(self) -> frozenset[str]:
        return frozenset(
            ip_address(proxy_ip.strip()).compressed.lower()
            for proxy_ip in self.trusted_proxy_ips
        )

    def as_payload(self) -> dict[str, object]:
        return {
            "allow_localhost_without_auth": self.allow_localhost_without_auth,
            "require_auth_for_internal": self.require_auth_for_internal,
            "internal_cidrs": list(self.internal_cidrs),
            "external_access_default": "deny",
            "allowed_external_hosts": list(self.allowed_external_hosts),
            # public-XFF: count only (never the IPs themselves) so the contract
            # surface advertises that a trusted-proxy seam exists + is fail-closed
            # by default, without leaking operator network topology.
            "trusted_proxy_count": len(self.trusted_proxy_ips),
            "trusted_proxy_forwarding": len(self.trusted_proxy_ips) > 0,
            "forwarded_header_trusted_only_behind_proxy": True,
            # B1: advertise the public-miner gate state (default OFF). Boolean only
            # — no network topology leaked.
            "public_miner_gate_enabled": self.public_miner_gate_enabled,
        }


@dataclass(frozen=True, slots=True)
class ShadowAccessDecision:
    allowed: bool
    reason_code: str
    client_scope: ClientScope
    auth_required: bool
    http_status: int


@dataclass(frozen=True, slots=True)
class ShadowRateLimitPolicy:
    enabled: bool = True
    window_seconds: int = 60
    max_requests_per_window: int = 120
    max_admin_requests_per_window: int = 10
    hourly_window_seconds: int = 3600
    max_requests_per_hour: int = 120
    burst_window_seconds: int = 60
    max_burst_requests: int = 30

    def as_payload(self) -> dict[str, object]:
        return {
            "enabled": self.enabled,
            "window_seconds": self.window_seconds,
            "max_requests_per_window": self.max_requests_per_window,
            "max_admin_requests_per_window": self.max_admin_requests_per_window,
            "hourly_window_seconds": self.hourly_window_seconds,
            "max_requests_per_hour": self.max_requests_per_hour,
            "burst_window_seconds": self.burst_window_seconds,
            "max_burst_requests": self.max_burst_requests,
            "identity_material": "hashed_only",
            "stateful_enforcement": "admission_store_in_memory",
        }


@dataclass(frozen=True, slots=True)
class ShadowRateLimitDecision:
    allowed: bool
    reason_code: str
    limit: int
    observed_count: int
    window_seconds: int
    retry_after_seconds: int | None = None
    identity_kind: RateLimitIdentityKind | None = None
    identity_hash: str | None = None
    window_kind: str | None = None
    remaining_hourly: int = 0
    remaining_burst: int = 0


@dataclass(frozen=True, slots=True)
class ShadowRateLimitIdentity:
    identity_kind: RateLimitIdentityKind
    identity_hash: str
    api_key_hash: str | None = None
    auth_token_hash: str | None = None
    passport_hash: str | None = None
    device_hash: str | None = None
    client_ip_hash: str | None = None
    cookie_hash: str | None = None

    @property
    def limit_key(self) -> str:
        return f"{self.identity_kind}:{self.identity_hash}"

    def as_audit_payload(self) -> dict[str, object]:
        return {
            key: value
            for key, value in {
                "identity_kind": self.identity_kind,
                "identity_hash": self.identity_hash,
                "api_key_hash": self.api_key_hash,
                "auth_token_hash": self.auth_token_hash,
                "passport_hash": self.passport_hash,
                "device_hash": self.device_hash,
                "client_ip_hash": self.client_ip_hash,
                "cookie_hash": self.cookie_hash,
            }.items()
            if value is not None
        }


def _rate_limit_decision(
    *,
    policy: ShadowRateLimitPolicy,
    identity: ShadowRateLimitIdentity,
    hourly: list[datetime],
    burst: list[datetime],
    observed_at: datetime,
) -> tuple[ShadowRateLimitDecision, bool]:
    """Shared rate-limit verdict over already-retained timestamps.

    Returns ``(decision, should_record)`` so both the in-memory and the durable
    file-backed limiter share identical hourly/burst semantics; only the storage
    of admission timestamps differs.
    """

    if len(hourly) >= policy.max_requests_per_hour:
        return (
            ShadowRateLimitDecision(
                False,
                RATE_LIMIT_HOURLY_REASON,
                policy.max_requests_per_hour,
                len(hourly),
                policy.hourly_window_seconds,
                retry_after_seconds=_retry_after_seconds(
                    oldest=hourly[0],
                    observed_at=observed_at,
                    window_seconds=policy.hourly_window_seconds,
                ),
                identity_kind=identity.identity_kind,
                identity_hash=identity.identity_hash,
                window_kind="hourly",
                remaining_hourly=0,
                remaining_burst=max(policy.max_burst_requests, 0),
            ),
            False,
        )
    if len(burst) >= policy.max_burst_requests:
        return (
            ShadowRateLimitDecision(
                False,
                RATE_LIMIT_BURST_REASON,
                policy.max_burst_requests,
                len(burst),
                policy.burst_window_seconds,
                retry_after_seconds=_retry_after_seconds(
                    oldest=burst[0],
                    observed_at=observed_at,
                    window_seconds=policy.burst_window_seconds,
                ),
                identity_kind=identity.identity_kind,
                identity_hash=identity.identity_hash,
                window_kind="burst",
                remaining_hourly=max(policy.max_requests_per_hour - len(hourly), 0),
                remaining_burst=0,
            ),
            False,
        )
    return (
        ShadowRateLimitDecision(
            True,
            "within_rate_limit",
            policy.max_requests_per_hour,
            len(hourly) + 1,
            policy.hourly_window_seconds,
            identity_kind=identity.identity_kind,
            identity_hash=identity.identity_hash,
            window_kind="hourly",
            remaining_hourly=max(policy.max_requests_per_hour - len(hourly) - 1, 0),
            remaining_burst=max(policy.max_burst_requests - len(burst) - 1, 0),
        ),
        True,
    )


def _disabled_rate_limit_decision(
    *,
    policy: ShadowRateLimitPolicy,
    identity: ShadowRateLimitIdentity,
) -> ShadowRateLimitDecision:
    return ShadowRateLimitDecision(
        True,
        "rate_limit_disabled",
        policy.max_requests_per_hour,
        0,
        0,
        identity_kind=identity.identity_kind,
        identity_hash=identity.identity_hash,
    )


class InMemoryShadowRateLimiter:

    def __init__(self) -> None:
        self._admissions_by_key: dict[str, list[datetime]] = {}
        self._lock = threading.RLock()

    def evaluate_and_record(
        self,
        *,
        policy: ShadowRateLimitPolicy,
        route: str,
        identity: ShadowRateLimitIdentity,
        observed_at: datetime,
    ) -> ShadowRateLimitDecision:
        if not policy.enabled:
            return _disabled_rate_limit_decision(policy=policy, identity=identity)
        _validate_rate_limit_policy(policy)
        key = _limit_key(route=route, identity=identity)
        with self._lock:
            hourly = self._retained(key, observed_at, policy.hourly_window_seconds)
            burst = [
                timestamp
                for timestamp in hourly
                if timestamp > observed_at - timedelta(seconds=policy.burst_window_seconds)
            ]
            decision, should_record = _rate_limit_decision(
                policy=policy,
                identity=identity,
                hourly=hourly,
                burst=burst,
                observed_at=observed_at,
            )
            if should_record:
                hourly.append(observed_at)
                self._admissions_by_key[key] = hourly
            return decision

    def _retained(self, key: str, observed_at: datetime, window_seconds: int) -> list[datetime]:
        cutoff = observed_at - timedelta(seconds=window_seconds)
        retained = [
            timestamp
            for timestamp in self._admissions_by_key.get(key, [])
            if timestamp > cutoff
        ]
        self._admissions_by_key[key] = retained
        return retained


class JsonlShadowRateLimiter:
    """Durable, file-backed sibling of :class:`InMemoryShadowRateLimiter` (Phase F, H5).

    Admission timestamps are appended to an append-only JSONL file and replayed on
    construction, so the per-identity hourly/burst windows SURVIVE a process
    restart -- a restart can no longer be used to reset a single instance's
    counters. It is a drop-in behind the same ``evaluate_and_record`` seam used by
    the in-memory limiter and ``SQLiteAdmissionStore``.

    NOTE: this gives *single-node* durability. A true cross-instance shared store
    (so two PS replicas share one bucket) needs shared infra (a real DB / Redis)
    and remains out of scope -- documented as owner-input/follow-up. Fail-closed:
    a write/read OSError surfaces as ``RATE_LIMIT_STORE_UNAVAILABLE_REASON`` so the
    caller denies rather than silently admitting.
    """

    def __init__(self, root_or_file: str | Path) -> None:
        path = Path(root_or_file)
        self.path = path if path.suffix == ".jsonl" else path / "shadow_rate_limit.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.touch(exist_ok=True)
        self._lock = threading.RLock()
        self._admissions_by_key: dict[str, list[datetime]] = {}
        self._load()

    def evaluate_and_record(
        self,
        *,
        policy: ShadowRateLimitPolicy,
        route: str,
        identity: ShadowRateLimitIdentity,
        observed_at: datetime,
    ) -> ShadowRateLimitDecision:
        if not policy.enabled:
            return _disabled_rate_limit_decision(policy=policy, identity=identity)
        _validate_rate_limit_policy(policy)
        key = _limit_key(route=route, identity=identity)
        with self._lock:
            hourly = self._retained(key, observed_at, policy.hourly_window_seconds)
            burst = [
                timestamp
                for timestamp in hourly
                if timestamp > observed_at - timedelta(seconds=policy.burst_window_seconds)
            ]
            decision, should_record = _rate_limit_decision(
                policy=policy,
                identity=identity,
                hourly=hourly,
                burst=burst,
                observed_at=observed_at,
            )
            if should_record:
                try:
                    self._append(key, observed_at)
                except OSError:
                    return ShadowRateLimitDecision(
                        False,
                        RATE_LIMIT_STORE_UNAVAILABLE_REASON,
                        policy.max_requests_per_hour,
                        len(hourly),
                        policy.hourly_window_seconds,
                        identity_kind=identity.identity_kind,
                        identity_hash=identity.identity_hash,
                    )
                hourly.append(observed_at)
                self._admissions_by_key[key] = hourly
            return decision

    def _retained(self, key: str, observed_at: datetime, window_seconds: int) -> list[datetime]:
        cutoff = observed_at - timedelta(seconds=window_seconds)
        retained = [
            timestamp
            for timestamp in self._admissions_by_key.get(key, [])
            if timestamp > cutoff
        ]
        self._admissions_by_key[key] = retained
        return retained

    def _append(self, key: str, observed_at: datetime) -> None:
        line = json.dumps(
            {"key": key, "observed_at": observed_at.isoformat()},
            sort_keys=True,
            separators=(",", ":"),
        )
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")
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
                        f"shadow_rate_limit_store_corrupt:{self.path}:{line_number}"
                    ) from exc
                if not isinstance(record, dict):
                    raise ValueError(
                        f"shadow_rate_limit_store_corrupt:{self.path}:{line_number}"
                    )
                key = record.get("key")
                observed_raw = record.get("observed_at")
                if not isinstance(key, str) or not isinstance(observed_raw, str):
                    continue
                try:
                    observed_at = datetime.fromisoformat(observed_raw)
                except ValueError:
                    continue
                self._admissions_by_key.setdefault(key, []).append(observed_at)


def build_shadow_health_payload(extra: dict[str, object] | None = None) -> dict[str, object]:
    payload = ShadowModeInvariants().as_payload()
    if extra:
        payload.update(extra)
    assert_shadow_mode_invariants(payload)
    return payload


def resolve_shadow_rate_limit_identity(
    *,
    headers: dict[str, str],
    payload: dict[str, object],
    client_host: str,
) -> ShadowRateLimitIdentity | None:
    api_key = _header_value(headers, "x-api-key")
    api_key_hash = _hash_text(f"api-key:{api_key}") if api_key else None
    token = _bearer_token(headers.get("authorization", ""))
    auth_token_hash = _hash_text(f"auth-token:{token}") if token else None
    passport_hash = _optional_hash(payload.get("passport_id"), namespace="passport")
    device_hash = _optional_hash(payload.get("device_id"), namespace="device")
    cookie_hash = _optional_hash(headers.get("cookie"), namespace="cookie")
    client_ip_hash = _client_host_hash(client_host)

    if api_key_hash:
        identity_hash = _stable_identity_hash(
            _identity_material(
                kind="api_key",
                api_key_hash=api_key_hash,
                passport_hash=passport_hash,
                device_hash=device_hash,
                client_ip_hash=client_ip_hash,
                cookie_hash=cookie_hash,
            )
        )
        return ShadowRateLimitIdentity(
            identity_kind="api_key",
            identity_hash=identity_hash,
            api_key_hash=api_key_hash,
            passport_hash=passport_hash,
            device_hash=device_hash,
            client_ip_hash=client_ip_hash,
            cookie_hash=cookie_hash,
        )
    if auth_token_hash:
        identity_hash = _stable_identity_hash(
            _identity_material(
                kind="auth_token",
                auth_token_hash=auth_token_hash,
                passport_hash=passport_hash,
                device_hash=device_hash,
                client_ip_hash=client_ip_hash,
                cookie_hash=cookie_hash,
            )
        )
        return ShadowRateLimitIdentity(
            identity_kind="auth_token",
            identity_hash=identity_hash,
            auth_token_hash=auth_token_hash,
            passport_hash=passport_hash,
            device_hash=device_hash,
            client_ip_hash=client_ip_hash,
            cookie_hash=cookie_hash,
        )
    if passport_hash and device_hash:
        identity_hash = _stable_identity_hash(
            _identity_material(
                kind="passport_device",
                passport_hash=passport_hash,
                device_hash=device_hash,
                client_ip_hash=client_ip_hash,
                cookie_hash=cookie_hash,
            )
        )
        return ShadowRateLimitIdentity(
            identity_kind="passport_device",
            identity_hash=identity_hash,
            passport_hash=passport_hash,
            device_hash=device_hash,
            client_ip_hash=client_ip_hash,
            cookie_hash=cookie_hash,
        )
    if cookie_hash:
        identity_hash = _stable_identity_hash(
            _identity_material(
                kind="cookie",
                client_ip_hash=client_ip_hash,
                cookie_hash=cookie_hash,
            )
        )
        return ShadowRateLimitIdentity(
            identity_kind="cookie",
            identity_hash=identity_hash,
            client_ip_hash=client_ip_hash,
            cookie_hash=cookie_hash,
        )
    if client_ip_hash:
        identity_hash = _stable_identity_hash(
            _identity_material(kind="client_ip", client_ip_hash=client_ip_hash)
        )
        return ShadowRateLimitIdentity(
            identity_kind="client_ip",
            identity_hash=identity_hash,
            client_ip_hash=client_ip_hash,
        )
    return None


def build_shadow_kill_switch_payload(
    *,
    enabled: bool,
    reason_code: str,
    actor: str,
    observed_at: datetime | None = None,
) -> dict[str, object]:
    if not reason_code:
        raise ValueError("missing_reason_code")
    if not actor:
        raise ValueError("missing_actor")
    recorded_at = observed_at or datetime.now(UTC)
    payload = build_shadow_health_payload(
        {
            "ok": True,
            "admin_action": "kill_switch_update",
            "kill_switch_enabled": enabled,
            "kill_switch": {
                "enabled": enabled,
                "state": "enabled" if enabled else "disabled",
                "reason_code": reason_code,
                "actor": actor,
                "recorded_at": recorded_at.isoformat(),
            },
            "reason_code": reason_code,
            "actor": actor,
            "recorded_at": recorded_at.isoformat(),
        }
    )
    return payload


def assert_shadow_mode_invariants(payload: dict[str, object]) -> None:
    required_false = (
        "live_reward_enabled",
        "payout_executor_enabled",
        "can_start_live_rewards",
        "can_start_payout_executor",
        "production_ready",
    )
    for key in required_false:
        if payload.get(key) is not False:
            raise ValueError(f"{key}_must_remain_false")
    if payload.get("shadow_mode") != SHADOW_SERVER_MODE:
        raise ValueError("shadow_mode_must_be_staging_shadow_only")
    if payload.get("staging_only") is not True:
        raise ValueError("staging_only_must_remain_true")


@dataclass(frozen=True, slots=True)
class ResolvedShadowClient:
    """The client host the edge classifies/rate-limits/audits on (public-XFF).

    ``client_host`` is the authoritative client IP: the rightmost-untrusted
    forwarded hop when the socket peer is a trusted proxy, else the socket peer
    itself. ``forwarded`` is True only when the value was recovered from a
    forwarded header behind an allowlisted proxy (audit signal); ``socket_peer``
    is always the raw connection peer for the audit trail.
    """

    client_host: str
    socket_peer: str
    forwarded: bool = False


def resolve_shadow_client_host(
    *,
    socket_peer: str,
    headers: dict[str, str],
    policy: ShadowAccessPolicy | None = None,
) -> ResolvedShadowClient:
    """Resolve the real client host fail-closed behind a trusted reverse proxy.

    public-XFF (shadow-edge port of the F-phase H1 fix). Priority:

      1. If the raw connection peer is a configured trusted proxy, derive the
         real client from ``X-Forwarded-For`` using the RIGHTMOST-untrusted hop:
         walk the chain right-to-left, skip any address that is itself a trusted
         proxy, and take the first non-proxy address. (Each proxy appends the
         address it received from, so the rightmost-non-proxy entry is the real
         client even with a chain of trusted proxies; a client cannot forge it by
         prepending extra left-hand hops.)
      2. Otherwise (the connection peer is NOT a trusted proxy) the forwarded
         header is IGNORED entirely and the socket peer is the client host. A
         client-supplied ``X-Forwarded-For`` therefore never influences
         classification/rate-limit-identity/audit unless it arrived via an
         allowlisted proxy.

    Default EMPTY allowlist => no peer is ever trusted => the socket peer always
    wins (fail-closed: an unconfigured edge trusts no forwarded header).
    """

    active_policy = policy or ShadowAccessPolicy()
    peer = socket_peer.strip()
    trusted = active_policy.normalized_trusted_proxy_ips
    if peer and _normalized_ip_or_none(peer) in trusted:
        forwarded_chain = _forwarded_for_chain(_header_value(headers, "x-forwarded-for"))
        real_client = _rightmost_untrusted_hop(forwarded_chain, trusted=trusted)
        if real_client is not None:
            return ResolvedShadowClient(
                client_host=real_client,
                socket_peer=peer,
                forwarded=True,
            )
    return ResolvedShadowClient(client_host=peer, socket_peer=peer, forwarded=False)


def _normalized_ip_or_none(value: str) -> str | None:
    try:
        return ip_address(value.strip()).compressed.lower()
    except ValueError:
        return None


def _forwarded_for_chain(value: str | None) -> tuple[str, ...]:
    if not value:
        return ()
    return tuple(hop.strip() for hop in value.split(",") if hop.strip())


def _rightmost_untrusted_hop(
    chain: tuple[str, ...],
    *,
    trusted: frozenset[str],
) -> str | None:
    """Return the rightmost forwarded hop that is NOT itself a trusted proxy.

    Walking right-to-left, each entry that normalizes to a trusted-proxy IP is a
    proxy in the chain and is skipped; the first remaining address is the real
    client. A malformed (non-IP) hop is treated as untrusted and returned
    normalized, so a garbage forwarded value cannot silently elevate to a proxy.
    """

    for hop in reversed(chain):
        normalized = _normalized_ip_or_none(hop)
        if normalized is None:
            # Not a parseable IP: it cannot be a trusted proxy, so it is the
            # client-supplied edge of the chain. Return a normalized form.
            return " ".join(hop.lower().split())
        if normalized not in trusted:
            return normalized
    return None


def classify_shadow_client(
    client_host: str,
    policy: ShadowAccessPolicy | None = None,
) -> ClientScope:
    active_policy = policy or ShadowAccessPolicy()
    normalized = client_host.strip().lower()
    if normalized in active_policy.local_hosts:
        return "local"
    try:
        observed_address = ip_address(normalized)
    except ValueError:
        return "external"
    if observed_address.is_loopback:
        return "local"
    for cidr in active_policy.internal_cidrs:
        if observed_address in ip_network(cidr):
            return "internal"
    # Fail-closed: "internal" (trusted) scope is granted ONLY by the explicit
    # internal_cidrs allowlist (operator RFC1918 + CGNAT/tailnet + ULA). Do NOT
    # fall back to ``ipaddress.is_private`` — it ALSO returns True for the RFC 5737
    # documentation ranges (192.0.2/24, 198.51.100/24, 203.0.113/24), the 198.18/15
    # benchmark range, link-local (169.254/16, fe80::/10) and other reserved blocks
    # that are NOT the operator's trusted network. Granting those "internal" scope
    # would hand untrusted hosts a trusted classification at the public edge.
    return "external"


# B1: the ONLY routes a ``public_miner``-scoped (external, via-proxy) client may
# reach. This is the mining flow end-to-end: mint the server nonce the PoP signs,
# self-enroll, issue a MINING-lane session, ingest a proof, and bump roster
# liveness. Everything else (admin, settlement, balance, previews, health/status/
# readiness, and the INFERENCE lanes) is NOT here, so a public miner is rejected
# from it at the chokepoint (fail-closed: the allowlist is closed by default).
#
# ``/session/issue`` is dual-use (mining vs inference): it is admitted here only as
# the route, and the chokepoint additionally rejects a public-miner ``/session/issue``
# (and ``/inference/*``) whose body is NOT a mining session_kind — see
# ``is_public_miner_mining_request``.
_PUBLIC_MINER_MINING_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {
        ("POST", "/session/nonce"),
        ("POST", "/device/register"),
        ("POST", "/session/issue"),
        ("POST", "/proof/ingest"),
        ("POST", "/heartbeat"),
    }
)

# B1: the session_kind that ``/session/issue`` must carry for a public miner. The
# value mirrors ``types.SESSION_KIND_MINING`` ("mining"); duplicated here as a
# bare literal to keep ``hardening`` free of a ``types`` import cycle.
_PUBLIC_MINER_SESSION_KIND = "mining"


def is_public_miner_mining_route(method: str, path: str) -> bool:
    """True iff ``(method, path)`` is in the closed public-miner mining allowlist.

    Route-level only; the dual-use ``/session/issue`` session_kind check is applied
    separately by :func:`is_public_miner_mining_request` at the chokepoint.
    """

    return (method.upper(), path) in _PUBLIC_MINER_MINING_ROUTES


def is_public_miner_mining_request(
    method: str,
    path: str,
    *,
    session_kind: str | None,
) -> bool:
    """True iff the request is a public-miner-admissible MINING-flow request.

    Extends the route allowlist with the dual-use guard: ``/session/issue`` is
    admitted ONLY for a mining ``session_kind``; an inference session_kind (or the
    ``/inference/*`` lanes, which are not in the route allowlist at all) is NOT a
    mining request, so a public miner cannot use the inference lane. ``session_kind``
    is ignored for the non-dual-use mining routes. Fail-closed: an unknown route is
    not a mining request.
    """

    if not is_public_miner_mining_route(method, path):
        return False
    if path == "/session/issue":
        return session_kind == _PUBLIC_MINER_SESSION_KIND
    return True


def evaluate_shadow_access(
    *,
    policy: ShadowAccessPolicy,
    client_host: str,
    authorization_header: str,
    auth_secret: str | None,
    via_trusted_proxy: bool = False,
    is_mining_route: bool = False,
) -> ShadowAccessDecision:
    """Single shadow-edge access chokepoint.

    B1 adds the ``public_miner`` branch: an EXTERNAL client is admitted to the
    DISTINCT ``public_miner`` scope ONLY when ALL of these hold —
      * the public-miner gate is ON (``policy.public_miner_gate_enabled``),
      * the request arrived via the trusted public proxy (``via_trusted_proxy`` —
        the real client IP was resolved through ``trusted_proxy_ips``), and
      * the request targets a mining-flow route (``is_mining_route`` — including
        the ``/session/issue`` mining-session_kind check the caller folds in).
    Identity (valid Ed25519 PoP + fresh server-issued nonce + on-roster) is proven
    by the UNCHANGED downstream ledger gates, so this branch only opens the
    transport for the mining flow; it never asserts identity itself and never
    grants the trusted ``internal`` scope.

    GATE OFF (default) or ANY condition missing => the function behaves EXACTLY as
    before: an external client falls through to the established
    ``non_internal_shadow_access`` 403 (or the prior internal-auth 401 path).
    """

    scope = classify_shadow_client(client_host, policy)
    auth_required = scope != "local" or not policy.allow_localhost_without_auth
    if scope == "internal":
        auth_required = policy.require_auth_for_internal
    # B1: public-miner admission is decided BEFORE the internal-auth-secret check,
    # because an external miner authenticates with its device PoP (downstream), NOT
    # the internal Bearer secret (which is nginx's probe credential). This branch is
    # reached ONLY when the gate is ON, so with the gate OFF the code below is
    # byte-for-byte the pre-B1 path. ``allowed_external_hosts`` still short-circuits
    # to the existing authorized-external path first (unchanged).
    if (
        scope == "external"
        and client_host not in policy.allowed_external_hosts
        and policy.public_miner_gate_enabled
        and via_trusted_proxy
    ):
        if not is_mining_route:
            # Admitted-but-wrong-route: a public client may ONLY touch the mining
            # flow. Reject every other route at the chokepoint (fail-closed),
            # without ever demanding the internal secret.
            return ShadowAccessDecision(
                False, PUBLIC_MINER_NON_MINING_REASON, "public_miner", False, 403
            )
        # Transport admitted to the public-miner scope. Identity is enforced by the
        # downstream PoP/nonce/roster ledger gates; no internal secret required.
        return ShadowAccessDecision(True, "authorized_public_miner", "public_miner", False, 200)
    if auth_required:
        if not auth_secret or not authorization_header:
            return ShadowAccessDecision(False, AUTH_MISSING_REASON, scope, True, 401)
        if authorization_header != f"Bearer {auth_secret}":
            return ShadowAccessDecision(False, AUTH_INVALID_REASON, scope, True, 401)
    if scope == "external" and client_host not in policy.allowed_external_hosts:
        return ShadowAccessDecision(False, NON_INTERNAL_REASON, scope, auth_required, 403)
    return ShadowAccessDecision(True, "authorized", scope, auth_required, 200)


def evaluate_shadow_rate_limit(
    *,
    policy: ShadowRateLimitPolicy,
    route: str,
    observed_count: int,
) -> ShadowRateLimitDecision:
    limit = (
        policy.max_admin_requests_per_window
        if route.startswith("/admin/")
        else policy.max_requests_per_window
    )
    if not policy.enabled:
        return ShadowRateLimitDecision(True, "rate_limit_disabled", limit, observed_count, 0)
    if observed_count > limit:
        return ShadowRateLimitDecision(
            False,
            RATE_LIMIT_REASON,
            limit,
            observed_count,
            policy.window_seconds,
        )
    return ShadowRateLimitDecision(
        True,
        "within_rate_limit",
        limit,
        observed_count,
        policy.window_seconds,
    )


def _bearer_token(authorization_header: str) -> str | None:
    scheme, _, token = authorization_header.strip().partition(" ")
    if scheme.lower() != "bearer" or not token:
        return None
    return token.strip() or None


def _header_value(headers: dict[str, str], key: str) -> str | None:
    for observed_key, value in headers.items():
        if observed_key.lower() == key and value.strip():
            return value.strip()
    return None


def _optional_hash(value: object, *, namespace: str) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return _hash_text(f"{namespace}:{value}")


def _client_host_hash(client_host: str) -> str | None:
    normalized = client_host.strip().lower()
    if not normalized:
        return None
    return _hash_text(f"client-ip:{normalized}")


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _stable_identity_hash(payload: dict[str, object]) -> str:
    canonical = "|".join(f"{key}={payload[key]}" for key in sorted(payload))
    return _hash_text(f"shadow-rate-limit:{canonical}")


def _identity_material(
    *,
    kind: str,
    api_key_hash: str | None = None,
    auth_token_hash: str | None = None,
    passport_hash: str | None = None,
    device_hash: str | None = None,
    client_ip_hash: str | None = None,
    cookie_hash: str | None = None,
) -> dict[str, object]:
    return {
        key: value
        for key, value in {
            "kind": kind,
            "api_key_hash": api_key_hash,
            "auth_token_hash": auth_token_hash,
            "passport_hash": passport_hash,
            "device_hash": device_hash,
            "client_ip_hash": client_ip_hash,
            "cookie_hash": cookie_hash,
        }.items()
        if value is not None
    }


def _limit_key(*, route: str, identity: ShadowRateLimitIdentity) -> str:
    return f"{route}:{identity.limit_key}"


def _retry_after_seconds(*, oldest: datetime, observed_at: datetime, window_seconds: int) -> int:
    retry_after = (oldest + timedelta(seconds=window_seconds) - observed_at).total_seconds()
    return max(1, math.ceil(retry_after))


def _validate_rate_limit_policy(policy: ShadowRateLimitPolicy) -> None:
    for field_name, value in (
        ("hourly_window_seconds", policy.hourly_window_seconds),
        ("max_requests_per_hour", policy.max_requests_per_hour),
        ("burst_window_seconds", policy.burst_window_seconds),
        ("max_burst_requests", policy.max_burst_requests),
    ):
        if value <= 0:
            raise ValueError(f"{field_name}_must_be_positive")
    if policy.burst_window_seconds > policy.hourly_window_seconds:
        raise ValueError("burst_window_must_not_exceed_hourly_window")

from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from ipaddress import ip_address
from pathlib import Path
from typing import Literal

from alice_acp.api_chat.types import DEFAULT_CHAT_MODEL_ID
from alice_acp.api_chat.validators import ensure_no_raw_secret
from alice_acp.api_chat_gateway.http_harness import StagingHttpRequest
from alice_acp.api_chat_gateway.public_http_wrapper import PublicChatStagingHttpWrapper

API_CHAT_STAGE4_READINESS_CONTRACT_VERSION = "stage4-api-chat-sidecar-readiness-v1"

REASON_STAGE4_DURABLE_READY = "api_chat_stage4_durable_store_ready"
REASON_STAGE4_DURABLE_HASH_ONLY_REQUIRED = (
    "api_chat_stage4_durable_key_store_hash_only_required"
)
REASON_STAGE4_DURABLE_RETENTION_REQUIRED = (
    "api_chat_stage4_durable_retention_config_required"
)
REASON_STAGE4_DURABLE_PLACEHOLDER_REF = (
    "api_chat_stage4_durable_store_placeholder_ref"
)
REASON_STAGE4_DURABLE_SECRET_REF = "api_chat_stage4_durable_store_secret_ref"
REASON_STAGE4_DURABLE_PRODUCTION_DB_ADAPTER = (
    "api_chat_stage4_durable_store_production_db_adapter_forbidden"
)
REASON_STAGE4_DURABLE_DEFAULT_OFF_REQUIRED = (
    "api_chat_stage4_durable_store_default_off_required"
)
REASON_STAGE4_DURABLE_RAW_PERSISTENCE_FORBIDDEN = (
    "api_chat_stage4_durable_raw_persistence_forbidden"
)

REASON_STAGE4_ROUTE_OPS_READY = "api_chat_stage4_route_ops_preflight_ready"
REASON_STAGE4_ROUTE_OPS_PORT_8083_FORBIDDEN = (
    "api_chat_stage4_route_ops_port_8083_forbidden"
)
REASON_STAGE4_ROUTE_OPS_PUBLIC_BIND_AUTH_REQUIRED = (
    "api_chat_stage4_route_ops_public_bind_requires_auth"
)
REASON_STAGE4_ROUTE_OPS_CATCH_ALL_API_FORBIDDEN = (
    "api_chat_stage4_route_ops_catch_all_api_forbidden"
)
REASON_STAGE4_ROUTE_OPS_SECRET_ENV_VALUE = (
    "api_chat_stage4_route_ops_secret_looking_env_value"
)
REASON_STAGE4_ROUTE_OPS_ROLLBACK_OWNER_REQUIRED = (
    "api_chat_stage4_route_ops_rollback_owner_required"
)
REASON_STAGE4_ROUTE_OPS_DEFAULT_OFF_REQUIRED = (
    "api_chat_stage4_route_ops_default_off_required"
)

REASON_STAGE4_SMOKE_READY = "api_chat_stage4_local_smoke_ready"
REASON_STAGE4_SMOKE_WRAPPER_DISABLED = "api_chat_stage4_local_smoke_wrapper_disabled"
REASON_STAGE4_SMOKE_HEALTH_FAILED = "api_chat_stage4_local_smoke_health_failed"
REASON_STAGE4_SMOKE_LIMITS_FAILED = "api_chat_stage4_local_smoke_limits_failed"
REASON_STAGE4_SMOKE_RATE_LIMIT_FAILED = (
    "api_chat_stage4_local_smoke_anonymous_11th_429_failed"
)
REASON_STAGE4_SMOKE_AUDIT_MISSING = "api_chat_stage4_local_smoke_audit_missing"
REASON_STAGE4_SMOKE_AUDIT_NOT_REDACTED = (
    "api_chat_stage4_local_smoke_audit_not_redacted"
)
REASON_STAGE4_SMOKE_DEFAULT_OFF_INVARIANTS_FAILED = (
    "api_chat_stage4_local_smoke_default_off_invariants_failed"
)

DurableStoreAdapterKind = Literal["local_jsonl", "local_sqlite"]

_LOCAL_DURABLE_ADAPTERS = frozenset({"local_jsonl", "local_sqlite"})
_PRODUCTION_DB_ADAPTERS = frozenset(
    {
        "aurora",
        "d1",
        "dynamodb",
        "mariadb",
        "mongo",
        "mongodb",
        "mysql",
        "neon",
        "planetscale",
        "postgres",
        "postgresql",
        "redis",
    }
)
_PLACEHOLDER_MARKERS = (
    "change_me",
    "changeme",
    "example",
    "insert_",
    "placeholder",
    "replace_me",
    "todo",
    "tbd",
    "your_",
)
_EXTRA_SECRET_VALUE_PATTERN = re.compile(
    "|".join(
        (
            "Bearer" + r"\s+[A-Za-z0-9._~+/=-]{8,}",
            "xox" + r"[abprs]-[A-Za-z0-9-]{8,}",
            "pass" + "word" + r"\s*[:=]",
            "private" + r"[_-]?" + "key" + r"\s*[:=]",
            "-----" + "BEGIN ",
        )
    ),
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class DurableStoreReadinessConfig:
    adapter_kind: DurableStoreAdapterKind | str = "local_sqlite"
    api_key_store_ref: str = "state.api_chat.key_hashes.local"
    api_key_store_hash_only: bool = True
    rate_limit_store_ref: str = "state.api_chat.rate_limits.local"
    rate_limit_retention_hours: int = 1
    audit_log_ref: str = "state.api_chat.audit_jsonl.local"
    audit_retention_days: int = 30
    storage_writes_enabled: bool = False
    production_db_adapter: bool = False
    public_service_enabled: bool = False
    raw_api_key_persistence_enabled: bool = False
    raw_prompt_persistence_enabled: bool = False
    raw_response_persistence_enabled: bool = False

    def to_public_dict(self) -> dict[str, object]:
        return {
            "contract_version": API_CHAT_STAGE4_READINESS_CONTRACT_VERSION,
            "adapter_kind": self.adapter_kind,
            "api_key_store_ref_present": bool(self.api_key_store_ref),
            "api_key_store_ref_hash": _hash_ref(self.api_key_store_ref),
            "api_key_store_hash_only": self.api_key_store_hash_only,
            "rate_limit_store_ref_present": bool(self.rate_limit_store_ref),
            "rate_limit_store_ref_hash": _hash_ref(self.rate_limit_store_ref),
            "rate_limit_retention_hours": self.rate_limit_retention_hours,
            "audit_log_ref_present": bool(self.audit_log_ref),
            "audit_log_ref_hash": _hash_ref(self.audit_log_ref),
            "audit_retention_days": self.audit_retention_days,
            "storage_writes_enabled": self.storage_writes_enabled,
            "production_db_adapter": self.production_db_adapter,
            "public_service_enabled": False,
            "raw_api_key_persisted": False,
            "raw_prompt_persisted": False,
            "raw_response_persisted": False,
            "default_off": True,
        }


@dataclass(frozen=True, slots=True)
class DurableStoreReadinessDTO:
    config: DurableStoreReadinessConfig
    reason_codes: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not self.reason_codes

    @property
    def fail_closed(self) -> bool:
        return bool(self.reason_codes)

    @property
    def status_reason_code(self) -> str:
        return REASON_STAGE4_DURABLE_READY if self.ready else self.reason_codes[0]

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": API_CHAT_STAGE4_READINESS_CONTRACT_VERSION,
            "ready": self.ready,
            "fail_closed": self.fail_closed,
            "reason_code": self.status_reason_code,
            "reason_codes": self.reason_codes,
            "config": self.config.to_public_dict(),
            "local_default_off_only": True,
            "no_production_db_adapter": True,
            "no_live_reward_coupling": True,
            "no_payout_executor_coupling": True,
        }


@dataclass(frozen=True, slots=True)
class SidecarRouteOpsPreflightConfig:
    bind_host: str = "127.0.0.1"
    port: int = 18131
    route_paths: tuple[str, ...] = (
        "/health",
        "/limits",
        "/chat",
        "/v1/chat/completions",
    )
    env: Mapping[str, str] = field(default_factory=dict)
    auth_policy_ref: str | None = None
    edge_auth_policy_ref: str | None = None
    rollback_owner_ref: str | None = "owner_api_chat_stage4_ops"
    public_service_enabled: bool = False
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False

    def to_public_dict(self) -> dict[str, object]:
        secret_like_env_keys = _secret_like_env_keys(self.env)
        return {
            "contract_version": API_CHAT_STAGE4_READINESS_CONTRACT_VERSION,
            "bind_host": self.bind_host,
            "bind_scope": _bind_scope_label(self.bind_host),
            "port": self.port,
            "route_paths": self.route_paths,
            "env_keys_checked": tuple(sorted(self.env)),
            "secret_like_env_keys": secret_like_env_keys,
            "auth_policy_configured": bool(self.auth_policy_ref),
            "edge_auth_policy_configured": bool(self.edge_auth_policy_ref),
            "rollback_owner_configured": bool(self.rollback_owner_ref),
            "public_service_enabled": False,
            "live_reward_enabled": False,
            "payout_executor_enabled": False,
            "default_off": True,
        }


@dataclass(frozen=True, slots=True)
class SidecarRouteOpsPreflightDTO:
    config: SidecarRouteOpsPreflightConfig
    reason_codes: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not self.reason_codes

    @property
    def fail_closed(self) -> bool:
        return bool(self.reason_codes)

    @property
    def status_reason_code(self) -> str:
        return REASON_STAGE4_ROUTE_OPS_READY if self.ready else self.reason_codes[0]

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": API_CHAT_STAGE4_READINESS_CONTRACT_VERSION,
            "ready": self.ready,
            "fail_closed": self.fail_closed,
            "reason_code": self.status_reason_code,
            "reason_codes": self.reason_codes,
            "config": self.config.to_public_dict(),
            "long_running_service_started": False,
            "local_default_off_only": True,
            "no_8083": REASON_STAGE4_ROUTE_OPS_PORT_8083_FORBIDDEN
            not in self.reason_codes,
            "no_catch_all_api": REASON_STAGE4_ROUTE_OPS_CATCH_ALL_API_FORBIDDEN
            not in self.reason_codes,
            "no_live_reward_coupling": True,
            "no_payout_executor_coupling": True,
        }


@dataclass(frozen=True, slots=True)
class LocalSidecarSmokeResultDTO:
    health_status_code: int | None
    limits_status_code: int | None
    anonymous_11th_status_code: int | None
    audit_record_count: int
    audit_redacted: bool
    default_off_invariants: bool
    long_running_service_started: bool
    reason_codes: tuple[str, ...]

    @property
    def ready(self) -> bool:
        return not self.reason_codes

    @property
    def fail_closed(self) -> bool:
        return bool(self.reason_codes)

    @property
    def status_reason_code(self) -> str:
        return REASON_STAGE4_SMOKE_READY if self.ready else self.reason_codes[0]

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": API_CHAT_STAGE4_READINESS_CONTRACT_VERSION,
            "ready": self.ready,
            "fail_closed": self.fail_closed,
            "reason_code": self.status_reason_code,
            "reason_codes": self.reason_codes,
            "health_status_code": self.health_status_code,
            "limits_status_code": self.limits_status_code,
            "anonymous_11th_status_code": self.anonymous_11th_status_code,
            "audit_record_count": self.audit_record_count,
            "audit_redacted": self.audit_redacted,
            "default_off_invariants": self.default_off_invariants,
            "long_running_service_started": self.long_running_service_started,
            "public_service_enabled": False,
            "live_reward_enabled": False,
            "payout_executor_enabled": False,
            "paid_acu": "0",
        }


def evaluate_durable_store_readiness(
    config: DurableStoreReadinessConfig | None = None,
) -> DurableStoreReadinessDTO:
    candidate = config or DurableStoreReadinessConfig()
    reasons: list[str] = []

    adapter = str(candidate.adapter_kind).strip().lower()
    if (
        adapter not in _LOCAL_DURABLE_ADAPTERS
        or adapter in _PRODUCTION_DB_ADAPTERS
        or candidate.production_db_adapter
    ):
        reasons.append(REASON_STAGE4_DURABLE_PRODUCTION_DB_ADAPTER)
    if not candidate.api_key_store_hash_only:
        reasons.append(REASON_STAGE4_DURABLE_HASH_ONLY_REQUIRED)
    if candidate.rate_limit_retention_hours <= 0 or candidate.audit_retention_days <= 0:
        reasons.append(REASON_STAGE4_DURABLE_RETENTION_REQUIRED)
    if (
        candidate.storage_writes_enabled
        or candidate.public_service_enabled
        or candidate.raw_api_key_persistence_enabled
        or candidate.raw_prompt_persistence_enabled
        or candidate.raw_response_persistence_enabled
    ):
        reasons.append(REASON_STAGE4_DURABLE_DEFAULT_OFF_REQUIRED)
    if (
        candidate.raw_api_key_persistence_enabled
        or candidate.raw_prompt_persistence_enabled
        or candidate.raw_response_persistence_enabled
    ):
        reasons.append(REASON_STAGE4_DURABLE_RAW_PERSISTENCE_FORBIDDEN)

    _append_ref_reasons(
        reasons,
        (
            candidate.api_key_store_ref,
            candidate.rate_limit_store_ref,
            candidate.audit_log_ref,
        ),
    )
    return DurableStoreReadinessDTO(
        config=candidate,
        reason_codes=_dedupe_reasons(reasons),
    )


def evaluate_sidecar_route_ops_preflight(
    config: SidecarRouteOpsPreflightConfig | None = None,
) -> SidecarRouteOpsPreflightDTO:
    candidate = config or SidecarRouteOpsPreflightConfig()
    reasons: list[str] = []

    if candidate.port == 8083:
        reasons.append(REASON_STAGE4_ROUTE_OPS_PORT_8083_FORBIDDEN)
    if _is_public_bind_host(candidate.bind_host) and not (
        candidate.auth_policy_ref and candidate.edge_auth_policy_ref
    ):
        reasons.append(REASON_STAGE4_ROUTE_OPS_PUBLIC_BIND_AUTH_REQUIRED)
    if any(_is_catch_all_api_route(path) for path in candidate.route_paths):
        reasons.append(REASON_STAGE4_ROUTE_OPS_CATCH_ALL_API_FORBIDDEN)
    if _secret_like_env_keys(candidate.env):
        reasons.append(REASON_STAGE4_ROUTE_OPS_SECRET_ENV_VALUE)
    if _value_missing_or_placeholder(candidate.rollback_owner_ref):
        reasons.append(REASON_STAGE4_ROUTE_OPS_ROLLBACK_OWNER_REQUIRED)
    elif candidate.rollback_owner_ref and _is_secret_looking_value(
        candidate.rollback_owner_ref
    ):
        reasons.append(REASON_STAGE4_ROUTE_OPS_SECRET_ENV_VALUE)
    if (
        candidate.public_service_enabled
        or candidate.live_reward_enabled
        or candidate.payout_executor_enabled
    ):
        reasons.append(REASON_STAGE4_ROUTE_OPS_DEFAULT_OFF_REQUIRED)

    return SidecarRouteOpsPreflightDTO(
        config=candidate,
        reason_codes=_dedupe_reasons(reasons),
    )


def run_local_sidecar_smoke_contract(
    wrapper: PublicChatStagingHttpWrapper,
) -> LocalSidecarSmokeResultDTO:
    reasons: list[str] = []
    health = wrapper.handle(_request("GET", "/health"))
    limits = wrapper.handle(_request("GET", "/limits"))

    if not wrapper.config.enabled:
        reasons.append(REASON_STAGE4_SMOKE_WRAPPER_DISABLED)
    if health.status_code != 200:
        reasons.append(REASON_STAGE4_SMOKE_HEALTH_FAILED)
    if limits.status_code != 200:
        reasons.append(REASON_STAGE4_SMOKE_LIMITS_FAILED)

    headers = {
        "X-Alice-Client-IP": "203.0.113.204",
        "X-Alice-Client-Fingerprint": "stage4-local-smoke",
    }
    chat_responses = [
        wrapper.handle(
            _request(
                "POST",
                "/chat?query_audit_sentinel=stage4-local-smoke-query",
                headers=headers,
                body=_chat_body(f"stage4-local-smoke-prompt-{index}"),
            ),
            client_host="127.0.0.1",
        )
        for index in range(11)
    ]
    anonymous_11th = chat_responses[-1]
    if anonymous_11th.status_code != 429:
        reasons.append(REASON_STAGE4_SMOKE_RATE_LIMIT_FAILED)

    default_off_invariants = all(
        _default_off_body_invariants(response.body)
        for response in (health, limits, anonymous_11th)
    )
    if not default_off_invariants:
        reasons.append(REASON_STAGE4_SMOKE_DEFAULT_OFF_INVARIANTS_FAILED)

    audit_record_count, audit_redacted = _audit_redaction_status(wrapper)
    if audit_record_count == 0:
        reasons.append(REASON_STAGE4_SMOKE_AUDIT_MISSING)
    elif not audit_redacted:
        reasons.append(REASON_STAGE4_SMOKE_AUDIT_NOT_REDACTED)

    long_running_service_started = bool(health.body.get("long_running_service_started"))
    if long_running_service_started:
        reasons.append(REASON_STAGE4_SMOKE_DEFAULT_OFF_INVARIANTS_FAILED)

    return LocalSidecarSmokeResultDTO(
        health_status_code=health.status_code,
        limits_status_code=limits.status_code,
        anonymous_11th_status_code=anonymous_11th.status_code,
        audit_record_count=audit_record_count,
        audit_redacted=audit_redacted,
        default_off_invariants=default_off_invariants,
        long_running_service_started=long_running_service_started,
        reason_codes=_dedupe_reasons(reasons),
    )


def _request(
    method: str,
    path: str,
    *,
    headers: Mapping[str, str] | None = None,
    body: Mapping[str, object] | None = None,
) -> StagingHttpRequest:
    return StagingHttpRequest(
        method=method,
        path=path,
        headers=dict(headers or {}),
        body=json.dumps(body).encode("utf-8") if body is not None else b"",
    )


def _chat_body(message: str) -> dict[str, object]:
    return {
        "message": message,
        "mode": "fast",
        "model": DEFAULT_CHAT_MODEL_ID,
    }


def _default_off_body_invariants(body: Mapping[str, object]) -> bool:
    metadata = body.get("metadata")
    payloads = [body]
    if isinstance(metadata, Mapping):
        payloads.append(metadata)
    for payload in payloads:
        if payload.get("public_service_enabled") is True:
            return False
        if payload.get("live_reward_enabled") is True:
            return False
        if payload.get("payout_executor_enabled") is True:
            return False
        if payload.get("paid_acu") not in (None, "0", 0):
            return False
    return True


def _audit_redaction_status(wrapper: PublicChatStagingHttpWrapper) -> tuple[int, bool]:
    audit_store = getattr(wrapper, "audit_store", None)
    audit_path = getattr(audit_store, "path", None)
    if not isinstance(audit_path, Path) or not audit_path.exists():
        return 0, False
    text = audit_path.read_text(encoding="utf-8")
    if not text.strip():
        return 0, False
    if (
        "stage4-local-smoke-prompt" in text
        or "stage4-local-smoke-query" in text
        or "?query_audit_sentinel" in text
    ):
        return 0, False
    records = []
    for line in text.splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            return 0, False
        if not _audit_record_shape_is_redacted(record):
            return 0, False
        records.append(record)
    return len(records), bool(records)


def _audit_record_shape_is_redacted(record: Mapping[str, object]) -> bool:
    required = {
        "contract_version",
        "observed_at",
        "method",
        "path",
        "status_code",
        "reason_code",
        "client_host_hash",
        "client_ip_hash",
        "raw_prompt_persisted",
        "raw_api_key_persisted",
        "public_service_enabled",
        "live_reward_enabled",
        "payout_executor_enabled",
        "paid_acu",
    }
    if not required.issubset(record):
        return False
    if record.get("raw_prompt_persisted") is not False:
        return False
    if record.get("raw_api_key_persisted") is not False:
        return False
    if record.get("public_service_enabled") is not False:
        return False
    if record.get("live_reward_enabled") is not False:
        return False
    if record.get("payout_executor_enabled") is not False:
        return False
    path = record.get("path")
    if not isinstance(path, str) or "?" in path:
        return False
    prompt_hash = record.get("prompt_hash")
    return prompt_hash is None or (
        isinstance(prompt_hash, str) and re.fullmatch(r"[0-9a-f]{64}", prompt_hash)
    )


def _append_ref_reasons(reasons: list[str], refs: Sequence[str]) -> None:
    for value in refs:
        if _value_missing_or_placeholder(value):
            reasons.append(REASON_STAGE4_DURABLE_PLACEHOLDER_REF)
        elif _is_secret_looking_value(value):
            reasons.append(REASON_STAGE4_DURABLE_SECRET_REF)


def _secret_like_env_keys(env: Mapping[str, str]) -> tuple[str, ...]:
    return tuple(
        sorted(
            key
            for key, value in env.items()
            if value.strip() and _is_secret_looking_value(value)
        )
    )


def _is_secret_looking_value(value: str) -> bool:
    try:
        ensure_no_raw_secret(value, field_name="value")
    except ValueError:
        return True
    return _EXTRA_SECRET_VALUE_PATTERN.search(value) is not None


def _value_missing_or_placeholder(value: str | None) -> bool:
    if value is None:
        return True
    normalized = value.strip().lower()
    if not normalized or normalized in {"none", "null"}:
        return True
    if normalized.startswith("<") and normalized.endswith(">"):
        return True
    return any(marker in normalized for marker in _PLACEHOLDER_MARKERS)


def _is_public_bind_host(bind_host: str) -> bool:
    try:
        host = ip_address(bind_host)
    except ValueError:
        return True
    if host.is_unspecified:
        return True
    return not (host.is_loopback or host.is_private)


def _bind_scope_label(bind_host: str) -> str:
    try:
        host = ip_address(bind_host)
    except ValueError:
        return "invalid"
    if host.is_unspecified:
        return "public-unspecified"
    if host.is_loopback:
        return "loopback"
    if host.is_private:
        return "private-internal"
    return "public"


def _is_catch_all_api_route(path: str) -> bool:
    normalized = path.strip().lower().rstrip("/")
    if normalized == "/api":
        return True
    return normalized.startswith("/api/") and any(
        marker in normalized for marker in ("*", "{", "<", ":path", "path:")
    )


def _hash_ref(value: str) -> str:
    if not value:
        return ""
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]
    return f"sha256:{digest}"


def _dedupe_reasons(reasons: Sequence[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(reasons))

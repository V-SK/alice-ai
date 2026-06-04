from __future__ import annotations

from dataclasses import dataclass, field
from ipaddress import ip_address
from typing import Literal
from urllib.parse import urlsplit

from alice_acp.api_chat.types import (
    REASON_LIVE_REWARD_FORBIDDEN,
    REASON_PAYOUT_EXECUTOR_FORBIDDEN,
    REASON_PUBLIC_SERVICE_FORBIDDEN,
    validate_public_identifier,
)
from alice_acp.api_chat.validators import ensure_no_raw_secret
from alice_acp.api_chat_gateway.route_policy import StagingRoutePolicyConfig

API_CHAT_SIDECAR_DEPLOYMENT_CONTRACT_VERSION = (
    "a4-api-chat-sidecar-deployment-contract-v1"
)

REASON_SIDECAR_PUBLIC_FLAG_DISABLED = "api_chat_sidecar_public_service_disabled"
REASON_SIDECAR_KILL_SWITCH_ENABLED = "api_chat_sidecar_kill_switch_enabled"
REASON_SIDECAR_NON_LOCAL_BIND_REQUIRES_EDGE_AUTH = (
    "api_chat_sidecar_non_local_bind_requires_cloudflare_auth_evidence"
)
REASON_SIDECAR_RATE_STORE_MISSING = "api_chat_sidecar_rate_store_missing"
REASON_SIDECAR_ADMIN_AUTH_MISSING = "api_chat_sidecar_admin_auth_missing"

SidecarBindScope = Literal["loopback", "private-internal"]


@dataclass(frozen=True, slots=True)
class SidecarEndpointContract:
    path: str
    methods: tuple[str, ...]
    public_read: bool = False
    admin_only: bool = False
    rate_limited: bool = False
    cache_control: str = "no-store"

    def __post_init__(self) -> None:
        if not self.path.startswith("/"):
            raise ValueError("endpoint path must start with /")
        if not self.methods:
            raise ValueError("endpoint methods must not be empty")
        for method in self.methods:
            if method.upper() != method or not method.isalpha():
                raise ValueError("endpoint methods must be uppercase HTTP verbs")
        if self.admin_only and self.public_read:
            raise ValueError("admin endpoint cannot be public_read")
        if self.cache_control != "no-store":
            raise ValueError("sidecar endpoint cache_control must remain no-store")

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "methods": self.methods,
            "public_read": self.public_read,
            "admin_only": self.admin_only,
            "rate_limited": self.rate_limited,
            "cache_control": self.cache_control,
        }


SIDECAR_ENDPOINTS: tuple[SidecarEndpointContract, ...] = (
    SidecarEndpointContract(path="/health", methods=("GET",), public_read=True),
    SidecarEndpointContract(path="/limits", methods=("GET",), public_read=True),
    SidecarEndpointContract(path="/v1/models", methods=("GET",), public_read=True),
    SidecarEndpointContract(
        path="/v1/chat/completions",
        methods=("POST",),
        rate_limited=True,
    ),
    SidecarEndpointContract(path="/v1/rate-limits", methods=("GET",), rate_limited=True),
    SidecarEndpointContract(
        path="/admin/kill-switch",
        methods=("GET", "POST"),
        admin_only=True,
    ),
)


@dataclass(frozen=True, slots=True)
class ApiChatSidecarConfigDTO:
    bind_host: str = "127.0.0.1"
    port: int = 18131
    public_service_enabled: bool = False
    kill_switch_enabled: bool = True
    allowed_origins: tuple[str, ...] = ()
    rate_limit_store_ref: str | None = None
    cloudflare_access_policy_ref: str | None = None
    auth_policy_ref: str | None = None
    local_contract_only: bool = True
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False
    route_policy_config: StagingRoutePolicyConfig = field(
        default_factory=StagingRoutePolicyConfig
    )
    service_name: str = "a4-api-chat-sidecar"

    def __post_init__(self) -> None:
        validate_public_identifier("service_name", self.service_name)
        _bind_scope(self.bind_host)
        if not 0 <= self.port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        if self.port == 8083:
            raise ValueError("sidecar port must not target PS 8083")
        for origin in self.allowed_origins:
            _validate_allowed_origin(origin)
        for field_name in (
            "rate_limit_store_ref",
            "cloudflare_access_policy_ref",
            "auth_policy_ref",
        ):
            value = getattr(self, field_name)
            if value is not None:
                _validate_ref(field_name, value)
        if not self.local_contract_only:
            raise ValueError("api_chat_sidecar_local_contract_only_required")
        if self.route_policy_config.public_service_enabled:
            raise ValueError(REASON_PUBLIC_SERVICE_FORBIDDEN)
        if self.route_policy_config.public_unauthenticated_api_enabled:
            raise ValueError("api_chat_public_unauthenticated_api_forbidden")
        if self.live_reward_enabled:
            raise ValueError(REASON_LIVE_REWARD_FORBIDDEN)
        if self.payout_executor_enabled:
            raise ValueError(REASON_PAYOUT_EXECUTOR_FORBIDDEN)

    @property
    def bind_scope(self) -> SidecarBindScope:
        return _bind_scope(self.bind_host)

    @property
    def has_edge_auth_evidence(self) -> bool:
        return bool(self.cloudflare_access_policy_ref and self.auth_policy_ref)

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": API_CHAT_SIDECAR_DEPLOYMENT_CONTRACT_VERSION,
            "service_name": self.service_name,
            "bind_host": self.bind_host,
            "bind_scope": self.bind_scope,
            "port": self.port,
            "public_service_enabled": self.public_service_enabled,
            "kill_switch_enabled": self.kill_switch_enabled,
            "allowed_origins": self.allowed_origins,
            "rate_limit_store_configured": self.rate_limit_store_ref is not None,
            "cloudflare_access_evidence_configured": (
                self.cloudflare_access_policy_ref is not None
            ),
            "auth_policy_configured": self.auth_policy_ref is not None,
            "local_contract_only": self.local_contract_only,
            "live_reward_enabled": False,
            "payout_executor_enabled": False,
        }


@dataclass(frozen=True, slots=True)
class SidecarDeploymentValidationDTO:
    config: ApiChatSidecarConfigDTO
    reason_codes: tuple[str, ...]
    endpoints: tuple[SidecarEndpointContract, ...] = SIDECAR_ENDPOINTS

    @property
    def fail_closed(self) -> bool:
        return bool(self.reason_codes)

    @property
    def deployable(self) -> bool:
        return not self.fail_closed

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": API_CHAT_SIDECAR_DEPLOYMENT_CONTRACT_VERSION,
            "deployable": self.deployable,
            "fail_closed": self.fail_closed,
            "reason_codes": self.reason_codes,
            "config": self.config.to_dict(),
            "endpoints": tuple(endpoint.to_dict() for endpoint in self.endpoints),
            "no_live_reward_coupling": True,
            "no_payout_executor_coupling": True,
            "long_running_service_started": False,
        }


def sidecar_endpoint_contract() -> tuple[dict[str, object], ...]:
    return tuple(endpoint.to_dict() for endpoint in SIDECAR_ENDPOINTS)


def validate_sidecar_deployment_contract(
    config: ApiChatSidecarConfigDTO | None = None,
) -> SidecarDeploymentValidationDTO:
    candidate = config or ApiChatSidecarConfigDTO()
    reasons: list[str] = []

    if not candidate.public_service_enabled:
        reasons.append(REASON_SIDECAR_PUBLIC_FLAG_DISABLED)
    if candidate.kill_switch_enabled:
        reasons.append(REASON_SIDECAR_KILL_SWITCH_ENABLED)
    if candidate.bind_scope != "loopback" and not candidate.has_edge_auth_evidence:
        reasons.append(REASON_SIDECAR_NON_LOCAL_BIND_REQUIRES_EDGE_AUTH)
    if candidate.rate_limit_store_ref is None:
        reasons.append(REASON_SIDECAR_RATE_STORE_MISSING)
    if candidate.auth_policy_ref is None:
        reasons.append(REASON_SIDECAR_ADMIN_AUTH_MISSING)

    return SidecarDeploymentValidationDTO(config=candidate, reason_codes=tuple(reasons))


def _bind_scope(bind_host: str) -> SidecarBindScope:
    try:
        host = ip_address(bind_host)
    except ValueError as exc:
        raise ValueError("bind_host must be an IP address") from exc
    if host.is_loopback:
        return "loopback"
    if host.is_private:
        return "private-internal"
    raise ValueError("bind_host must be loopback or private internal address")


def _validate_allowed_origin(origin: str) -> None:
    ensure_no_raw_secret(origin, field_name="allowed_origin")
    parsed = urlsplit(origin)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("allowed origin must be http or https")
    if not parsed.netloc or parsed.path not in ("", "/"):
        raise ValueError("allowed origin must contain only scheme and host")
    if "*" in origin:
        raise ValueError("allowed origin wildcard is forbidden")


def _validate_ref(field_name: str, value: str) -> None:
    if not value.strip():
        raise ValueError(f"{field_name} must be non-empty")
    validate_public_identifier(field_name, value)

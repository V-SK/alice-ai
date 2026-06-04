from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from ipaddress import ip_address
from typing import Literal
from urllib.parse import urlsplit

from alice_acp.api_chat.types import (
    REASON_PUBLIC_SERVICE_FORBIDDEN,
    validate_public_identifier,
)

RouteScope = Literal["same-origin", "private", "staging"]

REASON_ROUTE_ALLOWED = "api_chat_staging_route_allowed"
REASON_CORS_ORIGIN_DENIED = "api_chat_cors_origin_denied"
REASON_PUBLIC_UNAUTHENTICATED_API_FORBIDDEN = (
    "api_chat_public_unauthenticated_api_forbidden"
)
REASON_ANONYMOUS_FREE_CHAT_DISABLED = "api_chat_anonymous_free_chat_disabled"

CHAT_ROUTE_PATHS = frozenset({"/chat", "/v1/chat/completions"})
_VALID_ROUTE_SCOPES: tuple[RouteScope, ...] = ("same-origin", "private", "staging")


@dataclass(frozen=True, slots=True)
class StagingRoutePolicyConfig:
    route_scope: RouteScope = "same-origin"
    public_service_enabled: bool = False
    public_unauthenticated_api_enabled: bool = False
    anonymous_free_chat_enabled: bool = True
    anonymous_free_chat_limit_per_hour: int = 10
    cors_allowed_origins: tuple[str, ...] = ()
    cache_control: str = "no-store"
    rollback_route_ref: str = "rollback:remove-staging-route"

    def __post_init__(self) -> None:
        if self.route_scope not in _VALID_ROUTE_SCOPES:
            raise ValueError("route_scope must be same-origin, private, or staging")
        if self.public_service_enabled:
            raise ValueError(REASON_PUBLIC_SERVICE_FORBIDDEN)
        if self.public_unauthenticated_api_enabled:
            raise ValueError(REASON_PUBLIC_UNAUTHENTICATED_API_FORBIDDEN)
        if self.anonymous_free_chat_limit_per_hour <= 0:
            raise ValueError("anonymous_free_chat_limit_per_hour must be positive")
        if self.cache_control != "no-store":
            raise ValueError("cache_control must remain no-store")
        validate_public_identifier("rollback_route_ref", self.rollback_route_ref)
        for origin in self.cors_allowed_origins:
            _validate_origin(origin)


@dataclass(frozen=True, slots=True)
class StagingRoutePolicyDecision:
    admitted: bool
    status_code: int
    reason_code: str
    headers: dict[str, str]
    metadata: dict[str, object]


@dataclass(frozen=True, slots=True)
class StagingRoutePolicy:
    config: StagingRoutePolicyConfig = StagingRoutePolicyConfig()

    def evaluate(
        self,
        *,
        method: str,
        path: str,
        headers: Mapping[str, str],
        key_present: bool,
    ) -> StagingRoutePolicyDecision:
        normalized = {key.lower(): value.strip() for key, value in headers.items()}
        response_headers = self.response_headers(normalized.get("origin"))
        metadata = self.metadata()
        route_path = _route_path(path)

        origin = normalized.get("origin")
        if origin is not None and origin not in self.config.cors_allowed_origins:
            return StagingRoutePolicyDecision(
                admitted=False,
                status_code=403,
                reason_code=REASON_CORS_ORIGIN_DENIED,
                headers=response_headers,
                metadata=metadata,
            )

        if route_path in CHAT_ROUTE_PATHS and not key_present:
            if not self.config.anonymous_free_chat_enabled:
                return StagingRoutePolicyDecision(
                    admitted=False,
                    status_code=401,
                    reason_code=REASON_ANONYMOUS_FREE_CHAT_DISABLED,
                    headers=response_headers,
                    metadata=metadata,
                )
            if self.config.public_unauthenticated_api_enabled:
                return StagingRoutePolicyDecision(
                    admitted=False,
                    status_code=403,
                    reason_code=REASON_PUBLIC_UNAUTHENTICATED_API_FORBIDDEN,
                    headers=response_headers,
                    metadata=metadata,
                )

        return StagingRoutePolicyDecision(
            admitted=True,
            status_code=200,
            reason_code=REASON_ROUTE_ALLOWED,
            headers=response_headers,
            metadata=metadata,
        )

    def response_headers(self, origin: str | None = None) -> dict[str, str]:
        headers = {
            "Cache-Control": self.config.cache_control,
            "Pragma": "no-cache",
            "X-Alice-Staging-Route-Scope": self.config.route_scope,
            "X-Alice-Public-Service-Enabled": "false",
            "X-Alice-Rollback-Route-Ref": self.config.rollback_route_ref,
        }
        if origin is not None and origin in self.config.cors_allowed_origins:
            headers["Access-Control-Allow-Origin"] = origin
            headers["Vary"] = "Origin"
        return headers

    def metadata(self) -> dict[str, object]:
        return {
            "route_scope": self.config.route_scope,
            "same_origin_private_staging_only": True,
            "cors_default_deny": True,
            "cache_control": self.config.cache_control,
            "rollback_route_ref": self.config.rollback_route_ref,
            "public_service_enabled": self.config.public_service_enabled,
            "public_unauthenticated_api_enabled": (
                self.config.public_unauthenticated_api_enabled
            ),
            "anonymous_free_chat_enabled": self.config.anonymous_free_chat_enabled,
            "anonymous_free_chat_limit_per_hour": (
                self.config.anonymous_free_chat_limit_per_hour
            ),
        }


def validate_internal_bind_host(bind_host: str) -> None:
    try:
        host = ip_address(bind_host)
    except ValueError as exc:
        raise ValueError("bind_host must be an IP address") from exc
    if not (host.is_loopback or host.is_private):
        raise ValueError("bind_host must be loopback or private internal address")


def _route_path(path: str) -> str:
    parsed = urlsplit(path)
    return parsed.path or "/"


def _validate_origin(origin: str) -> None:
    parsed = urlsplit(origin)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("CORS origin must be http or https")
    if not parsed.netloc or parsed.path not in ("", "/"):
        raise ValueError("CORS origin must include only scheme and host")

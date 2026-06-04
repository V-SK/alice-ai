from __future__ import annotations

import argparse
import hashlib
import json
import os
import sqlite3
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from alice_acp.api_chat import ApiChatBackendConfig, ApiChatLocalBackend, ApiChatRateLimitPolicy
from alice_acp.api_chat.store import InMemoryApiChatStore
from alice_acp.api_chat.types import (
    REASON_LIVE_REWARD_FORBIDDEN,
    REASON_PAYOUT_EXECUTOR_FORBIDDEN,
    REASON_PUBLIC_SERVICE_FORBIDDEN,
)
from alice_acp.api_chat.validators import validate_aware_timestamp
from alice_acp.api_chat_gateway.adapter import OpenAICompatibleChatGateway
from alice_acp.api_chat_gateway.http_harness import (
    StagingApiChatHttpHarness,
    StagingGatewayHarnessConfig,
    StagingHttpRequest,
)
from alice_acp.api_chat_gateway.key_registry import ApiChatGatewayKeyRegistry
from alice_acp.api_chat_gateway.model_routing import ApiChatModelRouteScheduler
from alice_acp.api_chat_gateway.route_policy import StagingRoutePolicyConfig
from alice_acp.api_chat_gateway.types import GatewayConfig, ModelRegistry

AccessLogSink = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class ApiChatGatewayServiceConfig:
    enabled: bool = False
    bind_host: str = "127.0.0.1"
    port: int = 0
    path_prefix: str = ""
    admission_store_path: Path | None = None
    key_registry_path: Path | None = None
    redact_access_log: bool = True
    kill_switch_unavailable: bool = True
    public_service_enabled: bool = False
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False
    rate_limit_policy: ApiChatRateLimitPolicy = field(default_factory=ApiChatRateLimitPolicy)
    key_registry: ApiChatGatewayKeyRegistry = field(default_factory=ApiChatGatewayKeyRegistry)
    model_router: ApiChatModelRouteScheduler = field(
        default_factory=ApiChatModelRouteScheduler
    )
    route_policy_config: StagingRoutePolicyConfig = field(
        default_factory=StagingRoutePolicyConfig
    )
    service_name: str = "q22c-api-chat-staging-sidecar"

    def __post_init__(self) -> None:
        StagingGatewayHarnessConfig(
            enabled=self.enabled,
            bind_host=self.bind_host,
            contract_path_prefix=self.path_prefix,
            service_name=self.service_name,
            kill_switch_unavailable=self.kill_switch_unavailable,
            route_policy_config=self.route_policy_config,
        )
        if not 0 <= self.port <= 65535:
            raise ValueError("port must be between 0 and 65535")
        if self.public_service_enabled:
            raise ValueError(REASON_PUBLIC_SERVICE_FORBIDDEN)
        if self.live_reward_enabled:
            raise ValueError(REASON_LIVE_REWARD_FORBIDDEN)
        if self.payout_executor_enabled:
            raise ValueError(REASON_PAYOUT_EXECUTOR_FORBIDDEN)
        if self.key_registry_path is not None and self.key_registry.configured:
            raise ValueError("key_registry_path cannot be combined with key_registry")


@dataclass(slots=True)
class ApiChatGatewayService:
    config: ApiChatGatewayServiceConfig = field(default_factory=ApiChatGatewayServiceConfig)
    model_registry: ModelRegistry = field(default_factory=ModelRegistry)
    access_log_sink: AccessLogSink | None = None
    backend: ApiChatLocalBackend = field(init=False)
    harness: StagingApiChatHttpHarness = field(init=False)

    def __post_init__(self) -> None:
        store = (
            SQLiteApiChatAdmissionStore(self.config.admission_store_path)
            if self.config.admission_store_path is not None
            else InMemoryApiChatStore()
        )
        self.backend = ApiChatLocalBackend(
            ApiChatBackendConfig(
                rate_limit_policy=self.config.rate_limit_policy,
                public_service_enabled=False,
                live_reward_enabled=False,
                payout_executor_enabled=False,
                persist_raw_prompt=False,
            ),
            store=store,
        )
        gateway = OpenAICompatibleChatGateway(
            config=GatewayConfig(
                backend_available=not self.config.kill_switch_unavailable,
                local_contract_only=True,
                public_service_enabled=False,
            ),
            backend=self.backend,
            model_registry=self.model_registry,
            model_router=self.config.model_router,
        )
        key_registry = (
            _load_key_registry(self.config.key_registry_path)
            if self.config.key_registry_path is not None
            else self.config.key_registry
        )
        self.harness = StagingApiChatHttpHarness(
            config=StagingGatewayHarnessConfig(
                enabled=self.config.enabled,
                bind_host=self.config.bind_host,
                contract_path_prefix=self.config.path_prefix,
                service_name=self.config.service_name,
                kill_switch_unavailable=self.config.kill_switch_unavailable,
                route_policy_config=self.config.route_policy_config,
            ),
            gateway=gateway,
            key_registry=key_registry,
        )

    def build_http_server(self) -> HTTPServer:
        if not self.config.enabled:
            raise RuntimeError("api_chat_gateway_service_disabled")
        handler_class = build_service_handler_class(
            self.harness,
            config=self.config,
            access_log_sink=self.access_log_sink,
        )
        return HTTPServer((self.config.bind_host, self.config.port), handler_class)

    def close(self) -> None:
        close = getattr(self.backend.store, "close", None)
        if close is not None:
            close()


def api_chat_gateway_service_config_from_argv(
    argv: Sequence[str],
) -> ApiChatGatewayServiceConfig:
    parser = argparse.ArgumentParser(
        prog="alice-api-chat-staging-sidecar",
        description="Build API chat staging sidecar config without starting a service.",
    )
    parser.add_argument("--enabled", action="store_true")
    parser.add_argument("--bind-host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=0)
    parser.add_argument("--path-prefix", default="")
    parser.add_argument("--admission-store-path", type=Path)
    parser.add_argument("--key-registry-path", type=Path)
    parser.add_argument("--kill-switch-available", action="store_true")
    parser.add_argument("--no-redact-access-log", action="store_true")
    namespace = parser.parse_args(list(argv))
    return ApiChatGatewayServiceConfig(
        enabled=namespace.enabled,
        bind_host=namespace.bind_host,
        port=namespace.port,
        path_prefix=namespace.path_prefix,
        admission_store_path=namespace.admission_store_path,
        key_registry_path=namespace.key_registry_path,
        redact_access_log=not namespace.no_redact_access_log,
        kill_switch_unavailable=not namespace.kill_switch_available,
    )


def api_chat_gateway_service_config_from_env(
    env: Mapping[str, str],
) -> ApiChatGatewayServiceConfig:
    return ApiChatGatewayServiceConfig(
        enabled=_env_bool(env, "ALICE_API_CHAT_SIDECAR_ENABLED", default=False),
        bind_host=_env_str(env, "ALICE_API_CHAT_BIND_HOST", default="127.0.0.1"),
        port=_env_int(env, "ALICE_API_CHAT_PORT", default=0),
        path_prefix=_env_str(env, "ALICE_API_CHAT_PATH_PREFIX", default=""),
        admission_store_path=_env_path(env, "ALICE_API_CHAT_ADMISSION_STORE_PATH"),
        key_registry_path=_env_path(env, "ALICE_API_CHAT_KEY_REGISTRY_PATH"),
        redact_access_log=_env_bool(env, "ALICE_API_CHAT_REDACT_ACCESS_LOG", default=True),
        kill_switch_unavailable=_env_bool(
            env,
            "ALICE_API_CHAT_KILL_SWITCH_UNAVAILABLE",
            default=True,
        ),
        public_service_enabled=_env_bool(
            env,
            "ALICE_API_CHAT_PUBLIC_SERVICE_ENABLED",
            default=False,
        ),
        live_reward_enabled=_env_bool(
            env,
            "ALICE_API_CHAT_LIVE_REWARD_ENABLED",
            default=False,
        ),
        payout_executor_enabled=_env_bool(
            env,
            "ALICE_API_CHAT_PAYOUT_EXECUTOR_ENABLED",
            default=False,
        ),
    )


def main(argv: Sequence[str] | None = None) -> int:
    args = list(argv if argv is not None else sys.argv[1:])
    if args == ["--from-env"]:
        config = api_chat_gateway_service_config_from_env(os.environ)
    else:
        config = api_chat_gateway_service_config_from_argv(args)
    service = ApiChatGatewayService(config)
    server = service.build_http_server()
    try:
        server.serve_forever()
    finally:
        server.server_close()
        service.close()
    return 0


def build_service_handler_class(
    harness: StagingApiChatHttpHarness,
    *,
    config: ApiChatGatewayServiceConfig,
    access_log_sink: AccessLogSink | None = None,
) -> type[BaseHTTPRequestHandler]:
    class AliceApiChatSidecarServiceHandler(BaseHTTPRequestHandler):
        server_version = "AliceApiChatStagingSidecar/1.0"

        def do_GET(self) -> None:
            self._handle()

        def do_POST(self) -> None:
            self._handle()

        def log_message(self, format: str, *args: object) -> None:
            return

        def _handle(self) -> None:
            length = int(self.headers.get("Content-Length", "0") or "0")
            request = StagingHttpRequest(
                method=self.command,
                path=self.path,
                headers={key: value for key, value in self.headers.items()},
                body=self.rfile.read(length) if length else b"",
            )
            response = harness.handle(request)
            body = response.to_json_bytes()
            self.send_response(response.status_code)
            for key, value in _json_headers(response.headers, len(body)).items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)
            if access_log_sink is not None:
                access_log_sink(
                    service_access_log_line(
                        method=self.command,
                        path=self.path,
                        status_code=response.status_code,
                        headers={key: value for key, value in self.headers.items()},
                        client_host=self.client_address[0],
                        redact=config.redact_access_log,
                    )
                )

    return AliceApiChatSidecarServiceHandler


class SQLiteApiChatAdmissionStore(InMemoryApiChatStore):
    def __init__(self, path: str | Path) -> None:
        super().__init__()
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._connection = sqlite3.connect(
                str(self.path),
                isolation_level=None,
                check_same_thread=False,
            )
            self._connection.execute("PRAGMA journal_mode=WAL")
            self._initialize()
        except sqlite3.DatabaseError as exc:
            raise RuntimeError("api_chat_admission_store_unavailable") from exc

    def mark_admitted(self, *, subject: Any, observed_at: datetime) -> None:
        validate_aware_timestamp("observed_at", observed_at)
        with self._lock:
            self._delete_expired_locked(subject.limit_key, observed_at)
            try:
                self._connection.execute(
                    "INSERT INTO admissions(limit_key, observed_at) VALUES (?, ?)",
                    (subject.limit_key, observed_at.isoformat()),
                )
            except sqlite3.DatabaseError as exc:
                raise RuntimeError("api_chat_admission_store_unavailable") from exc

    def _retained_admissions(self, limit_key: str, observed_at: datetime) -> list[datetime]:
        with self._lock:
            self._delete_expired_locked(limit_key, observed_at)
            try:
                rows = self._connection.execute(
                    """
                    SELECT observed_at
                    FROM admissions
                    WHERE limit_key = ?
                    ORDER BY observed_at
                    """,
                    (limit_key,),
                ).fetchall()
            except sqlite3.DatabaseError as exc:
                raise RuntimeError("api_chat_admission_store_unavailable") from exc
        return [datetime.fromisoformat(row[0]) for row in rows]

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _initialize(self) -> None:
        self._connection.execute(
            """
            CREATE TABLE IF NOT EXISTS admissions (
                limit_key TEXT NOT NULL,
                observed_at TEXT NOT NULL
            )
            """
        )
        self._connection.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_api_chat_admissions_key_time
            ON admissions(limit_key, observed_at)
            """
        )

    def _delete_expired_locked(self, limit_key: str, observed_at: datetime) -> None:
        cutoff = observed_at - timedelta(hours=1)
        try:
            self._connection.execute(
                """
                DELETE FROM admissions
                WHERE limit_key = ? AND observed_at <= ?
                """,
                (limit_key, cutoff.isoformat()),
            )
        except sqlite3.DatabaseError as exc:
            raise RuntimeError("api_chat_admission_store_unavailable") from exc


def service_access_log_line(
    *,
    method: str,
    path: str,
    status_code: int,
    headers: Mapping[str, str],
    client_host: str,
    redact: bool = True,
) -> str:
    normalized = {key.lower(): value for key, value in headers.items()}
    payload: dict[str, object] = {
        "method": method,
        "path": _safe_path(path),
        "status": status_code,
        "client_host": _redact_value(client_host) if redact else client_host,
        "user_agent": normalized.get("user-agent", ""),
        "authorization": "redacted" if normalized.get("authorization") else None,
        "api_key_hash_present": bool(normalized.get("x-alice-api-key-hash")),
        "api_key_id_present": bool(normalized.get("x-alice-api-key-id")),
    }
    if redact:
        payload["user_agent"] = _redact_value(str(payload["user_agent"]))
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def _safe_path(path: str) -> str:
    parsed = urlsplit(path)
    return parsed.path or "/"


def _redact_value(value: str) -> str:
    if not value:
        return ""
    return "sha256:" + hashlib.sha256(value.encode("utf-8")).hexdigest()[:16]


def _json_headers(headers: Mapping[str, str], content_length: int) -> dict[str, str]:
    response_headers = dict(headers)
    response_headers.setdefault("Content-Type", "application/json")
    response_headers["Content-Length"] = str(content_length)
    return response_headers


def _env_str(env: Mapping[str, str], name: str, *, default: str) -> str:
    value = env.get(name)
    if value is None or not value.strip():
        return default
    return value.strip()


def _env_int(env: Mapping[str, str], name: str, *, default: int) -> int:
    value = env.get(name)
    if value is None or not value.strip():
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc


def _env_bool(env: Mapping[str, str], name: str, *, default: bool) -> bool:
    value = env.get(name)
    if value is None or not value.strip():
        return default
    normalized = value.strip().lower()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise ValueError(f"{name} must be true or false")


def _env_path(env: Mapping[str, str], name: str) -> Path | None:
    value = env.get(name)
    if value is None or not value.strip():
        return None
    return Path(value.strip())


def _load_key_registry(path: str | Path) -> ApiChatGatewayKeyRegistry:
    try:
        raw_payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as exc:
        raise RuntimeError("api_chat_key_registry_unavailable") from exc
    except json.JSONDecodeError as exc:
        raise ValueError("api_chat_key_registry_json_invalid") from exc

    if isinstance(raw_payload, dict):
        raw_records = raw_payload.get("keys")
    else:
        raw_records = raw_payload
    if not isinstance(raw_records, list):
        raise ValueError("api_chat_key_registry_keys_must_be_a_list")

    registry = ApiChatGatewayKeyRegistry.from_mappings(
        tuple(_key_record_payload_from_json(item) for item in raw_records)
    )
    if not registry.configured:
        raise ValueError("api_chat_key_registry_must_not_be_empty")
    return registry


def _key_record_payload_from_json(payload: object) -> dict[str, object]:
    if not isinstance(payload, dict):
        raise ValueError("api_chat_key_registry_record_must_be_an_object")
    converted = dict(payload)
    for field_name in ("created_at", "revoked_at"):
        value = converted.get(field_name)
        if value is None:
            continue
        if not isinstance(value, str):
            raise ValueError(f"{field_name} must be an ISO-8601 timestamp string")
        converted[field_name] = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return converted


if __name__ == "__main__":
    raise SystemExit(main())

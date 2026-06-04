from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

from alice_acp.api_chat.types import DEFAULT_CHAT_MODEL_ID, utc_now, validate_public_identifier
from alice_acp.api_chat.validators import (
    ensure_no_raw_secret,
    validate_aware_timestamp,
    validate_sha256,
)

OPENAI_CHAT_GATEWAY_CONTRACT_VERSION = "q22b-openai-chat-gateway-contract-v1"
OPENAI_CHAT_GATEWAY_SERVICE = "q22b-openai-compatible-api-chat-gateway"
DEFAULT_MODEL_CREATED = 1767225600

GatewayMode = Literal["Auto", "Fast", "Standard", "Roleplay", "Best", "RP Lite", "RP Pro"]
ChatRole = Literal["system", "user", "assistant", "tool"]

VALID_GATEWAY_MODES: tuple[GatewayMode, ...] = (
    "Auto",
    "Fast",
    "Standard",
    "Roleplay",
    "Best",
    "RP Lite",
    "RP Pro",
)
VALID_CHAT_ROLES: tuple[ChatRole, ...] = ("system", "user", "assistant", "tool")
_MODE_BY_LOWER = {mode.lower(): mode for mode in VALID_GATEWAY_MODES}


@dataclass(frozen=True, slots=True)
class GatewayModel:
    id: str
    created: int = DEFAULT_MODEL_CREATED
    owned_by: str = "alice-foundation"

    def __post_init__(self) -> None:
        validate_public_identifier("model_id", self.id)
        validate_public_identifier("owned_by", self.owned_by)
        if self.created < 0:
            raise ValueError("created must be non-negative")

    def to_openai_dict(self) -> dict[str, object]:
        return {
            "id": self.id,
            "object": "model",
            "created": self.created,
            "owned_by": self.owned_by,
        }


DEFAULT_MODEL_REGISTRY: tuple[GatewayModel, ...] = (
    GatewayModel(id=DEFAULT_CHAT_MODEL_ID),
    GatewayModel(id="alice-lite-4b@contract"),
    GatewayModel(id="alice-standard-9b@contract"),
    GatewayModel(id="alice-pro-27b@contract"),
    GatewayModel(id="alice-pro-35b-moe@contract"),
    GatewayModel(id="alice-rp-lite-9b@contract"),
    GatewayModel(id="alice-rp-pro-27b@contract"),
)


@dataclass(frozen=True, slots=True)
class ModelRegistry:
    models: tuple[GatewayModel, ...] = DEFAULT_MODEL_REGISTRY

    def __post_init__(self) -> None:
        if not self.models:
            raise ValueError("model registry must not be empty")
        if len({model.id for model in self.models}) != len(self.models):
            raise ValueError("model registry contains duplicate model ids")

    @classmethod
    def from_iterable(cls, models: Sequence[GatewayModel]) -> ModelRegistry:
        return cls(models=tuple(models))

    def contains(self, model_id: str) -> bool:
        return any(model.id == model_id for model in self.models)

    def to_openai_list(self) -> dict[str, object]:
        return {
            "object": "list",
            "data": [model.to_openai_dict() for model in self.models],
        }


@dataclass(frozen=True, slots=True)
class GatewayMetadata:
    mode: GatewayMode = "Auto"

    @classmethod
    def from_payload(cls, payload: object) -> GatewayMetadata:
        if payload is None:
            return cls()
        if not isinstance(payload, Mapping):
            raise ValueError("metadata must be an object")
        mode = payload.get("mode", "Auto")
        return cls(mode=canonical_gateway_mode(mode))

    def to_dict(self) -> dict[str, object]:
        return {"mode": self.mode}


@dataclass(frozen=True, slots=True)
class ChatMessage:
    role: ChatRole
    content: str
    name: str | None = None

    def __post_init__(self) -> None:
        if self.role not in VALID_CHAT_ROLES:
            raise ValueError("message role is unsupported")
        if not isinstance(self.content, str) or not self.content.strip():
            raise ValueError("message content must be a non-empty string")
        if self.name is not None:
            validate_public_identifier("message.name", self.name)

    @classmethod
    def from_payload(cls, payload: object) -> ChatMessage:
        if not isinstance(payload, Mapping):
            raise ValueError("message must be an object")
        role = payload.get("role")
        content = payload.get("content")
        name = payload.get("name")
        if role not in VALID_CHAT_ROLES:
            raise ValueError("message role is unsupported")
        if not isinstance(content, str):
            raise ValueError("message content must be a string")
        if name is not None and not isinstance(name, str):
            raise ValueError("message name must be a string")
        return cls(role=role, content=content, name=name)  # type: ignore[arg-type]

    def to_openai_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {"role": self.role, "content": self.content}
        if self.name is not None:
            payload["name"] = self.name
        return payload


@dataclass(frozen=True, slots=True)
class ChatCompletionRequest:
    model: str
    messages: tuple[ChatMessage, ...]
    stream: bool = False
    metadata: GatewayMetadata = field(default_factory=GatewayMetadata)
    max_tokens: int | None = None
    user: str | None = None

    def __post_init__(self) -> None:
        validate_public_identifier("model", self.model)
        if not self.messages:
            raise ValueError("messages must not be empty")
        if not isinstance(self.stream, bool):
            raise ValueError("stream must be a boolean")
        if self.max_tokens is not None and self.max_tokens <= 0:
            raise ValueError("max_tokens must be positive")
        if self.user is not None:
            validate_public_identifier("user", self.user)

    @classmethod
    def from_payload(
        cls,
        payload: Mapping[str, object] | ChatCompletionRequest,
    ) -> ChatCompletionRequest:
        if isinstance(payload, ChatCompletionRequest):
            return payload
        if not isinstance(payload, Mapping):
            raise ValueError("request payload must be an object")
        model = payload.get("model")
        if not isinstance(model, str) or not model.strip():
            raise ValueError("model must be a non-empty string")
        raw_messages = payload.get("messages")
        if not isinstance(raw_messages, Sequence) or isinstance(raw_messages, str | bytes):
            raise ValueError("messages must be an array")
        stream = payload.get("stream", False)
        if not isinstance(stream, bool):
            raise ValueError("stream must be a boolean")
        max_tokens = payload.get("max_tokens")
        if max_tokens is not None and (
            not isinstance(max_tokens, int) or isinstance(max_tokens, bool)
        ):
            raise ValueError("max_tokens must be an integer")
        user = payload.get("user")
        if user is not None and not isinstance(user, str):
            raise ValueError("user must be a string")
        return cls(
            model=model,
            messages=tuple(ChatMessage.from_payload(message) for message in raw_messages),
            stream=stream,
            metadata=GatewayMetadata.from_payload(payload.get("metadata")),
            max_tokens=max_tokens,
            user=user,
        )


@dataclass(frozen=True, slots=True)
class GatewayRequestContext:
    client_ip: str = "127.0.0.1"
    user_agent: str = "alice-q22b-openai-gateway-local-contract/1.0"
    observed_at: datetime = field(default_factory=utc_now)
    api_key_id: str | None = None
    api_key_hash: str | None = None
    user_id: str | None = None

    def __post_init__(self) -> None:
        if not self.client_ip:
            raise ValueError("client_ip must be non-empty")
        if not self.user_agent:
            raise ValueError("user_agent must be non-empty")
        ensure_no_raw_secret(self.client_ip, field_name="client_ip")
        ensure_no_raw_secret(self.user_agent, field_name="user_agent")
        validate_aware_timestamp("observed_at", self.observed_at)
        if self.api_key_id is not None:
            validate_public_identifier("api_key_id", self.api_key_id)
        if self.api_key_hash is not None:
            validate_sha256(self.api_key_hash, field_name="api_key_hash")
        if self.user_id is not None:
            validate_public_identifier("user_id", self.user_id)

    @property
    def backend_api_key_id(self) -> str | None:
        if self.api_key_id is not None:
            return self.api_key_id
        if self.api_key_hash is not None:
            return f"api-key-sha256-{self.api_key_hash[:24]}"
        return None


@dataclass(frozen=True, slots=True)
class GatewayResponse:
    status_code: int
    headers: dict[str, str]
    body: dict[str, Any]

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


@dataclass(frozen=True, slots=True)
class GatewayConfig:
    contract_version: str = OPENAI_CHAT_GATEWAY_CONTRACT_VERSION
    require_api_key: bool = False
    backend_available: bool = True
    local_contract_only: bool = True
    public_service_enabled: bool = False

    def __post_init__(self) -> None:
        validate_public_identifier("contract_version", self.contract_version)
        if not self.local_contract_only or self.public_service_enabled:
            raise ValueError("api_chat_gateway_public_service_forbidden")


def canonical_gateway_mode(value: object) -> GatewayMode:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("metadata.mode must be a non-empty string")
    try:
        return _MODE_BY_LOWER[value.strip().lower()]
    except KeyError as exc:
        raise ValueError(
            "metadata.mode must be Auto, Fast, Standard, Roleplay, Best, RP Lite, or RP Pro"
        ) from exc

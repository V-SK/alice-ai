from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from alice_acp.api_chat.model_catalog import (
    ALICE_LITE_4B,
    ALICE_PRO_27B,
    ALICE_PRO_35B_MOE,
    ALICE_STANDARD_9B,
    MODEL_PROFILES,
    RP_LITE_9B,
    RP_PRO_27B,
)
from alice_acp.api_chat.types import (
    ApiChatAbuseGuardDecision,
    ApiChatModelClass,
    ApiChatSchedulerRuntime,
    validate_public_identifier,
)
from alice_acp.api_chat.validators import validate_sha256
from alice_acp.api_chat_gateway.types import GatewayMode

PUBLIC_CHAT_API_MODEL_ROUTE_CONTRACT_VERSION = "q23-q24-chat-api-model-routing-v1"

PublicModelFamily = Literal["general", "roleplay"]

PUBLIC_MODEL_TIERS: tuple[ApiChatModelClass, ...] = (
    ALICE_LITE_4B,
    ALICE_STANDARD_9B,
    ALICE_PRO_27B,
    ALICE_PRO_35B_MOE,
    RP_LITE_9B,
    RP_PRO_27B,
)

PUBLIC_ROUTE_TARGETS: dict[GatewayMode, tuple[ApiChatModelClass, ...]] = {
    "Auto": (ALICE_STANDARD_9B, ALICE_LITE_4B),
    "Fast": (ALICE_LITE_4B,),
    "Standard": (ALICE_STANDARD_9B, ALICE_LITE_4B),
    "Roleplay": (RP_LITE_9B,),
    "Best": (ALICE_PRO_35B_MOE, ALICE_PRO_27B, ALICE_STANDARD_9B, ALICE_LITE_4B),
    "RP Lite": (RP_LITE_9B,),
    "RP Pro": (RP_PRO_27B, RP_LITE_9B),
}


@dataclass(frozen=True, slots=True)
class PublicModelTierDTO:
    model_class: ApiChatModelClass
    display_name: str
    family: PublicModelFamily
    parameter_billions: int
    minimum_memory_gb: int
    mac_runtime_order: tuple[ApiChatSchedulerRuntime, ...] = ("mlx", "gguf")
    request_time_model_download_allowed: bool = False

    def __post_init__(self) -> None:
        validate_public_identifier("model_class", self.model_class)
        validate_public_identifier("display_name", self.display_name.replace(" ", "_"))
        if self.family not in ("general", "roleplay"):
            raise ValueError("model family is unsupported")
        if self.parameter_billions <= 0:
            raise ValueError("parameter_billions must be positive")
        if self.minimum_memory_gb < 16:
            raise ValueError("minimum_memory_gb must be at least 16")
        for runtime in self.mac_runtime_order:
            if runtime not in ("mlx", "gguf"):
                raise ValueError("mac_runtime_order must contain mlx/gguf only")
        if self.request_time_model_download_allowed:
            raise ValueError("public_contract_request_time_download_forbidden")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "model_class": self.model_class,
            "display_name": self.display_name,
            "family": self.family,
            "parameter_billions": self.parameter_billions,
            "minimum_memory_gb": self.minimum_memory_gb,
            "mac_runtime_order": self.mac_runtime_order,
            "request_time_model_download_allowed": False,
        }


@dataclass(frozen=True, slots=True)
class PublicRouteModeDTO:
    route_mode: GatewayMode
    target_model_classes: tuple[ApiChatModelClass, ...]
    general_downgrade_allowed: bool
    roleplay_fail_closed: bool

    def __post_init__(self) -> None:
        validate_public_identifier("route_mode", self.route_mode.replace(" ", "_"))
        if not self.target_model_classes:
            raise ValueError("target_model_classes must not be empty")
        for model_class in self.target_model_classes:
            validate_public_identifier("target_model_class", model_class)
        if self.roleplay_fail_closed and self.general_downgrade_allowed:
            raise ValueError("roleplay route cannot allow general downgrade")

    def to_public_dict(self) -> dict[str, object]:
        return {
            "route_mode": self.route_mode,
            "target_model_classes": self.target_model_classes,
            "general_downgrade_allowed": self.general_downgrade_allowed,
            "roleplay_fail_closed": self.roleplay_fail_closed,
            "request_time_model_download_allowed": False,
        }


@dataclass(frozen=True, slots=True)
class PublicAbuseGuardDTO:
    admitted: bool
    reason_code: str
    prompt_hash: str
    denied_reasons: tuple[str, ...] = ()
    raw_prompt_persisted: bool = False
    fail_closed: bool = False

    def __post_init__(self) -> None:
        validate_public_identifier("reason_code", self.reason_code)
        validate_sha256(self.prompt_hash, field_name="prompt_hash")
        for reason in self.denied_reasons:
            validate_public_identifier("denied_reason", reason)
        if self.raw_prompt_persisted:
            raise ValueError("api_chat_public_abuse_raw_prompt_persistence_forbidden")
        if self.admitted and self.denied_reasons:
            raise ValueError("admitted abuse guard cannot include denied reasons")

    @classmethod
    def from_decision(
        cls,
        decision: ApiChatAbuseGuardDecision,
        *,
        fail_closed: bool,
    ) -> PublicAbuseGuardDTO:
        return cls(
            admitted=decision.admitted,
            reason_code=decision.reason_code,
            prompt_hash=decision.prompt_hash,
            denied_reasons=() if decision.admitted else (decision.reason_code,),
            raw_prompt_persisted=False,
            fail_closed=fail_closed,
        )

    def to_public_dict(self) -> dict[str, object]:
        return {
            "admitted": self.admitted,
            "reason_code": self.reason_code,
            "prompt_hash": self.prompt_hash,
            "denied_reasons": self.denied_reasons,
            "raw_prompt_persisted": False,
            "fail_closed": self.fail_closed,
        }


def public_model_tier_contract() -> tuple[PublicModelTierDTO, ...]:
    return tuple(_tier_for_model_class(model_class) for model_class in PUBLIC_MODEL_TIERS)


def public_route_policy_contract() -> dict[str, object]:
    return {
        "contract_version": PUBLIC_CHAT_API_MODEL_ROUTE_CONTRACT_VERSION,
        "route_modes": tuple(
            _route_mode_for_contract(route_mode, target_models).to_public_dict()
            for route_mode, target_models in PUBLIC_ROUTE_TARGETS.items()
        ),
        "model_tiers": tuple(tier.to_public_dict() for tier in public_model_tier_contract()),
        "online_miner_capabilities_required": True,
        "minimum_inference_memory_gb": 16,
        "cached_or_loaded_model_required": True,
        "loaded_model_preferred": True,
        "mac_runtime_order": ("mlx", "gguf"),
        "request_time_model_download_allowed": False,
        "live_reward_enabled": False,
        "payout_executor_enabled": False,
    }


def _tier_for_model_class(model_class: ApiChatModelClass) -> PublicModelTierDTO:
    profile = MODEL_PROFILES[model_class]
    return PublicModelTierDTO(
        model_class=profile.model_class,
        display_name=profile.display_name,
        family=profile.family,
        parameter_billions=profile.parameter_billions,
        minimum_memory_gb=profile.minimum_memory_gb,
    )


def _route_mode_for_contract(
    route_mode: GatewayMode,
    target_models: tuple[ApiChatModelClass, ...],
) -> PublicRouteModeDTO:
    roleplay = all(
        MODEL_PROFILES[model_class].family == "roleplay" for model_class in target_models
    )
    return PublicRouteModeDTO(
        route_mode=route_mode,
        target_model_classes=target_models,
        general_downgrade_allowed=not roleplay and len(target_models) > 1,
        roleplay_fail_closed=roleplay,
    )

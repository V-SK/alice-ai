from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Literal

from alice_acp.api_chat.validators import ensure_no_raw_secret, validate_aware_timestamp

API_CHAT_CONTRACT_VERSION = "q22-api-chat-local-contract-v1"
DEFAULT_CHAT_MODEL_ID = "alice-foundation-chat-local@contract"
FOUNDATION_REVENUE_SOURCE = "foundation_api_chat_mock"

STATUS_ADMITTED = "admitted"
STATUS_REJECTED = "rejected"
STATUS_QUEUED = "queued"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"

REASON_CHAT_ADMITTED = "api_chat_admitted"
REASON_CHAT_REJECTED = "api_chat_rejected"
REASON_CHAT_QUEUED = "api_chat_queued"
REASON_CHAT_COMPLETED = "api_chat_completed"
REASON_CHAT_FAILED = "api_chat_failed"
REASON_FREE_RATE_LIMIT = "anonymous_free_hourly_limit_exceeded"
REASON_FREE_BURST_LIMIT = "anonymous_free_burst_limit_exceeded"
REASON_API_KEY_RATE_LIMIT = "api_key_hourly_limit_exceeded"
REASON_API_KEY_BURST_LIMIT = "api_key_burst_limit_exceeded"
REASON_USER_RATE_LIMIT = "user_hourly_limit_exceeded"
REASON_USER_BURST_LIMIT = "user_burst_limit_exceeded"
REASON_LIVE_REWARD_FORBIDDEN = "api_chat_live_reward_forbidden"
REASON_PAYOUT_EXECUTOR_FORBIDDEN = "api_chat_payout_executor_forbidden"
REASON_RAW_PROMPT_PERSISTENCE_FORBIDDEN = "api_chat_raw_prompt_persistence_forbidden"
REASON_PUBLIC_SERVICE_FORBIDDEN = "api_chat_public_service_forbidden"

ZERO_DECIMAL = Decimal("0")
PUBLIC_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@/-]{0,191}$")

LifecycleStatus = Literal["admitted", "rejected", "queued", "completed", "failed"]
IdentityKind = Literal["anonymous", "api_key", "user_id"]
ApiChatRequestKind = Literal["chat", "api"]
ApiChatGatewayMode = Literal[
    "auto",
    "fast",
    "standard",
    "roleplay",
    "best",
    "rp_lite",
    "rp_pro",
]
ApiChatModelClass = Literal[
    "auto",
    "fast",
    "roleplay",
    "best",
    "moe_35b_long_context",
    "alice_lite_4b",
    "alice_standard_9b",
    "alice_pro_27b",
    "alice_pro_35b_moe",
    "rp_lite_9b",
    "rp_pro_27b",
]
ApiChatGatewayDecisionStatus = Literal["admitted", "queued", "rejected"]
ApiChatSchedulerRuntime = Literal["mlx", "gguf", "cuda", "cpu"]

VALID_API_CHAT_MODEL_CLASSES: tuple[ApiChatModelClass, ...] = (
    "auto",
    "fast",
    "roleplay",
    "best",
    "moe_35b_long_context",
    "alice_lite_4b",
    "alice_standard_9b",
    "alice_pro_27b",
    "alice_pro_35b_moe",
    "rp_lite_9b",
    "rp_pro_27b",
)


def utc_now() -> datetime:
    return datetime.now(UTC)


def validate_public_identifier(field_name: str, value: str) -> None:
    if not value:
        raise ValueError(f"{field_name} must be non-empty")
    if not PUBLIC_ID_PATTERN.fullmatch(value):
        raise ValueError(f"{field_name} is malformed")
    ensure_no_raw_secret(value, field_name=field_name)


@dataclass(frozen=True, slots=True)
class ApiChatUsage:
    input_tokens: int = 0
    output_tokens: int = 0

    def __post_init__(self) -> None:
        if self.input_tokens < 0:
            raise ValueError("input_tokens must be non-negative")
        if self.output_tokens < 0:
            raise ValueError("output_tokens must be non-negative")

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class ApiChatRateLimitPolicy:
    free_requests_per_hour: int = 10
    free_burst_requests: int = 4
    free_burst_window: timedelta = timedelta(minutes=1)
    api_key_requests_per_hour: int = 120
    api_key_burst_requests: int = 20
    api_key_burst_window: timedelta = timedelta(minutes=1)
    user_requests_per_hour: int = 60
    user_burst_requests: int = 10
    user_burst_window: timedelta = timedelta(minutes=1)

    def __post_init__(self) -> None:
        for field_name, value in (
            ("free_requests_per_hour", self.free_requests_per_hour),
            ("free_burst_requests", self.free_burst_requests),
            ("api_key_requests_per_hour", self.api_key_requests_per_hour),
            ("api_key_burst_requests", self.api_key_burst_requests),
            ("user_requests_per_hour", self.user_requests_per_hour),
            ("user_burst_requests", self.user_burst_requests),
        ):
            if value <= 0:
                raise ValueError(f"{field_name} must be positive")
        for field_name, value in (
            ("free_burst_window", self.free_burst_window),
            ("api_key_burst_window", self.api_key_burst_window),
            ("user_burst_window", self.user_burst_window),
        ):
            if value.total_seconds() <= 0:
                raise ValueError(f"{field_name} must be positive")


@dataclass(frozen=True, slots=True)
class ApiChatGatewayApiKey:
    key_id: str
    requests_per_hour: int
    enabled: bool = True

    def __post_init__(self) -> None:
        validate_public_identifier("key_id", self.key_id)
        if self.requests_per_hour <= 0:
            raise ValueError("requests_per_hour must be positive")


@dataclass(frozen=True, slots=True)
class ApiChatGatewayRatePolicy:
    free_chat_requests_per_hour: int = 10
    api_key_default_requests_per_hour: int = 120
    ip_requests_per_hour: int = 600
    user_requests_per_hour: int = 120

    def __post_init__(self) -> None:
        for field_name, value in (
            ("free_chat_requests_per_hour", self.free_chat_requests_per_hour),
            ("api_key_default_requests_per_hour", self.api_key_default_requests_per_hour),
            ("ip_requests_per_hour", self.ip_requests_per_hour),
            ("user_requests_per_hour", self.user_requests_per_hour),
        ):
            if value <= 0:
                raise ValueError(f"{field_name} must be positive")


@dataclass(frozen=True, slots=True)
class ApiChatGatewayConfig:
    rate_policy: ApiChatGatewayRatePolicy = field(default_factory=ApiChatGatewayRatePolicy)
    api_keys: tuple[ApiChatGatewayApiKey, ...] = ()
    local_contract_only: bool = True
    public_service_enabled: bool = False
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False
    max_prompt_chars: int = 8000
    repeated_prompt_limit_per_hour: int = 3
    blocked_phrases: tuple[str, ...] = ("blocked-test-fixture",)

    def __post_init__(self) -> None:
        if not self.local_contract_only or self.public_service_enabled:
            raise ValueError(REASON_PUBLIC_SERVICE_FORBIDDEN)
        if self.live_reward_enabled:
            raise ValueError(REASON_LIVE_REWARD_FORBIDDEN)
        if self.payout_executor_enabled:
            raise ValueError(REASON_PAYOUT_EXECUTOR_FORBIDDEN)
        if self.max_prompt_chars <= 0:
            raise ValueError("max_prompt_chars must be positive")
        if self.repeated_prompt_limit_per_hour <= 0:
            raise ValueError("repeated_prompt_limit_per_hour must be positive")
        if len({api_key.key_id for api_key in self.api_keys}) != len(self.api_keys):
            raise ValueError("api_keys contains duplicate key_id")
        for phrase in self.blocked_phrases:
            if not phrase.strip():
                raise ValueError("blocked phrase must be non-empty")
            ensure_no_raw_secret(phrase, field_name="blocked_phrase")

    def api_key_policy(self, key_id: str) -> ApiChatGatewayApiKey | None:
        for api_key in self.api_keys:
            if api_key.key_id == key_id:
                return api_key
        return None


@dataclass(frozen=True, slots=True)
class ApiChatGatewayRequest:
    request_kind: ApiChatRequestKind
    prompt: str
    client_ip: str
    mode: ApiChatGatewayMode = "auto"
    requested_model_class: ApiChatModelClass = "auto"
    requested_at: datetime = field(default_factory=utc_now)
    user_id: str | None = None
    api_key_id: str | None = None

    def __post_init__(self) -> None:
        if self.request_kind not in ("chat", "api"):
            raise ValueError("request_kind must be chat or api")
        if self.mode not in (
            "auto",
            "fast",
            "standard",
            "roleplay",
            "best",
            "rp_lite",
            "rp_pro",
        ):
            raise ValueError(
                "mode must be auto, fast, standard, roleplay, best, rp_lite, or rp_pro"
            )
        if self.requested_model_class not in VALID_API_CHAT_MODEL_CLASSES:
            raise ValueError("requested_model_class is unsupported")
        if not isinstance(self.prompt, str):
            raise ValueError("prompt must be a string")
        if not self.client_ip:
            raise ValueError("client_ip must be non-empty")
        ensure_no_raw_secret(self.client_ip, field_name="client_ip")
        validate_aware_timestamp("requested_at", self.requested_at)
        if self.user_id is not None:
            validate_public_identifier("user_id", self.user_id)
        if self.api_key_id is not None:
            validate_public_identifier("api_key_id", self.api_key_id)


@dataclass(frozen=True, slots=True)
class ApiChatRateLimitBucket:
    name: str
    key_hash: str
    requests_per_hour: int
    reason_code: str

    def __post_init__(self) -> None:
        validate_public_identifier("bucket_name", self.name)
        _validate_hash_id("key_hash", self.key_hash)
        validate_public_identifier("reason_code", self.reason_code)
        if self.requests_per_hour <= 0:
            raise ValueError("requests_per_hour must be positive")

    @property
    def limit_key(self) -> str:
        return f"{self.name}:{self.key_hash}"


@dataclass(frozen=True, slots=True)
class ApiChatRateLimitDecision:
    admitted: bool
    reason_code: str
    limit_name: str | None = None
    retry_after_seconds: int | None = None
    remaining_requests: int = 0

    def __post_init__(self) -> None:
        validate_public_identifier("reason_code", self.reason_code)
        if self.limit_name is not None:
            validate_public_identifier("limit_name", self.limit_name)
        if self.retry_after_seconds is not None and self.retry_after_seconds < 0:
            raise ValueError("retry_after_seconds must be non-negative")
        if self.remaining_requests < 0:
            raise ValueError("remaining_requests must be non-negative")


@dataclass(frozen=True, slots=True)
class ApiChatAbuseGuardDecision:
    admitted: bool
    reason_code: str
    prompt_hash: str

    def __post_init__(self) -> None:
        validate_public_identifier("reason_code", self.reason_code)
        _validate_hash_id("prompt_hash", self.prompt_hash)


@dataclass(frozen=True, slots=True)
class ApiChatInferenceDevice:
    device_id: str
    miner_id: str
    platform: Literal["mac", "cuda", "cpu"]
    online: bool
    memory_gb: int
    supports_mlx: bool = False
    supports_gguf: bool = False
    supported_model_classes: tuple[ApiChatModelClass, ...] = ("fast",)
    busy: bool = False

    def __post_init__(self) -> None:
        validate_public_identifier("device_id", self.device_id)
        validate_public_identifier("miner_id", self.miner_id)
        if self.platform not in ("mac", "cuda", "cpu"):
            raise ValueError("platform is unsupported")
        if self.memory_gb <= 0:
            raise ValueError("memory_gb must be positive")
        for model_class in self.supported_model_classes:
            if model_class not in VALID_API_CHAT_MODEL_CLASSES:
                raise ValueError("supported model class is unsupported")

    @property
    def inference_eligible(self) -> bool:
        return self.online and self.memory_gb >= 16

    @property
    def prefetch_eligible(self) -> bool:
        return self.inference_eligible


@dataclass(frozen=True, slots=True)
class ApiChatSchedulerRequest:
    request_id: str
    mode: ApiChatGatewayMode
    requested_model_class: ApiChatModelClass
    prompt_hash: str

    def __post_init__(self) -> None:
        validate_public_identifier("request_id", self.request_id)
        if self.mode not in (
            "auto",
            "fast",
            "standard",
            "roleplay",
            "best",
            "rp_lite",
            "rp_pro",
        ):
            raise ValueError("mode is unsupported")
        if self.requested_model_class not in VALID_API_CHAT_MODEL_CLASSES:
            raise ValueError("requested_model_class is unsupported")
        _validate_hash_id("prompt_hash", self.prompt_hash)


@dataclass(frozen=True, slots=True)
class ApiChatSchedulerDecision:
    status: ApiChatGatewayDecisionStatus
    reason_code: str
    request_id: str | None = None
    selected_device_id: str | None = None
    selected_miner_id: str | None = None
    model_lane: ApiChatModelClass | None = None
    runtime: ApiChatSchedulerRuntime | None = None
    fallback_from: ApiChatModelClass | None = None
    downgrade_applied: bool = False
    demand_driven: bool = False
    should_throttle_mining: bool = False
    background_inference_started: bool = False

    def __post_init__(self) -> None:
        validate_public_identifier("reason_code", self.reason_code)
        if self.request_id is not None:
            validate_public_identifier("request_id", self.request_id)
        if self.selected_device_id is not None:
            validate_public_identifier("selected_device_id", self.selected_device_id)
        if self.selected_miner_id is not None:
            validate_public_identifier("selected_miner_id", self.selected_miner_id)
        if self.model_lane is not None and self.model_lane not in VALID_API_CHAT_MODEL_CLASSES:
            raise ValueError("model_lane is unsupported")
        if self.runtime is not None and self.runtime not in ("mlx", "gguf", "cuda", "cpu"):
            raise ValueError("runtime is unsupported")
        if (
            self.fallback_from is not None
            and self.fallback_from not in VALID_API_CHAT_MODEL_CLASSES
        ):
            raise ValueError("fallback_from is unsupported")


@dataclass(frozen=True, slots=True)
class ApiChatGatewayDecision:
    status: ApiChatGatewayDecisionStatus
    reason_code: str
    request_hash: str
    prompt_hash: str
    rate_limit: ApiChatRateLimitDecision | None = None
    abuse: ApiChatAbuseGuardDecision | None = None
    scheduler: ApiChatSchedulerDecision | None = None
    retry_after_seconds: int | None = None

    def __post_init__(self) -> None:
        validate_public_identifier("reason_code", self.reason_code)
        _validate_hash_id("request_hash", self.request_hash)
        _validate_hash_id("prompt_hash", self.prompt_hash)
        if self.retry_after_seconds is not None and self.retry_after_seconds < 0:
            raise ValueError("retry_after_seconds must be non-negative")


@dataclass(frozen=True, slots=True)
class ApiChatBackendConfig:
    contract_version: str = API_CHAT_CONTRACT_VERSION
    default_model_id: str = DEFAULT_CHAT_MODEL_ID
    rate_limit_policy: ApiChatRateLimitPolicy = field(default_factory=ApiChatRateLimitPolicy)
    local_contract_only: bool = True
    public_service_enabled: bool = False
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False
    persist_raw_prompt: bool = False
    revenue_source: str = FOUNDATION_REVENUE_SOURCE

    def __post_init__(self) -> None:
        validate_public_identifier("contract_version", self.contract_version)
        validate_public_identifier("default_model_id", self.default_model_id)
        validate_public_identifier("revenue_source", self.revenue_source)
        if not self.local_contract_only or self.public_service_enabled:
            raise ValueError(REASON_PUBLIC_SERVICE_FORBIDDEN)
        if self.live_reward_enabled:
            raise ValueError(REASON_LIVE_REWARD_FORBIDDEN)
        if self.payout_executor_enabled:
            raise ValueError(REASON_PAYOUT_EXECUTOR_FORBIDDEN)
        if self.persist_raw_prompt:
            raise ValueError(REASON_RAW_PROMPT_PERSISTENCE_FORBIDDEN)


@dataclass(frozen=True, slots=True)
class ApiChatRequest:
    prompt: str
    client_ip: str
    user_agent: str
    requested_at: datetime = field(default_factory=utc_now)
    model_id: str = DEFAULT_CHAT_MODEL_ID
    user_id: str | None = None
    api_key_id: str | None = None
    input_tokens: int = 0
    max_output_tokens: int = 256
    simulated_foundation_revenue: Decimal = ZERO_DECIMAL

    def __post_init__(self) -> None:
        if not self.prompt:
            raise ValueError("prompt must be non-empty")
        if not self.client_ip:
            raise ValueError("client_ip must be non-empty")
        if not self.user_agent:
            raise ValueError("user_agent must be non-empty")
        validate_aware_timestamp("requested_at", self.requested_at)
        validate_public_identifier("model_id", self.model_id)
        if self.user_id is not None:
            validate_public_identifier("user_id", self.user_id)
        if self.api_key_id is not None:
            validate_public_identifier("api_key_id", self.api_key_id)
        if self.input_tokens < 0:
            raise ValueError("input_tokens must be non-negative")
        if self.max_output_tokens <= 0:
            raise ValueError("max_output_tokens must be positive")
        if self.simulated_foundation_revenue < ZERO_DECIMAL:
            raise ValueError("simulated_foundation_revenue must be non-negative")


@dataclass(frozen=True, slots=True)
class ApiChatAbuseSubject:
    identity_kind: IdentityKind
    identity_hash: str
    ip_hash: str
    user_agent_hash: str
    user_id_hash: str | None = None
    api_key_id: str | None = None
    api_key_hash: str | None = None

    def __post_init__(self) -> None:
        for field_name, value in (
            ("identity_hash", self.identity_hash),
            ("ip_hash", self.ip_hash),
            ("user_agent_hash", self.user_agent_hash),
        ):
            _validate_hash_id(field_name, value)
        if self.user_id_hash is not None:
            _validate_hash_id("user_id_hash", self.user_id_hash)
        if self.api_key_hash is not None:
            _validate_hash_id("api_key_hash", self.api_key_hash)
        if self.api_key_id is not None:
            validate_public_identifier("api_key_id", self.api_key_id)

    @property
    def limit_key(self) -> str:
        return f"{self.identity_kind}:{self.identity_hash}"


@dataclass(frozen=True, slots=True)
class ApiChatAbuseDecision:
    admitted: bool
    subject: ApiChatAbuseSubject
    reason_code: str
    denied_reasons: tuple[str, ...] = ()
    retry_after_seconds: int | None = None
    remaining_hourly: int = 0
    remaining_burst: int = 0

    def __post_init__(self) -> None:
        validate_public_identifier("reason_code", self.reason_code)
        for reason in self.denied_reasons:
            validate_public_identifier("denied_reason", reason)
        if self.retry_after_seconds is not None and self.retry_after_seconds < 0:
            raise ValueError("retry_after_seconds must be non-negative")
        if self.remaining_hourly < 0:
            raise ValueError("remaining_hourly must be non-negative")
        if self.remaining_burst < 0:
            raise ValueError("remaining_burst must be non-negative")


@dataclass(frozen=True, slots=True)
class ApiChatRecord:
    request_id: str
    request_hash: str
    prompt_hash: str
    status: LifecycleStatus
    reason_code: str
    subject: ApiChatAbuseSubject
    model_id: str
    usage: ApiChatUsage
    latency_ms: int
    requested_at: datetime
    recorded_at: datetime
    completed_at: datetime | None = None
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False
    paid_acu: Decimal = ZERO_DECIMAL

    def __post_init__(self) -> None:
        validate_public_identifier("request_id", self.request_id)
        validate_public_identifier("reason_code", self.reason_code)
        validate_public_identifier("model_id", self.model_id)
        _validate_hash_id("request_hash", self.request_hash)
        _validate_hash_id("prompt_hash", self.prompt_hash)
        validate_aware_timestamp("requested_at", self.requested_at)
        validate_aware_timestamp("recorded_at", self.recorded_at)
        if self.completed_at is not None:
            validate_aware_timestamp("completed_at", self.completed_at)
        if self.latency_ms < 0:
            raise ValueError("latency_ms must be non-negative")
        if self.live_reward_enabled:
            raise ValueError(REASON_LIVE_REWARD_FORBIDDEN)
        if self.payout_executor_enabled:
            raise ValueError(REASON_PAYOUT_EXECUTOR_FORBIDDEN)
        if self.paid_acu != ZERO_DECIMAL:
            raise ValueError("api_chat_paid_acu_must_remain_zero")


@dataclass(frozen=True, slots=True)
class ApiChatLifecycleEvent:
    request_id: str
    status: LifecycleStatus
    reason_code: str
    recorded_at: datetime

    def __post_init__(self) -> None:
        validate_public_identifier("request_id", self.request_id)
        validate_public_identifier("reason_code", self.reason_code)
        validate_aware_timestamp("recorded_at", self.recorded_at)


@dataclass(frozen=True, slots=True)
class FoundationRevenueMockRecord:
    revenue_id: str
    request_id: str
    amount: Decimal
    source: str
    recorded_at: datetime
    paid_acu: Decimal = ZERO_DECIMAL
    live_reward_enabled: bool = False
    payout_executor_enabled: bool = False

    def __post_init__(self) -> None:
        validate_public_identifier("revenue_id", self.revenue_id)
        validate_public_identifier("request_id", self.request_id)
        validate_public_identifier("source", self.source)
        validate_aware_timestamp("recorded_at", self.recorded_at)
        if self.amount < ZERO_DECIMAL:
            raise ValueError("amount must be non-negative")
        if self.paid_acu != ZERO_DECIMAL:
            raise ValueError("foundation_revenue_mock_must_not_pay_acu")
        if self.live_reward_enabled:
            raise ValueError(REASON_LIVE_REWARD_FORBIDDEN)
        if self.payout_executor_enabled:
            raise ValueError(REASON_PAYOUT_EXECUTOR_FORBIDDEN)


@dataclass(frozen=True, slots=True)
class ApiChatResult:
    status: LifecycleStatus
    reason_code: str
    request_id: str
    record: ApiChatRecord
    abuse_decision: ApiChatAbuseDecision | None = None
    retry_after_seconds: int | None = None
    foundation_revenue: FoundationRevenueMockRecord | None = None

    @property
    def accepted(self) -> bool:
        return self.status != STATUS_REJECTED


def _validate_hash_id(field_name: str, value: str) -> None:
    if not re.fullmatch(r"[0-9a-f]{64}", value):
        raise ValueError(f"{field_name} must be a lowercase sha256 hex digest")

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta

from alice_acp.api_chat.contracts import prompt_hash
from alice_acp.api_chat.rate_limit import hashed_bucket_value
from alice_acp.api_chat.types import (
    ApiChatAbuseGuardDecision,
    ApiChatGatewayConfig,
    ApiChatGatewayRequest,
)
from alice_acp.api_chat.validators import validate_aware_timestamp

REASON_ABUSE_ADMITTED = "api_chat_abuse_admitted"
REASON_EMPTY_PROMPT = "api_chat_empty_prompt"
REASON_PROMPT_TOO_LONG = "api_chat_prompt_too_long"
REASON_REPEATED_PROMPT = "api_chat_repeated_prompt"
REASON_BLOCKED_PHRASE = "api_chat_blocked_phrase"


@dataclass(slots=True)
class InMemoryApiChatAbuseGuard:
    _prompt_hits_by_subject: dict[str, list[tuple[datetime, str]]] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock, init=False, repr=False)

    def assess(
        self,
        request: ApiChatGatewayRequest,
        *,
        config: ApiChatGatewayConfig,
    ) -> ApiChatAbuseGuardDecision:
        validate_aware_timestamp("requested_at", request.requested_at)
        digest = prompt_hash(request.prompt)
        normalized = _normalized_prompt(request.prompt)

        if not normalized:
            return ApiChatAbuseGuardDecision(
                admitted=False,
                reason_code=REASON_EMPTY_PROMPT,
                prompt_hash=digest,
            )
        if len(request.prompt) > config.max_prompt_chars:
            return ApiChatAbuseGuardDecision(
                admitted=False,
                reason_code=REASON_PROMPT_TOO_LONG,
                prompt_hash=digest,
            )
        if any(phrase.lower() in normalized for phrase in config.blocked_phrases):
            return ApiChatAbuseGuardDecision(
                admitted=False,
                reason_code=REASON_BLOCKED_PHRASE,
                prompt_hash=digest,
            )

        subject_key = abuse_subject_key(request)
        with self._lock:
            retained = self._retained(subject_key, request.requested_at)
            repeated = [hit for hit in retained if hit[1] == digest]
            if len(repeated) >= config.repeated_prompt_limit_per_hour:
                return ApiChatAbuseGuardDecision(
                    admitted=False,
                    reason_code=REASON_REPEATED_PROMPT,
                    prompt_hash=digest,
                )
            retained.append((request.requested_at, digest))
            self._prompt_hits_by_subject[subject_key] = retained

        return ApiChatAbuseGuardDecision(
            admitted=True,
            reason_code=REASON_ABUSE_ADMITTED,
            prompt_hash=digest,
        )

    def _retained(self, subject_key: str, observed_at: datetime) -> list[tuple[datetime, str]]:
        cutoff = observed_at - timedelta(hours=1)
        retained = [
            (timestamp, digest)
            for timestamp, digest in self._prompt_hits_by_subject.get(subject_key, [])
            if timestamp > cutoff
        ]
        self._prompt_hits_by_subject[subject_key] = retained
        return retained


def abuse_subject_key(request: ApiChatGatewayRequest) -> str:
    if request.api_key_id is not None:
        return f"api_key:{hashed_bucket_value('api_key_id', request.api_key_id)}"
    if request.user_id is not None:
        return f"user:{hashed_bucket_value('user_id', request.user_id)}"
    return f"ip:{hashed_bucket_value('client_ip', request.client_ip)}"


def _normalized_prompt(prompt: str) -> str:
    return " ".join(prompt.strip().lower().split())

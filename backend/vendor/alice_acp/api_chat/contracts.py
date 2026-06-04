from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Any

from alice_acp.api_chat.types import (
    ApiChatAbuseSubject,
    ApiChatRequest,
    validate_public_identifier,
)
from alice_acp.api_chat.validators import ensure_no_raw_secret


def stub_api_key_id(api_key_stub: str) -> str:
    if not api_key_stub:
        raise ValueError("api_key_stub must be non-empty")
    ensure_no_raw_secret(api_key_stub, field_name="api_key_stub")
    digest = _hash_text(f"api-key-stub:{api_key_stub}")
    return f"api-key-{digest[:24]}"


def resolve_abuse_subject(request: ApiChatRequest) -> ApiChatAbuseSubject:
    ip_hash = _hash_text(f"ip:{request.client_ip}")
    user_agent_hash = _hash_text(f"user-agent:{request.user_agent}")
    api_key_hash = _hash_text(f"api-key-id:{request.api_key_id}") if request.api_key_id else None

    if request.user_id is not None:
        user_id_hash = _hash_text(f"user-id:{request.user_id}")
        return ApiChatAbuseSubject(
            identity_kind="user_id",
            identity_hash=user_id_hash,
            ip_hash=ip_hash,
            user_agent_hash=user_agent_hash,
            user_id_hash=user_id_hash,
            api_key_id=request.api_key_id,
            api_key_hash=api_key_hash,
        )

    if request.api_key_id is not None:
        return ApiChatAbuseSubject(
            identity_kind="api_key",
            identity_hash=api_key_hash or _hash_text("api-key-id:missing"),
            ip_hash=ip_hash,
            user_agent_hash=user_agent_hash,
            api_key_id=request.api_key_id,
            api_key_hash=api_key_hash,
        )

    anonymous_hash = ip_hash
    return ApiChatAbuseSubject(
        identity_kind="anonymous",
        identity_hash=anonymous_hash,
        ip_hash=ip_hash,
        user_agent_hash=user_agent_hash,
    )


def resolve_anonymous_abuse_subject(request: ApiChatRequest) -> ApiChatAbuseSubject:
    ip_hash = _hash_text(f"ip:{request.client_ip}")
    user_agent_hash = _hash_text(f"user-agent:{request.user_agent}")
    return ApiChatAbuseSubject(
        identity_kind="anonymous",
        identity_hash=ip_hash,
        ip_hash=ip_hash,
        user_agent_hash=user_agent_hash,
    )


def prompt_hash(prompt: str) -> str:
    return _hash_text(f"prompt:{prompt}")


def request_hash(request: ApiChatRequest, subject: ApiChatAbuseSubject) -> str:
    return stable_hash(
        {
            "prompt_hash": prompt_hash(request.prompt),
            "model_id": request.model_id,
            "subject": subject.limit_key,
            "input_tokens": request.input_tokens,
            "max_output_tokens": request.max_output_tokens,
            "requested_at": request.requested_at,
        }
    )


def request_id_for_hash(digest: str) -> str:
    validate_public_identifier("request_hash_prefix", f"req-{digest[:24]}")
    return f"chat-req-{digest[:24]}"


def revenue_id_for_request(*, request_id: str, amount: Decimal, source: str) -> str:
    digest = stable_hash({"request_id": request_id, "amount": amount, "source": source})
    return f"chat-revenue-{digest[:24]}"


def stable_hash(payload: Any) -> str:
    canonical = json.dumps(_jsonable(payload), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(key): _jsonable(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_jsonable(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        return value.isoformat()
    return value

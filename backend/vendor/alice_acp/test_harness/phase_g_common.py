from __future__ import annotations

from dataclasses import asdict
from decimal import Decimal
from typing import Any, Literal

from alice_acp.evidence.types import (
    ensure_no_production_alice_reference,
    ensure_no_raw_secret,
    parse_evidence_ref,
)

PhaseGRefScheme = Literal["evidence", "approval"]

PHASE_G_REASON_WHY_NOT_LIVE = (
    "external_review_is_local_metadata_only",
    "no_live_reward_execution_approval",
    "no_payout_executor_implementation",
    "production_HA_claim_still_blocked",
    "representative_benchmark_not_binding_live_authority",
    "four_week_shadow_window_not_binding_live_authority",
    "no_real_payment_processor",
    "no_real_P1_sanitizer",
    "no_production_verifier_fleet",
    "no_service_API",
)


def require_ref(ref: str, *, field_name: str, scheme: PhaseGRefScheme) -> None:
    parsed = parse_evidence_ref(ref)
    if parsed.scheme != scheme:
        raise ValueError(f"{field_name} must use {scheme}://")
    ensure_safe_metadata(field_name, ref)


def require_non_empty(value: str | None, *, field_name: str) -> bool:
    if value is None or not value.strip():
        return False
    ensure_safe_metadata(field_name, value)
    return True


def ensure_safe_metadata(field_name: str, value: str) -> None:
    ensure_no_raw_secret(value, field_name=field_name)
    ensure_no_production_alice_reference(value, field_name=field_name)
    if "alice_live" in value.replace("\\", "/").split("/"):
        raise ValueError(f"{field_name} must not reference alice_live")


def dedupe_reason_codes(reason_codes: list[str]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(reason_codes))


def json_ready_dataclass(value: Any) -> dict[str, Any]:
    return _json_ready(asdict(value))


def _json_ready(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, tuple):
        return [_json_ready(item) for item in value]
    if isinstance(value, list):
        return [_json_ready(item) for item in value]
    if isinstance(value, dict):
        return {key: _json_ready(item) for key, item in value.items()}
    return value

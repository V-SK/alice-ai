from __future__ import annotations

import re
from datetime import datetime

SECRET_PATTERN = re.compile(
    "|".join(
        (
            "BE" + "GIN " + ".*" + "KEY",
            "sk" + r"-[A-Za-z0-9_-]+",
            "ghp" + r"_[A-Za-z0-9_]+",
            "hf" + r"_[A-Za-z0-9_]+",
            "AK" + r"IA[A-Z0-9]{16}",
            "SECRET" + "=",
            "TOKEN" + "=",
        )
    )
)
SHA256_HEX_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def validate_aware_timestamp(name: str, value: datetime) -> None:
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware")


def validate_sha256(value: str, *, field_name: str = "content_sha256") -> None:
    if SHA256_HEX_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase sha256 hex digest")


def ensure_no_raw_secret(value: str, *, field_name: str = "value") -> None:
    if SECRET_PATTERN.search(value) is not None:
        raise ValueError(f"{field_name} must not contain raw secret material")

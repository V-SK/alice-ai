from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from alice_acp.evidence.types import ensure_no_raw_secret
from alice_acp.mining_runtime.types import DISABLED_POOL_URL, MinerCommandSpec

GUARD_ACCEPTED = "RUNTIME_GUARD_ACCEPTED"
GUARD_BINARY_OUTSIDE_ALLOWLIST = "RUNTIME_GUARD_BINARY_OUTSIDE_ALLOWLIST"
GUARD_NETWORK_DISABLED = "RUNTIME_GUARD_NETWORK_DISABLED"
GUARD_REAL_POOL_URL_REJECTED = "RUNTIME_GUARD_REAL_POOL_URL_REJECTED"
GUARD_ENV_KEY_NOT_ALLOWED = "RUNTIME_GUARD_ENV_KEY_NOT_ALLOWED"
GUARD_ENV_OVERRIDE_REJECTED = "RUNTIME_GUARD_ENV_OVERRIDE_REJECTED"
GUARD_ENV_SECRET_REJECTED = "RUNTIME_GUARD_ENV_SECRET_REJECTED"
GUARD_COLLECTION_ADDRESS_MISMATCH = "RUNTIME_GUARD_COLLECTION_ADDRESS_MISMATCH"
GUARD_SHELL_REJECTED = "RUNTIME_GUARD_SHELL_REJECTED"

_FORBIDDEN_ENV_KEY_FRAGMENTS = (
    "KEY",
    "SECRET",
    "TOKEN",
    "WALLET",
    "PAYOUT",
    "REWARD_ADDRESS",
    "COLLECTION_ADDRESS",
)
_LOCAL_SCHEMES = frozenset({"fixture", "disabled"})
_REAL_NETWORK_SCHEMES = frozenset({"http", "https", "stratum", "stratum+tcp", "tcp"})


def validate_command_spec(
    spec: MinerCommandSpec,
    *,
    expected_collection_address: str | None = None,
    shell: bool = False,
) -> None:
    if shell:
        raise ValueError(GUARD_SHELL_REJECTED)
    _validate_binary_path(spec.binary_path, spec.allowed_binary_roots)
    _validate_pool_url(spec)
    _validate_env(spec)
    if (
        expected_collection_address is not None
        and spec.alice_collection_address != expected_collection_address
    ):
        raise ValueError(GUARD_COLLECTION_ADDRESS_MISMATCH)


def _validate_binary_path(binary_path: Path, allowed_roots: tuple[Path, ...]) -> None:
    resolved_binary = binary_path.resolve()
    if not resolved_binary.exists():
        raise ValueError(GUARD_BINARY_OUTSIDE_ALLOWLIST)
    resolved_roots = tuple(root.resolve() for root in allowed_roots)
    if not any(_is_relative_to(resolved_binary, root) for root in resolved_roots):
        raise ValueError(GUARD_BINARY_OUTSIDE_ALLOWLIST)


def _validate_pool_url(spec: MinerCommandSpec) -> None:
    if spec.network_enabled:
        raise ValueError(GUARD_NETWORK_DISABLED)
    parsed = urlparse(spec.pool_url)
    if spec.pool_url == DISABLED_POOL_URL:
        return
    if parsed.scheme in _LOCAL_SCHEMES:
        return
    if parsed.scheme in _REAL_NETWORK_SCHEMES or "." in parsed.netloc:
        raise ValueError(GUARD_REAL_POOL_URL_REJECTED)
    raise ValueError(GUARD_REAL_POOL_URL_REJECTED)


def _validate_env(spec: MinerCommandSpec) -> None:
    allowed = set(spec.env_allowlist)
    for key, value in spec.env.items():
        if key not in allowed:
            raise ValueError(GUARD_ENV_KEY_NOT_ALLOWED)
        upper_key = key.upper()
        if any(fragment in upper_key for fragment in _FORBIDDEN_ENV_KEY_FRAGMENTS):
            raise ValueError(GUARD_ENV_OVERRIDE_REJECTED)
        try:
            ensure_no_raw_secret(value, field_name=key)
        except ValueError as exc:
            raise ValueError(GUARD_ENV_SECRET_REJECTED) from exc


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True

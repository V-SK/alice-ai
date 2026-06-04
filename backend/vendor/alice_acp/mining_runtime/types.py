from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Literal

from alice_acp.evidence.types import validate_aware_timestamp
from alice_acp.mining_proofs.types import MiningShareProof, PoolShareResult
from alice_acp.mining_session.types import RVN_KAWPOW, MiningAlgorithm

DISABLED_POOL_URL = "fixture://alice-rvn-kawpow/no-network"
DEFAULT_ENV_ALLOWLIST = (
    "ALICE_MINER_FIXTURE_MODE",
    "PATH",
    "PYTHONUNBUFFERED",
)

RuntimeState = Literal[
    "idle",
    "running",
    "exited",
    "restarting",
    "failed",
    "throttled_for_ai",
    "paused_for_ai",
]
LogEventType = Literal["candidate_proof", "nonrewardable_share", "info", "warning"]


@dataclass(frozen=True, slots=True)
class MinerCommandSpec:
    binary_path: Path
    alice_collection_address: str
    worker_name: str
    session_id: str
    working_dir: Path
    allowed_binary_roots: tuple[Path, ...]
    pool_url: str = DISABLED_POOL_URL
    algorithm: MiningAlgorithm = RVN_KAWPOW
    env_allowlist: tuple[str, ...] = DEFAULT_ENV_ALLOWLIST
    env: dict[str, str] = field(default_factory=dict)
    network_enabled: bool = False
    extra_args: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        required = (
            self.binary_path,
            self.alice_collection_address,
            self.worker_name,
            self.session_id,
            self.working_dir,
            self.pool_url,
            self.algorithm,
            self.allowed_binary_roots,
        )
        if any(not value for value in required):
            raise ValueError("miner command spec fields must be non-empty")
        if self.algorithm != RVN_KAWPOW:
            raise ValueError("only RVN_KAWPOW miner commands are supported")


@dataclass(frozen=True, slots=True)
class RuntimeGuardResult:
    accepted: bool
    reason_code: str


@dataclass(frozen=True, slots=True)
class MiningRuntimeSnapshot:
    state: RuntimeState
    pid: int | None = None
    exit_code: int | None = None
    restart_count: int = 0
    reason_code: str | None = None


@dataclass(frozen=True, slots=True)
class MinerLogEvent:
    event_type: LogEventType
    raw_line: str
    reason_code: str
    proof: MiningShareProof | None = None
    pool_result: PoolShareResult | None = None
    hashrate_khs: Decimal | None = None
    temperature_c: Decimal | None = None
    observed_at: datetime | None = None

    def __post_init__(self) -> None:
        if not self.raw_line:
            raise ValueError("raw_line must be non-empty")
        if not self.reason_code:
            raise ValueError("reason_code must be non-empty")
        if self.observed_at is not None:
            validate_aware_timestamp("observed_at", self.observed_at)

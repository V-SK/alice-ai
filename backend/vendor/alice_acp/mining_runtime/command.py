from __future__ import annotations

import os
import subprocess
from collections.abc import Mapping

from alice_acp.mining_runtime.guards import validate_command_spec
from alice_acp.mining_runtime.types import MinerCommandSpec

COMMAND_BUILT = "MINER_COMMAND_BUILT"


def build_miner_argv(
    spec: MinerCommandSpec,
    *,
    expected_collection_address: str | None = None,
) -> list[str]:
    validate_command_spec(spec, expected_collection_address=expected_collection_address)
    return [
        str(spec.binary_path.resolve()),
        "--algorithm",
        spec.algorithm,
        "--pool-url",
        spec.pool_url,
        "--alice-collection-address",
        spec.alice_collection_address,
        "--worker-name",
        spec.worker_name,
        "--session-id",
        spec.session_id,
        "--network-enabled",
        "false",
        *spec.extra_args,
    ]


def build_miner_env(
    spec: MinerCommandSpec,
    *,
    base_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    validate_command_spec(spec)
    source = os.environ if base_env is None else base_env
    env = {key: source[key] for key in spec.env_allowlist if key in source}
    env.update(spec.env)
    return env


def build_subprocess_kwargs(
    spec: MinerCommandSpec,
    *,
    expected_collection_address: str | None = None,
    shell: bool = False,
) -> dict[str, object]:
    validate_command_spec(
        spec,
        expected_collection_address=expected_collection_address,
        shell=shell,
    )
    return {
        "args": build_miner_argv(
            spec,
            expected_collection_address=expected_collection_address,
        ),
        "cwd": str(spec.working_dir.resolve()),
        "env": build_miner_env(spec),
        "stdout": subprocess.PIPE,
        "stderr": subprocess.STDOUT,
        "text": True,
        "bufsize": 1,
    }

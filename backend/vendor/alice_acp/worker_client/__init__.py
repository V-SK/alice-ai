"""The EXTERNAL Alice worker-pull client (task #7).

A miner runs this to be an Alice AI worker: authenticate with an Alice address,
register the model tiers it can serve, pull jobs, run the REAL model on its GPU,
submit ``{completion, usage}`` for Alice CREDIT (paid_acu=0; no payout). The role
seam (:mod:`alice_acp.worker_client.roles`) is built for ONE role now
(``AiInferenceRole``); a PRL-mining role can be ADDED later (GPU profit-switch)
without touching the pull loop.
"""

from alice_acp.worker_client.client import (
    WORKER_CLIENT_CONTRACT_VERSION,
    JobOutcome,
    WorkerClientConfig,
    WorkerPullClient,
)
from alice_acp.worker_client.device_identity import (
    DeviceIdentity,
    generate_device_id,
    load_or_create_device_id,
)
from alice_acp.worker_client.profit_switch import (
    DEFAULT_AI_IDLE_COOLDOWN,
    PROFIT_SWITCH_CONTRACT_VERSION,
    STATE_MINING_PRL,
    STATE_SERVING_AI,
    ProfitSwitchSnapshot,
    ProfitSwitchState,
    ProfitSwitchSupervisor,
)
from alice_acp.worker_client.roles import (
    AiInferenceRole,
    RunResult,
    WorkerRoleImpl,
)

__all__ = [
    "DEFAULT_AI_IDLE_COOLDOWN",
    "PROFIT_SWITCH_CONTRACT_VERSION",
    "STATE_MINING_PRL",
    "STATE_SERVING_AI",
    "WORKER_CLIENT_CONTRACT_VERSION",
    "AiInferenceRole",
    "DeviceIdentity",
    "JobOutcome",
    "ProfitSwitchSnapshot",
    "ProfitSwitchState",
    "ProfitSwitchSupervisor",
    "RunResult",
    "WorkerClientConfig",
    "WorkerPullClient",
    "WorkerRoleImpl",
    "generate_device_id",
    "load_or_create_device_id",
]

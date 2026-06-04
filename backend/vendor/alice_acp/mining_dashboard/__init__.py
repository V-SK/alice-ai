"""Local miner dashboard report contracts."""

from alice_acp.mining_dashboard.report import (
    DASHBOARD_LIVE_DISABLED,
    DASHBOARD_SHADOW_ONLY,
    build_miner_dashboard_report,
)
from alice_acp.mining_dashboard.types import MinerDashboardReport

__all__ = [
    "DASHBOARD_LIVE_DISABLED",
    "DASHBOARD_SHADOW_ONLY",
    "MinerDashboardReport",
    "build_miner_dashboard_report",
]

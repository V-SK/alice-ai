"""Deterministic mining device capability contracts."""

from alice_acp.mining_device.detector import (
    AMD_BACKEND_UNAVAILABLE,
    APPLE_SILICON_BEST_EFFORT,
    CPU_RANDOMX_IDLE_CAPABLE,
    GPU_MINING_CAPABLE,
    UNSUPPORTED_BACKEND,
    detect_backend_capability,
)
from alice_acp.mining_device.types import BackendCapabilityResult, DeviceProbe

__all__ = [
    "AMD_BACKEND_UNAVAILABLE",
    "APPLE_SILICON_BEST_EFFORT",
    "CPU_RANDOMX_IDLE_CAPABLE",
    "GPU_MINING_CAPABLE",
    "UNSUPPORTED_BACKEND",
    "BackendCapabilityResult",
    "DeviceProbe",
    "detect_backend_capability",
]

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

OperatingSystem = Literal["windows", "linux", "macos", "unknown"]
DeviceKind = Literal["gpu", "cpu", "apple_silicon", "unknown"]
GpuVendor = Literal["nvidia", "amd", "apple", "unknown", "none"]
CapabilityStatus = Literal["capable", "best_effort", "unsupported"]


@dataclass(frozen=True, slots=True)
class DeviceProbe:
    operating_system: OperatingSystem
    device_kind: DeviceKind
    vendor: GpuVendor = "none"
    backend_available: bool = False
    vram_gb: int | None = None
    cpu_threads: int | None = None
    metal_available: bool = False
    rocm_available: bool = False
    cuda_available: bool = False

    def __post_init__(self) -> None:
        if self.vram_gb is not None and self.vram_gb < 0:
            raise ValueError("vram_gb must be non-negative")
        if self.cpu_threads is not None and self.cpu_threads < 0:
            raise ValueError("cpu_threads must be non-negative")


@dataclass(frozen=True, slots=True)
class BackendCapabilityResult:
    status: CapabilityStatus
    mining_supported: bool
    ai_supported: bool
    cpu_idle_supported: bool
    backend: str
    reason_code: str

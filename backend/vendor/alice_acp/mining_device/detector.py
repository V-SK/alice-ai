from __future__ import annotations

from alice_acp.mining_device.types import BackendCapabilityResult, DeviceProbe

GPU_MINING_CAPABLE = "GPU_MINING_CAPABLE"
AMD_BACKEND_UNAVAILABLE = "AMD_BACKEND_UNAVAILABLE"
APPLE_SILICON_BEST_EFFORT = "APPLE_SILICON_BEST_EFFORT"
CPU_RANDOMX_IDLE_CAPABLE = "CPU_RANDOMX_IDLE_CAPABLE"
UNSUPPORTED_BACKEND = "UNSUPPORTED_BACKEND"


def detect_backend_capability(probe: DeviceProbe) -> BackendCapabilityResult:
    if probe.device_kind == "gpu" and probe.vendor == "nvidia":
        if probe.operating_system in {"windows", "linux"} and probe.cuda_available:
            return BackendCapabilityResult(
                status="capable",
                mining_supported=True,
                ai_supported=(probe.vram_gb or 0) >= 8,
                cpu_idle_supported=False,
                backend="cuda_kawpow",
                reason_code=GPU_MINING_CAPABLE,
            )
        return _unsupported("NVIDIA_CUDA_UNAVAILABLE")

    if probe.device_kind == "gpu" and probe.vendor == "amd":
        if probe.backend_available or probe.rocm_available:
            return BackendCapabilityResult(
                status="capable",
                mining_supported=True,
                ai_supported=(probe.vram_gb or 0) >= 8 and probe.rocm_available,
                cpu_idle_supported=False,
                backend="rocm_or_opencl_kawpow",
                reason_code=GPU_MINING_CAPABLE,
            )
        return _unsupported(AMD_BACKEND_UNAVAILABLE)

    if probe.device_kind == "apple_silicon" or probe.vendor == "apple":
        return BackendCapabilityResult(
            status="best_effort",
            mining_supported=False,
            ai_supported=probe.metal_available,
            cpu_idle_supported=False,
            backend="metal_best_effort",
            reason_code=APPLE_SILICON_BEST_EFFORT,
        )

    if probe.device_kind == "cpu":
        return BackendCapabilityResult(
            status="capable",
            mining_supported=False,
            ai_supported=False,
            cpu_idle_supported=True,
            backend="cpu_randomx_idle",
            reason_code=CPU_RANDOMX_IDLE_CAPABLE,
        )

    return _unsupported(UNSUPPORTED_BACKEND)


def _unsupported(reason_code: str) -> BackendCapabilityResult:
    return BackendCapabilityResult(
        status="unsupported",
        mining_supported=False,
        ai_supported=False,
        cpu_idle_supported=False,
        backend="none",
        reason_code=reason_code,
    )

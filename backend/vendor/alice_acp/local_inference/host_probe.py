"""Best-effort live host probe -> DeviceProbe + HostMemoryHint.

Builds the ``DeviceProbe`` the detector consumes from the *local* machine, with
no third-party dependency: it uses only stdlib + optional, lazily-imported
hints. It performs NO network call. Detection is conservative: anything it
cannot positively confirm degrades to cpu, and the caller can always override.

This is intentionally small -- the heavy/accurate probing (nvidia-smi, ROCm
SMI, Metal device query) is a follow-up for the real-hardware verification; the
seam (returning a ``DeviceProbe``) is what matters so the detector + selection
logic is real now.
"""

from __future__ import annotations

import os
import platform
import shutil

from alice_acp.local_inference.hardware_select import HostMemoryHint
from alice_acp.mining_device.types import DeviceProbe, OperatingSystem


def _operating_system() -> OperatingSystem:
    system = platform.system().lower()
    if system == "darwin":
        return "macos"
    if system == "windows":
        return "windows"
    if system == "linux":
        return "linux"
    return "unknown"


def _system_memory_gb() -> int:
    # os.sysconf is available on macOS + Linux; Windows degrades to 0 (override).
    try:
        page_size = os.sysconf("SC_PAGE_SIZE")
        page_count = os.sysconf("SC_PHYS_PAGES")
        return max(0, int((page_size * page_count) / (1024**3)))
    except (ValueError, OSError, AttributeError):
        return 0


def probe_local_host() -> tuple[DeviceProbe, HostMemoryHint]:
    """Probe the local machine. NO network; conservative; overridable."""
    operating_system = _operating_system()
    memory_gb = _system_memory_gb()
    machine = platform.machine().lower()

    # Apple silicon: macOS on arm64 with Metal (assumed present on Apple GPUs).
    if operating_system == "macos" and machine in ("arm64", "aarch64"):
        probe = DeviceProbe(
            operating_system="macos",
            device_kind="apple_silicon",
            vendor="apple",
            metal_available=True,
        )
        return probe, HostMemoryHint(system_memory_gb=memory_gb)

    # NVIDIA discrete GPU: nvidia-smi on PATH is a strong positive signal.
    if shutil.which("nvidia-smi") is not None and operating_system in ("windows", "linux"):
        probe = DeviceProbe(
            operating_system=operating_system,
            device_kind="gpu",
            vendor="nvidia",
            cuda_available=True,
            # VRAM unknown without querying; leave None so memory hint (system
            # RAM) is used as a floor and the caller can override with --vram.
            vram_gb=None,
        )
        return probe, HostMemoryHint(system_memory_gb=memory_gb)

    # AMD discrete GPU: rocm-smi on PATH.
    if shutil.which("rocm-smi") is not None and operating_system == "linux":
        probe = DeviceProbe(
            operating_system="linux",
            device_kind="gpu",
            vendor="amd",
            rocm_available=True,
            backend_available=True,
            vram_gb=None,
        )
        return probe, HostMemoryHint(system_memory_gb=memory_gb)

    # Fallback: CPU.
    cpu_threads = os.cpu_count() or 1
    probe = DeviceProbe(
        operating_system=operating_system,
        device_kind="cpu",
        vendor="none",
        cpu_threads=cpu_threads,
    )
    return probe, HostMemoryHint(system_memory_gb=memory_gb)

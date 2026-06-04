"""Device-probe augmentation (design 03 §3.2 / brief item 5).

``alice_acp.local_inference.probe_local_host()`` is deliberately conservative —
on macOS it reports unified RAM correctly but on Windows it reports 0 GB
(``os.sysconf`` is absent) and for NVIDIA/AMD it leaves ``vram_gb=None``. This
module fills those gaps so the tier recommendation + the context-size memory
warning are correct, **without a new dependency and without any network call**.

  * macOS: RAM via ``sysctl hw.memsize`` (a robust cross-check of the stdlib
    probe); GPU/VRAM via ``system_profiler SPDisplaysDataType`` — but Apple
    silicon is *unified* memory, so usable inference memory there is the unified
    RAM (already correct from the probe); we surface a GPU label for the UI.
  * Windows: RAM via ``GlobalMemoryStatusEx`` (ctypes), VRAM via ``nvidia-smi`` /
    ``rocm-smi`` — STUBBED with a TODO (its milestone is M5); falls through to
    the conservative probe so a Windows box is never *up*-ranked on a guess.
  * Linux: NVIDIA/AMD VRAM via ``nvidia-smi`` / ``rocm-smi`` — STUBBED with a
    TODO (M6); the probe's system-RAM floor is used until then.

``augment()`` returns a *new* frozen ``DeviceProbe`` + ``HostMemoryHint`` plus a
small human-readable label for the UI ("Apple M2 Max · 32 GB · Metal").
"""

from __future__ import annotations

import platform
import re
import shutil
import subprocess
from dataclasses import dataclass, replace
from typing import Optional

from alice_acp.local_inference.hardware_select import HostMemoryHint, usable_memory_gb
from alice_acp.mining_device.types import DeviceProbe

_RUN_TIMEOUT_S = 4.0


def _run(cmd: list[str]) -> Optional[str]:
    """Run a probe subprocess, returning stdout or None (never raises)."""
    try:
        out = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=_RUN_TIMEOUT_S,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout


@dataclass(frozen=True, slots=True)
class AugmentedDevice:
    """The augmented probe + memory hint + a UI-facing device label."""

    probe: DeviceProbe
    memory: HostMemoryHint
    usable_memory_gb: int
    device_label: str  # e.g. "Apple M2 Max" — display-safe (no model leak)
    accelerator_label: str  # "Metal" | "CUDA" | "ROCm" | "CPU"


# --------------------------------------------------------------------------- #
# macOS
# --------------------------------------------------------------------------- #
def _macos_ram_gb() -> Optional[int]:
    out = _run(["sysctl", "-n", "hw.memsize"])
    if not out:
        return None
    try:
        return max(0, int(int(out.strip()) / (1024**3)))
    except ValueError:
        return None


def _macos_chip_label() -> str:
    # `sysctl machdep.cpu.brand_string` gives "Apple M2 Max" on Apple silicon.
    out = _run(["sysctl", "-n", "machdep.cpu.brand_string"])
    if out:
        label = out.strip()
        if label:
            return label
    return "Apple silicon"


def _augment_macos(probe: DeviceProbe, memory: HostMemoryHint) -> AugmentedDevice:
    ram = _macos_ram_gb()
    if ram and ram > memory.system_memory_gb:
        memory = HostMemoryHint(system_memory_gb=ram)
    elif memory.system_memory_gb == 0 and ram:
        memory = HostMemoryHint(system_memory_gb=ram)
    chip = _macos_chip_label()
    is_apple_gpu = probe.vendor == "apple" or probe.device_kind == "apple_silicon"
    accel = "Metal" if is_apple_gpu else "CPU"
    usable = usable_memory_gb(probe, memory)
    return AugmentedDevice(
        probe=probe,
        memory=memory,
        usable_memory_gb=usable,
        device_label=chip,
        accelerator_label=accel,
    )


# --------------------------------------------------------------------------- #
# NVIDIA / AMD VRAM (used by Windows + Linux GPU paths; safe on any OS).
# --------------------------------------------------------------------------- #
def _nvidia_vram_gb() -> Optional[int]:
    if shutil.which("nvidia-smi") is None:
        return None
    out = _run(
        ["nvidia-smi", "--query-gpu=memory.total", "--format=csv,noheader,nounits"]
    )
    if not out:
        return None
    # First GPU's total MiB.
    first = out.strip().splitlines()[0].strip() if out.strip() else ""
    try:
        mib = int(re.sub(r"[^0-9]", "", first))
    except ValueError:
        return None
    if mib <= 0:
        return None
    return max(1, int(mib / 1024))


def _amd_vram_gb() -> Optional[int]:
    if shutil.which("rocm-smi") is None:
        return None
    out = _run(["rocm-smi", "--showmeminfo", "vram"])
    if not out:
        return None
    # rocm-smi prints "Total Memory (B): <bytes>" — grab the largest byte count.
    candidates = [int(x) for x in re.findall(r"(\d{9,})", out)]
    if not candidates:
        return None
    return max(1, int(max(candidates) / (1024**3)))


def _gpu_vram_gb(probe: DeviceProbe) -> Optional[int]:
    if probe.vendor == "nvidia":
        return _nvidia_vram_gb()
    if probe.vendor == "amd":
        return _amd_vram_gb()
    return None


# --------------------------------------------------------------------------- #
# Windows  (STUB — RAM/VRAM probing is its own milestone, M5)
# --------------------------------------------------------------------------- #
def _windows_ram_gb() -> Optional[int]:
    # TODO(M5): ctypes GlobalMemoryStatusEx — Windows packaging milestone.
    # Implemented now (no dependency) so the recommend()/gate logic is correct
    # the moment the Windows build lands; falls through to the conservative
    # probe value if ctypes is unavailable for any reason.
    if platform.system().lower() != "windows":
        return None
    try:
        import ctypes  # noqa: PLC0415

        class _MEMORYSTATUSEX(ctypes.Structure):
            _fields_ = [
                ("dwLength", ctypes.c_ulong),
                ("dwMemoryLoad", ctypes.c_ulong),
                ("ullTotalPhys", ctypes.c_ulonglong),
                ("ullAvailPhys", ctypes.c_ulonglong),
                ("ullTotalPageFile", ctypes.c_ulonglong),
                ("ullAvailPageFile", ctypes.c_ulonglong),
                ("ullTotalVirtual", ctypes.c_ulonglong),
                ("ullAvailVirtual", ctypes.c_ulonglong),
                ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
            ]

        stat = _MEMORYSTATUSEX()
        stat.dwLength = ctypes.sizeof(_MEMORYSTATUSEX)
        ok = ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(stat))  # type: ignore[attr-defined]
        if not ok:
            return None
        return max(0, int(stat.ullTotalPhys / (1024**3)))
    except Exception:  # noqa: BLE001 — never let a probe crash the app
        return None


def _augment_windows(probe: DeviceProbe, memory: HostMemoryHint) -> AugmentedDevice:
    ram = _windows_ram_gb()
    if ram and ram > 0:
        memory = HostMemoryHint(system_memory_gb=ram)
    vram = _gpu_vram_gb(probe)
    if vram is not None and probe.device_kind == "gpu":
        probe = replace(probe, vram_gb=vram)
    accel = (
        "CUDA"
        if probe.vendor == "nvidia"
        else "ROCm"
        if probe.vendor == "amd"
        else "CPU"
    )
    label = "NVIDIA GPU" if probe.vendor == "nvidia" else "AMD GPU" if probe.vendor == "amd" else "Windows PC"
    return AugmentedDevice(
        probe=probe,
        memory=memory,
        usable_memory_gb=usable_memory_gb(probe, memory),
        device_label=label,
        accelerator_label=accel,
    )


# --------------------------------------------------------------------------- #
# Linux  (VRAM probe live; RAM already correct via os.sysconf in the base probe)
# --------------------------------------------------------------------------- #
def _augment_linux(probe: DeviceProbe, memory: HostMemoryHint) -> AugmentedDevice:
    vram = _gpu_vram_gb(probe)
    if vram is not None and probe.device_kind == "gpu":
        probe = replace(probe, vram_gb=vram)
    # TODO(M6): richer GPU naming via nvidia-smi --query-gpu=name on the Linux
    # packaging milestone; the VRAM total above is enough for gating today.
    accel = (
        "CUDA"
        if probe.vendor == "nvidia"
        else "ROCm"
        if probe.vendor == "amd"
        else "CPU"
    )
    label = "NVIDIA GPU" if probe.vendor == "nvidia" else "AMD GPU" if probe.vendor == "amd" else "Linux PC"
    return AugmentedDevice(
        probe=probe,
        memory=memory,
        usable_memory_gb=usable_memory_gb(probe, memory),
        device_label=label,
        accelerator_label=accel,
    )


def augment(probe: DeviceProbe, memory: HostMemoryHint) -> AugmentedDevice:
    """Fill the probe gaps for the current OS (design 03 §3.2). No network.

    Conservative: an unknown GPU/VRAM is left as the probe's system-RAM floor
    (never up-ranked on a guess). Returns a new frozen probe + memory hint.
    """
    os_name = probe.operating_system
    if os_name == "macos":
        return _augment_macos(probe, memory)
    if os_name == "windows":
        return _augment_windows(probe, memory)
    if os_name == "linux":
        return _augment_linux(probe, memory)
    # Unknown OS: pass through unchanged.
    return AugmentedDevice(
        probe=probe,
        memory=memory,
        usable_memory_gb=usable_memory_gb(probe, memory),
        device_label="This device",
        accelerator_label="CPU",
    )

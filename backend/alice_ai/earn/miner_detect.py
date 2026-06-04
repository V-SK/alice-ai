"""Detect whether the sibling **Alice Miner** is installed — per-OS, best-effort.

SECURITY (design 04 §3.2 + the M7 brief): detection checks **only the known,
fixed install locations** for the Alice Miner. The resolved ``launch_target`` is
therefore always one of a small, hard-coded allow-list of paths — it is **never**
derived from client input, an env var, a request body, or a disk scan. The
companion ``miner_launch`` module re-validates that the target it is handed is on
this allow-list before spawning anything, so there is no path-injection / RCE
surface (the M7 security invariant).

Detection is **best-effort and non-authoritative** (design 04 D2): a false
negative (Miner installed somewhere nonstandard) simply shows "Get the Miner",
which lands on the download page — harmless. We never block on detection and we
never scan the whole disk (slow + creepy).

This module does no network and no writes; it only ``stat``s known paths.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import List, Optional

# Launch "kinds" surfaced to the API/UI (design 04 §5.1). The kind tells the
# launcher which fixed, safe spawn strategy to use.
KIND_MACOS_APP = "macos_app"
KIND_BINARY = "binary"
KIND_WINDOWS_EXE = "windows_exe"

# The canonical macOS bundle (KNOWN fixed path — the only thing we ever launch).
# The Miner ships as /Applications/AliceMiner.app (bundle id org.aliceprotocol.miner,
# inner exec Contents/MacOS/AliceMiner — verified on this Mac).
_MACOS_BUNDLE_NAME = "AliceMiner.app"
_MACOS_INNER_EXEC = "Contents/MacOS/AliceMiner"


@dataclass(frozen=True)
class MinerInstall:
    """Result of :func:`detect_miner`.

    ``launch_target`` is ``None`` when not installed, else the absolute path to
    the fixed launch object (the ``.app`` bundle on macOS, the binary on Linux,
    the ``.exe`` on Windows). ``launch_kind`` selects the safe spawn strategy.
    """

    installed: bool
    launch_kind: Optional[str] = None
    launch_target: Optional[str] = None

    def to_public(self) -> dict:
        """Display-safe projection for ``GET /alice/earn/status``.

        We expose ``installed`` + ``launch_kind`` but deliberately NOT the
        absolute ``launch_target`` path (the UI never needs it, and the launch
        route resolves it server-side from the same fixed allow-list — the
        client can never choose what gets launched).
        """
        return {"installed": self.installed, "launch_kind": self.launch_kind}


# --------------------------------------------------------------------------- #
# Per-OS candidate locations. Each list is the FIXED, hard-coded allow-list.
# The launcher imports allowed_launch_targets() to re-validate.
# --------------------------------------------------------------------------- #
def _macos_candidates() -> List[Path]:
    # Test/CI ONLY: $ALICE_EARN_MACOS_APPS_DIR overrides the search roots so a
    # test can isolate from the host's real /Applications (mirrors the
    # $ALICE_IDENTITY_DIR precedent). Unset in production → the fixed locations.
    override = os.environ.get("ALICE_EARN_MACOS_APPS_DIR", "").strip()
    if override:
        return [Path(d) / _MACOS_BUNDLE_NAME for d in override.split(os.pathsep) if d]
    home = Path.home()
    return [
        home / "Applications" / _MACOS_BUNDLE_NAME,
        Path("/Applications") / _MACOS_BUNDLE_NAME,
    ]


def _linux_candidates() -> List[Path]:
    # NOTE: Linux packaging lands in a later milestone (M6). These are the
    # design-04 §3.2 fixed locations, wired now so detection is correct once the
    # Miner ships a Linux build. `which` is consulted as a convenience but the
    # result is intersected with the known dirs below to stay on the allow-list.
    home = Path.home()
    fixed = [
        Path("/usr/bin/alice-miner"),
        Path("/opt/alice-miner/bin/alice-miner"),
        home / ".local" / "bin" / "alice-miner",
    ]
    return fixed


def _windows_candidates() -> List[Path]:
    # NOTE: Windows packaging lands in a later milestone (M5). Fixed
    # %LOCALAPPDATA% / %ProgramFiles% locations per design 04 §3.2 (TODO: a
    # HKCU/HKLM uninstall "InstallLocation" fallback when M5 wires the installer).
    out: List[Path] = []
    local = os.environ.get("LOCALAPPDATA")
    if local:
        out.append(Path(local) / "Programs" / "AliceMiner" / "alice-miner.exe")
    pf = os.environ.get("ProgramFiles")
    if pf:
        out.append(Path(pf) / "AliceMiner" / "alice-miner.exe")
    pf86 = os.environ.get("ProgramFiles(x86)")
    if pf86:
        out.append(Path(pf86) / "AliceMiner" / "alice-miner.exe")
    return out


def _platform() -> str:
    """Override-able platform string (``$ALICE_EARN_OS`` for cross-OS tests)."""
    forced = os.environ.get("ALICE_EARN_OS", "").strip().lower()
    if forced in ("darwin", "linux", "windows"):
        return forced
    p = sys.platform
    if p.startswith("darwin"):
        return "darwin"
    if p.startswith("win"):
        return "windows"
    return "linux"


def _candidates_for(platform: str) -> List[Path]:
    if platform == "darwin":
        return _macos_candidates()
    if platform == "windows":
        return _windows_candidates()
    return _linux_candidates()


def allowed_launch_targets() -> List[str]:
    """The complete fixed allow-list of launch targets for THIS platform.

    The launcher uses this to assert a target is known before spawning. The list
    is independent of any request/client input — it is the same hard-coded set
    detection scans.
    """
    return [str(p) for p in _candidates_for(_platform())]


def _macos_bundle_ok(bundle: Path) -> bool:
    """A macOS ``.app`` counts as installed only if its inner exec exists."""
    if not bundle.is_dir():
        return False
    inner = bundle / _MACOS_INNER_EXEC
    return inner.is_file()


def detect_miner() -> MinerInstall:
    """Return whether the Alice Miner is installed + the fixed launch target.

    First matching known location wins. Pure ``stat`` of the hard-coded
    candidate list — no disk scan, no network, no writes.
    """
    platform = _platform()
    candidates = _candidates_for(platform)

    if platform == "darwin":
        for bundle in candidates:
            if _macos_bundle_ok(bundle):
                return MinerInstall(True, KIND_MACOS_APP, str(bundle))
        return MinerInstall(False)

    kind = KIND_WINDOWS_EXE if platform == "windows" else KIND_BINARY
    for path in candidates:
        try:
            if path.is_file():
                # On POSIX, require the regular file to be executable; on Windows
                # an .exe existing is sufficient (no +x bit concept).
                if platform != "windows" and not os.access(path, os.X_OK):
                    continue
                return MinerInstall(True, kind, str(path))
        except OSError:
            continue
    return MinerInstall(False)

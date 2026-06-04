"""Safely launch the KNOWN Alice Miner — fixed action, no injection surface.

SECURITY (the M7 invariant): this launches **only** a target that ``miner_detect``
resolved from its hard-coded allow-list, and re-validates that membership here
before spawning. It NEVER accepts a path/command/argument from the client, an
env var, or a request body — the ``POST /alice/earn/open-miner`` route takes
**no body** and simply re-runs detection server-side. So there is exactly one
spawnable thing (the Alice Miner at its fixed install path), no new exec/RCE
surface, and ``shell=True`` is never used (args are a fixed list).

Launch is **fire-and-forget** (design 04 §3.3 / D3): we do not wait for, parse,
supervise, or IPC with the Miner — it is a sibling app with its own lifecycle
that reads the SAME ``~/.alice/identity.json`` on start, so both apps are in
sync with zero coupling. We only report whether the spawn *call* succeeded; on
failure the route returns the download URL so the UI can fall back.

No network, no writes to ``~/.alice``.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from typing import Optional

from alice_ai.earn.miner_detect import (
    KIND_BINARY,
    KIND_MACOS_APP,
    KIND_WINDOWS_EXE,
    MinerInstall,
    allowed_launch_targets,
    detect_miner,
)

logger = logging.getLogger("alice_earn")


class UnsafeLaunchTarget(ValueError):
    """Raised if a launch target is not on the fixed detection allow-list.

    This is the guard that makes launch a fixed action: even if some caller
    fabricated a ``MinerInstall`` with an arbitrary path, the launcher refuses
    it unless it is byte-for-byte one of ``allowed_launch_targets()``.
    """


@dataclass(frozen=True)
class LaunchResult:
    launched: bool
    reason: Optional[str] = None  # short machine code on failure (for logs/UI)


def _assert_allowed(target: str) -> None:
    """Refuse any target that is not on the hard-coded detection allow-list."""
    allowed = set(allowed_launch_targets())
    if target not in allowed:
        raise UnsafeLaunchTarget(
            f"launch target is not a known Alice Miner location: {target!r}"
        )


def build_launch_command(install: MinerInstall) -> list[str]:
    """Return the EXACT argv that would be spawned for *install* (no shell).

    Pure + side-effect-free, so tests can assert the command is the fixed, safe
    form for the KNOWN path and contains nothing client-controlled. Raises
    :class:`UnsafeLaunchTarget` if the target isn't on the allow-list, or
    ``ValueError`` if not installed.

    macOS:  ``open -a <AliceMiner.app>``  (GUI-correct; foregrounds if running).
    Linux:  ``<resolved binary>``         (spawned detached by :func:`launch_miner`).
    Windows: ``<alice-miner.exe>``        (spawned detached, no console).
    """
    if not install.installed or not install.launch_target:
        raise ValueError("Alice Miner is not installed; nothing to launch")
    target = install.launch_target
    _assert_allowed(target)

    if install.launch_kind == KIND_MACOS_APP:
        # `open -a "<bundle>"` is the GUI-correct launch: it activates an already
        # running instance instead of starting a second copy, and runs it in the
        # user's desktop session. We pass the KNOWN bundle path as a fixed arg —
        # NOT a user string — so there is no injection.
        return ["open", "-a", target]
    if install.launch_kind in (KIND_BINARY, KIND_WINDOWS_EXE):
        # A single fixed argv element: the resolved, allow-listed executable.
        return [target]
    raise ValueError(f"unknown launch kind: {install.launch_kind!r}")


def launch_miner(install: Optional[MinerInstall] = None) -> LaunchResult:
    """Spawn the KNOWN Alice Miner, detached. Fire-and-forget (design 04 §3.3).

    *install* defaults to a fresh server-side ``detect_miner()`` so the route can
    call this with no client input at all. Returns a :class:`LaunchResult`;
    failures are reported (never raised to the route) so the UI can fall back to
    the download page.
    """
    if install is None:
        install = detect_miner()
    if not install.installed or not install.launch_target:
        return LaunchResult(False, "not_installed")

    try:
        argv = build_launch_command(install)
    except UnsafeLaunchTarget:
        logger.warning("[earn] refused launch of non-allow-listed target")
        return LaunchResult(False, "unsafe_target")
    except ValueError:
        return LaunchResult(False, "not_installed")

    try:
        if install.launch_kind == KIND_WINDOWS_EXE:
            # Detached, no console window. CREATE_NO_WINDOW | DETACHED_PROCESS.
            creationflags = 0
            for name in ("CREATE_NO_WINDOW", "DETACHED_PROCESS"):
                creationflags |= int(getattr(subprocess, name, 0))
            subprocess.Popen(  # noqa: S603 — fixed argv, no shell, allow-listed path
                argv,
                creationflags=creationflags,
                close_fds=True,
            )
        elif install.launch_kind == KIND_MACOS_APP:
            # `open` returns immediately; it hands off to launchd. No shell.
            subprocess.Popen(  # noqa: S603 — fixed argv (open -a <known bundle>)
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                close_fds=True,
            )
        else:
            # Linux: detach into its own session (no controlling tty), inherit
            # the desktop env (DISPLAY/WAYLAND_DISPLAY) so the GUI shows.
            subprocess.Popen(  # noqa: S603 — fixed argv, no shell, allow-listed path
                argv,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
                close_fds=True,
            )
    except (OSError, ValueError) as exc:
        logger.warning("[earn] miner launch failed: %s", exc)
        return LaunchResult(False, "spawn_failed")
    logger.info("[earn] Alice Miner launch requested (%s)", install.launch_kind)
    return LaunchResult(True)

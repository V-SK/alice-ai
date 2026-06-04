from __future__ import annotations

import subprocess
import threading
from dataclasses import dataclass, field
from typing import TextIO

from alice_acp.mining_runtime.command import build_subprocess_kwargs
from alice_acp.mining_runtime.types import MinerCommandSpec, MiningRuntimeSnapshot

RUNTIME_STARTED = "RUNTIME_STARTED"
RUNTIME_STOPPED = "RUNTIME_STOPPED"
RUNTIME_TERMINATE_TIMEOUT_KILLED = "RUNTIME_TERMINATE_TIMEOUT_KILLED"
RUNTIME_RESTARTING_AFTER_CRASH = "RUNTIME_RESTARTING_AFTER_CRASH"
RUNTIME_FAILED_AFTER_CRASH = "RUNTIME_FAILED_AFTER_CRASH"
RUNTIME_EXITED = "RUNTIME_EXITED"
RUNTIME_THROTTLED_FOR_AI = "RUNTIME_THROTTLED_FOR_AI"
RUNTIME_PAUSED_FOR_AI = "RUNTIME_PAUSED_FOR_AI"
RUNTIME_RESUMED_AFTER_AI = "RUNTIME_RESUMED_AFTER_AI"


@dataclass(slots=True)
class MiningProcessSupervisor:
    command_spec: MinerCommandSpec
    max_restart_count: int = 1
    terminate_timeout_seconds: float = 1.0
    restart_count: int = 0
    snapshot: MiningRuntimeSnapshot = field(
        default_factory=lambda: MiningRuntimeSnapshot(state="idle")
    )
    _process: subprocess.Popen[str] | None = field(default=None, init=False)
    _stdout_thread: threading.Thread | None = field(default=None, init=False)
    _log_lines: list[str] = field(default_factory=list, init=False)
    _log_lock: threading.Lock = field(default_factory=threading.Lock, init=False)

    def start(self) -> MiningRuntimeSnapshot:
        if self._process is not None and self._process.poll() is None:
            return self.snapshot
        return self._spawn(reason_code=RUNTIME_STARTED)

    def stop(self) -> MiningRuntimeSnapshot:
        process = self._process
        if process is None:
            self.snapshot = MiningRuntimeSnapshot(
                state="exited",
                restart_count=self.restart_count,
                reason_code=RUNTIME_STOPPED,
            )
            return self.snapshot
        if process.poll() is None:
            process.terminate()
            try:
                exit_code = process.wait(timeout=self.terminate_timeout_seconds)
                reason_code = RUNTIME_STOPPED
            except subprocess.TimeoutExpired:
                process.kill()
                exit_code = process.wait(timeout=self.terminate_timeout_seconds)
                reason_code = RUNTIME_TERMINATE_TIMEOUT_KILLED
        else:
            exit_code = process.returncode
            reason_code = RUNTIME_STOPPED
        self._join_reader()
        self._process = None
        self.snapshot = MiningRuntimeSnapshot(
            state="exited",
            exit_code=exit_code,
            restart_count=self.restart_count,
            reason_code=reason_code,
        )
        return self.snapshot

    def poll_and_restart_if_needed(self) -> MiningRuntimeSnapshot:
        process = self._process
        if process is None:
            return self.snapshot
        exit_code = process.poll()
        if exit_code is None:
            self.snapshot = MiningRuntimeSnapshot(
                state="running",
                pid=process.pid,
                restart_count=self.restart_count,
                reason_code=self.snapshot.reason_code,
            )
            return self.snapshot
        self._join_reader()
        self._process = None
        if exit_code == 0:
            self.snapshot = MiningRuntimeSnapshot(
                state="exited",
                exit_code=exit_code,
                restart_count=self.restart_count,
                reason_code=RUNTIME_EXITED,
            )
            return self.snapshot
        if self.restart_count >= self.max_restart_count:
            self.snapshot = MiningRuntimeSnapshot(
                state="failed",
                exit_code=exit_code,
                restart_count=self.restart_count,
                reason_code=RUNTIME_FAILED_AFTER_CRASH,
            )
            return self.snapshot
        self.restart_count += 1
        self.snapshot = MiningRuntimeSnapshot(
            state="restarting",
            exit_code=exit_code,
            restart_count=self.restart_count,
            reason_code=RUNTIME_RESTARTING_AFTER_CRASH,
        )
        return self._spawn(reason_code=RUNTIME_RESTARTING_AFTER_CRASH)

    def throttle_for_ai(self) -> MiningRuntimeSnapshot:
        self.stop()
        self.snapshot = MiningRuntimeSnapshot(
            state="throttled_for_ai",
            restart_count=self.restart_count,
            reason_code=RUNTIME_THROTTLED_FOR_AI,
        )
        return self.snapshot

    def pause_for_ai(self) -> MiningRuntimeSnapshot:
        self.stop()
        self.snapshot = MiningRuntimeSnapshot(
            state="paused_for_ai",
            restart_count=self.restart_count,
            reason_code=RUNTIME_PAUSED_FOR_AI,
        )
        return self.snapshot

    def resume_after_ai(self) -> MiningRuntimeSnapshot:
        return self._spawn(reason_code=RUNTIME_RESUMED_AFTER_AI)

    def log_lines(self) -> tuple[str, ...]:
        with self._log_lock:
            return tuple(self._log_lines)

    def _spawn(self, *, reason_code: str) -> MiningRuntimeSnapshot:
        kwargs = build_subprocess_kwargs(self.command_spec)
        process = subprocess.Popen(**kwargs)
        self._process = process
        self._stdout_thread = threading.Thread(
            target=self._read_stdout,
            args=(process.stdout,),
            daemon=True,
        )
        self._stdout_thread.start()
        self.snapshot = MiningRuntimeSnapshot(
            state="running",
            pid=process.pid,
            restart_count=self.restart_count,
            reason_code=reason_code,
        )
        return self.snapshot

    def _read_stdout(self, stdout: TextIO | None) -> None:
        if stdout is None:
            return
        for line in stdout:
            with self._log_lock:
                self._log_lines.append(line.rstrip("\n"))

    def _join_reader(self) -> None:
        if self._stdout_thread is not None:
            self._stdout_thread.join(timeout=1)
            self._stdout_thread = None

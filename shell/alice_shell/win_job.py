"""Windows Job Object child supervision for the backend (M5).

On POSIX the shell puts the backend in its own session/process-group and
SIGTERM/SIGKILLs the group on quit (``supervisor.py``). Windows has no process
groups in that sense; the robust equivalent is a **Job Object**.

The contract this module gives the shell on Windows:

  * **Kill-on-parent-die (the M5 requirement):** the job is created with
    ``JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE``. The job handle is owned by the shell
    process. When the shell exits **for any reason — clean quit, crash, or an
    external ``taskkill``/Task-Manager "End task" of the parent** — the last
    handle to the job closes and the OS terminates every process still in the
    job (the frozen uvicorn backend + any grandchildren). So killing the parent
    kills the frozen backend child, with no cooperation from the child.
  * **Whole-tree hard kill on demand:** ``terminate()`` calls
    ``TerminateJobObject``, which kills the entire job atomically — strictly
    better than ``Popen.kill()`` (which kills only the direct child and can leak
    grandchildren that hold the loopback port).

Spawn ordering matters: we create the child **suspended**, assign it to the job,
then resume it. Assigning before the first instruction runs closes the race
where the child could spawn its own children *before* it joins the job (those
would escape kill-on-close). ``subprocess.Popen`` does not expose the suspended
handle cleanly, so on Windows the supervisor builds the process via this module.

Everything here is ``ctypes`` against ``kernel32`` — no third-party dep, present
on every Windows. The module imports cleanly on non-Windows (the functions just
aren't called there); ``supervisor.py`` branches on ``os.name == "nt"``.
"""

from __future__ import annotations

import os
import subprocess
import sys

# --------------------------------------------------------------------------- #
# Win32 constants (documented values; we avoid importing pywin32).
# --------------------------------------------------------------------------- #
_JobObjectExtendedLimitInformation = 9
_JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE = 0x00002000

_PROCESS_ALL_ACCESS = 0x1F0FFF
_CREATE_SUSPENDED = 0x00000004
_CREATE_NEW_PROCESS_GROUP = 0x00000200
_CREATE_NO_WINDOW = 0x08000000

_INFINITE = 0xFFFFFFFF


def is_windows() -> bool:
    return os.name == "nt"


class JobObjectProcess:
    """A child process pinned to a kill-on-close Windows Job Object.

    Mirrors the small slice of ``subprocess.Popen`` the supervisor uses
    (``poll`` / ``wait`` / ``send_signal`` / ``kill`` / ``pid``) so the rest of
    ``supervisor.py`` treats it like a Popen. Only constructed on Windows.
    """

    def __init__(
        self,
        cmd: list[str],
        *,
        cwd: str,
        env: dict,
        stdout,
        stderr,
    ) -> None:
        import ctypes  # noqa: PLC0415 — Windows-only, lazy

        self._ctypes = ctypes
        self._k32 = ctypes.WinDLL("kernel32", use_last_error=True)

        # 1) Create the job + set kill-on-close, BEFORE the child can run.
        self._job = self._create_job()

        # 2) Spawn the child SUSPENDED so we can assign it to the job before its
        #    first instruction. CREATE_NEW_PROCESS_GROUP lets us deliver
        #    CTRL_BREAK for a graceful stop; CREATE_NO_WINDOW keeps the frozen
        #    no-console backend from flashing a console (matches the windowed
        #    shell). We use Popen for the heavy lifting (handles, env, fds) and
        #    pass the suspend flag through creationflags.
        creationflags = (
            _CREATE_SUSPENDED
            | _CREATE_NEW_PROCESS_GROUP
            | _CREATE_NO_WINDOW
        )
        self._proc = subprocess.Popen(
            cmd,
            cwd=cwd,
            env=env,
            stdout=stdout,
            stderr=stderr,
            creationflags=creationflags,
        )

        # 3) Assign the suspended child to the job, then resume its main thread.
        #    If assignment fails we still resume (so we don't strand a suspended
        #    process) but log — the process simply won't get kill-on-close.
        try:
            self._assign_to_job(self._proc.pid)
        finally:
            self._resume_main_thread(self._proc.pid)

    # ------------------------------------------------------------------ #
    # Job lifecycle
    # ------------------------------------------------------------------ #
    def _create_job(self):
        ctypes = self._ctypes
        from ctypes import wintypes  # noqa: PLC0415

        job = self._k32.CreateJobObjectW(None, None)
        if not job:
            raise ctypes.WinError(ctypes.get_last_error())

        # JOBOBJECT_EXTENDED_LIMIT_INFORMATION { JOBOBJECT_BASIC_LIMIT_INFORMATION
        # BasicLimitInformation; IO_COUNTERS IoInfo; SIZE_T ... }. We only need
        # to set LimitFlags |= KILL_ON_JOB_CLOSE inside BasicLimitInformation.
        class _BASIC_LIMIT(ctypes.Structure):
            _fields_ = [
                ("PerProcessUserTimeLimit", ctypes.c_int64),
                ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", wintypes.DWORD),
                ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t),
                ("ActiveProcessLimit", wintypes.DWORD),
                ("Affinity", ctypes.c_size_t),
                ("PriorityClass", wintypes.DWORD),
                ("SchedulingClass", wintypes.DWORD),
            ]

        class _IO_COUNTERS(ctypes.Structure):
            _fields_ = [
                ("ReadOperationCount", ctypes.c_uint64),
                ("WriteOperationCount", ctypes.c_uint64),
                ("OtherOperationCount", ctypes.c_uint64),
                ("ReadTransferCount", ctypes.c_uint64),
                ("WriteTransferCount", ctypes.c_uint64),
                ("OtherTransferCount", ctypes.c_uint64),
            ]

        class _EXTENDED_LIMIT(ctypes.Structure):
            _fields_ = [
                ("BasicLimitInformation", _BASIC_LIMIT),
                ("IoInfo", _IO_COUNTERS),
                ("ProcessMemoryLimit", ctypes.c_size_t),
                ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t),
                ("PeakJobMemoryUsed", ctypes.c_size_t),
            ]

        info = _EXTENDED_LIMIT()
        info.BasicLimitInformation.LimitFlags = _JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        ok = self._k32.SetInformationJobObject(
            job,
            _JobObjectExtendedLimitInformation,
            ctypes.byref(info),
            ctypes.sizeof(info),
        )
        if not ok:
            err = ctypes.get_last_error()
            self._k32.CloseHandle(job)
            raise ctypes.WinError(err)
        return job

    def _assign_to_job(self, pid: int) -> None:
        ctypes = self._ctypes
        hproc = self._k32.OpenProcess(_PROCESS_ALL_ACCESS, False, pid)
        if not hproc:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            ok = self._k32.AssignProcessToJobObject(self._job, hproc)
            if not ok:
                raise ctypes.WinError(ctypes.get_last_error())
        finally:
            self._k32.CloseHandle(hproc)

    def _resume_main_thread(self, pid: int) -> None:
        """Resume the suspended child by resuming all its threads.

        ``Popen`` doesn't hand us the primary thread handle, so we snapshot the
        process's threads and ``ResumeThread`` each. For a just-spawned suspended
        process that is exactly its one primary thread.
        """
        ctypes = self._ctypes
        from ctypes import wintypes  # noqa: PLC0415

        TH32CS_SNAPTHREAD = 0x00000004
        THREAD_SUSPEND_RESUME = 0x0002
        INVALID = wintypes.HANDLE(-1).value

        class _THREADENTRY32(ctypes.Structure):
            _fields_ = [
                ("dwSize", wintypes.DWORD),
                ("cntUsage", wintypes.DWORD),
                ("th32ThreadID", wintypes.DWORD),
                ("th32OwnerProcessID", wintypes.DWORD),
                ("tpBasePri", wintypes.LONG),
                ("tpDeltaPri", wintypes.LONG),
                ("dwFlags", wintypes.DWORD),
            ]

        snap = self._k32.CreateToolhelp32Snapshot(TH32CS_SNAPTHREAD, 0)
        if snap == INVALID or not snap:
            return
        try:
            entry = _THREADENTRY32()
            entry.dwSize = ctypes.sizeof(_THREADENTRY32)
            if not self._k32.Thread32First(snap, ctypes.byref(entry)):
                return
            while True:
                if entry.th32OwnerProcessID == pid:
                    hthread = self._k32.OpenThread(
                        THREAD_SUSPEND_RESUME, False, entry.th32ThreadID
                    )
                    if hthread:
                        self._k32.ResumeThread(hthread)
                        self._k32.CloseHandle(hthread)
                if not self._k32.Thread32Next(snap, ctypes.byref(entry)):
                    break
        finally:
            self._k32.CloseHandle(snap)

    # ------------------------------------------------------------------ #
    # Popen-compatible surface used by supervisor.py
    # ------------------------------------------------------------------ #
    @property
    def pid(self) -> int:
        return self._proc.pid

    def poll(self):
        return self._proc.poll()

    def wait(self, timeout: float | None = None):
        return self._proc.wait(timeout=timeout)

    def send_signal(self, sig) -> None:
        self._proc.send_signal(sig)

    def kill(self) -> None:
        """Hard-kill the WHOLE job (child + grandchildren), then close handles."""
        self.terminate_job()
        try:
            self._proc.kill()
        except OSError:
            pass

    def terminate_job(self) -> None:
        """``TerminateJobObject`` — atomic whole-tree kill."""
        if getattr(self, "_job", None):
            try:
                self._k32.TerminateJobObject(self._job, 1)
            except OSError:
                pass

    def close_job(self) -> None:
        """Close the job handle. Because of KILL_ON_JOB_CLOSE this alone kills
        the tree — the safety net if ``terminate_job`` was never called (e.g. a
        crash). Call from the supervisor's final cleanup."""
        if getattr(self, "_job", None):
            try:
                self._k32.CloseHandle(self._job)
            except OSError:
                pass
            self._job = None


def self_check() -> int:
    """Importable smoke check (no child spawn): assert the ctypes surface binds.

    Run on Windows CI as ``python -m alice_shell.win_job`` to confirm the
    kernel32 entry points + struct layout resolve before a real build. On
    non-Windows it returns 0 after confirming the module imports.
    """
    if not is_windows():
        print("win_job: non-Windows — import OK, Job-Object path is Windows-only")
        return 0
    import ctypes  # noqa: PLC0415

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)
    job = k32.CreateJobObjectW(None, None)
    if not job:
        print("win_job: CreateJobObjectW FAILED", file=sys.stderr)
        return 1
    k32.CloseHandle(job)
    print("win_job: CreateJobObjectW + CloseHandle OK (kill-on-close ready)")
    return 0


if __name__ == "__main__":
    raise SystemExit(self_check())

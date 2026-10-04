"""Windows worker process containment with a portable no-op fallback.

Only the spawned worker is assigned to the Job Object.  Its browser
descendants inherit that membership; unrelated user-launched Chrome/Edge
processes are never opened or terminated by this module.
"""

from __future__ import annotations

import os
import time
from contextlib import suppress
from ctypes import (
    POINTER,
    Structure,
    byref,
    c_size_t,
    c_uint32,
    c_void_p,
    sizeof,
    wintypes,
)
from dataclasses import dataclass
from datetime import datetime, timezone


@dataclass(slots=True)
class ProcessContainment:
    handle: int | None
    pid: int


def attach_current_process() -> ProcessContainment:
    if os.name != "nt":
        return ProcessContainment(None, os.getpid())
    try:
        from ctypes import WinDLL, get_last_error

        kernel32 = WinDLL("kernel32", use_last_error=True)
        kernel32.CreateJobObjectW.argtypes = [c_void_p, wintypes.LPCWSTR]
        kernel32.CreateJobObjectW.restype = wintypes.HANDLE
        kernel32.SetInformationJobObject.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, c_void_p, wintypes.DWORD
        ]
        kernel32.SetInformationJobObject.restype = wintypes.BOOL
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, POINTER(wintypes.DWORD)]
        kernel32.GetExitCodeProcess.restype = wintypes.BOOL
        kernel32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        kernel32.AssignProcessToJobObject.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL

        handle = kernel32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(get_last_error(), "CreateJobObjectW failed")

        class BasicLimitInformation(Structure):
            _fields_ = [("per_process_user_time_limit", c_size_t), ("per_job_user_time_limit", c_size_t),
                        ("limit_flags", c_uint32), ("minimum_working_set_size", c_size_t),
                        ("maximum_working_set_size", c_size_t), ("active_process_limit", c_uint32),
                        ("affinity", c_size_t), ("priority_class", c_uint32), ("scheduling_class", c_uint32)]

        class IoCounters(Structure):
            _fields_ = [("read_ops", c_size_t), ("write_ops", c_size_t), ("other_ops", c_size_t),
                        ("read_bytes", c_size_t), ("write_bytes", c_size_t), ("other_bytes", c_size_t)]

        class ExtendedLimitInformation(Structure):
            _fields_ = [("basic", BasicLimitInformation), ("io", IoCounters),
                        ("process_memory_limit", c_size_t), ("job_memory_limit", c_size_t),
                        ("peak_process_memory_used", c_size_t), ("peak_job_memory_used", c_size_t)]

        info = ExtendedLimitInformation()
        # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        info.basic.limit_flags = 0x00002000
        # JobObjectExtendedLimitInformation = 9.  The Win32 API has exactly
        # four arguments; passing a fifth argument corrupts the call on some
        # 64-bit Python builds and silently disables containment.
        if not kernel32.SetInformationJobObject(handle, 9, byref(info), c_uint32(sizeof(info))):
            kernel32.CloseHandle(handle)
            raise OSError(get_last_error(), "SetInformationJobObject failed")
        process = kernel32.OpenProcess(0x001F0FFF, False, os.getpid())
        if not process or not kernel32.AssignProcessToJobObject(handle, process):
            if process:
                kernel32.CloseHandle(process)
            kernel32.CloseHandle(handle)
            raise OSError(get_last_error(), "AssignProcessToJobObject failed")
        kernel32.CloseHandle(process)
        return ProcessContainment(int(handle), os.getpid())
    except (AttributeError, OSError, TypeError) as exc:
        # A Windows worker without containment must never open a browser: its
        # descendants could survive a crash and be mistaken for a replacement.
        raise RuntimeError("worker process containment could not be established") from exc


def close(containment: ProcessContainment | None) -> None:
    if containment is None or containment.handle is None or os.name != "nt":
        return
    try:
        from ctypes import WinDLL

        kernel32 = WinDLL("kernel32", use_last_error=True)
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        kernel32.CloseHandle(wintypes.HANDLE(containment.handle))
    except (AttributeError, OSError):
        pass


def descendants_gone(pid: int) -> bool:
    """Return whether a worker PID has no live descendants.

    The Job Object accounting query is authoritative.  ``wmic`` was removed
    from recent Windows installations and cannot be used as a containment
    proof.
    """
    if os.name != "nt":
        return True
    # This PID-only compatibility helper can prove the worker itself is gone,
    # but only a retained Job handle can account for descendants. Callers that
    # own the handle use ``job_active_processes`` below.
    return _pid_gone(pid)


def job_active_processes(handle: int | None) -> int | None:
    if os.name != "nt" or not handle:
        return 0
    try:
        from ctypes import WinDLL

        class BasicAccounting(Structure):
            _fields_ = [("total_processes", c_uint32), ("active_processes", c_uint32),
                        ("total_terminated_processes", c_uint32)]

        kernel32 = WinDLL("kernel32", use_last_error=True)
        kernel32.QueryInformationJobObject.argtypes = [
            wintypes.HANDLE, wintypes.DWORD, c_void_p, wintypes.DWORD,
            POINTER(wintypes.DWORD)
        ]
        kernel32.QueryInformationJobObject.restype = wintypes.BOOL
        info = BasicAccounting()
        returned = wintypes.DWORD()
        if not kernel32.QueryInformationJobObject(
            wintypes.HANDLE(handle), 2, byref(info), sizeof(info), byref(returned)
        ):
            return None
        return int(info.active_processes)
    except (AttributeError, OSError, TypeError):
        return None


def _pid_gone(pid: int) -> bool:
    if pid <= 0:
        return True
    try:
        from ctypes import WinDLL
        kernel32 = WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        kernel32.CloseHandle.restype = wintypes.BOOL
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return True
        exit_code = wintypes.DWORD()
        alive = bool(kernel32.GetExitCodeProcess(handle, byref(exit_code))) and exit_code.value == 259
        kernel32.CloseHandle(handle)
        return not alive
    except (AttributeError, OSError, TypeError):
        return True


def wait_descendants_gone(pid: int, timeout: float = 15.0, *, handle: int | None = None) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        active = job_active_processes(handle)
        if _pid_gone(pid) and (active in (None, 0)):
            return True
        time.sleep(0.05)
    active = job_active_processes(handle)
    return _pid_gone(pid) and active in (None, 0)


def process_identity_matches(pid: int, started_at: datetime | None) -> bool:
    """Prove that *pid* is the worker recorded by durable runtime state."""
    if os.name != "nt" or started_at is None or pid <= 0:
        return False
    try:
        from ctypes import WinDLL

        kernel32 = WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = wintypes.HANDLE
        kernel32.GetProcessTimes.argtypes = [
            wintypes.HANDLE, POINTER(wintypes.FILETIME), POINTER(wintypes.FILETIME),
            POINTER(wintypes.FILETIME), POINTER(wintypes.FILETIME),
        ]
        kernel32.GetProcessTimes.restype = wintypes.BOOL
        kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
        handle = kernel32.OpenProcess(0x1000, False, pid)
        if not handle:
            return False
        creation = wintypes.FILETIME()
        exit_time = wintypes.FILETIME()
        kernel_time = wintypes.FILETIME()
        user_time = wintypes.FILETIME()
        valid = bool(kernel32.GetProcessTimes(
            handle, byref(creation), byref(exit_time), byref(kernel_time), byref(user_time)
        ))
        kernel32.CloseHandle(handle)
        if not valid:
            return False
        ticks = (creation.dwHighDateTime << 32) | creation.dwLowDateTime
        actual = datetime.fromtimestamp(
            (ticks - 116444736000000000) / 10_000_000, tz=timezone.utc
        )
        expected = started_at if started_at.tzinfo else started_at.replace(tzinfo=timezone.utc)
        return abs((actual - expected).total_seconds()) <= 10
    except (AttributeError, OSError, TypeError, ValueError):
        return False


def terminate_process_tree(
    pid: int, timeout: float = 5.0, *, started_at: datetime | None = None
) -> bool:
    """Terminate a stale worker and its browser descendants after API restart.

    Windows Job Objects are intentionally private to the worker, so a new API
    process cannot retain the old handle.  ``taskkill /T`` is used only after
    the durable process-start timestamp proves that the PID still identifies
    this worker; it is never used for an unverified/reused PID.
    """
    if pid <= 0 or _pid_gone(pid):
        return True
    if os.name == "nt":
        if not process_identity_matches(pid, started_at):
            # Never taskkill an unproven/reused PID.  The caller must wait for
            # the durable worker to disappear or use a retained Job handle.
            return False
        import subprocess

        try:
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                check=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=max(1.0, timeout),
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            return False
    else:
        with suppress(ProcessLookupError, PermissionError):
            os.kill(pid, 15)
    return wait_descendants_gone(pid, timeout=timeout)

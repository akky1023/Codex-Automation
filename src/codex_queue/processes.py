from contextlib import contextmanager
import os
import signal
import subprocess
import time
from uuid import NAMESPACE_URL, uuid5

from .store import queue_folder


@contextmanager
def process_lock(name):
    with (queue_folder() / name).open("a+b") as file:
        file.seek(0)
        if os.name == "nt":
            import msvcrt
            try:
                msvcrt.locking(file.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError:
                yield False
                return
        else:
            import fcntl
            try:
                fcntl.flock(file, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                yield False
                return
        yield True


def job_name(attempt_id):
    scope = uuid5(NAMESPACE_URL, str(queue_folder().resolve()))
    return f"Local\\CodexQueue-{scope}-{attempt_id}"


def wait_for_job(kernel, job):
    import ctypes
    from ctypes import wintypes

    class Accounting(ctypes.Structure):
        _fields_ = [
            ("times", ctypes.c_int64 * 4),
            ("page_faults", wintypes.DWORD), ("total", wintypes.DWORD),
            ("active", wintypes.DWORD), ("terminated", wintypes.DWORD),
        ]

    query = kernel.QueryInformationJobObject
    query.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.c_void_p,
    ]
    accounting = Accounting()
    while True:
        if not query(job, 1, ctypes.byref(accounting), ctypes.sizeof(accounting), None):
            raise ctypes.WinError(ctypes.get_last_error())
        if not accounting.active:
            return
        time.sleep(0.05)


def wait_for_previous_job(attempt_id):
    if os.name != "nt":
        return
    import ctypes
    from ctypes import wintypes

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.OpenJobObjectW.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.LPCWSTR]
    kernel.OpenJobObjectW.restype = wintypes.HANDLE
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    job = kernel.OpenJobObjectW(0x0004, False, job_name(attempt_id))  # JOB_OBJECT_QUERY
    if not job:
        if ctypes.get_last_error() == 2:
            return
        raise ctypes.WinError(ctypes.get_last_error())
    try:
        wait_for_job(kernel, job)
    finally:
        kernel.CloseHandle(job)


def spawn(command, attempt_id, **kwargs):
    if os.name != "nt":
        return subprocess.Popen(command, start_new_session=True, **kwargs), None

    import ctypes
    from ctypes import wintypes

    class BasicLimits(ctypes.Structure):
        _fields_ = [
            ("process_time", ctypes.c_int64), ("job_time", ctypes.c_int64),
            ("flags", wintypes.DWORD), ("min_working_set", ctypes.c_size_t),
            ("max_working_set", ctypes.c_size_t), ("active_processes", wintypes.DWORD),
            ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD),
            ("scheduling", wintypes.DWORD),
        ]

    class ExtendedLimits(ctypes.Structure):
        _fields_ = [
            ("basic", BasicLimits), ("io", ctypes.c_uint64 * 6),
            ("memory", ctypes.c_size_t * 4),
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.argtypes = [ctypes.c_void_p, wintypes.LPCWSTR]
    kernel.CreateJobObjectW.restype = wintypes.HANDLE
    kernel.SetInformationJobObject.argtypes = [
        wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD,
    ]
    kernel.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
    kernel.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    resume = ctypes.WinDLL("ntdll").NtResumeProcess
    resume.argtypes = [wintypes.HANDLE]
    job = kernel.CreateJobObjectW(None, job_name(attempt_id))
    if not job:
        raise ctypes.WinError(ctypes.get_last_error())
    process = None
    try:
        limits = ExtendedLimits()
        limits.basic.flags = 0x2000  # JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        if not kernel.SetInformationJobObject(job, 9, ctypes.byref(limits), ctypes.sizeof(limits)):
            raise ctypes.WinError(ctypes.get_last_error())
        process = subprocess.Popen(command, creationflags=0x00000004 | subprocess.CREATE_NO_WINDOW, **kwargs)
        handle = int(process._handle)
        if not kernel.AssignProcessToJobObject(job, handle):
            raise ctypes.WinError(ctypes.get_last_error())
        if resume(handle):
            raise OSError("Could not resume the Codex process.")
        def terminate():
            try:
                if not kernel.TerminateJobObject(job, 1):
                    raise ctypes.WinError(ctypes.get_last_error())
                wait_for_job(kernel, job)
            finally:
                kernel.CloseHandle(job)

        return process, terminate
    except BaseException:
        kernel.CloseHandle(job)
        if process is not None:
            process.kill()
            process.wait()
        raise


def stop_tree(process, close_job):
    if close_job:
        close_job()
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass

# SPDX-License-Identifier: AGPL-3.0-only
"""OS-enforced isolation for the opt-in model worker (DI-1 hardening on top of the RSS watchdog).

The worker stays opt-in. When it is used, this module adds kernel-enforced limits; it needs no privilege and
no dependency beyond the standard library and the already required ``psutil``. See
``docs/packs/document_intelligence/MODEL_WORKER_ISOLATION.md`` for the design, the measurements behind the
headroom constants and what remains an owner decision.

* **Windows: a Job Object.** The worker is created SUSPENDED, assigned to a fresh job and only then resumed, so
  no instruction of it (and no child such as the interpreter a venv launcher starts) runs outside the job.
  Limits: ``JOB_OBJECT_LIMIT_JOB_MEMORY`` (committed memory of every process in the job together),
  ``JOB_OBJECT_LIMIT_ACTIVE_PROCESS`` (while starting, two behind a venv launcher, else one; at READY sealed to
  the processes then alive, not counting the console host the kernel never limits, so the worker can start no
  helper process), ``KILL_ON_JOB_CLOSE`` (the parent closing
  its handle, or dying, ends the whole tree) and ``DIE_ON_UNHANDLED_EXCEPTION`` (no error-reporting dialog keeps
  a crashed worker alive).
* **Linux: limits the worker applies to itself first.** Before it reads the pin or imports the engine, the
  worker sets ``RLIMIT_AS`` (address space), ``RLIMIT_CORE = 0`` and ``RLIMIT_NOFILE``, reads them back, and
  installs a seccomp filter (``PR_SET_NO_NEW_PRIVS`` + ``SECCOMP_MODE_FILTER``, a fixed BPF program; no
  libseccomp) that makes ``socket()`` fail with ``EACCES`` for every address family and ``io_uring_setup`` fail
  with ``ENOSYS`` (an io_uring could open sockets without the syscall), and refuses ``ptrace``,
  ``process_vm_readv/writev`` and ``pidfd_open/getfd`` (no borrowing another process's sockets). Threads and
  children inherit it. A
  foreign-architecture or x32 system call kills the process. No Python code runs between fork and exec in the
  parent (no ``preexec_fn``). After READY the parent re-reads ``/proc/<pid>`` to confirm the address-space limit
  and the filter from outside.

Any step that cannot be completed is a closed failure (``MODEL_WORKER_ISOLATION_FAILED``): the worker never runs
with less isolation than was requested. Other platforms have no implementation and fail closed.
"""
from __future__ import annotations

import ctypes
import errno
import ntpath
import platform
import sys
from pathlib import Path
from typing import Any, Final

if sys.platform != "win32":
    import resource

ISOLATION_FAILED: Final = "MODEL_WORKER_ISOLATION_FAILED"
MEMORY_REFUSED: Final = "MODEL_WORKER_MEMORY_REFUSED"
MIB: Final = 1024 * 1024
# Measured headroom above the pin's max_ram_mb (see MODEL_WORKER_ISOLATION.md, "Headroom"). The RSS watchdog
# still enforces max_ram_mb itself; the kernel cap is the hard ceiling a fast spike cannot pass between polls.
# Measured: peak address space above peak RSS was 333-527 MiB (VM, real engines, MALLOC_ARENA_MAX=2), so 1024 MiB
# leaves about twice the largest measured gap. Windows: commit exceeded RSS by at most 281 MiB, so 512 MiB.
LINUX_ADDRESS_SPACE_HEADROOM_MB: Final = 1024  # ORT, tokenizers and glibc reserve address space they never touch
MALLOC_ARENAS: Final = 2  # without it the gap reached 1108 MiB and grows with the thread count
WINDOWS_COMMIT_HEADROOM_MB: Final = 512  # commit charge above the resident set (both processes of the job)
MAX_OPEN_FILES: Final = 256
VENV_LAUNCHER_PROCESSES: Final = 2  # a Windows venv ``python.exe`` is a launcher that starts the real interpreter


def starting_processes(executable: str = sys.executable,
                       base_executable: str | None = getattr(sys, "_base_executable", None)) -> int:
    """How many processes a worker started from ``executable`` legitimately consists of (Windows): two behind a
    venv launcher, else one. An undercount fails closed (the interpreter cannot start); never an overcount."""
    if base_executable and Path(base_executable).resolve() != Path(executable).resolve():
        return VENV_LAUNCHER_PROCESSES
    return 1
CONSOLE_HOSTS: Final = 1  # the conhost.exe of a console worker: counted in ActiveProcesses, never against the limit
CONSOLE_HOST_IMAGE: Final = "conhost.exe"


def expected_workers(root: int, interpreter: int, members: list[tuple[int, int, str, str]],
                     system_root: str) -> int | None:
    """How many processes the job may hold from now on, or ``None`` when the tree at READY is not exactly what a
    worker consists of (pure; Windows). ``members`` are (pid, parent pid, image name, image path) of the tree.

    Allowed: the worker itself, or a venv launcher plus the interpreter it started (its only child, the process
    that answered READY), and at most one console host whose image is the system's own conhost.exe. Anything
    else (a helper started before READY) fails closed."""
    pids = {pid for pid, _, _, _ in members}
    system_conhost = ntpath.join(system_root, "System32", CONSOLE_HOST_IMAGE).lower() if system_root else None
    consoles = [pid for pid, parent, name, image in members
                if name.lower() == CONSOLE_HOST_IMAGE and image.lower() == system_conhost and parent in pids]
    workers = {pid: parent for pid, parent, _, _ in members if pid not in consoles}
    if len(consoles) > CONSOLE_HOSTS:
        return None
    if interpreter == root and set(workers) == {root}:
        return 1
    if set(workers) == {root, interpreter} and workers[interpreter] == root:
        return VENV_LAUNCHER_PROCESSES
    return None

# -- Linux: seccomp -----------------------------------------------------------------------------------------
_PR_SET_NO_NEW_PRIVS: Final = 38
_PR_SET_SECCOMP: Final = 22
_SECCOMP_MODE_FILTER: Final = 2
_BPF_LD_W_ABS: Final = 0x20
_BPF_JEQ_K: Final = 0x15
_BPF_JGE_K: Final = 0x35
_BPF_RET_K: Final = 0x06
_RET_ALLOW: Final = 0x7FFF0000
_RET_ERRNO: Final = 0x00050000
_RET_KILL_PROCESS: Final = 0x80000000
_X32_SYSCALL_BIT: Final = 0x40000000
_NR_IO_URING_SETUP: Final = 425  # the same number on x86_64 and aarch64
_NR_PIDFD_OPEN: Final = 434
_NR_PIDFD_GETFD: Final = 438
_ARCHES: Final = {"x86_64": (0xC000003E, 41), "aarch64": (0xC00000B7, 198)}  # (AUDIT_ARCH_*, __NR_socket)
# Reaching another process's sockets: ptrace, process_vm_readv / process_vm_writev (per arch), pidfd_open/getfd.
_FOREIGN_PROCESS_CALLS: Final = {"x86_64": (101, 310, 311), "aarch64": (117, 270, 271)}


class IsolationError(RuntimeError):
    """Closed-code isolation failure (``MODEL_WORKER_ISOLATION_FAILED``)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.code = ISOLATION_FAILED
        self.reason = reason


def memory_cap_bytes(max_ram_mb: int, platform_name: str = sys.platform) -> int:
    """The kernel-enforced memory cap for a pin's ``max_ram_mb`` on ``platform_name``."""
    headroom = WINDOWS_COMMIT_HEADROOM_MB if platform_name == "win32" else LINUX_ADDRESS_SPACE_HEADROOM_MB
    return (max_ram_mb + headroom) * MIB


def supported(platform_name: str = sys.platform, machine: str | None = None) -> bool:
    if platform_name == "win32":
        return True
    return platform_name.startswith("linux") and (machine or platform.machine()) in _ARCHES


def seccomp_program(machine: str) -> list[tuple[int, int, int, int]]:
    """The BPF program as (code, jt, jf, k) rows. Offsets: ``nr`` at 0, ``arch`` at 4 of ``seccomp_data``."""
    if machine not in _ARCHES:
        raise IsolationError("SECCOMP_ARCH_UNSUPPORTED")
    arch, socket_nr = _ARCHES[machine]
    rows = [(_BPF_LD_W_ABS, 0, 0, 4), (_BPF_JEQ_K, 1, 0, arch), (_BPF_RET_K, 0, 0, _RET_KILL_PROCESS),
            (_BPF_LD_W_ABS, 0, 0, 0)]
    if machine == "x86_64":
        rows += [(_BPF_JGE_K, 0, 1, _X32_SYSCALL_BIT), (_BPF_RET_K, 0, 0, _RET_KILL_PROCESS)]
    denials = [(socket_nr, errno.EACCES), (_NR_IO_URING_SETUP, errno.ENOSYS),
               *((nr, errno.EPERM) for nr in (*_FOREIGN_PROCESS_CALLS[machine], _NR_PIDFD_OPEN, _NR_PIDFD_GETFD))]
    for nr, code in denials:
        rows += [(_BPF_JEQ_K, 0, 1, nr), (_BPF_RET_K, 0, 0, _RET_ERRNO | code)]
    return [*rows, (_BPF_RET_K, 0, 0, _RET_ALLOW)]


class _SockFilter(ctypes.Structure):
    _fields_ = [("code", ctypes.c_uint16), ("jt", ctypes.c_uint8), ("jf", ctypes.c_uint8), ("k", ctypes.c_uint32)]


class _SockFprog(ctypes.Structure):
    _fields_ = [("len", ctypes.c_ushort), ("filter", ctypes.POINTER(_SockFilter))]


def _status_field(text: str, name: str) -> str | None:
    for line in text.splitlines():
        key, _, value = line.partition(":")
        if key == name:
            return value.strip()
    return None


def _install_seccomp() -> None:
    status = Path("/proc/self/status").read_text(encoding="ascii", errors="replace")
    if _status_field(status, "Threads") != "1":
        raise IsolationError("SECCOMP_NOT_SINGLE_THREADED")  # a filter set by prctl binds only this thread
    rows = seccomp_program(platform.machine())
    table = (_SockFilter * len(rows))(*(_SockFilter(code, jt, jf, k) for code, jt, jf, k in rows))
    program = _SockFprog(len(rows), table)
    libc = ctypes.CDLL(None, use_errno=True)
    libc.prctl.argtypes = [ctypes.c_int, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong, ctypes.c_ulong]
    if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
        raise IsolationError("NO_NEW_PRIVS_REFUSED")
    if libc.prctl(_PR_SET_SECCOMP, _SECCOMP_MODE_FILTER, ctypes.addressof(program), 0, 0) != 0:
        raise IsolationError("SECCOMP_REFUSED")
    after = Path("/proc/self/status").read_text(encoding="ascii", errors="replace")
    if _status_field(after, "Seccomp") != "2" or _status_field(after, "NoNewPrivs") != "1":
        raise IsolationError("SECCOMP_NOT_ACTIVE")


def apply_worker_limits(address_space: int | None) -> None:
    """Run by a POSIX worker itself, first. Always: no core dumps and bounded descriptors. With an
    ``address_space`` (Linux only): ``RLIMIT_AS`` and the socket filter. Every limit is read back.
    Raises ``IsolationError``."""
    if sys.platform == "win32" or (address_space is not None and not supported()):
        raise IsolationError("PLATFORM_UNSUPPORTED")
    if sys.platform != "win32":  # repeated so the type checker narrows the POSIX-only ``resource`` module
        limits = [(resource.RLIMIT_CORE, 0), (resource.RLIMIT_NOFILE, MAX_OPEN_FILES)]
        if address_space is not None:
            limits.append((resource.RLIMIT_AS, address_space))
        try:
            for limit, value in limits:
                resource.setrlimit(limit, (value, value))
                if resource.getrlimit(limit) != (value, value):
                    raise IsolationError("RLIMIT_NOT_APPLIED")
            if address_space is not None:
                _install_seccomp()
        except (OSError, ValueError, AttributeError) as exc:
            raise IsolationError(type(exc).__name__) from None


def linux_isolation_observed(pid: int) -> tuple[int | None, str | None]:
    """(address-space limit in bytes, seccomp mode) of ``pid`` as the kernel reports them; ``None`` if unknown."""
    try:
        limits = Path(f"/proc/{pid}/limits").read_text(encoding="ascii", errors="replace")
        status = Path(f"/proc/{pid}/status").read_text(encoding="ascii", errors="replace")
    except OSError:
        return None, None
    address_space: int | None = None
    for line in limits.splitlines():
        if line.startswith("Max address space"):
            fields = line.split()
            address_space = int(fields[3]) if fields[3].isdigit() else None
    return address_space, _status_field(status, "Seccomp")


# -- Windows: Job Object ------------------------------------------------------------------------------------
CREATE_SUSPENDED: Final = 0x00000004
_JOB_MEMORY: Final = 0x00000200
_ACTIVE_PROCESS: Final = 0x00000008
_KILL_ON_JOB_CLOSE: Final = 0x00002000
_DIE_ON_UNHANDLED_EXCEPTION: Final = 0x00000400
_BASIC_ACCOUNTING: Final = 1
_EXTENDED_LIMIT: Final = 9
_PROCESS_TERMINATE: Final = 0x0001
_PROCESS_SET_QUOTA: Final = 0x0100
_PROCESS_QUERY_LIMITED: Final = 0x1000


class _BasicLimit(ctypes.Structure):
    _fields_ = [("PerProcessUserTimeLimit", ctypes.c_int64), ("PerJobUserTimeLimit", ctypes.c_int64),
                ("LimitFlags", ctypes.c_uint32), ("MinimumWorkingSetSize", ctypes.c_size_t),
                ("MaximumWorkingSetSize", ctypes.c_size_t), ("ActiveProcessLimit", ctypes.c_uint32),
                ("Affinity", ctypes.c_size_t), ("PriorityClass", ctypes.c_uint32),
                ("SchedulingClass", ctypes.c_uint32)]


class _IoCounters(ctypes.Structure):
    _fields_ = [(name, ctypes.c_uint64) for name in ("ReadOps", "WriteOps", "OtherOps", "ReadBytes", "WriteBytes",
                                                     "OtherBytes")]


class _ExtendedLimit(ctypes.Structure):
    _fields_ = [("BasicLimitInformation", _BasicLimit), ("IoInfo", _IoCounters),
                ("ProcessMemoryLimit", ctypes.c_size_t), ("JobMemoryLimit", ctypes.c_size_t),
                ("PeakProcessMemoryUsed", ctypes.c_size_t), ("PeakJobMemoryUsed", ctypes.c_size_t)]


class _BasicAccounting(ctypes.Structure):
    _fields_ = [("TotalUserTime", ctypes.c_int64), ("TotalKernelTime", ctypes.c_int64),
                ("ThisPeriodTotalUserTime", ctypes.c_int64), ("ThisPeriodTotalKernelTime", ctypes.c_int64),
                ("TotalPageFaultCount", ctypes.c_uint32), ("TotalProcesses", ctypes.c_uint32),
                ("ActiveProcesses", ctypes.c_uint32), ("TotalTerminatedProcesses", ctypes.c_uint32)]


def _kernel32() -> Any:
    if sys.platform != "win32":
        raise IsolationError("PLATFORM_UNSUPPORTED")
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    handle, flag = ctypes.c_void_p, ctypes.c_int
    kernel32.CreateJobObjectW.argtypes, kernel32.CreateJobObjectW.restype = [ctypes.c_void_p, ctypes.c_wchar_p], handle
    kernel32.SetInformationJobObject.argtypes = [handle, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
    kernel32.QueryInformationJobObject.argtypes = [handle, ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32,
                                                   ctypes.c_void_p]
    kernel32.AssignProcessToJobObject.argtypes = [handle, handle]
    kernel32.IsProcessInJob.argtypes = [handle, handle, ctypes.POINTER(flag)]
    kernel32.OpenProcess.argtypes, kernel32.OpenProcess.restype = [ctypes.c_uint32, flag, ctypes.c_uint32], handle
    kernel32.CloseHandle.argtypes = [handle]
    for name in ("SetInformationJobObject", "QueryInformationJobObject", "AssignProcessToJobObject",
                 "IsProcessInJob", "CloseHandle"):
        getattr(kernel32, name).restype = flag
    return kernel32


class WindowsJob:
    """One Job Object per worker; the parent holds the only handle (kill-on-close)."""

    def __init__(self, memory_bytes: int, processes: int) -> None:
        self._kernel32 = _kernel32()
        self.memory_bytes, self.processes = memory_bytes, processes
        self._handle: int | None = self._kernel32.CreateJobObjectW(None, None)
        if not self._handle:
            raise IsolationError("JOB_CREATE_FAILED")
        try:
            self._set_limits(processes)
        except IsolationError:
            self.close()
            raise

    def _set_limits(self, processes: int) -> None:
        info = _ExtendedLimit()
        info.BasicLimitInformation.LimitFlags = (_JOB_MEMORY | _ACTIVE_PROCESS | _KILL_ON_JOB_CLOSE
                                                 | _DIE_ON_UNHANDLED_EXCEPTION)
        info.BasicLimitInformation.ActiveProcessLimit = processes
        info.JobMemoryLimit = self.memory_bytes
        if not self._kernel32.SetInformationJobObject(self._handle, _EXTENDED_LIMIT, ctypes.byref(info),
                                                      ctypes.sizeof(info)):
            raise IsolationError("JOB_LIMITS_REFUSED")

    def _query(self, info_class: int, info: ctypes.Structure) -> None:
        if not self._kernel32.QueryInformationJobObject(self._handle, info_class, ctypes.byref(info),
                                                        ctypes.sizeof(info), None):
            raise IsolationError("JOB_QUERY_FAILED")

    def _open(self, pid: int, access: int) -> int:
        process = self._kernel32.OpenProcess(access, 0, pid)
        if not process:
            raise IsolationError("PROCESS_OPEN_FAILED")
        return int(process)

    def assign(self, pid: int) -> None:
        process = self._open(pid, _PROCESS_SET_QUOTA | _PROCESS_TERMINATE)
        try:
            if not self._kernel32.AssignProcessToJobObject(self._handle, process):
                raise IsolationError("JOB_ASSIGN_FAILED")
        finally:
            self._kernel32.CloseHandle(process)

    def contains(self, pid: int) -> bool:
        process = self._open(pid, _PROCESS_QUERY_LIMITED)
        try:
            result = ctypes.c_int(0)
            if not self._kernel32.IsProcessInJob(process, self._handle, ctypes.byref(result)):
                raise IsolationError("JOB_QUERY_FAILED")
            return bool(result.value)
        finally:
            self._kernel32.CloseHandle(process)

    def active_processes(self) -> int:
        info = _BasicAccounting()
        self._query(_BASIC_ACCOUNTING, info)
        return int(info.ActiveProcesses)

    def seal(self, workers: int) -> None:
        """Allow exactly the ``workers`` processes alive now (the console host is not limited by the kernel and
        is not counted here): the worker can start no further process."""
        active = self.active_processes()
        if not 1 <= workers <= self.processes or not workers <= active <= workers + CONSOLE_HOSTS:
            raise IsolationError("JOB_PROCESS_COUNT_UNEXPECTED")
        self._set_limits(workers)

    def limits(self) -> tuple[int, int, int]:
        """(job memory limit, active-process limit, peak job memory used) as the kernel reports them."""
        info = _ExtendedLimit()
        self._query(_EXTENDED_LIMIT, info)
        return (int(info.JobMemoryLimit), int(info.BasicLimitInformation.ActiveProcessLimit),
                int(info.PeakJobMemoryUsed))

    def close(self) -> None:
        """Closing the only handle kills every process still in the job (idempotent)."""
        handle, self._handle = self._handle, None
        if handle:
            self._kernel32.CloseHandle(handle)


def resume(pid: int) -> None:
    """Resume a process created suspended (``NtResumeProcess`` through psutil)."""
    import psutil  # type: ignore[import-untyped]  # declared runtime dependency; no stubs are pinned

    try:
        psutil.Process(pid).resume()
    except (psutil.Error, OSError):
        raise IsolationError("RESUME_FAILED") from None


def address_space_hard_limit() -> int | None:
    """The caller's own hard ``RLIMIT_AS`` (``None`` when unlimited or not POSIX): a child cannot exceed it."""
    if sys.platform != "win32":
        hard = resource.getrlimit(resource.RLIMIT_AS)[1]
        return None if hard == resource.RLIM_INFINITY else int(hard)
    return None


def effective_address_space(max_ram_mb: int) -> int:
    cap = memory_cap_bytes(max_ram_mb)
    inherited = address_space_hard_limit()
    return cap if inherited is None else min(cap, inherited)


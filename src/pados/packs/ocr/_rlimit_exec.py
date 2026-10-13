# SPDX-License-Identifier: AGPL-3.0-only
"""Resource limits applied by the child itself, and the launcher that execs an engine binary under them.

No Python code runs between ``fork`` and ``exec`` in the PADOS process: ``_isolated`` starts every
child with a plain fork+exec (``_posixsubprocess``, no ``preexec_fn``), so a multithreaded caller
cannot deadlock a half-forked child. The limits are applied afterwards, by the
freshly started child, as its first action:

* the PDF worker (``_pdf_worker.main``) and the engine-binding worker (``_binding_worker.main``) apply
  them before they read stdin or import anything else;
* an engine binary (Tesseract) is started through this module,
  ``python -I -B -m pados.packs.ocr._rlimit_exec <limits> -- <absolute binary> [args...]``, which
  re-validates the binary (``executable_refusal``), applies the limits to itself and then replaces
  itself with the binary (``os.execv``; limits survive exec).

The limits travel as ONE fixed-format argv token (``encode_limits``), parsed strictly. If the token is
malformed, the process is privileged, or any limit cannot be set and read back, the child exits
``EXIT_LIMITS_REFUSED`` without doing anything else. Limits: ``RLIMIT_AS`` (address space),
``RLIMIT_CPU`` (soft; hard = soft + 1), ``RLIMIT_FSIZE = 0`` (no file growth), ``RLIMIT_NOFILE``,
``RLIMIT_CORE = 0`` and, ALWAYS, ``RLIMIT_NPROC = 0``: the child can create no process and no thread
(Linux counts every process AND thread of the real uid against it, system-wide, so 0 is the only
value that bounds one child). There is no switch to relax it: an engine that needs a thread or a
process fails closed.

``RLIMIT_NPROC`` is not enforced for a privileged process (real uid 0, or ``CAP_SYS_RESOURCE`` /
``CAP_SYS_ADMIN``; any other retained capability is refused too), so a privileged process never launches or becomes an isolated child
(``privilege_refusal``; checked by the parent before spawning and again by the child). On Linux the
child also sets ``PR_SET_NO_NEW_PRIVS`` before it execs anything, so neither a setuid/setgid binary
nor file capabilities can raise its privileges later.

Standard library only. POSIX only: on Windows ``apply_limits`` refuses (no Job Object yet).
"""
from __future__ import annotations

import errno
import os
import re
import stat
import sys
from collections.abc import Sequence
from dataclasses import dataclass

if sys.platform != "win32":
    import resource

RLIMIT_EXEC_MODULE = "pados.packs.ocr._rlimit_exec"
EXIT_LIMITS_REFUSED = 117  # malformed limits, a privileged process, or a limit could not be applied
EXIT_EXEC_FAILED = 118  # the launcher could not exec the configured binary
EXIT_EXECUTABLE_REFUSED = 119  # the configured binary failed re-validation in the launcher
LIMITS_PREFIX = "pados-rlimits/2:"
_TOKEN = re.compile(r"pados-rlimits/2:as=([0-9]{1,12}),cpu=([0-9]{1,4}),fsize=0,nofile=([0-9]{1,5}),core=0,"
                    r"nproc=0")
_MAX_ARGS = 64
_MAX_ARG_CHARS = 4096
CAP_SYS_ADMIN = 21
CAP_SYS_RESOURCE = 24
_CAPABILITY_SETS = ("CapEff", "CapPrm", "CapAmb")  # effective, permitted (can be raised), ambient (kept on exec)
_PR_SET_NO_NEW_PRIVS, _PR_GET_NO_NEW_PRIVS = 38, 39


@dataclass(frozen=True)
class Limits:
    """Per-child limits. ``RLIMIT_NPROC = 0`` is not a field: it is always applied."""

    memory_bytes: int
    cpu_seconds: int
    max_open_files: int


def encode_limits(limits: Limits) -> str:
    """The one argv token a child parses (fixed field order; nothing optional)."""
    return (f"{LIMITS_PREFIX}as={limits.memory_bytes},cpu={limits.cpu_seconds},fsize=0,"
            f"nofile={limits.max_open_files},core=0,nproc=0")


def parse_limits(value: str) -> Limits | None:
    match = _TOKEN.fullmatch(value)
    if match is None:
        return None
    memory, cpu, nofile = match.groups()
    limits = Limits(int(memory), int(cpu), int(nofile))
    if limits.memory_bytes < 1 or limits.cpu_seconds < 1 or limits.max_open_files < 1:
        return None
    return limits


# --- privilege: RLIMIT_NPROC does not hold for a privileged process ------------------------------------------

def privilege_refusal_for(uid: int, euid: int, status: str | None, *, linux: bool) -> str | None:
    """Why a process with these ids and this ``/proc/self/status`` text cannot be contained (pure).

    Real or effective uid 0 is refused everywhere. On Linux the capability sets must be readable and the
    effective, permitted and ambient sets must be EMPTY: ``CAP_SYS_RESOURCE`` (bit 24) and ``CAP_SYS_ADMIN``
    (bit 21) skip the ``RLIMIT_NPROC`` check directly, and any other retained capability (for example
    ``CAP_SETUID``, which can change the uid to 0) can defeat the containment premise (CR-15d OCR-16,
    integrator mitigation after the third fixer cycle: refuse conservatively rather than enumerate).
    """
    if uid == 0:
        return "PRIVILEGED_UID"
    if euid == 0:
        return "PRIVILEGED_EUID"
    if not linux:
        return None
    if status is None:
        return "CAPABILITIES_UNREADABLE"
    found: dict[str, int] = {}
    for line in status.splitlines():
        name, _, value = line.partition(":")
        if name in _CAPABILITY_SETS:
            try:
                found[name] = int(value.strip(), 16)
            except ValueError:
                return "CAPABILITIES_UNREADABLE"
    if set(found) != set(_CAPABILITY_SETS):
        return "CAPABILITIES_UNREADABLE"
    held = 0
    for bits in found.values():
        held |= bits
    if held >> CAP_SYS_RESOURCE & 1:
        return "CAP_SYS_RESOURCE"
    if held >> CAP_SYS_ADMIN & 1:
        return "CAP_SYS_ADMIN"
    if held:
        return "CAPABILITIES_HELD"
    return None


def _identity() -> tuple[int, int]:
    """(real uid, effective uid) of this process (a test seam)."""
    if sys.platform == "win32":
        return -1, -1
    return os.getuid(), os.geteuid()


def _proc_status() -> str | None:
    """``/proc/self/status`` text, or ``None`` when it cannot be read (a test seam)."""
    try:
        with open("/proc/self/status", encoding="ascii", errors="replace") as handle:
            return handle.read(65536)
    except OSError:
        return None


def privilege_refusal() -> str | None:
    """Why THIS process cannot launch or be a contained child, or ``None`` (POSIX; Windows refuses)."""
    if sys.platform == "win32":
        return "WINDOWS_NO_JOB_OBJECT"
    uid, euid = _identity()
    linux = sys.platform.startswith("linux")
    return privilege_refusal_for(uid, euid, _proc_status() if linux else None, linux=linux)


# --- the executable an engine launch execs ---------------------------------------------------------------------

if sys.platform == "win32":

    def executable_refusal(path: str) -> str | None:
        """Nothing is ever launched on Windows (fail closed)."""
        return "EXECUTABLE_PLATFORM_UNSUPPORTED"

else:

    def _file_capabilities(path: str) -> str | None:
        if not sys.platform.startswith("linux"):
            return None
        try:
            os.getxattr(path, "security.capability", follow_symlinks=False)
        except OSError as exc:
            if exc.errno in {errno.ENODATA, errno.ENOTSUP, errno.EOPNOTSUPP}:
                return None
            return "EXECUTABLE_UNREADABLE"
        return "EXECUTABLE_FILE_CAPABILITIES"

    def executable_refusal(path: str) -> str | None:
        """Why ``path`` may not be exec'd, or ``None``.

        It must be an absolute, normalized path whose every component is a real directory (no
        symlink anywhere), naming a regular, executable file that is not setuid/setgid and carries no
        file capabilities. The final component is opened with ``O_NOFOLLOW`` (``O_PATH`` on Linux,
        else non-blocking read-only, so a FIFO swapped in cannot block) and must be the same inode.
        """
        if not path.startswith("/") or os.path.normpath(path) != path or "\x00" in path or path == "/":
            return "EXECUTABLE_PATH_INVALID"
        try:
            prefix = ""
            parts = path.split("/")[1:]
            info = os.lstat("/")
            for index, part in enumerate(parts):
                prefix += "/" + part
                info = os.lstat(prefix)
                if stat.S_ISLNK(info.st_mode):
                    return "EXECUTABLE_PATH_SYMLINK"
                if index < len(parts) - 1 and not stat.S_ISDIR(info.st_mode):
                    return "EXECUTABLE_PATH_INVALID"
            if not stat.S_ISREG(info.st_mode):
                return "EXECUTABLE_NOT_REGULAR"
            if info.st_mode & (stat.S_ISUID | stat.S_ISGID):
                return "EXECUTABLE_SETID"
            if not os.access(path, os.X_OK):
                return "EXECUTABLE_NOT_EXECUTABLE"
            flags = os.O_NOFOLLOW | os.O_CLOEXEC | (getattr(os, "O_PATH", 0) or (os.O_RDONLY | os.O_NONBLOCK))
            handle = os.open(path, flags)
            try:
                opened = os.fstat(handle)
            finally:
                os.close(handle)
        except OSError:
            return "EXECUTABLE_UNREADABLE"
        if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
            return "EXECUTABLE_CHANGED"
        return _file_capabilities(path)


def interpreter_refusal(executable: str) -> str | None:
    """The PADOS interpreter that every child starts as (``sys.executable``).

    A virtual environment's ``bin/python`` is a symlink by design and must stay the exec target (it
    selects the environment), so its RESOLVED file is validated with ``executable_refusal``.
    """
    if not os.path.isabs(executable):
        return "EXECUTABLE_PATH_INVALID"
    return executable_refusal(os.path.realpath(executable))


# --- applying the limits (in the child) ------------------------------------------------------------------------

if sys.platform == "win32":

    def apply_limits(limits: Limits) -> bool:
        """No ``resource`` module and no Job Object yet: limits can never be applied (fail closed)."""
        return False

    def lock_privileges() -> bool:
        return False

else:

    def apply_limits(limits: Limits) -> bool:
        """Set every limit on this process and read each one back; ``False`` if anything did not hold."""
        targets = [(resource.RLIMIT_AS, limits.memory_bytes, limits.memory_bytes),
                   (resource.RLIMIT_CPU, limits.cpu_seconds, limits.cpu_seconds + 1),
                   (resource.RLIMIT_FSIZE, 0, 0),
                   (resource.RLIMIT_NOFILE, limits.max_open_files, limits.max_open_files),
                   (resource.RLIMIT_CORE, 0, 0),
                   (resource.RLIMIT_NPROC, 0, 0)]
        try:
            for kind, soft, hard in targets:
                _, current_hard = resource.getrlimit(kind)
                if current_hard != resource.RLIM_INFINITY:  # never above the inherited hard limit
                    soft, hard = min(soft, current_hard), min(hard, current_hard)
                resource.setrlimit(kind, (soft, hard))
                if resource.getrlimit(kind) != (soft, hard):
                    return False
        except (OSError, ValueError):
            return False
        return True

    def lock_privileges() -> bool:
        """Linux: ``PR_SET_NO_NEW_PRIVS`` (irreversible; survives exec), read back. Elsewhere: True."""
        if not sys.platform.startswith("linux"):
            return True
        import ctypes  # in the child only; never loaded by the PADOS process for this

        try:
            libc = ctypes.CDLL(None, use_errno=True)
            if libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0) != 0:
                return False
            return bool(libc.prctl(_PR_GET_NO_NEW_PRIVS, 0, 0, 0, 0) == 1)
        except (OSError, AttributeError):
            return False


def enter_limits(argv: Sequence[str]) -> bool:
    """A child's first action: ``argv`` must be exactly one limits token, the process must be
    unprivileged, every limit must apply, and (Linux) no-new-privileges must be set."""
    if len(argv) != 1:
        return False
    limits = parse_limits(argv[0])
    return (limits is not None and privilege_refusal() is None and apply_limits(limits)
            and lock_privileges())


def launcher_argv(limits: Limits, command: Sequence[str]) -> list[str]:
    """``sys.executable -I -B -m <this module> <limits> -- <absolute binary> [args...]``."""
    return [sys.executable, "-I", "-B", "-m", RLIMIT_EXEC_MODULE, encode_limits(limits), "--", *command]


def main(argv: Sequence[str]) -> int:
    if (len(argv) < 3 or argv[1] != "--" or len(argv) > _MAX_ARGS
            or any(len(arg) > _MAX_ARG_CHARS or "\x00" in arg for arg in argv)):
        return EXIT_LIMITS_REFUSED
    binary, arguments = argv[2], list(argv[3:])
    if not os.path.isabs(binary) or parse_limits(argv[0]) is None:
        return EXIT_LIMITS_REFUSED
    if executable_refusal(binary) is not None:  # re-validated in the child, right before the exec
        return EXIT_EXECUTABLE_REFUSED
    if not enter_limits(argv[:1]):
        return EXIT_LIMITS_REFUSED
    try:
        os.execv(binary, [binary, *arguments])  # replaces this process; the limits stay in force
    except OSError:
        return EXIT_EXEC_FAILED
    return EXIT_EXEC_FAILED  # pragma: no cover - execv does not return


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

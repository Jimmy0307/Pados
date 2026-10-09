# SPDX-License-Identifier: AGPL-3.0-only
"""Small durable-file helpers for PADOS-owned operational stores.

Used by the connectivity snapshot/history and the notification outbox. Writes
are atomic (temp file + fsync + replace, mode 0600) and every record passes the
shared data-minimization guard against an explicit top-level key schema first.
``store_lock`` is a cross-process exclusive lock on a sidecar ``.lock`` file
(``fcntl.flock`` on POSIX, ``msvcrt.locking`` on Windows) plus an in-process
lock, so reload + modify + persist is one transaction even when several
processes share a store. These files are operational projections only; they
never hold semantic state.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import sys
import threading
import time
from collections.abc import Iterable, Iterator, Mapping
from contextlib import contextmanager
from pathlib import Path
from typing import Any

if sys.platform == "win32":
    import msvcrt
else:
    import fcntl

from pados.lifecycle.minimization import DataMinimizationViolation, assert_minimized

_KEY = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,63}")
STORE_LOCK_TIMEOUT = 10.0
_PROCESS_LOCKS: dict[str, threading.Lock] = {}
_PROCESS_LOCKS_GUARD = threading.Lock()


class StoreLockTimeout(OSError):
    code = "OPERATIONAL_STORE_LOCK_TIMEOUT"


def _process_lock(path: Path) -> threading.Lock:
    key = os.path.normcase(os.path.abspath(path))
    with _PROCESS_LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(key, threading.Lock())


def _try_lock(descriptor: int) -> bool:
    try:
        if sys.platform == "win32":
            msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
        else:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        return False
    return True


def _unlock(descriptor: int) -> None:
    if sys.platform == "win32":
        os.lseek(descriptor, 0, os.SEEK_SET)
        msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
    else:
        fcntl.flock(descriptor, fcntl.LOCK_UN)


@contextmanager
def store_lock(path: Path, *, timeout: float = STORE_LOCK_TIMEOUT) -> Iterator[None]:
    """Exclusive cross-process lock for the store at ``path`` (bounded wait)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    local = _process_lock(path)
    if not local.acquire(timeout=max(timeout, 0.0)):
        raise StoreLockTimeout(StoreLockTimeout.code)
    try:
        flags = os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
        descriptor = os.open(path.with_name(f"{path.name}.lock"), flags, 0o600)
        try:
            while not _try_lock(descriptor):
                if time.monotonic() >= deadline:
                    raise StoreLockTimeout(StoreLockTimeout.code)
                time.sleep(0.01)
            try:
                yield
            finally:
                _unlock(descriptor)
        finally:
            os.close(descriptor)
    finally:
        local.release()


def assert_record_schema(record: Mapping[str, Any], allowed_keys: Iterable[str]) -> None:
    """Fail closed unless ``record`` uses only the declared ASCII-identifier keys."""
    allowed = frozenset(allowed_keys)
    for key in record:
        if not isinstance(key, str) or not _KEY.fullmatch(key):
            raise DataMinimizationViolation("RECORD_KEY_NOT_IDENTIFIER", str(key))
        if key not in allowed:
            raise DataMinimizationViolation("RECORD_KEY_NOT_IN_SCHEMA", key)
    assert_minimized(record, allowed_keys=allowed)


def atomic_write_json(path: Path, data: Any) -> None:
    """Replace ``path`` atomically with ``data`` as JSON (owner-only permissions)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise OSError("OPERATIONAL_STORE_SYMLINK_REFUSED")
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_BINARY", 0)
    descriptor = os.open(temporary, flags, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(json.dumps(data, sort_keys=True).encode("utf-8"))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    if os.name != "nt":
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def read_json(path: Path) -> Any | None:
    """Read a store file; a missing or corrupt file reads as ``None`` (never invented)."""
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None

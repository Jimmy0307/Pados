# SPDX-License-Identifier: AGPL-3.0-only
"""Process-safe exclusive file locks and durable directory creation for Lifecycle.

``exclusive_lock`` holds ``fcntl.flock`` (POSIX) or ``msvcrt.locking`` (Windows)
on a sidecar lock file for the duration of a block. Locks coordinate Lifecycle
writers (tombstone ledger, payload purge, recovery) across processes; they do
not bind writers that never take them. ``ensure_durable_dir`` creates missing
directories one level at a time and fsyncs each new directory's parent, so a
crash cannot lose a freshly created ledger or lock directory entry
(directory fsync is a no-op on Windows: ACCEPTED_RISK_WINDOWS).
"""
from __future__ import annotations

import os
import stat
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from pados.lifecycle.fswalk import FsPolicyError, fsync_directory, is_link

if sys.platform == "win32":
    import msvcrt

    def _lock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_LOCK, 1)

    def _unlock(fd: int) -> None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)

    _LOCK_FLAGS = os.O_RDWR | os.O_CREAT | os.O_BINARY
else:
    import fcntl

    def _lock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_EX)

    def _unlock(fd: int) -> None:
        fcntl.flock(fd, fcntl.LOCK_UN)

    _LOCK_FLAGS = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC


def ensure_durable_dir(path: Path) -> None:
    """Create ``path`` and missing ancestors, fsyncing each new entry's parent; refuse links."""
    target = Path(path)
    missing: list[Path] = []
    current = target
    while True:
        try:
            info = os.lstat(current)
        except FileNotFoundError:
            missing.append(current)
            if current.parent == current:
                break
            current = current.parent
            continue
        if is_link(info) and current == target:
            raise FsPolicyError("LOCK_OR_LEDGER_DIR_IS_LINK")
        if not stat.S_ISDIR(os.stat(current).st_mode):
            raise FsPolicyError("NOT_A_DIRECTORY")
        break
    for directory in reversed(missing):
        try:
            os.mkdir(directory, 0o700)
        except FileExistsError:
            pass
        fsync_directory(directory.parent)


@contextmanager
def exclusive_lock(lock_path: Path) -> Iterator[None]:
    """Hold an exclusive, process-safe lock on ``lock_path`` (created if absent, never via a link)."""
    fd = os.open(lock_path, _LOCK_FLAGS, 0o600)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise FsPolicyError("LOCK_FILE_NOT_REGULAR")
        _lock(fd)
        try:
            yield
        finally:
            _unlock(fd)
    finally:
        os.close(fd)

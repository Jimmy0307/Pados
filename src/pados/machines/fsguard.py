# SPDX-License-Identifier: AGPL-3.0-only
"""Owner-only file access for Machine Connection authority files.

The grant ledger, its head, the operator registry and the credential policy are
opened descriptor-based (``O_NOFOLLOW``) and checked on the opened descriptor:
a regular file owned by the current user with no group/other permission bits,
inside a parent directory that is not a link and not group/world-writable;
with a trusted ``root`` (the inventory directory) every ancestor up to it is
checked the same way, so a swapped-in symlinked or writable ancestor is refused.
Every failure is a code-only ``MachineError``. On Windows ownership and mode
bits are not POSIX-shaped; only the link and regular-file checks apply there
(ACCEPTED_RISK_WINDOWS: rely on the profile directory ACL).
"""
from __future__ import annotations

import json
import os
import secrets
import stat
import sys
from pathlib import Path
from typing import Any

from pados.lifecycle.fswalk import fsync_directory
from pados.machines.errors import MachineError

MAX_AUTHORITY_FILE_BYTES = 4 * 1024 * 1024
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_BINARY", 0)


def _ancestors(path: Path, root: Path | None) -> list[Path]:
    """``path.parent`` and every ancestor up to ``root`` (inclusive), without following links."""
    parent = Path(os.path.abspath(path)).parent
    if root is None:
        return [parent]
    top = Path(os.path.abspath(root))
    if parent != top and top not in parent.parents:
        return []
    chain = [parent]
    while chain[-1] != top:
        chain.append(chain[-1].parent)
    return chain


def check_parent(path: Path, prefix: str, root: Path | None = None) -> None:
    """The parent and every ancestor up to the trusted ``root``: real directories (no links), owned by
    the current user or root, not group/world-writable."""
    chain = _ancestors(path, root)
    if not chain:
        raise MachineError(f"{prefix}_OUTSIDE_ROOT")
    for directory in chain:
        try:
            info = os.lstat(directory)
        except OSError:
            raise MachineError(f"{prefix}_PARENT_INSECURE") from None
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            raise MachineError(f"{prefix}_PARENT_INSECURE")
        if sys.platform != "win32" and (info.st_mode & (stat.S_IWGRP | stat.S_IWOTH)
                                        or info.st_uid not in {os.getuid(), 0}):
            raise MachineError(f"{prefix}_PARENT_INSECURE")


def check_descriptor(descriptor: int, prefix: str) -> os.stat_result:
    info = os.fstat(descriptor)
    if not stat.S_ISREG(info.st_mode):
        raise MachineError(f"{prefix}_NOT_REGULAR")
    if sys.platform != "win32":
        if info.st_uid != os.getuid():
            raise MachineError(f"{prefix}_OWNER_INVALID")
        if info.st_mode & 0o077:
            raise MachineError(f"{prefix}_PERMISSIONS_TOO_OPEN")
    return info


def open_owner_only(path: Path, flags: int, prefix: str, root: Path | None = None) -> int:
    """Open ``path`` with ``flags`` (no link following) and verify it is an owner-only regular file."""
    check_parent(path, prefix, root)
    try:
        descriptor = os.open(path, flags | _NOFOLLOW, 0o600)
    except FileNotFoundError:
        raise
    except OSError:
        raise MachineError(f"{prefix}_OPEN_REFUSED") from None
    try:
        check_descriptor(descriptor, prefix)
    except BaseException:
        os.close(descriptor)
        raise
    return descriptor


def read_owner_only(path: Path, prefix: str, root: Path | None = None) -> bytes | None:
    """Bytes of an owner-only file, ``None`` when it does not exist."""
    try:
        descriptor = open_owner_only(path, os.O_RDONLY, prefix, root)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor, "rb") as handle:
        data = handle.read(MAX_AUTHORITY_FILE_BYTES + 1)
    if len(data) > MAX_AUTHORITY_FILE_BYTES:
        raise MachineError(f"{prefix}_TOO_LARGE")
    return data


def read_owner_only_json(path: Path, prefix: str, root: Path | None = None) -> Any:
    data = read_owner_only(path, prefix, root)
    if data is None:
        raise MachineError(f"{prefix}_MISSING")
    try:
        return json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise MachineError(f"{prefix}_INVALID") from None


def replace_owner_only(path: Path, content: bytes, prefix: str, root: Path | None = None) -> None:
    """Atomically replace ``path`` with ``content`` (temp file 0600, fsync, rename, directory fsync)."""
    check_parent(path, prefix, root)
    temporary = path.with_name(f".{path.name}.{secrets.token_hex(6)}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    fsync_directory(path.parent)

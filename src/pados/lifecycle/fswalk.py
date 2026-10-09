# SPDX-License-Identifier: AGPL-3.0-only
"""Descriptor-pinned, link-refusing filesystem access for accounting and purge.

On POSIX every directory is opened with ``O_DIRECTORY | O_NOFOLLOW`` relative to
its already-open parent, and every stat, open, rename and unlink is relative to
a pinned directory descriptor with ``follow_symlinks=False``. Swapping a
directory for a link after it was checked therefore cannot redirect a walk or
a deletion outside the root. A configured root whose final component is itself
a link is refused (``ROOT_IS_SYMLINK``); its parent components are trusted
operator configuration.

Windows lacks descriptor-relative ``open``/``unlink``; there a path fallback
re-validates the directory's real path and the file identity immediately
before each mutation. The remaining check-to-use window is ACCEPTED_RISK_WINDOWS
(single-user host). Entry budgets are enforced while enumerating, so a huge
directory cannot exhaust memory. Nothing here reads file contents except
through ``open_file``/``read_capped``, which purge uses for PADOS-owned records
and payload hashes; accounting never calls them.
"""
from __future__ import annotations

import errno
import os
import stat
import sys
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_MAX_ENTRIES = 100_000
MAX_DEPTH = 64

if sys.platform == "win32":
    FD_SAFE = False
    _DIR_FLAGS = 0
    _FILE_FLAGS = os.O_RDONLY | os.O_BINARY
else:
    FD_SAFE = {os.open, os.stat, os.unlink, os.rename, os.rmdir, os.link} <= os.supports_dir_fd \
        and os.scandir in os.supports_fd and os.link in os.supports_follow_symlinks
    _DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    _FILE_FLAGS = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK


class FsPolicyError(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class FileIdentity:
    dev: int
    ino: int
    size: int
    mtime_ns: int

    @classmethod
    def of(cls, st: os.stat_result) -> FileIdentity:
        return cls(st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)

    def to_dict(self) -> dict[str, int]:
        return {"dev": self.dev, "ino": self.ino, "size": self.size, "mtime_ns": self.mtime_ns}


@dataclass(frozen=True)
class FileEntry:
    relpath: str  # POSIX-style path relative to the walked root
    identity: FileIdentity

    @property
    def size_bytes(self) -> int:
        return self.identity.size

    @property
    def mtime_ns(self) -> int:
        return self.identity.mtime_ns


@dataclass
class WalkStats:
    max_entries: int = DEFAULT_MAX_ENTRIES
    entries: int = 0
    truncated: bool = False
    symlinks_skipped: int = 0
    special_skipped: int = 0
    unreadable: int = 0


def real_root(root: Path) -> Path:
    """Real path of a configured root, for configuration overlap checks only."""
    return Path(os.path.realpath(root))


def is_within(path: Path, root: Path) -> bool:
    return path == root or root in path.parents


def overlaps(left: Path, right: Path) -> bool:
    a, b = real_root(left), real_root(right)
    return is_within(a, b) or is_within(b, a)


def is_link(st: os.stat_result) -> bool:
    """Symbolic link, or any Windows reparse point (junction, mount point)."""
    return stat.S_ISLNK(st.st_mode) or bool(getattr(st, "st_reparse_tag", 0))


def _check_name(name: str) -> str:
    if not name or name in {".", ".."} or "/" in name or "\\" in name or "\x00" in name:
        raise FsPolicyError("PATH_COMPONENT_INVALID")
    return name


class PinnedDir:
    """A directory held open (POSIX) or re-validated by real path (fallback)."""

    def close(self) -> None:
        raise NotImplementedError

    def __enter__(self) -> PinnedDir:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def identity(self) -> tuple[int, int]:
        raise NotImplementedError

    def scan(self, stats: WalkStats) -> list[tuple[str, os.stat_result]]:
        """Entries (no-follow stat), stopping once ``stats.max_entries`` is reached."""
        raise NotImplementedError

    def child(self, name: str) -> PinnedDir:
        raise NotImplementedError

    def lstat(self, name: str) -> os.stat_result | None:
        raise NotImplementedError

    def open_file(self, name: str) -> int:
        """Read-only descriptor of a regular, non-link file."""
        raise NotImplementedError

    def unlink(self, name: str) -> None:
        raise NotImplementedError

    def unlink_verified(self, name: str, expected: FileIdentity) -> bool:
        """Unlink ``name`` only if it is still the regular file ``expected``; False otherwise.

        POSIX: the member is opened with ``O_NOFOLLOW | O_NONBLOCK``, its ``fstat`` identity
        must equal ``expected``, and the name is unlinked only if a no-follow ``stat`` of the
        name still equals that open inode (the narrowest window without kernel support for
        unlink-by-descriptor). The remaining window is ACCEPTED_RISK: a swap must happen
        inside this already pinned PADOS-owned directory, so deletion stays inside the root.
        """
        raise NotImplementedError

    def link(self, src: str, dst: str) -> None:
        """No-clobber hard link inside this directory; ``FileExistsError`` if ``dst`` exists."""
        raise NotImplementedError

    def replace(self, src: str, dst: str) -> None:
        raise NotImplementedError

    def rmdir(self, name: str) -> None:
        raise NotImplementedError

    def fsync(self) -> None:
        raise NotImplementedError


def _regular_fd(fd: int) -> int:
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise FsPolicyError("NOT_REGULAR_FILE")
    return fd


def _bounded_scan(iterator: Iterator[os.DirEntry[str]], stats: WalkStats,
                  stat_entry: Any) -> list[tuple[str, os.stat_result]]:
    found: list[tuple[str, os.stat_result]] = []
    for entry in iterator:
        if stats.entries >= stats.max_entries:
            stats.truncated = True
            break
        stats.entries += 1
        try:
            found.append((entry.name, stat_entry(entry)))
        except FileNotFoundError:
            continue
        except OSError:
            stats.unreadable += 1
    return sorted(found, key=lambda item: item[0])


class _FdDir(PinnedDir):
    def __init__(self, fd: int) -> None:
        self.fd = fd

    def close(self) -> None:
        if self.fd >= 0:
            os.close(self.fd)
            self.fd = -1

    def identity(self) -> tuple[int, int]:
        st = os.fstat(self.fd)
        return st.st_dev, st.st_ino

    def scan(self, stats: WalkStats) -> list[tuple[str, os.stat_result]]:
        with os.scandir(self.fd) as iterator:
            return _bounded_scan(iterator, stats,
                                 lambda e: os.stat(e.name, dir_fd=self.fd, follow_symlinks=False))

    def child(self, name: str) -> PinnedDir:
        try:
            return _FdDir(os.open(_check_name(name), _DIR_FLAGS, dir_fd=self.fd))
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.ENOTDIR, errno.EMLINK}:
                raise FsPolicyError("LINK_OR_NOT_DIRECTORY") from None
            raise

    def lstat(self, name: str) -> os.stat_result | None:
        try:
            return os.stat(_check_name(name), dir_fd=self.fd, follow_symlinks=False)
        except FileNotFoundError:
            return None

    def open_file(self, name: str) -> int:
        try:
            return _regular_fd(os.open(_check_name(name), _FILE_FLAGS, dir_fd=self.fd))
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.EMLINK}:
                raise FsPolicyError("LINK_REFUSED") from None
            raise

    def unlink(self, name: str) -> None:
        os.unlink(_check_name(name), dir_fd=self.fd)

    def unlink_verified(self, name: str, expected: FileIdentity) -> bool:
        try:
            fd = os.open(_check_name(name), _FILE_FLAGS, dir_fd=self.fd)
        except FileNotFoundError:
            return False
        except OSError as exc:
            if exc.errno in {errno.ELOOP, errno.EMLINK}:
                return False
            raise
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or FileIdentity.of(opened) != expected:
                return False
            current = os.stat(name, dir_fd=self.fd, follow_symlinks=False)
            if is_link(current) or FileIdentity.of(current) != FileIdentity.of(opened):
                return False
            self.unlink(name)
            return True
        finally:
            os.close(fd)

    def link(self, src: str, dst: str) -> None:
        os.link(_check_name(src), _check_name(dst), src_dir_fd=self.fd, dst_dir_fd=self.fd,
                follow_symlinks=False)

    def replace(self, src: str, dst: str) -> None:
        os.rename(_check_name(src), _check_name(dst), src_dir_fd=self.fd, dst_dir_fd=self.fd)

    def rmdir(self, name: str) -> None:
        os.rmdir(_check_name(name), dir_fd=self.fd)

    def fsync(self) -> None:
        os.fsync(self.fd)


class _PathDir(PinnedDir):
    """Fallback (Windows): re-validates the real path before every operation."""

    def __init__(self, path: Path, real: str) -> None:
        self.path = path
        self.real = real

    def _checked(self, name: str | None = None) -> Path:
        if os.path.realpath(self.path) != self.real:
            raise FsPolicyError("DIRECTORY_MOVED")
        return self.path if name is None else self.path / _check_name(name)

    def close(self) -> None:
        return None

    def identity(self) -> tuple[int, int]:
        st = os.lstat(self._checked())
        return st.st_dev, st.st_ino

    def scan(self, stats: WalkStats) -> list[tuple[str, os.stat_result]]:
        with os.scandir(self._checked()) as iterator:
            return _bounded_scan(iterator, stats, lambda e: os.lstat(e.path))

    def child(self, name: str) -> PinnedDir:
        path = self._checked(name)
        st = os.lstat(path)
        real = os.path.join(self.real, name)
        if is_link(st) or not stat.S_ISDIR(st.st_mode) or os.path.realpath(path) != real:
            raise FsPolicyError("LINK_OR_NOT_DIRECTORY")
        return _PathDir(path, real)

    def lstat(self, name: str) -> os.stat_result | None:
        try:
            return os.lstat(self._checked(name))
        except FileNotFoundError:
            return None

    def open_file(self, name: str) -> int:
        path = self._checked(name)
        before = os.lstat(path)
        if is_link(before) or not stat.S_ISREG(before.st_mode):
            raise FsPolicyError("LINK_REFUSED")
        fd = os.open(path, _FILE_FLAGS)
        after = os.fstat(fd)
        if (after.st_dev, after.st_ino) != (before.st_dev, before.st_ino):
            os.close(fd)
            raise FsPolicyError("FILE_REPLACED")
        return _regular_fd(fd)

    def unlink(self, name: str) -> None:
        os.unlink(self._checked(name))

    def unlink_verified(self, name: str, expected: FileIdentity) -> bool:
        current = self.lstat(name)  # ACCEPTED_RISK_WINDOWS: path re-validated just before unlink
        if current is None or is_link(current) or not stat.S_ISREG(current.st_mode) \
                or FileIdentity.of(current) != expected:
            return False
        self.unlink(name)
        return True

    def link(self, src: str, dst: str) -> None:
        source = self._checked(src)
        if is_link(os.lstat(source)):
            raise FsPolicyError("LINK_REFUSED")
        os.link(source, self._checked(dst))

    def replace(self, src: str, dst: str) -> None:
        os.replace(self._checked(src), self._checked(dst))

    def rmdir(self, name: str) -> None:
        os.rmdir(self._checked(name))

    def fsync(self) -> None:
        fsync_directory(self._checked())


def fsync_directory(path: Path) -> None:
    """Make directory entries durable (no-op on Windows, which cannot open directories)."""
    if sys.platform == "win32":
        return
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def open_root(root: Path) -> PinnedDir:
    """Pin a configured root directory; a root that is itself a link is refused."""
    path = Path(root)
    st = os.lstat(path)
    if is_link(st):
        raise FsPolicyError("ROOT_IS_SYMLINK")
    if not stat.S_ISDIR(st.st_mode):
        raise FsPolicyError("ROOT_NOT_DIRECTORY")
    if FD_SAFE:
        pinned = _FdDir(os.open(path, _DIR_FLAGS))
        if pinned.identity() != (st.st_dev, st.st_ino):
            pinned.close()
            raise FsPolicyError("ROOT_REPLACED")
        return pinned
    return _PathDir(path, os.path.realpath(path))


def read_capped(directory: PinnedDir, name: str, cap: int) -> tuple[bytes, os.stat_result] | None:
    """Read a small regular file through a no-follow descriptor; None if it exceeds ``cap``."""
    fd = directory.open_file(name)
    try:
        st = os.fstat(fd)
        if st.st_size > cap:
            return None
        chunks: list[bytes] = []
        total = 0
        while total <= cap:
            block = os.read(fd, cap + 1 - total)
            if not block:
                break
            chunks.append(block)
            total += len(block)
        return None if total > cap else (b"".join(chunks), st)
    finally:
        os.close(fd)


def _walk(directory: PinnedDir, prefix: str, stats: WalkStats, depth: int) -> Iterator[FileEntry]:
    subdirs: list[str] = []
    for name, st in directory.scan(stats):
        if is_link(st):
            stats.symlinks_skipped += 1
        elif stat.S_ISDIR(st.st_mode):
            subdirs.append(name)
        elif stat.S_ISREG(st.st_mode):
            yield FileEntry(f"{prefix}{name}", FileIdentity.of(st))
        else:
            stats.special_skipped += 1
    for name in subdirs:
        if stats.truncated:
            return
        if depth >= MAX_DEPTH:
            stats.truncated = True
            return
        try:
            child = directory.child(name)
        except FsPolicyError:
            stats.symlinks_skipped += 1  # swapped for a link after it was listed
            continue
        except OSError:
            stats.unreadable += 1
            continue
        with child:
            yield from _walk(child, f"{prefix}{name}/", stats, depth + 1)


def walk_files(root: Path, *, max_entries: int = DEFAULT_MAX_ENTRIES,
               stats: WalkStats | None = None) -> Iterator[FileEntry]:
    """Yield regular files under ``root`` (or ``root`` itself when it is a regular file).

    Raises ``FsPolicyError("ROOT_IS_SYMLINK")`` for a linked root; a missing root yields nothing.
    """
    counters = stats if stats is not None else WalkStats()
    counters.max_entries = max_entries
    try:
        st = os.lstat(root)
    except FileNotFoundError:
        return
    if is_link(st):
        raise FsPolicyError("ROOT_IS_SYMLINK")
    if stat.S_ISREG(st.st_mode):
        counters.entries += 1
        yield FileEntry(Path(root).name, FileIdentity.of(st))
        return
    if not stat.S_ISDIR(st.st_mode):
        counters.special_skipped += 1
        return
    with open_root(root) as pinned:
        yield from _walk(pinned, "", counters, 0)


def backend() -> dict[str, Any]:
    return {"descriptor_relative": FD_SAFE, "residual_risk": None if FD_SAFE else "ACCEPTED_RISK_WINDOWS"}

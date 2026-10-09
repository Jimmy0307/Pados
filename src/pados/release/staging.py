# SPDX-License-Identifier: AGPL-3.0-only
"""Home Base-owned immutable staging of a fetched release candidate.

Verification never reads the fetched directory twice. Each source file is
opened once without following symlinks (directory-fd relative on POSIX), and
the same bytes are hashed and copied in one pass into a fresh staging
directory the Home Base owns (mode 0700). Staged files are fsynced and made
read-only, the directory is made read-only, and ``staged_digest`` binds the
sorted ``(name, sha256, size)`` list. JSON documents are parsed from the bytes
captured during that pass. Readiness and approval bind to ``staged_digest`` and
re-hash the staged copy (``staged_integrity_error``) before trusting it.

Windows has no ``O_NOFOLLOW``/``dir_fd``: a path-based fallback with symlink and
regular-file checks is used and recorded as ``ACCEPTED_RISK_WINDOWS``.
"""
from __future__ import annotations

import hashlib
import os
import re
import secrets
import shutil
import stat
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path

from pados.execution.context import canonical_hash

ACCEPTED_RISK_WINDOWS = "ACCEPTED_RISK_WINDOWS_PATH_BASED_STAGING"
MAX_FILES = 64
MAX_FILE_BYTES = 2 * 1024 * 1024 * 1024
MAX_JSON_BYTES = 16 * 1024 * 1024
_CHUNK = 1024 * 1024
SAFE_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._+-]{0,199}")
_NOFOLLOW = getattr(os, "O_NOFOLLOW", 0)
_DIRECTORY = getattr(os, "O_DIRECTORY", 0)
_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
_BINARY = getattr(os, "O_BINARY", 0)
_POSIX_SAFE = bool(_NOFOLLOW) and os.open in os.supports_dir_fd and os.listdir in os.supports_fd


class StagingError(ValueError):
    def __init__(self, code: str, name: str = "") -> None:
        super().__init__(f"{code}:{name}" if name else code)
        self.code = code
        self.name = name


@dataclass(frozen=True)
class StagedFile:
    name: str
    sha256: str
    size: int


@dataclass(frozen=True)
class StagedRelease:
    directory: Path
    files: tuple[StagedFile, ...]
    staged_digest: str
    mode: str  # "POSIX_NOFOLLOW_DIR_FD" | ACCEPTED_RISK_WINDOWS
    documents: dict[str, bytes] = field(default_factory=dict, compare=False, repr=False)

    def file(self, name: str) -> StagedFile | None:
        return next((f for f in self.files if f.name == name), None)


def staged_digest(files: tuple[StagedFile, ...] | list[StagedFile]) -> str:
    return canonical_hash([[f.name, f.sha256, f.size] for f in sorted(files, key=lambda f: f.name)])


def _copy_stream(read_fd: int, write_fd: int, name: str, keep: bool) -> tuple[str, int, bytes]:
    digest = hashlib.sha256()
    kept = bytearray()
    size = 0
    while chunk := os.read(read_fd, _CHUNK):
        size += len(chunk)
        if size > MAX_FILE_BYTES:
            raise StagingError("RELEASE_FILE_TOO_LARGE", name)
        digest.update(chunk)
        if keep:
            if size > MAX_JSON_BYTES:
                raise StagingError("RELEASE_DOCUMENT_TOO_LARGE", name)
            kept.extend(chunk)
        view = memoryview(chunk)
        while view:
            written = os.write(write_fd, view)
            view = view[written:]
    os.fsync(write_fd)
    return digest.hexdigest(), size, bytes(kept)


def _check_name(name: str) -> None:
    if not SAFE_NAME.fullmatch(name) or ".." in name:
        raise StagingError("RELEASE_FILE_NAME_UNSAFE", name)


def _stage_posix(source: Path, target: Path) -> tuple[list[StagedFile], dict[str, bytes]]:
    files: list[StagedFile] = []
    documents: dict[str, bytes] = {}
    try:
        src_dir = os.open(source, os.O_RDONLY | _DIRECTORY | _NOFOLLOW)
    except OSError as error:
        raise StagingError("RELEASE_DIRECTORY_UNREADABLE") from error
    dst_dir = os.open(target, os.O_RDONLY | _DIRECTORY | _NOFOLLOW)
    try:
        names = sorted(os.listdir(src_dir))
        if len(names) > MAX_FILES:
            raise StagingError("RELEASE_TOO_MANY_FILES")
        for name in names:
            _check_name(name)
            try:
                read_fd = os.open(name, os.O_RDONLY | _NOFOLLOW | _NONBLOCK, dir_fd=src_dir)
            except OSError as error:
                raise StagingError("RELEASE_FILE_NOT_REGULAR", name) from error
            try:
                if not stat.S_ISREG(os.fstat(read_fd).st_mode):
                    raise StagingError("RELEASE_FILE_NOT_REGULAR", name)
                write_fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _NOFOLLOW, 0o600, dir_fd=dst_dir)
                try:
                    sha, size, kept = _copy_stream(read_fd, write_fd, name, name.endswith((".json", ".txt")))
                    if sys.platform != "win32":
                        os.fchmod(write_fd, 0o444)
                finally:
                    os.close(write_fd)
            finally:
                os.close(read_fd)
            files.append(StagedFile(name, sha, size))
            if name.endswith((".json", ".txt")):
                documents[name] = kept
        os.fsync(dst_dir)
    finally:
        os.close(src_dir)
        os.close(dst_dir)
    return files, documents


def _stage_path_based(source: Path, target: Path) -> tuple[list[StagedFile], dict[str, bytes]]:
    if source.is_symlink() or not source.is_dir():
        raise StagingError("RELEASE_DIRECTORY_UNREADABLE")
    paths = sorted(source.iterdir())
    if len(paths) > MAX_FILES:
        raise StagingError("RELEASE_TOO_MANY_FILES")
    files: list[StagedFile] = []
    documents: dict[str, bytes] = {}
    for path in paths:
        _check_name(path.name)
        if path.is_symlink() or not path.is_file():
            raise StagingError("RELEASE_FILE_NOT_REGULAR", path.name)
        read_fd = os.open(path, os.O_RDONLY | _BINARY)
        try:
            write_fd = os.open(target / path.name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | _BINARY, 0o600)
            try:
                sha, size, kept = _copy_stream(read_fd, write_fd, path.name, path.name.endswith((".json", ".txt")))
            finally:
                os.close(write_fd)
        finally:
            os.close(read_fd)
        os.chmod(target / path.name, stat.S_IREAD)
        files.append(StagedFile(path.name, sha, size))
        if path.name.endswith((".json", ".txt")):
            documents[path.name] = kept
    return files, documents


def stage_release(source: Path, staging_root: Path) -> StagedRelease:
    """Copy ``source`` into a fresh read-only directory under ``staging_root``."""
    staging_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = staging_root / f"stage-{secrets.token_hex(16)}"
    target.mkdir(mode=0o700)
    try:
        if _POSIX_SAFE:
            files, documents = _stage_posix(source, target)
            mode = "POSIX_NOFOLLOW_DIR_FD"
        else:
            files, documents = _stage_path_based(source, target)
            mode = ACCEPTED_RISK_WINDOWS
    except BaseException:
        _remove(target)
        raise
    if sys.platform != "win32":
        os.chmod(target, 0o500)
    return StagedRelease(target, tuple(files), staged_digest(files), mode, documents)


def _remove(target: Path) -> None:
    def retry_writable(function: Callable[[str], object], path: str, _error: BaseException) -> None:
        os.chmod(path, stat.S_IWRITE | stat.S_IREAD | stat.S_IEXEC)
        function(path)

    shutil.rmtree(target, onexc=retry_writable)


def staged_integrity_error(staged: StagedRelease) -> str | None:
    """Re-hash the staged copy; a code when it no longer matches what was verified."""
    if staged_digest(staged.files) != staged.staged_digest:
        return "STAGED_DIGEST_INCONSISTENT"
    try:
        present = sorted(os.listdir(staged.directory))
    except OSError:
        return "STAGED_DIRECTORY_MISSING"
    if present != sorted(f.name for f in staged.files):
        return "STAGED_CONTENT_CHANGED"
    for expected in staged.files:
        path = staged.directory / expected.name
        try:
            if path.is_symlink():
                return "STAGED_CONTENT_CHANGED"
            fd = os.open(path, os.O_RDONLY | _NOFOLLOW | _BINARY)
        except OSError:
            return "STAGED_CONTENT_CHANGED"
        digest = hashlib.sha256()
        size = 0
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                return "STAGED_CONTENT_CHANGED"
            while chunk := os.read(fd, _CHUNK):
                size += len(chunk)
                digest.update(chunk)
        finally:
            os.close(fd)
        if digest.hexdigest() != expected.sha256 or size != expected.size:
            return "STAGED_CONTENT_CHANGED"
    return None

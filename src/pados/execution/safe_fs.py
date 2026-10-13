# SPDX-License-Identifier: AGPL-3.0-only
"""SafeWorkspaceFS: governed, confined file access for PADOS execution.

One interface with two backends:

* POSIX: descriptor-bound ``dir_fd`` traversal with ``O_NOFOLLOW`` (the
  original PADOS implementation, unchanged in behaviour).
* Windows: Win32 handle traversal. Every ancestor directory is held open
  without ``FILE_SHARE_DELETE`` so it cannot be renamed or replaced mid-
  operation; reparse points (symlinks, junctions, mount points) are rejected
  from handle attributes; only disk files are accepted; and each opened
  object's final path must equal the expected in-root spelling exactly
  (exact spelling, drive letter normalized), which defeats case variants, 8.3 short names, junction aliases,
  device namespaces, UNC escapes and cross-drive tricks.

Paths are workspace-relative POSIX spellings (``a/b.txt``). Anything that
cannot be proven safe fails closed with ``SafeFSError`` (a PermissionError).
"""
from __future__ import annotations

import os
import re
import stat
import sys
from pathlib import Path
from typing import Protocol

MAX_FILE_BYTES = 400_000


class SafeFSError(PermissionError):
    """A confinement or file-type check failed; the operation did not happen."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class SafeWorkspaceFS(Protocol):
    root: Path

    def read_file(self, relative: str, *, max_bytes: int = MAX_FILE_BYTES, allow_truncate: bool = False) -> bytes | None:
        """Return file bytes, ``None`` if absent; raise if unsafe or (strict) oversized."""

    def write_file(self, relative: str, payload: bytes) -> None:
        """Atomically replace one regular file beneath the root, creating parents."""

    def is_regular_file(self, relative: str) -> bool:
        """Return whether a confined regular file exists at ``relative``."""

    def list_regular_files(self) -> set[str]:
        """List every regular file beneath the root; any link or special file fails closed."""


def split_relative(relative: str) -> list[str]:
    """Validate a workspace-relative POSIX spelling shared by both backends."""
    parts = relative.split("/")
    if (
        not relative
        or any(part in {"", ".", ".."} for part in parts)
        or any("\\" in part or ":" in part or "\x00" in part for part in parts)
    ):
        raise SafeFSError("SAFE_FS_PATH_UNSAFE")
    return parts




if sys.platform != "win32":

    class PosixWorkspaceFS:
        def __init__(self, root: Path) -> None:
            self.root = Path(root)

        def _parent_fd(self, parts: list[str], *, create: bool) -> int:
            descriptor = os.open(self.root, os.O_RDONLY | os.O_DIRECTORY)
            try:
                for part in parts[:-1]:
                    try:
                        child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                    except FileNotFoundError:
                        if not create:
                            raise
                        os.mkdir(part, 0o700, dir_fd=descriptor)
                        child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=descriptor)
                    os.close(descriptor)
                    descriptor = child
                return descriptor
            except NotADirectoryError as exc:
                os.close(descriptor)
                raise SafeFSError("SAFE_FS_PARENT_NOT_DIRECTORY") from exc
            except OSError as exc:
                os.close(descriptor)
                if isinstance(exc, FileNotFoundError):
                    raise
                raise SafeFSError("SAFE_FS_PARENT_UNSAFE") from exc

        def read_file(self, relative: str, *, max_bytes: int = MAX_FILE_BYTES, allow_truncate: bool = False) -> bytes | None:
            parts = split_relative(relative)
            try:
                parent = self._parent_fd(parts, create=False)
            except FileNotFoundError:
                return None
            try:
                try:
                    file_fd = os.open(parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=parent)
                except FileNotFoundError:
                    return None
                except OSError as exc:
                    raise SafeFSError("SAFE_FS_NOT_REGULAR_FILE") from exc
                # Check the descriptor before wrapping it: a directory must fail closed
                # as a confinement error, not surface as IsADirectoryError.
                if not stat.S_ISREG(os.fstat(file_fd).st_mode):
                    os.close(file_fd)
                    raise SafeFSError("SAFE_FS_NOT_REGULAR_FILE")
                with os.fdopen(file_fd, "rb") as stream:
                    payload = stream.read(max_bytes + 1)
                if len(payload) > max_bytes and not allow_truncate:
                    raise SafeFSError("SAFE_FS_FILE_BUDGET_EXCEEDED")
                return payload
            finally:
                os.close(parent)

        def is_regular_file(self, relative: str) -> bool:
            try:
                return self.read_file(relative, max_bytes=0, allow_truncate=True) is not None
            except SafeFSError:
                return False

        def write_file(self, relative: str, payload: bytes) -> None:
            parts = split_relative(relative)
            parent = self._parent_fd(parts, create=True)
            temporary = f".pados-{os.urandom(12).hex()}.tmp"
            try:
                try:
                    existing = os.lstat(parts[-1], dir_fd=parent)
                except FileNotFoundError:
                    existing = None
                if existing is not None and not stat.S_ISREG(existing.st_mode):
                    raise SafeFSError("SAFE_FS_NOT_REGULAR_FILE")
                descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600, dir_fd=parent)
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(payload)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, parts[-1], src_dir_fd=parent, dst_dir_fd=parent)
                os.fsync(parent)
            finally:
                try:
                    os.unlink(temporary, dir_fd=parent)
                except FileNotFoundError:
                    pass
                os.close(parent)

        def list_regular_files(self) -> set[str]:
            result: set[str] = set()
            for directory, dirnames, filenames in os.walk(self.root, followlinks=False):
                directory_path = Path(directory)
                for name in dirnames:
                    if (directory_path / name).is_symlink():
                        raise SafeFSError("SAFE_FS_LINK_REJECTED")
                for name in filenames:
                    path = directory_path / name
                    if not stat.S_ISREG(path.lstat().st_mode):
                        raise SafeFSError("SAFE_FS_NOT_REGULAR_FILE")
                    result.add(path.relative_to(self.root).as_posix())
            return result


_WINDOWS_RESERVED = re.compile(r"^(con|prn|aux|nul|com[0-9¹²³]|lpt[0-9¹²³]|conin\$|conout\$)(\..*)?$", re.IGNORECASE)
_WINDOWS_FORBIDDEN_CHARS = re.compile(r'[<>:"|?*\x00-\x1f]')
_WINDOWS_SHORT_NAME = re.compile(r"~\d")
_WINDOWS_DRIVE_ROOT = re.compile(r"^[A-Za-z]:\\")


def windows_component_error(part: str) -> str | None:
    """Return why a path component is unsafe on Windows, or ``None`` (platform-independent logic)."""
    if _WINDOWS_FORBIDDEN_CHARS.search(part) or part.endswith((".", " ")):
        return "SAFE_FS_PATH_UNSAFE"
    if _WINDOWS_RESERVED.match(part):
        return "SAFE_FS_DEVICE_NAME_REJECTED"
    if _WINDOWS_SHORT_NAME.search(part):
        return "SAFE_FS_SHORT_NAME_REJECTED"
    return None


def windows_root_error(root: str) -> str | None:
    """Reject device namespaces, UNC roots and non-absolute drive roots."""
    if root.startswith(("\\\\", "//")):
        return "SAFE_FS_UNC_OR_DEVICE_ROOT_REJECTED"
    if not _WINDOWS_DRIVE_ROOT.match(root):
        return "SAFE_FS_ROOT_NOT_ABSOLUTE_DRIVE"
    return None


def _drive_upper(path: str) -> str:
    return path[:1].upper() + path[1:] if _WINDOWS_DRIVE_ROOT.match(path) else path


def windows_final_path(raw: str) -> str | None:
    """Normalize a GetFinalPathNameByHandleW result; ``None`` for UNC/device targets."""
    if raw.startswith("\\\\?\\UNC\\") or not raw.startswith("\\\\?\\"):
        return None
    path = raw[4:]
    return path if _WINDOWS_DRIVE_ROOT.match(path) else None


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    _INVALID_HANDLE = wintypes.HANDLE(-1).value
    _GENERIC_READ, _GENERIC_WRITE = 0x80000000, 0x40000000
    _SHARE_READ, _SHARE_WRITE = 0x1, 0x2
    _OPEN_EXISTING, _CREATE_NEW = 3, 1
    _FLAG_BACKUP_SEMANTICS, _FLAG_OPEN_REPARSE_POINT = 0x02000000, 0x00200000
    _ATTR_DIRECTORY, _ATTR_REPARSE_POINT = 0x10, 0x400
    _FILE_TYPE_DISK = 1
    _FILE_ATTRIBUTE_TAG_INFO = 9
    _MOVEFILE_REPLACE_EXISTING, _MOVEFILE_WRITE_THROUGH = 0x1, 0x8
    _NOT_FOUND = {2, 3}

    class _AttributeTagInfo(ctypes.Structure):
        _fields_ = [("FileAttributes", wintypes.DWORD), ("ReparseTag", wintypes.DWORD)]

    class _ByHandleInfo(ctypes.Structure):
        _fields_ = [("dwFileAttributes", wintypes.DWORD), ("ftCreationTime", wintypes.FILETIME),
                    ("ftLastAccessTime", wintypes.FILETIME), ("ftLastWriteTime", wintypes.FILETIME),
                    ("dwVolumeSerialNumber", wintypes.DWORD), ("nFileSizeHigh", wintypes.DWORD),
                    ("nFileSizeLow", wintypes.DWORD), ("nNumberOfLinks", wintypes.DWORD),
                    ("nFileIndexHigh", wintypes.DWORD), ("nFileIndexLow", wintypes.DWORD)]

    _kernel32.CreateFileW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
                                      wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE]
    _kernel32.CreateFileW.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.GetFileType.argtypes = [wintypes.HANDLE]
    _kernel32.GetFileType.restype = wintypes.DWORD
    _kernel32.GetFileInformationByHandleEx.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
    _kernel32.GetFileInformationByHandle.argtypes = [wintypes.HANDLE, ctypes.POINTER(_ByHandleInfo)]
    _kernel32.GetFinalPathNameByHandleW.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    _kernel32.GetFinalPathNameByHandleW.restype = wintypes.DWORD
    _kernel32.ReadFile.argtypes = [wintypes.HANDLE, wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    _kernel32.WriteFile.argtypes = [wintypes.HANDLE, wintypes.LPCVOID, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD), wintypes.LPVOID]
    _kernel32.FlushFileBuffers.argtypes = [wintypes.HANDLE]
    _kernel32.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, wintypes.LPVOID]
    _kernel32.MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
    _kernel32.DeleteFileW.argtypes = [wintypes.LPCWSTR]

    def _open(path: str, *, access: int, disposition: int = _OPEN_EXISTING, share: int = _SHARE_READ | _SHARE_WRITE,
              directory: bool = False) -> int | None:
        flags = _FLAG_OPEN_REPARSE_POINT | (_FLAG_BACKUP_SEMANTICS if directory else 0)
        handle = _kernel32.CreateFileW(path, access, share, None, disposition, flags, None)
        if handle in (None, _INVALID_HANDLE):
            error = ctypes.get_last_error()
            if error in _NOT_FOUND:
                return None
            raise SafeFSError(f"SAFE_FS_OPEN_FAILED:{error}")
        return int(handle)

    def _attributes(handle: int) -> int:
        info = _AttributeTagInfo()
        if not _kernel32.GetFileInformationByHandleEx(handle, _FILE_ATTRIBUTE_TAG_INFO, ctypes.byref(info), ctypes.sizeof(info)):
            raise SafeFSError("SAFE_FS_ATTRIBUTES_UNAVAILABLE")
        return int(info.FileAttributes)

    def _final(handle: int) -> str:
        buffer = ctypes.create_unicode_buffer(32768)
        length = _kernel32.GetFinalPathNameByHandleW(handle, buffer, 32768, 0)
        final = windows_final_path(buffer.value) if 0 < length < 32768 else None
        if final is None:
            raise SafeFSError("SAFE_FS_FINAL_PATH_UNSAFE")
        return final

    def _link_count(handle: int) -> int:
        info = _ByHandleInfo()
        if not _kernel32.GetFileInformationByHandle(handle, ctypes.byref(info)):
            raise SafeFSError("SAFE_FS_ATTRIBUTES_UNAVAILABLE")
        return int(info.nNumberOfLinks)

    class WindowsWorkspaceFS:
        def __init__(self, root: Path) -> None:
            self.root = Path(root)
            error = windows_root_error(str(self.root))
            if error:
                raise SafeFSError(error)

        def _expect(self, handle: int, expected: str, *, directory: bool) -> None:
            attributes = _attributes(handle)
            if attributes & _ATTR_REPARSE_POINT:
                raise SafeFSError("SAFE_FS_LINK_REJECTED")
            if bool(attributes & _ATTR_DIRECTORY) != directory:
                raise SafeFSError("SAFE_FS_PARENT_NOT_DIRECTORY" if directory else "SAFE_FS_NOT_REGULAR_FILE")
            if _kernel32.GetFileType(handle) != _FILE_TYPE_DISK:
                raise SafeFSError("SAFE_FS_NOT_REGULAR_FILE")
            # Exact spelling (drive letter normalized): case variants, 8.3 names and
            # junction/hard-link aliases can never stand in for an allowlisted path.
            if _drive_upper(_final(handle)) != _drive_upper(expected):
                raise SafeFSError("SAFE_FS_ALIAS_OR_ESCAPE_REJECTED")

        def _walk_parents(self, parts: list[str], *, create: bool) -> tuple[list[int], str] | None:
            """Open and hold the root and every parent; return handles and the parent path."""
            for part in parts:
                error = windows_component_error(part)
                if error:
                    raise SafeFSError(error)
            handles: list[int] = []
            try:
                root = os.path.abspath(str(self.root))
                handle = _open(root, access=0, directory=True)
                if handle is None:
                    raise SafeFSError("SAFE_FS_ROOT_MISSING")
                handles.append(handle)
                self._expect(handle, root, directory=True)
                current = root
                for part in parts[:-1]:
                    current = current.rstrip("\\") + "\\" + part
                    handle = _open(current, access=0, directory=True)
                    if handle is None:
                        if not create:
                            return None
                        if not _kernel32.CreateDirectoryW(current, None):
                            raise SafeFSError(f"SAFE_FS_MKDIR_FAILED:{ctypes.get_last_error()}")
                        handle = _open(current, access=0, directory=True)
                        if handle is None:
                            raise SafeFSError("SAFE_FS_PARENT_UNSAFE")
                    handles.append(handle)
                    self._expect(handle, current, directory=True)
                return handles, current
            except BaseException:
                for held in reversed(handles):
                    _kernel32.CloseHandle(held)
                raise

        def read_file(self, relative: str, *, max_bytes: int = MAX_FILE_BYTES, allow_truncate: bool = False) -> bytes | None:
            parts = split_relative(relative)
            walked = self._walk_parents(parts, create=False)
            if walked is None:
                return None
            handles, parent = walked
            try:
                target = parent.rstrip("\\") + "\\" + parts[-1]
                handle = _open(target, access=_GENERIC_READ, share=_SHARE_READ)
                if handle is None:
                    return None
                handles.append(handle)
                self._expect(handle, target, directory=False)
                if _link_count(handle) != 1:
                    raise SafeFSError("SAFE_FS_HARD_LINK_REJECTED")
                chunks: list[bytes] = []
                remaining = max_bytes + 1
                while remaining > 0:
                    buffer = ctypes.create_string_buffer(min(remaining, 65536))
                    read = wintypes.DWORD(0)
                    if not _kernel32.ReadFile(handle, buffer, len(buffer), ctypes.byref(read), None):
                        raise SafeFSError("SAFE_FS_READ_FAILED")
                    if read.value == 0:
                        break
                    chunks.append(buffer.raw[: read.value])
                    remaining -= read.value
                payload = b"".join(chunks)
                if len(payload) > max_bytes and not allow_truncate:
                    raise SafeFSError("SAFE_FS_FILE_BUDGET_EXCEEDED")
                return payload
            finally:
                for held in reversed(handles):
                    _kernel32.CloseHandle(held)

        def is_regular_file(self, relative: str) -> bool:
            try:
                return self.read_file(relative, max_bytes=0, allow_truncate=True) is not None
            except SafeFSError:
                return False

        def write_file(self, relative: str, payload: bytes) -> None:
            parts = split_relative(relative)
            walked = self._walk_parents(parts, create=True)
            assert walked is not None
            handles, parent = walked
            target = parent.rstrip("\\") + "\\" + parts[-1]
            temporary = parent.rstrip("\\") + f"\\.pados-{os.urandom(12).hex()}.tmp"
            try:
                existing = _open(target, access=0, share=_SHARE_READ | _SHARE_WRITE)
                if existing is not None:
                    try:
                        self._expect(existing, target, directory=False)
                    finally:
                        _kernel32.CloseHandle(existing)
                handle = _open(temporary, access=_GENERIC_WRITE, disposition=_CREATE_NEW, share=0)
                if handle is None:
                    raise SafeFSError("SAFE_FS_TEMP_CREATE_FAILED")
                try:
                    self._expect(handle, temporary, directory=False)
                    written = wintypes.DWORD(0)
                    view = memoryview(payload)
                    while view:
                        chunk = bytes(view[:65536])
                        if not _kernel32.WriteFile(handle, chunk, len(chunk), ctypes.byref(written), None) or written.value == 0:
                            raise SafeFSError("SAFE_FS_WRITE_FAILED")
                        view = view[written.value:]
                    if not _kernel32.FlushFileBuffers(handle):
                        raise SafeFSError("SAFE_FS_FLUSH_FAILED")
                finally:
                    _kernel32.CloseHandle(handle)
                # Parents are still held without delete sharing, so the rename target directory
                # cannot be swapped; MoveFileExW replaces a link entry rather than following it.
                if not _kernel32.MoveFileExW(temporary, target, _MOVEFILE_REPLACE_EXISTING | _MOVEFILE_WRITE_THROUGH):
                    raise SafeFSError(f"SAFE_FS_REPLACE_FAILED:{ctypes.get_last_error()}")
            finally:
                _kernel32.DeleteFileW(temporary)
                for held in reversed(handles):
                    _kernel32.CloseHandle(held)

        def list_regular_files(self) -> set[str]:
            result: set[str] = set()
            root = os.path.abspath(str(self.root))
            root_handle = _open(root, access=0, directory=True)
            if root_handle is None:
                raise SafeFSError("SAFE_FS_ROOT_MISSING")
            handles = [root_handle]  # held without delete sharing for the whole walk
            try:
                self._expect(root_handle, root, directory=True)
                pending = [(root, "")]
                while pending:
                    directory, prefix = pending.pop()
                    with os.scandir(directory) as entries:
                        for entry in entries:
                            error = windows_component_error(entry.name)
                            if error:
                                raise SafeFSError(error)
                            info = entry.stat(follow_symlinks=False)
                            if info.st_file_attributes & _ATTR_REPARSE_POINT:
                                raise SafeFSError("SAFE_FS_LINK_REJECTED")
                            relative = f"{prefix}{entry.name}"
                            if stat.S_ISDIR(info.st_mode):
                                pending.append((entry.path, relative + "/"))
                            elif stat.S_ISREG(info.st_mode):
                                result.add(relative)
                            else:
                                raise SafeFSError("SAFE_FS_NOT_REGULAR_FILE")
            finally:
                for held in reversed(handles):
                    _kernel32.CloseHandle(held)
            return result


def workspace_fs(root: Path) -> SafeWorkspaceFS:
    """Return the confined filesystem for ``root`` on the current platform."""
    if sys.platform == "win32":
        return WindowsWorkspaceFS(root)
    else:
        return PosixWorkspaceFS(root)

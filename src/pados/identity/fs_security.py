# SPDX-License-Identifier: AGPL-3.0-only
"""File-system protection of the identity store (design §3.9).

**Windows.** ``os.chmod`` cannot express owner-only there, and a user name string proves nothing. The store
directory is therefore created by ``CreateDirectoryW`` with an explicit ``SECURITY_ATTRIBUTES`` whose security
descriptor is built from SDDL::

    O:<user SID>D:P(A;OICI;FA;;;<user SID>)(A;OICI;FA;;;SY)

- ``<user SID>`` is read from this process's own access token (``OpenProcessToken`` + ``GetTokenInformation
  (TokenUser)`` + ``ConvertSidToStringSidW``), never from ``USERNAME`` or any other environment value.
- ``P`` protects the DACL: nothing is inherited from the parent. ``OICI`` makes both ACEs inherit to every file
  created inside (``sessions.json``, ``server.key`` and their staged siblings), so a file is protected from its first
  byte; ``os.replace`` and ``os.link`` keep that descriptor.
- The directory therefore grants access to exactly this user and ``SYSTEM`` when it is created.

Every load re-reads the owner and DACL (``GetNamedSecurityInfoW``) of the directory and each store file and fails
closed (``IDENTITY_STORE_INSECURE``) when the owner is not this user, ``SYSTEM`` or ``Administrators``, when the DACL
is absent (a NULL DACL grants everyone everything), or when an allow ACE grants any read, write, delete or permission
change to another SID. ``Administrators`` (S-1-5-32-544) is *accepted* on load but never *granted* at creation: it is
on every Windows user profile's inherited ACL (a store created before this check, or by an elevated shell, carries
it), its members already control the machine (backup and take-ownership privileges bypass any DACL), and the
installer's owner-only contract (``packages/cli/lib/home.js``, ``classifyWindowsAcl``) accepts it on the same
grounds. Deny ACEs only restrict and are ignored; an ACE type this check does not understand fails closed.

**POSIX.** The directory is created ``0700`` and files ``0600`` (``mkstemp``); every load requires that the
directory and each file are owned by the effective user and grant nothing to group or others.

This protects the store's *files*. It authenticates no Windows user: binding a session to a Windows account needs an
OS-attested channel (design §5, not implemented).
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path
from typing import Final

SYSTEM_SID: Final = "S-1-5-18"
ADMINISTRATORS_SID: Final = "S-1-5-32-544"
CREATOR_OWNER_SID: Final = "S-1-3-0"  # only meaningful in inherit-only ACEs: becomes the creator of a new child
OWNER_RIGHTS_SID: Final = "S-1-3-4"  # the object's owner, which is checked separately

# Access bits that grant nothing worth protecting: synchronize, read the security descriptor, read attributes,
# traverse. Any other bit of an allow ACE (read or write data, EA, attributes, delete, WRITE_DAC, WRITE_OWNER,
# generic rights, MAXIMUM_ALLOWED ...) counts as read/write access.
_HARMLESS_MASK: Final = 0x00100000 | 0x00020000 | 0x00000080 | 0x00000020


class StoreInsecure(Exception):
    """The store's file-system protection is not owner-only; ``reason`` is a fixed word, never a path or a SID."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def protected_sddl(user_sid: str) -> str:
    """The descriptor of a new store directory: this user and SYSTEM, inherited by every file, nothing else."""
    if not user_sid.startswith("S-1-") or any(ch in user_sid for ch in "();:"):
        raise StoreInsecure("user_sid")
    return f"O:{user_sid}D:P(A;OICI;FA;;;{user_sid})(A;OICI;FA;;;SY)"


def acceptable_sids(user_sid: str) -> frozenset[str]:
    return frozenset({user_sid, SYSTEM_SID, ADMINISTRATORS_SID, CREATOR_OWNER_SID, OWNER_RIGHTS_SID})


def acceptable_owners(user_sid: str) -> frozenset[str]:
    return frozenset({user_sid, SYSTEM_SID, ADMINISTRATORS_SID})


def grants_access(mask: int) -> bool:
    return bool(mask & ~_HARMLESS_MASK)


if sys.platform == "win32":
    import ctypes
    from ctypes import wintypes

    _advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
    _kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

    _TOKEN_QUERY: Final = 0x0008
    _TOKEN_USER_CLASS: Final = 1
    _ERROR_INSUFFICIENT_BUFFER: Final = 122
    _ERROR_ALREADY_EXISTS: Final = 183
    _SDDL_REVISION_1: Final = 1
    _SE_FILE_OBJECT: Final = 1
    _OWNER_SECURITY_INFORMATION: Final = 0x1
    _DACL_SECURITY_INFORMATION: Final = 0x4
    _ACCESS_ALLOWED_ACE_TYPE: Final = 0x0
    _ACCESS_ALLOWED_CALLBACK_ACE_TYPE: Final = 0x9
    _DENY_ACE_TYPES: Final = frozenset({0x1, 0x6, 0xA, 0xC})

    class _SecurityAttributes(ctypes.Structure):
        _fields_ = [("nLength", wintypes.DWORD), ("lpSecurityDescriptor", wintypes.LPVOID),
                    ("bInheritHandle", wintypes.BOOL)]

    _kernel32.GetCurrentProcess.restype = wintypes.HANDLE
    _kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    _kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
    _kernel32.LocalFree.restype = wintypes.HLOCAL
    _kernel32.CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.POINTER(_SecurityAttributes)]
    _kernel32.CreateDirectoryW.restype = wintypes.BOOL
    _advapi32.OpenProcessToken.argtypes = [wintypes.HANDLE, wintypes.DWORD, ctypes.POINTER(wintypes.HANDLE)]
    _advapi32.OpenProcessToken.restype = wintypes.BOOL
    _advapi32.GetTokenInformation.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD,
                                              ctypes.POINTER(wintypes.DWORD)]
    _advapi32.GetTokenInformation.restype = wintypes.BOOL
    _advapi32.ConvertSidToStringSidW.argtypes = [wintypes.LPVOID, ctypes.POINTER(wintypes.LPWSTR)]
    _advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
    _advapi32.IsValidSid.argtypes = [wintypes.LPVOID]
    _advapi32.IsValidSid.restype = wintypes.BOOL
    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.ULONG)]
    _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW.restype = wintypes.BOOL
    _advapi32.GetNamedSecurityInfoW.argtypes = [
        wintypes.LPCWSTR, ctypes.c_int, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.LPVOID), ctypes.POINTER(wintypes.LPVOID),
        ctypes.POINTER(wintypes.LPVOID)]
    _advapi32.GetNamedSecurityInfoW.restype = wintypes.DWORD
    _advapi32.GetAce.argtypes = [wintypes.LPVOID, wintypes.DWORD, ctypes.POINTER(wintypes.LPVOID)]
    _advapi32.GetAce.restype = wintypes.BOOL

    def _sid_string(sid: int | None) -> str:
        if not sid or not _advapi32.IsValidSid(sid):
            raise StoreInsecure("invalid_sid")
        text = wintypes.LPWSTR()
        if not _advapi32.ConvertSidToStringSidW(sid, ctypes.byref(text)):
            raise StoreInsecure("sid_unreadable")
        try:
            return str(text.value)
        finally:
            _kernel32.LocalFree(ctypes.cast(text, wintypes.LPVOID))

    def current_user_sid() -> str:
        """The user SID of this process's access token (never a user name or an environment value)."""
        process_handle = wintypes.HANDLE()
        if not _advapi32.OpenProcessToken(_kernel32.GetCurrentProcess(), _TOKEN_QUERY, ctypes.byref(process_handle)):
            raise StoreInsecure("token_unavailable")
        try:
            needed = wintypes.DWORD(0)
            _advapi32.GetTokenInformation(process_handle, _TOKEN_USER_CLASS, None, 0, ctypes.byref(needed))
            if ctypes.get_last_error() != _ERROR_INSUFFICIENT_BUFFER or needed.value == 0:
                raise StoreInsecure("token_unreadable")
            buffer = ctypes.create_string_buffer(needed.value)
            if not _advapi32.GetTokenInformation(process_handle, _TOKEN_USER_CLASS, buffer, needed,
                                                 ctypes.byref(needed)):
                raise StoreInsecure("token_unreadable")
            # TOKEN_USER starts with SID_AND_ATTRIBUTES, whose first member is the PSID.
            sid = ctypes.cast(buffer, ctypes.POINTER(ctypes.c_void_p))[0]
            return _sid_string(sid)
        finally:
            _kernel32.CloseHandle(process_handle)

    def create_private_directory(path: Path) -> None:
        """Create ``path`` with the protected descriptor from its first moment (no window with an inherited ACL)."""
        descriptor = wintypes.LPVOID()
        if not _advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
                protected_sddl(current_user_sid()), _SDDL_REVISION_1, ctypes.byref(descriptor), None):
            raise StoreInsecure("descriptor_build")
        try:
            attributes = _SecurityAttributes(ctypes.sizeof(_SecurityAttributes), descriptor, False)
            if not _kernel32.CreateDirectoryW(str(path), ctypes.byref(attributes)):
                error = ctypes.get_last_error()
                if error != _ERROR_ALREADY_EXISTS:
                    raise OSError(error, "CreateDirectoryW failed")  # an ordinary I/O failure, not a policy one
        finally:
            _kernel32.LocalFree(descriptor)

    def _ace_problem(ace: int, allowed: frozenset[str]) -> str | None:
        ace_type, size = ctypes.c_ubyte.from_address(ace).value, ctypes.c_ushort.from_address(ace + 2).value
        if ace_type in _DENY_ACE_TYPES:
            return None
        if ace_type not in (_ACCESS_ALLOWED_ACE_TYPE, _ACCESS_ALLOWED_CALLBACK_ACE_TYPE) or size < 16:
            return "unknown_ace"  # object and compound ACEs never belong on these files: fail closed
        mask = ctypes.c_uint32.from_address(ace + 4).value
        if grants_access(mask) and _sid_string(ace + 8) not in allowed:
            return "foreign_ace"
        return None

    def windows_problem(path: Path, user_sid: str | None = None) -> str | None:
        """``None`` when ``path`` is owner-only by the rules above, else a fixed reason word."""
        sid = user_sid or current_user_sid()
        owner, dacl, descriptor = wintypes.LPVOID(), wintypes.LPVOID(), wintypes.LPVOID()
        error = _advapi32.GetNamedSecurityInfoW(
            str(path), _SE_FILE_OBJECT, _OWNER_SECURITY_INFORMATION | _DACL_SECURITY_INFORMATION,
            ctypes.byref(owner), None, ctypes.byref(dacl), None, ctypes.byref(descriptor))
        if error != 0:
            return "acl_unreadable"
        try:
            if _sid_string(owner.value) not in acceptable_owners(sid):
                return "foreign_owner"
            if not dacl.value:
                return "null_dacl"
            count = ctypes.c_ushort.from_address(dacl.value + 4).value  # ACL header: AceCount
            allowed = acceptable_sids(sid)
            for index in range(count):
                ace = wintypes.LPVOID()
                if not _advapi32.GetAce(dacl, index, ctypes.byref(ace)) or not ace.value:
                    return "acl_unreadable"
                problem = _ace_problem(ace.value, allowed)
                if problem is not None:
                    return problem
            return None
        finally:
            _kernel32.LocalFree(descriptor)


def posix_problem(path: Path) -> str | None:
    """``None`` when ``path`` is owned by the effective user and grants nothing to group or others."""
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        return "symlink"
    geteuid = getattr(os, "geteuid", None)
    if geteuid is not None and info.st_uid != geteuid():
        return "foreign_owner"
    if stat.S_IMODE(info.st_mode) & 0o077:
        return "group_or_other_access"
    return None


def problem(path: Path) -> str | None:
    """The platform check for one existing store path."""
    if sys.platform == "win32":
        return windows_problem(path)
    return posix_problem(path)


def make_private_directory(path: Path) -> None:
    """Create the store directory owner-only from creation (parents are created normally)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        create_private_directory(path)
        return
    try:
        os.mkdir(path, 0o700)
    except FileExistsError:
        return
    os.chmod(path, 0o700)  # the umask may have narrowed further, never widened; make it exact

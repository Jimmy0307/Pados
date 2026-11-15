# SPDX-License-Identifier: AGPL-3.0-only
"""The identity store is owner-only from creation and refused when it is not (design §3.9).

Windows: the store directory is created with an explicit protected DACL (this process's token user SID and SYSTEM,
inherited by every file); every load re-reads the DACL and refuses a store readable or writable by another SID with
``IDENTITY_STORE_INSECURE``. The tests read and change ACLs with ``icacls`` and SIDs (``*S-1-1-0``), never account
names, so they do not depend on the Windows display language, and they never use the code under test to check it.
POSIX: 0700 directory, 0600 files, owned by the effective user.
"""
from __future__ import annotations

import os
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from tests.identity.identity_support import TOKEN_HEADERS, Harness

WINDOWS_ONLY = pytest.mark.skipif(sys.platform != "win32", reason="Windows DACLs (CreateDirectoryW with an SDDL "
                                  "security descriptor, GetNamedSecurityInfoW) exist only on win32")
POSIX_ONLY = pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits and uids: on Windows os.chmod only "
                                "toggles the read-only flag; the DACL tests above cover win32")
EVERYONE, USERS = "S-1-1-0", "S-1-5-32-545"
SYSTEM = "S-1-5-18"


def _code(response_json: dict[str, object]) -> str:
    return str(response_json["error"]["code"])  # type: ignore[index]


def _system32(tool: str) -> str:
    return str(Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / tool)


def _token_sid_from_whoami() -> str:
    out = subprocess.run([_system32("whoami.exe"), "/user", "/fo", "csv", "/nh"], capture_output=True, text=True,
                         check=True).stdout
    return out.strip().rsplit(",", 1)[1].strip().strip('"')


def _dacl_sddl(path: Path, scratch: Path) -> str:
    """The DACL in SDDL form, through icacls (independent of the implementation and of the display language)."""
    saved = scratch / f"acl-{path.name}.txt"
    subprocess.run([_system32("icacls.exe"), str(path), "/save", str(saved)], capture_output=True, check=True)
    lines = [line for line in saved.read_bytes().decode("utf-16-le").splitlines() if line.startswith("D:")]
    assert len(lines) == 1, lines
    return lines[0]


def _aces(sddl: str) -> list[tuple[str, str, str]]:
    """(flags, rights, SID) of every ACE; SY is expanded to its SID."""
    found = re.findall(r"\(([A-Z]+);([A-Z]*);([^;]*);;;([^)]+)\)", sddl)
    return [(flags, rights, SYSTEM if sid == "SY" else sid) for _type, flags, rights, sid in found]


def _grant(path: Path, grant: str) -> None:
    subprocess.run([_system32("icacls.exe"), str(path), "/grant", grant], capture_output=True, check=True)


# --- Windows --------------------------------------------------------------------------------------------------------

@WINDOWS_ONLY
def test_the_sid_comes_from_the_process_token_not_the_user_name(monkeypatch: pytest.MonkeyPatch) -> None:
    from pados.identity import fs_security

    expected = _token_sid_from_whoami()
    monkeypatch.setenv("USERNAME", "someone-else")
    monkeypatch.setenv("USERDOMAIN", "ELSEWHERE")
    monkeypatch.setenv("USERPROFILE", r"D:\profiles\someone-else")
    assert fs_security.current_user_sid() == expected


@WINDOWS_ONLY
def test_a_fresh_store_has_the_protected_dacl_and_its_files_inherit_it(harness: Harness, tmp_path: Path) -> None:
    harness.paired(harness.client())
    user = _token_sid_from_whoami()
    directory = _dacl_sddl(harness.store_dir, tmp_path)
    assert directory.startswith("D:P"), directory  # protected: nothing inherited from the parent
    assert sorted(_aces(directory)) == sorted([("OICI", "FA", user), ("OICI", "FA", SYSTEM)]), directory
    for name in ("sessions.json", "server.key"):
        sddl = _dacl_sddl(harness.store_dir / name, tmp_path)
        aces = _aces(sddl)
        assert {sid for _, _, sid in aces} == {user, SYSTEM}, (name, sddl)
        assert all("ID" in flags for flags, _, _ in aces), (name, sddl)  # inherited from the protected directory


@WINDOWS_ONLY
@pytest.mark.parametrize(("target", "grant"), [
    ("sessions.json", f"*{EVERYONE}:(R)"),
    ("server.key", f"*{USERS}:(R)"),
    (".", f"*{USERS}:(OI)(CI)(R)"),
    (".", f"*{EVERYONE}:(W)"),
])
def test_a_store_readable_or_writable_by_another_sid_is_refused(harness: Harness, target: str, grant: str) -> None:
    client = harness.client()
    harness.paired(client)
    _grant((harness.store_dir / target).resolve(), grant)
    session = client.get("/identity/v1/session")
    control = client.get("/control/projects")
    mint = client.post("/identity/v1/pairing-codes", headers=TOKEN_HEADERS)
    for response in (session, control, mint):
        assert response.status_code == 503 and _code(response.json()) == "IDENTITY_STORE_INSECURE", response.text


@WINDOWS_ONLY
def test_a_pre_existing_directory_open_to_users_is_refused_before_any_secret_is_written(
        make_harness: object, tmp_path: Path) -> None:
    store = tmp_path / "precreated"
    store.mkdir()
    _grant(store, f"*{USERS}:(OI)(CI)(M)")
    harness = make_harness(store_dir=store)  # type: ignore[operator]
    response = harness.client().post("/identity/v1/pairing-codes", headers=TOKEN_HEADERS)
    assert response.status_code == 503 and _code(response.json()) == "IDENTITY_STORE_INSECURE"
    assert sorted(p.name for p in store.iterdir()) == []  # no key, no document


@WINDOWS_ONLY
def test_the_check_accepts_administrators_but_creation_never_grants_them(tmp_path: Path) -> None:
    from pados.identity import fs_security

    created = tmp_path / "store"
    fs_security.make_private_directory(created)
    assert "BA" not in _dacl_sddl(created, tmp_path) and "S-1-5-32-544" not in _dacl_sddl(created, tmp_path)
    _grant(created, "*S-1-5-32-544:(OI)(CI)(F)")
    assert fs_security.windows_problem(created) is None
    _grant(created, f"*{EVERYONE}:(RX)")
    assert fs_security.windows_problem(created) == "foreign_ace"


# --- every platform -------------------------------------------------------------------------------------------------

def test_the_creation_descriptor_names_only_the_user_and_system() -> None:
    from pados.identity import fs_security

    sid = "S-1-5-21-1-2-3-1001"
    assert fs_security.protected_sddl(sid) == f"O:{sid}D:P(A;OICI;FA;;;{sid})(A;OICI;FA;;;SY)"
    for bad in ("", "someone", "S-1-5-21-1)(A;;FA;;;WD", "S-1-5;x"):
        with pytest.raises(fs_security.StoreInsecure):
            fs_security.protected_sddl(bad)
    assert fs_security.grants_access(0x00120089)  # FILE_GENERIC_READ
    assert not fs_security.grants_access(0x00100000 | 0x00020000 | 0x80)  # synchronize, read control, attributes


def test_an_insecure_store_is_logged_by_reason_word_only(harness: Harness, monkeypatch: pytest.MonkeyPatch) -> None:
    import structlog

    from pados.identity import fs_security

    client = harness.client()
    harness.paired(client)
    monkeypatch.setattr(fs_security, "problem", lambda _path: "foreign_ace")
    with structlog.testing.capture_logs() as events:
        response = client.get("/identity/v1/session")
    assert response.status_code == 503 and _code(response.json()) == "IDENTITY_STORE_INSECURE"
    logged = [e for e in events if e.get("event") == "identity.store.insecure"]
    assert logged and logged[0]["reason"] == "foreign_ace"
    assert str(harness.store_dir) not in repr(logged)


# --- POSIX ----------------------------------------------------------------------------------------------------------

@POSIX_ONLY
def test_the_directory_is_0700_from_creation_whatever_the_umask(harness: Harness,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    previous = os.umask(0)
    try:
        harness.paired(harness.client())
    finally:
        os.umask(previous)
    assert stat.S_IMODE(os.stat(harness.store_dir).st_mode) == 0o700
    for name in ("sessions.json", "server.key"):
        assert stat.S_IMODE(os.stat(harness.store_dir / name).st_mode) == 0o600


@POSIX_ONLY
@pytest.mark.parametrize(("target", "mode"), [(".", 0o750), (".", 0o701), ("sessions.json", 0o640),
                                              ("server.key", 0o604)])
def test_a_store_open_to_group_or_others_is_refused(harness: Harness, target: str, mode: int) -> None:
    client = harness.client()
    harness.paired(client)
    os.chmod(harness.store_dir / target, mode)
    response = client.get("/identity/v1/session")
    assert response.status_code == 503 and _code(response.json()) == "IDENTITY_STORE_INSECURE"

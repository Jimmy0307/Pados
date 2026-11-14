# SPDX-License-Identifier: AGPL-3.0-only
"""`pados init` writes `.env` (it holds PADOS_DB_PASSWORD) owner-only from the first byte.

The file used to be written with the default mode and narrowed by a later chmod, so on a POSIX host its credentials
were briefly readable under the usual umask, and `--force` wrote them through whatever already sat at that path
(an existing file keeps its mode until the chmod; a hard link shares the new credentials with the other name).
The new content now goes to a fresh owner-only sibling file that atomically replaces `.env`.
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest
from typer.testing import CliRunner

DB_SECRET = "init-db-secret-value"


@pytest.fixture(autouse=True)
def _db_password(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PADOS_DB_PASSWORD", DB_SECRET)


def _init(directory: Path, *extra: str) -> int:
    from pados.cli.main import app

    return CliRunner().invoke(app, ["init", "--dir", str(directory), *extra]).exit_code


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_env_is_owner_only_without_relying_on_a_later_chmod(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(os, "chmod", lambda *_args, **_kwargs: None)  # the old code was only safe after this call
    previous = os.umask(0o022)
    try:
        assert _init(tmp_path) == 0
    finally:
        os.umask(previous)
    env_path = tmp_path / ".env"
    assert DB_SECRET in env_path.read_text(encoding="utf-8")
    assert stat.S_IMODE(os.stat(env_path).st_mode) & 0o077 == 0


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_force_does_not_write_credentials_into_a_world_readable_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    env_path = tmp_path / ".env"
    env_path.write_text("PADOS_ENV=local\n", encoding="utf-8")
    os.chmod(env_path, 0o644)
    monkeypatch.setattr(os, "chmod", lambda *_args, **_kwargs: None)
    assert _init(tmp_path, "--force") == 0
    assert DB_SECRET in env_path.read_text(encoding="utf-8")
    assert stat.S_IMODE(os.stat(env_path).st_mode) & 0o077 == 0


def test_force_replaces_a_hard_linked_env_instead_of_writing_through_it(tmp_path: Path) -> None:
    other = tmp_path / "shared-elsewhere.txt"
    other.write_text("keep me", encoding="utf-8")
    try:
        os.link(other, tmp_path / ".env")
    except OSError:
        pytest.skip("hard links are not supported on this file system")
    assert _init(tmp_path, "--force") == 0
    assert other.read_text(encoding="utf-8") == "keep me"
    assert DB_SECRET in (tmp_path / ".env").read_text(encoding="utf-8")


def test_init_leaves_no_temporary_credential_file_behind(tmp_path: Path) -> None:
    assert _init(tmp_path) == 0
    assert _init(tmp_path, "--force") == 0
    assert sorted(path.name for path in tmp_path.iterdir()) == [".env", "runtime"]

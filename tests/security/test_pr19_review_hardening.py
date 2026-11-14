# SPDX-License-Identifier: AGPL-3.0-only
"""Fresh-review hardening for the local dogfood path: precise HTTP errors for unknown or malformed ids, `pados init`
never writes credentials through a planted link, and a stale stop request cannot stop a new server."""
from __future__ import annotations

import contextlib
import hashlib
import os
import time
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import DataError, NoResultFound
from typer.testing import CliRunner

CONTROL_VALUE = "unique-test-control-value"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("PADOS_CONTROL_API_TOKEN_HASH", hashlib.sha256(CONTROL_VALUE.encode()).hexdigest().upper())
    from pados import config
    config.get_settings.cache_clear()
    from pados.api import app as api_app
    from pados.worker import service as worker_service

    @contextlib.contextmanager
    def no_db(_: Any) -> Iterator[object]:
        yield object()

    def get_job(_session: object, job_id: str) -> dict[str, Any]:
        if job_id == "not-a-uuid":
            raise DataError("SELECT", {}, Exception("invalid input syntax for type uuid"))
        raise NoResultFound("No row was found")

    monkeypatch.setattr(api_app, "session_scope", no_db)
    monkeypatch.setattr(worker_service, "get_job", get_job)
    yield TestClient(api_app.create_app())
    config.get_settings.cache_clear()


def test_unknown_and_malformed_ids_are_404_and_422(client: TestClient) -> None:
    headers = {"X-PADOS-Control-Token": CONTROL_VALUE}
    unknown = client.get("/control/jobs/00000000-0000-0000-0000-0000000000ee", headers=headers)
    malformed = client.get("/control/jobs/not-a-uuid", headers=headers)
    assert (unknown.status_code, unknown.json()["detail"]) == (404, "NOT_FOUND")
    assert (malformed.status_code, malformed.json()["detail"]) == (422, "INVALID_IDENTIFIER")


def test_init_refuses_to_write_the_control_token_through_a_link(tmp_path: Path) -> None:
    from pados.cli.main import app

    victim = tmp_path / "victim.txt"
    victim.write_text("keep me", encoding="utf-8")
    local = tmp_path / "runtime" / "local"
    local.mkdir(parents=True)
    try:
        (local / "control.token").symlink_to(victim)
    except OSError:
        pytest.skip("creating symbolic links needs a privilege on this platform")
    result = CliRunner().invoke(app, ["init", "--dir", str(tmp_path)])
    assert result.exit_code != 0
    assert victim.read_text(encoding="utf-8") == "keep me"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permission bits")
def test_init_writes_credentials_owner_only(tmp_path: Path) -> None:
    from pados.cli.main import app

    assert CliRunner().invoke(app, ["init", "--dir", str(tmp_path)]).exit_code == 0
    for private in (tmp_path / ".env", tmp_path / "runtime" / "local" / "control.token"):
        assert os.stat(private).st_mode & 0o077 == 0, private


def test_a_stale_stop_request_is_cleared_when_the_server_starts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import uvicorn

    from pados.install import serve

    class FakeServer:
        def __init__(self, _config: object) -> None:
            self.should_exit = False

        def run(self) -> None:
            return None

    monkeypatch.setattr(uvicorn, "Config", lambda *_a, **_k: object())
    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    monkeypatch.setattr(serve, "create_app", lambda: object())
    stale = tmp_path / serve.STOP_FILE
    stale.parent.mkdir(parents=True)
    stale.write_text("instance-a", encoding="utf-8")
    os.utime(stale, (1_000_000_000, 1_000_000_000))  # written long before this server process started
    serve.run("instance-a", tmp_path)
    assert not stale.exists()


def test_a_stop_request_written_after_the_server_process_started_is_kept(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """R19-7: `pados down` issued while the server was still starting is this launch's request, not a stale one."""
    import uvicorn

    from pados.install import serve

    seen: list[bool] = []

    class FakeServer:
        def __init__(self, _config: object) -> None:
            self.should_exit = False

        def run(self) -> None:
            for _ in range(100):  # the watcher acts within its first poll
                if self.should_exit:
                    break
                time.sleep(0.02)
            seen.append(self.should_exit)

    monkeypatch.setattr(uvicorn, "Config", lambda *_a, **_k: object())
    monkeypatch.setattr(uvicorn, "Server", FakeServer)
    monkeypatch.setattr(serve, "create_app", lambda: object())
    request = tmp_path / serve.STOP_FILE
    request.parent.mkdir(parents=True)
    request.write_text("instance-a", encoding="utf-8")  # newer than this (test) process
    serve.run("instance-a", tmp_path)
    assert seen == [True] and not request.exists()

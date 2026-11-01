# SPDX-License-Identifier: AGPL-3.0-only
"""Execution output location (roadmap: runtime/artifacts persistence and exposure).

Job artifacts are recorded by path, and verification re-reads them, so the root must not depend on the server's
working directory: it is ``PADOS_ARTIFACT_ROOT`` resolved to an absolute path, and ``pados init`` writes one under its
local runtime directory. Local secrets and job outputs in a checkout are never committed by accident.
"""
from __future__ import annotations

import contextlib
import hashlib
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

REPO = Path(__file__).resolve().parents[2]
CONTROL_VALUE = "unique-test-control-value"


def test_operator_step_uses_an_absolute_artifact_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PADOS_CONTROL_API_TOKEN_HASH", hashlib.sha256(CONTROL_VALUE.encode()).hexdigest().upper())
    monkeypatch.setenv("PADOS_ARTIFACT_ROOT", "relative/artifacts")
    monkeypatch.chdir(tmp_path)
    from pados import config
    config.get_settings.cache_clear()
    from pados.api import app as api_app
    from pados.loop import service as loop_service

    seen: list[Path] = []

    @contextlib.contextmanager
    def no_db(_: Any) -> Iterator[object]:
        yield object()

    def run_step(_settings: object, **kwargs: Any) -> dict[str, Any]:
        seen.append(kwargs["artifact_root"])
        return {"loop": {"status": "RUNNING"}}

    monkeypatch.setattr(api_app, "session_scope", no_db)
    monkeypatch.setattr(loop_service, "run_step", run_step)
    client = TestClient(api_app.create_app())
    response = client.post("/control/loops/00000000-0000-0000-0000-0000000000dd/step",
                           json={"idempotency_key": "k"}, headers={"X-PADOS-Control-Token": CONTROL_VALUE})
    config.get_settings.cache_clear()
    assert response.status_code == 200
    assert seen == [(tmp_path / "relative" / "artifacts").resolve()]


def test_init_writes_an_absolute_artifact_root(tmp_path: Path) -> None:
    from pados.cli.main import app

    result = CliRunner().invoke(app, ["init", "--dir", str(tmp_path)])
    assert result.exit_code == 0, result.output
    lines = dict(line.split("=", 1) for line in (tmp_path / ".env").read_text(encoding="utf-8").splitlines()
                 if "=" in line and not line.startswith("#"))
    root = Path(lines["PADOS_ARTIFACT_ROOT"])
    assert root.is_absolute() and root == (tmp_path / "runtime" / "local" / "artifacts").resolve()


@pytest.mark.skipif(shutil.which("git") is None or not (REPO / ".git").exists(), reason="needs a git checkout")
@pytest.mark.parametrize("path", ["runtime/local/control.token", "runtime/artifacts/worker_jobs/job/stdout.txt"])
def test_local_secrets_and_job_outputs_are_git_ignored(path: str) -> None:
    ignored = subprocess.run(["git", "check-ignore", "-q", path], cwd=REPO, check=False)
    assert ignored.returncode == 0, f"{path} could be committed by accident"

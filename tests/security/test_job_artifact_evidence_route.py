"""Operators can read a job's execution evidence (artifact type, path, SHA-256) through the control API.

The deterministic worker records artifacts in ``worker_job_artifacts``, but no control route exposed them, so the
operator could not see the digests their completed task produced. The route is read-only and control-token gated.
"""
from __future__ import annotations

import contextlib
import hashlib
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

CONTROL_VALUE = "unique-test-control-value"
JOB = "00000000-0000-0000-0000-0000000000aa"
ROWS = [{"artifact_type": "result_manifest", "path": "runtime/jobs/x/manifest.json", "sha256": "a" * 64}]


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> TestClient:
    monkeypatch.setenv("PADOS_CONTROL_API_TOKEN_HASH", hashlib.sha256(CONTROL_VALUE.encode()).hexdigest().upper())
    from pados import config
    config.get_settings.cache_clear()
    from pados.api import app as api_app
    from pados.worker import service as worker_service

    @contextlib.contextmanager
    def no_db(_: Any) -> Iterator[object]:
        yield object()

    monkeypatch.setattr(api_app, "session_scope", no_db)
    monkeypatch.setattr(worker_service, "list_job_artifacts", lambda session, job_id: ROWS if job_id == JOB else [])
    yield TestClient(api_app.create_app())
    config.get_settings.cache_clear()


def test_job_artifacts_are_readable_with_the_control_token(client: TestClient) -> None:
    response = client.get(f"/control/jobs/{JOB}/artifacts", headers={"X-PADOS-Control-Token": CONTROL_VALUE})
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "job_id": JOB, "artifacts": ROWS}


def test_job_artifacts_require_the_control_token(client: TestClient) -> None:
    assert client.get(f"/control/jobs/{JOB}/artifacts").status_code in (401, 403)
    assert client.get(f"/control/jobs/{JOB}/artifacts",
                      headers={"X-PADOS-Control-Token": "wrong"}).status_code in (401, 403)

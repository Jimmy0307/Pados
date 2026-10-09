from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from pados.api.app import create_app
from pados.control import security as control_security
from pados.worker.command import build_argv
from pados.worker.schemas import CommandInvocation
from tests.windows_links import create_link_or_skip


def test_worker_routes_require_control_token_before_database() -> None:
    client = TestClient(create_app())

    assert client.get("/control/workers").status_code == 403
    assert client.get("/control/jobs").status_code == 403
    assert client.post("/control/work-requests/11111111-1111-4111-8111-111111111111/route", json={}).status_code == 403


def test_route_endpoint_does_not_accept_shell_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(control_security, "authorize_token", lambda settings, value: None)

    client = TestClient(create_app())
    response = client.post(
        "/control/work-requests/11111111-1111-4111-8111-111111111111/route",
        headers={"X-PADOS-Control-Token": "test"},
        json={
            "approval_id": "22222222-2222-4222-8222-222222222222",
            "capability_id": "RUN_TESTS",
            "context_query": "status",
            "allowed_actions": ["RUN_TESTS; whoami"],
            "idempotency_key": "bad",
        },
    )

    assert response.status_code == 422


def test_command_governance_blocks_symlink_escape(tmp_path: Path) -> None:
    outside = tmp_path.parent / "outside-target"
    outside.mkdir(exist_ok=True)
    link = tmp_path / "link"
    create_link_or_skip(outside, link, directory=True)

    with pytest.raises(PermissionError, match="outside allowed"):
        build_argv(
            CommandInvocation(
                command_id="pytest_targeted",
                arguments={"target": "link/test.py"},
                working_directory=tmp_path,
                timeout_seconds=1,
            ),
            project_root=tmp_path,
        )

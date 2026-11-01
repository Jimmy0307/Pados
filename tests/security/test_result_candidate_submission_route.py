# SPDX-License-Identifier: AGPL-3.0-only
"""Candidate submission is a control-plane operation; semantic acceptance stays on WebAuthn + CAS."""
from __future__ import annotations

import contextlib
import hashlib
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

CONTROL_VALUE = "local-dogfood-operator-token"
LOOP_ID = "00000000-0000-0000-0000-000000000001"
PROJECT_ID = "00000000-0000-0000-0000-000000000002"
APPROVAL_ID = "00000000-0000-0000-0000-000000000003"


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    from pados.config import Settings
    from pados.api import app as api_app
    from pados.loop import acceptance, scope_authority

    settings = Settings(
        PADOS_CONTROL_API_TOKEN_HASH=hashlib.sha256(CONTROL_VALUE.encode()).hexdigest().upper(),
        PADOS_PRODUCT_DATABASE_NAME="pados_dogfood_proof",
    )
    monkeypatch.setattr(api_app, "get_settings", lambda: settings)

    @contextlib.contextmanager
    def no_db(_: Any) -> Iterator[object]:
        yield object()

    monkeypatch.setattr(api_app, "session_scope", no_db)
    monkeypatch.setattr(api_app.loop_service, "get_loop", lambda _session, _loop_id: {"project_id": PROJECT_ID})
    monkeypatch.setattr(scope_authority, "read_current_scope_version", lambda _settings, _project_id: 7)
    row = {
        "candidate_id": "candidate-123",
        "candidate_digest": "a" * 64,
        "task_id": "task-123",
        "project_id": PROJECT_ID,
        "classification": "PUBLIC",
        "expected_scope_version": 7,
        "verification_digest": "b" * 64,
        "loop_id": LOOP_ID,
        "verification_status": "PASSED",
        "approval_id": APPROVAL_ID,
        "candidate_json": {"private_body": "must not be returned by this summary endpoint"},
    }
    submissions: list[tuple[str, int | None]] = []

    def submit(session: object, loop_id: str, *, current_scope_version: int | None) -> dict[str, Any]:
        submissions.append((loop_id, current_scope_version))
        return row

    monkeypatch.setattr(acceptance, "submit_result_candidate", submit)
    yield TestClient(api_app.create_app())
    assert submissions == [] or submissions == [(LOOP_ID, 7)]


def test_candidate_submission_requires_control_auth(client: TestClient) -> None:
    response = client.post(f"/control/loops/{LOOP_ID}/result-candidate")
    assert response.status_code == 403


def test_candidate_submission_returns_minimized_projection_not_acceptance(client: TestClient) -> None:
    response = client.post(
        f"/control/loops/{LOOP_ID}/result-candidate",
        headers={"X-PADOS-Control-Token": CONTROL_VALUE},
    )

    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["candidate"] == {
        "kind": "RESULT_CANDIDATE",
        "candidate_id": "candidate-123",
        "candidate_digest": "a" * 64,
        "task_id": "task-123",
        "project_id": PROJECT_ID,
        "classification": "PUBLIC",
        "expected_scope_version": 7,
        "verification_digest": "b" * 64,
        "loop_id": LOOP_ID,
        "verification_status": "PASSED",
        "approval_ref": APPROVAL_ID,
        "decision_status": "WAITING_APPROVAL",
    }
    assert "private_body" not in response.text
    assert "accept" not in payload["candidate"]["decision_status"].lower()


def test_cli_can_submit_a_candidate_without_gaining_acceptance_authority(monkeypatch: pytest.MonkeyPatch) -> None:
    import contextlib

    from pados.config import Settings
    from pados.cli import main as cli
    from pados.loop import acceptance, scope_authority

    settings = Settings(PADOS_PRODUCT_DATABASE_NAME="pados_dogfood_proof")
    monkeypatch.setattr(cli, "get_settings", lambda: settings)

    @contextlib.contextmanager
    def no_db(_: Any) -> Iterator[object]:
        yield object()

    monkeypatch.setattr(cli, "session_scope", no_db)
    monkeypatch.setattr(cli.loop_service, "get_loop", lambda _session, _loop_id: {"project_id": PROJECT_ID})
    monkeypatch.setattr(scope_authority, "read_current_scope_version", lambda _settings, _project_id: 7)
    monkeypatch.setattr(
        acceptance,
        "submit_result_candidate",
        lambda _session, _loop_id, *, current_scope_version: {
            "candidate_id": "candidate-123", "candidate_digest": "a" * 64, "task_id": "task-123",
            "project_id": PROJECT_ID, "classification": "PUBLIC", "expected_scope_version": 7,
            "verification_digest": "b" * 64, "loop_id": LOOP_ID, "verification_status": "PASSED",
            "approval_id": APPROVAL_ID, "candidate_json": {"private_body": "must not be printed"},
        },
    )

    result = CliRunner().invoke(
        cli.app, ["loop", "submit-result-candidate", "--loop-id", LOOP_ID, "--json"]
    )

    assert result.exit_code == 0, result.output
    assert '"decision_status": "WAITING_APPROVAL"' in result.output
    assert "private_body" not in result.output
    assert "semantic_acceptance" not in result.output


def test_a_verified_candidate_without_approval_fails_closed() -> None:
    from pados.loop.acceptance import submission_projection

    with pytest.raises(ValueError, match="RESULT_CANDIDATE_APPROVAL_INCOHERENT"):
        submission_projection({"verification_status": "PASSED", "approval_id": None})

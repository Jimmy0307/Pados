"""RC2 D1: the control API route endpoint is structurally CONTROL_API and never a plan step."""
from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from fastapi.testclient import TestClient

from pados.api import app as api_app
from pados.control import security as control_security
from pados.worker import service as worker_service
from pados.worker.schemas import RouteRequest


def test_client_cannot_claim_task_loop_surface_or_step(monkeypatch: pytest.MonkeyPatch) -> None:
    captured: list[RouteRequest] = []

    @contextmanager
    def fake_session_scope(_settings: object = None) -> Iterator[object]:
        yield object()

    def fake_route(_session: object, *, route: RouteRequest, **_kwargs: Any) -> dict[str, Any]:
        captured.append(route)
        return {"job": {"status": "ROUTED"}}

    monkeypatch.setattr(control_security, "authorize_token", lambda settings, value: None)
    monkeypatch.setattr(api_app, "session_scope", fake_session_scope)
    monkeypatch.setattr(worker_service, "route_work_request", fake_route)
    client = TestClient(api_app.create_app())

    response = client.post(
        "/control/work-requests/11111111-1111-4111-8111-111111111111/route",
        headers={"X-PADOS-Control-Token": "test"},
        json={
            "approval_id": "22222222-2222-4222-8222-222222222222",
            "capability_id": "RUN_STATIC_ANALYSIS",
            "command_id": "python_version",
            "context_query": "status",
            "idempotency_key": "surface-claim",
            "surface": "TASK_LOOP",
            "step_sequence": 1,
        },
    )

    assert response.status_code == 200, response.text
    assert len(captured) == 1
    assert captured[0].surface == "CONTROL_API"
    assert captured[0].step_sequence is None


def test_route_refusal_code_is_returned_as_403_detail(monkeypatch: pytest.MonkeyPatch) -> None:
    @contextmanager
    def fake_session_scope(_settings: object = None) -> Iterator[object]:
        yield object()

    def refuse(_session: object, **_kwargs: Any) -> dict[str, Any]:
        raise PermissionError("APPROVAL_SCOPE_MISMATCH")

    monkeypatch.setattr(control_security, "authorize_token", lambda settings, value: None)
    monkeypatch.setattr(api_app, "session_scope", fake_session_scope)
    monkeypatch.setattr(worker_service, "route_work_request", refuse)
    client = TestClient(api_app.create_app())

    response = client.post(
        "/control/work-requests/11111111-1111-4111-8111-111111111111/route",
        headers={"X-PADOS-Control-Token": "test"},
        json={
            "approval_id": "22222222-2222-4222-8222-222222222222",
            "capability_id": "BUILD_RELEASE",
            "context_query": "status",
            "idempotency_key": "rc1-exploit",
        },
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "APPROVAL_SCOPE_MISMATCH"

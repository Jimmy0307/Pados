# SPDX-License-Identifier: AGPL-3.0-only
"""Operators can revoke a WORK_ROUTE approval through the control API (RC2 SEC-4); the route is token gated and
delegates to the existing control service, which refuses anything that is not a pending or approved WORK_ROUTE."""
from __future__ import annotations

import contextlib
import hashlib
from collections.abc import Iterator
from typing import Any

import pytest
from fastapi.testclient import TestClient

CONTROL_VALUE = "unique-test-control-value"
APPROVAL = "00000000-0000-0000-0000-0000000000bb"
BODY = {"version": 2, "reason": "scope changed", "idempotency_key": "revoke-1"}


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, list[dict[str, Any]]]]:
    monkeypatch.setenv("PADOS_CONTROL_API_TOKEN_HASH", hashlib.sha256(CONTROL_VALUE.encode()).hexdigest().upper())
    from pados import config
    config.get_settings.cache_clear()
    from pados.api import app as api_app
    from pados.control import service as control_service

    calls: list[dict[str, Any]] = []

    @contextlib.contextmanager
    def no_db(_: Any) -> Iterator[object]:
        yield object()

    def revoke(_session: object, approval_id: str, **kwargs: Any) -> dict[str, Any]:
        calls.append({"approval_id": approval_id, **kwargs})
        if approval_id != APPROVAL:
            raise ValueError("APPROVAL_NOT_REVOCABLE")
        return {"id": approval_id, "status": "CANCELLED", "version": 3}

    monkeypatch.setattr(api_app, "session_scope", no_db)
    monkeypatch.setattr(control_service, "revoke_approval", revoke)
    yield TestClient(api_app.create_app()), calls
    config.get_settings.cache_clear()


def test_revoke_delegates_to_the_control_service(client: tuple[TestClient, list[dict[str, Any]]]) -> None:
    http, calls = client
    response = http.post(f"/control/approvals/{APPROVAL}/revoke", json=BODY,
                         headers={"X-PADOS-Control-Token": CONTROL_VALUE})
    assert response.status_code == 200
    assert response.json()["approval"]["status"] == "CANCELLED"
    assert calls == [{"approval_id": APPROVAL, "version": 2, "reason": "scope changed",
                      "idempotency_key": "revoke-1"}]


def test_revoke_refusal_is_a_conflict(client: tuple[TestClient, list[dict[str, Any]]]) -> None:
    http, _ = client
    response = http.post("/control/approvals/00000000-0000-0000-0000-0000000000cc/revoke", json=BODY,
                         headers={"X-PADOS-Control-Token": CONTROL_VALUE})
    assert response.status_code == 409 and "APPROVAL_NOT_REVOCABLE" in response.text


def test_revoke_requires_the_control_token(client: tuple[TestClient, list[dict[str, Any]]]) -> None:
    http, calls = client
    assert http.post(f"/control/approvals/{APPROVAL}/revoke", json=BODY).status_code in (401, 403)
    assert calls == []

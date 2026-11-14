# SPDX-License-Identifier: AGPL-3.0-only
"""A work request from the local Control Center can carry a server-captured TaskEnvelope.

Without one, a plan bound to product goals (the ResultCandidate path) is refused with
TASK_ENVELOPE_REQUIRED_FOR_RUNTIME_BINDING. Capture is opt-in through ``requested_capability``: plain requests and
client-built envelope bodies are stored exactly as before.
"""
from __future__ import annotations

import contextlib
import hashlib
import json
from collections.abc import Iterator
from datetime import UTC, datetime
from typing import Any

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from pados.control.schemas import WorkRequestCreate
from pados.control.work_request_capture import (
    CAPTURED_BY,
    MAX_CAPTURED_INTENT_CHARS,
    captured_request_body,
    captured_task_id,
)
from pados.enterprise.classification import DataClassification
from pados.work_protocol.models import TaskEnvelope

PROJECT_ID = "00000000-0000-0000-0000-0000000000aa"
NOW = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)


def _body(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "project_id": PROJECT_ID, "idempotency_key": "web-work-1", "title": "Check the docs links",
        "request_text": "Run the read-only Markdown reference check.", "capability": "RUN_STATIC_ANALYSIS",
        "classification": DataClassification.INTERNAL, "scope_version": 3, "created_at": NOW}
    values.update(overrides)
    return json.loads(captured_request_body(**values))


def test_the_captured_body_holds_a_valid_hashed_envelope_bound_to_the_request() -> None:
    body = _body()
    envelope = TaskEnvelope.model_validate(body["task_envelope"])
    assert body["task_envelope_hash"] == envelope.content_hash
    assert body["request_text"] == "Run the read-only Markdown reference check."
    assert body["requested_capability"] == "RUN_STATIC_ANALYSIS"
    assert envelope.project_ref == PROJECT_ID
    assert envelope.classification == DataClassification.INTERNAL
    assert envelope.scope_version_ref == "scope:3"
    assert envelope.created_by == CAPTURED_BY
    assert envelope.task_id == captured_task_id(PROJECT_ID, "web-work-1")
    assert envelope.original_intent.splitlines()[0] == "Check the docs links"
    (goal,) = envelope.normalized_goals
    # The plan step's capability must be one the bound goal requires (route-time revalidation).
    assert goal.required_capabilities == ("RUN_STATIC_ANALYSIS",)
    assert goal.title == "Check the docs links"


def test_the_task_id_depends_only_on_project_and_idempotency_key() -> None:
    assert captured_task_id(PROJECT_ID, "k1") == captured_task_id(PROJECT_ID, "k1")
    assert captured_task_id(PROJECT_ID, "k1") != captured_task_id(PROJECT_ID, "k2")
    assert captured_task_id(PROJECT_ID, "k1") != captured_task_id("00000000-0000-0000-0000-0000000000bb", "k1")


def test_an_intent_longer_than_a_goal_objective_is_refused_with_a_closed_code() -> None:
    with pytest.raises(ValueError, match="^TASK_INTENT_TOO_LONG$"):
        _body(title="t", request_text="x" * MAX_CAPTURED_INTENT_CHARS)


def test_an_unknown_capability_is_refused_by_the_request_schema() -> None:
    with pytest.raises(ValidationError):
        WorkRequestCreate(project_id=PROJECT_ID, title="t", body="b", idempotency_key="k",
                          requested_capability="DEPLOY")  # type: ignore[arg-type]


# --- the route ---------------------------------------------------------------------------------------------------

CONTROL_VALUE = "local-capture-operator-value"


@pytest.fixture
def api(monkeypatch: pytest.MonkeyPatch) -> Iterator[tuple[TestClient, list[tuple[str, dict[str, Any]]]]]:
    from pados.api import app as api_app
    from pados.config import Settings
    from pados.control import work_request_capture
    from pados.loop import scope_authority

    settings = Settings(PADOS_CONTROL_API_TOKEN_HASH=hashlib.sha256(CONTROL_VALUE.encode()).hexdigest().upper(),
                        PADOS_PRODUCT_DATABASE_NAME="pados_capture_proof")
    monkeypatch.setattr(api_app, "get_settings", lambda: settings)

    @contextlib.contextmanager
    def no_db(_: Any) -> Iterator[object]:
        yield object()

    monkeypatch.setattr(api_app, "session_scope", no_db)
    calls: list[tuple[str, dict[str, Any]]] = []

    def plain(_session: object, payload: dict[str, Any]) -> dict[str, Any]:
        calls.append(("plain", dict(payload)))
        return {"id": "r1", **payload}

    def captured(_session: object, payload: WorkRequestCreate, *, scope_version: int | None,
                 now: datetime) -> dict[str, Any]:
        calls.append(("captured", {**payload.model_dump(mode="json"), "scope_version": scope_version}))
        if scope_version is None:
            raise ValueError("TASK_ENVELOPE_SCOPE_VERSION_UNAVAILABLE")
        return {"id": "r2", "project_id": payload.project_id}

    monkeypatch.setattr(api_app.control_service, "create_work_request", plain)
    monkeypatch.setattr(work_request_capture, "create_captured_work_request", captured)
    monkeypatch.setattr(scope_authority, "read_current_scope_version", lambda _settings, _project_id: 5)
    yield TestClient(api_app.create_app()), calls


def _post(client: TestClient, **extra: Any) -> Any:
    return client.post("/control/work-requests", headers={"X-PADOS-Control-Token": CONTROL_VALUE},
                       json={"project_id": PROJECT_ID, "title": "t", "body": "b", "idempotency_key": "k", **extra})


def test_a_request_without_a_capability_is_stored_exactly_as_before(api: Any) -> None:
    client, calls = api
    response = _post(client)
    assert response.status_code == 200, response.text
    assert calls == [("plain", {"project_id": PROJECT_ID, "title": "t", "body": "b", "idempotency_key": "k"})]


def test_a_request_with_a_capability_is_captured_against_the_current_scope_version(api: Any) -> None:
    client, calls = api
    response = _post(client, requested_capability="RUN_STATIC_ANALYSIS")
    assert response.status_code == 200, response.text
    assert [kind for kind, _ in calls] == ["captured"]
    assert calls[0][1]["scope_version"] == 5
    assert calls[0][1]["requested_capability"] == "RUN_STATIC_ANALYSIS"


def test_capture_fails_closed_when_no_scope_version_can_be_read(api: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    from pados.loop import scope_authority

    monkeypatch.setattr(scope_authority, "read_current_scope_version", lambda _settings, _project_id: None)
    client, calls = api
    response = _post(client, requested_capability="RUN_STATIC_ANALYSIS")
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "TASK_ENVELOPE_SCOPE_VERSION_UNAVAILABLE"
    assert response.json()["error"]["write_occurred"] in {"NO", "UNKNOWN"}


def test_the_console_offers_exactly_the_capabilities_the_server_accepts() -> None:
    """web/src/control-actions.tsx lists the form's choices; they must stay CapabilityId, nothing more or less."""
    import re
    from pathlib import Path

    from pados.worker.schemas import CapabilityId

    source = (Path(__file__).resolve().parents[2] / "web" / "src" / "control-actions.tsx").read_text(encoding="utf-8")
    block = source.split("export const WORK_CAPABILITIES", 1)[1].split("];", 1)[0]
    offered = re.findall(r'\{ id: "([A-Z_]+)"', block)
    assert len(offered) == len(set(offered))
    assert set(offered) == {item.value for item in CapabilityId}

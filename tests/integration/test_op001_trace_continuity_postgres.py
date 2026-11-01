# SPDX-License-Identifier: AGPL-3.0-only
"""OP-001: one request/correlation id is traceable from the API through the TaskLoop and worker job to the receipt.

The trace is identifiers only, stored beside durable records and never inside sealed content. It never authorizes,
never changes a refusal, and never replaces the receipt as evidence: the receipt and capability-result hashes verify
exactly as before. Uses the residual amendment database (PADOS_RESIDUAL_AMENDMENT_TEST_DATABASE_URL).
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine, make_url
from sqlalchemy.orm import Session

from pados.execution.context import canonical_hash
from pados.loop import scope_authority
from pados.loop import service as loop_service
from pados.operability.context import CORRELATION_ID_HEADER, REQUEST_ID_HEADER
from pados.work_protocol.models import CapabilityResult
from pados.worker import service as worker_service
from tests.integration.test_residual_plan_amendment_postgres import (
    _TEST_DATABASE_ENV,
    _install_deterministic_router,
    _seed_runtime,
)

CONTROL_VALUE = "pados-test-control-" + "value"
SECRET_SHAPED = "sk-" + "op001tracecontinuity0"


@pytest.fixture
def traced(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]) -> Iterator[dict[str, Any]]:
    database_url = os.environ.get(_TEST_DATABASE_ENV)
    if not database_url:
        pytest.skip(f"set {_TEST_DATABASE_ENV} to the dedicated local disposable test database")
    url = make_url(database_url)
    if url.database != "pados_residual_plan_test" or url.host not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail("OP-001 trace integration requires the dedicated loopback residual test database")
    for key, value in {"PADOS_DB_HOST": url.host, "PADOS_DB_PORT": str(url.port or 5432),
                       "PADOS_DB_USER": url.username or "", "PADOS_DB_PASSWORD": url.password or "",
                       "PADOS_DB_NAME": url.database, "PADOS_ARTIFACT_ROOT": str(tmp_path / "artifacts"),
                       "PADOS_CONTROL_API_TOKEN_HASH": hashlib.sha256(CONTROL_VALUE.encode()).hexdigest().upper()
                       }.items():
        monkeypatch.setenv(key, value)
    from pados import db
    from pados.config import get_settings

    get_settings.cache_clear()
    if hasattr(db.get_engine, "cache_clear"):
        db.get_engine.cache_clear()
    monkeypatch.setattr(scope_authority, "read_current_scope_version", lambda _settings, _project_id: 0)
    _install_deterministic_router(monkeypatch, ["COMPLETE"] * 6)
    engine = create_engine(database_url, pool_pre_ping=True)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    with Session(engine) as session:
        project_id, request_id, _envelope, plan_id, loop_id, approval_id = _seed_runtime(
            session, workspace, child_capability="READ_REPOSITORY")
        session.commit()
    from pados.api.app import create_app
    from pados.logging_config import get_logger

    app = create_app()  # configures JSON logging for this test's captured stdout
    # Module loggers cache their output stream on first use; bind fresh ones to this test's capture.
    monkeypatch.setattr(loop_service, "_LOG", get_logger("pados.task_loop"))
    monkeypatch.setattr(worker_service, "_LOG", get_logger("pados.worker"))
    yield {"client": TestClient(app, raise_server_exceptions=False), "engine": engine,
           "loop_id": loop_id, "request_id": request_id, "approval_id": approval_id, "capsys": capsys}
    engine.dispose()
    get_settings.cache_clear()


def _step(traced: dict[str, Any], request_id: str | None, correlation_id: str | None) -> Any:
    headers = {"X-PADOS-Control-Token": CONTROL_VALUE}
    if request_id:
        headers[REQUEST_ID_HEADER] = request_id
    if correlation_id:
        headers[CORRELATION_ID_HEADER] = correlation_id
    return traced["client"].post(f"/control/loops/{traced['loop_id']}/step", headers=headers,
                                 json={"idempotency_key": f"op001-step-{uuid4()}"})


def _loop_events(engine: Engine, loop_id: str) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        rows = connection.execute(text(
            "SELECT event_type, payload_json FROM task_loop_events WHERE loop_id = CAST(:l AS uuid) "
            "ORDER BY sequence_number"), {"l": loop_id}).mappings().all()
    return [{"type": row["event_type"], "payload": row["payload_json"]} for row in rows]


def _job_events(engine: Engine, job_id: str) -> list[dict[str, Any]]:
    with engine.connect() as connection:
        rows = connection.execute(text(
            "SELECT event_type, payload_json FROM worker_job_events WHERE job_id = CAST(:j AS uuid) "
            "ORDER BY sequence_number"), {"j": job_id}).mappings().all()
    return [{"type": row["event_type"], "payload": row["payload_json"]} for row in rows]


def _traced_as(events: list[dict[str, Any]], request_id: str) -> list[dict[str, Any]]:
    return [event for event in events if (event["payload"].get("trace") or {}).get("request_id") == request_id]


def _logs(traced: dict[str, Any]) -> list[dict[str, Any]]:
    lines = []
    for line in traced["capsys"].readouterr().out.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            lines.append(value)
    return lines


def test_one_id_is_traceable_from_the_api_through_loop_and_job_to_the_receipt(traced: dict[str, Any]) -> None:
    response = _step(traced, "req-trace-1", "corr-trace")
    assert response.status_code == 200, response.text
    assert response.headers[REQUEST_ID_HEADER] == "req-trace-1"
    events = _traced_as(_loop_events(traced["engine"], traced["loop_id"]), "req-trace-1")
    types = {event["type"] for event in events}
    assert {"ITERATION_CREATED", "JOB_ROUTED", "CAPABILITY_RESULT_NORMALIZED"} <= types
    assert all(event["payload"]["trace"]["correlation_id"] == "corr-trace" for event in events)
    job_id = next(event["payload"]["job_id"] for event in events if event["type"] == "JOB_ROUTED")
    job_events = _job_events(traced["engine"], job_id)
    assert job_events and all(event["payload"]["trace"] == {"request_id": "req-trace-1",
                                                            "correlation_id": "corr-trace"} for event in job_events)
    receipt_event = next(event["payload"] for event in events if event["type"] == "CAPABILITY_RESULT_NORMALIZED")
    # The trace sits beside the sealed objects, never inside them, and their own hashes still verify.
    sealed = json.dumps({"r": receipt_event["execution_receipt"], "c": receipt_event["capability_result"]})
    assert "req-trace-1" not in sealed and "corr-trace" not in sealed
    result = CapabilityResult.model_validate(receipt_event["capability_result"])
    assert result.content_hash == receipt_event["capability_result_hash"]
    receipt = receipt_event["execution_receipt"]
    assert canonical_hash({k: v for k, v in receipt.items() if k not in {"receipt_id", "content_hash"}}) == \
        receipt["content_hash"] == receipt_event["execution_receipt_hash"]
    logs = [line for line in _logs(traced) if line.get("request_id") == "req-trace-1"]
    assert {line.get("event") for line in logs} >= {"http.request.finished", "task_loop.iteration.finished",
                                                    "worker.job.routed"}
    assert {line.get("component") for line in logs} >= {"api", "task_loop", "worker"}
    assert all(CONTROL_VALUE not in json.dumps(line) for line in logs)


def test_a_retry_keeps_the_correlation_and_carries_its_own_request_id(traced: dict[str, Any]) -> None:
    assert _step(traced, "req-attempt-1", "corr-retry").status_code == 200
    assert _step(traced, "req-attempt-2", "corr-retry").status_code == 200
    events = _loop_events(traced["engine"], traced["loop_id"])
    first, second = _traced_as(events, "req-attempt-1"), _traced_as(events, "req-attempt-2")
    assert first and second
    assert {e["payload"]["trace"]["correlation_id"] for e in first + second} == {"corr-retry"}
    iterations = {e["payload"].get("iteration_id") for e in first + second if e["type"] == "ITERATION_CREATED"}
    assert len(iterations) == 2


def test_work_without_a_request_gets_a_generated_trace(traced: dict[str, Any], tmp_path: Path) -> None:
    from pados.config import get_settings
    from pados.db import session_scope

    result = loop_service.run_step(get_settings(), loop_id=traced["loop_id"], artifact_root=tmp_path / "artifacts",
                                   idempotency_key=f"op001-cli-{uuid4()}", session_factory=session_scope)
    assert result["status"] in {"RUNNING", "COMPLETED"}
    created = [e for e in _loop_events(traced["engine"], traced["loop_id"]) if e["type"] == "ITERATION_CREATED"]
    trace = created[-1]["payload"]["trace"]
    assert re.fullmatch(r"[0-9a-f]{32}", trace["request_id"]) and trace["correlation_id"] == trace["request_id"]


def test_a_refusal_is_traceable_and_the_trace_never_changes_the_decision(traced: dict[str, Any]) -> None:
    # The request id equals the approval id: identifiers are never read as authorization.
    forged = traced["approval_id"]
    response = traced["client"].post(
        f"/control/work-requests/{traced['request_id']}/route",
        headers={"X-PADOS-Control-Token": CONTROL_VALUE, REQUEST_ID_HEADER: forged},
        json={"approval_id": traced["approval_id"], "capability_id": "BUILD_RELEASE", "risk_level": "LOW",
              "context_query": "op001 refusal", "idempotency_key": f"op001-route-{uuid4()}"})
    assert response.status_code == 403
    error = response.json()["error"]
    assert error["category"] == "AUTHORIZATION" and error["request_id"] == forged
    assert error["code"].startswith("APPROVAL_")
    with traced["engine"].connect() as connection:
        audits = connection.execute(text(
            "SELECT payload_json FROM control_sync_events WHERE event_type = 'ROUTE_REFUSED' "
            "AND payload_json->'trace'->>'request_id' = :r"), {"r": forged}).scalars().all()
    assert audits and audits[-1]["code"] == error["code"]
    refused = [line for line in _logs(traced) if line.get("event") == "worker.route.refused"
               and line.get("request_id") == forged]
    assert refused and refused[-1]["error_code"] == error["code"]


def test_an_unhandled_failure_is_traced_and_its_text_never_leaks(traced: dict[str, Any],
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    def failing_command(_invocation: object, *, project_root: Path) -> dict[str, object]:
        raise RuntimeError(f"command crashed with {SECRET_SHAPED} in its message")

    monkeypatch.setattr(worker_service, "run_registered_command", failing_command)
    response = _step(traced, "req-crash", "corr-crash")
    assert response.status_code == 500
    assert response.json()["error"]["code"] == "INTERNAL_ERROR"
    assert SECRET_SHAPED not in response.text
    logs = [line for line in _logs(traced) if line.get("request_id") == "req-crash"]
    assert any(line.get("event") == "task_loop.iteration.refused"
               and line.get("error_code") == "UNCLASSIFIED_RUNTIME_ERROR" for line in logs)
    assert all(SECRET_SHAPED not in json.dumps(line) for line in logs)
    # The failed step committed nothing, so no event claims the crashed request.
    assert not _traced_as(_loop_events(traced["engine"], traced["loop_id"]), "req-crash")

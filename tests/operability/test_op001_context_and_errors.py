# SPDX-License-Identifier: AGPL-3.0-only
"""OP-001: one request/operation context and one safe, versioned error envelope at the existing boundaries.

The context reaches the response headers and error body, the structured log line for the request, and a downstream
call made while serving it. Error responses keep the existing ``detail`` and add ``error`` (``pados.error.v1``). Nothing
here authorizes, schedules or accepts anything.
"""
from __future__ import annotations

import hashlib
import json
import re
from collections.abc import Iterator
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from pados.operability.context import (
    CORRELATION_ID_HEADER,
    REQUEST_ID_HEADER,
    OperationContext,
    bind_context,
    current_context,
    downstream_headers,
)
from pados.operability.errors import (
    ERROR_SCHEMA_VERSION,
    HTTP_STATUS_BY_CATEGORY,
    ErrorCategory,
    ErrorEnvelope,
    PadosError,
    WriteOutcome,
)

CONTROL_VALUE = "pados-test-control-" + "value"
GENERATED = re.compile(r"^[0-9a-f]{32}$")


@pytest.fixture(autouse=True)
def _unique_control_hash(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from pados.config import get_settings

    monkeypatch.setenv("PADOS_CONTROL_API_TOKEN_HASH", hashlib.sha256(CONTROL_VALUE.encode()).hexdigest().upper())
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _app() -> Any:
    from pados.api.app import create_app

    return create_app()


def _log_lines(captured: str) -> list[dict[str, Any]]:
    lines = []
    for line in captured.splitlines():
        try:
            value = json.loads(line)
        except ValueError:
            continue
        if isinstance(value, dict):
            lines.append(value)
    return lines


# --- the envelope model -------------------------------------------------------------------------------------------

def test_every_category_has_an_explicit_http_status() -> None:
    assert set(HTTP_STATUS_BY_CATEGORY) == set(ErrorCategory)
    assert HTTP_STATUS_BY_CATEGORY[ErrorCategory.VALIDATION] == 400
    assert HTTP_STATUS_BY_CATEGORY[ErrorCategory.CONFLICT] == 409
    assert HTTP_STATUS_BY_CATEGORY[ErrorCategory.TIMEOUT] == 504
    assert HTTP_STATUS_BY_CATEGORY[ErrorCategory.INTERNAL] == 500


@pytest.mark.parametrize("code", ["lower_case", "Has Space", "TRAILING_", "", "X" * 81])
def test_error_codes_are_stable_upper_snake_case(code: str) -> None:
    with pytest.raises(ValidationError):
        ErrorEnvelope(code=code, category=ErrorCategory.VALIDATION, message="m", retryable=False,
                      request_id="r", correlation_id="c", write_occurred=WriteOutcome.NO)


def test_the_envelope_is_versioned_and_never_retryable_for_non_transient_categories() -> None:
    for category in (ErrorCategory.VALIDATION, ErrorCategory.AUTHORIZATION, ErrorCategory.AUTHORITY,
                     ErrorCategory.CONFLICT):
        with pytest.raises(ValidationError):
            ErrorEnvelope(code="REFUSED", category=category, message="m", retryable=True, request_id="r",
                          correlation_id="c", write_occurred=WriteOutcome.NO)
    envelope = ErrorEnvelope(code="NODE_TIMEOUT", category=ErrorCategory.TIMEOUT, message="m", retryable=True,
                             request_id="r", correlation_id="c", write_occurred=WriteOutcome.UNKNOWN)
    assert envelope.schema_version == ERROR_SCHEMA_VERSION == "pados.error.v1"


# --- ingress context ----------------------------------------------------------------------------------------------

def test_a_safe_request_id_is_echoed_and_correlation_defaults_to_it() -> None:
    response = TestClient(_app()).get("/health", headers={REQUEST_ID_HEADER: "req-123.A:b"})
    assert response.headers[REQUEST_ID_HEADER] == "req-123.A:b"
    assert response.headers[CORRELATION_ID_HEADER] == "req-123.A:b"


@pytest.mark.parametrize("unsafe", ["has space", "x" * 129, "<script>", "a\tb", ""])
def test_an_unsafe_request_id_is_replaced_never_echoed(unsafe: str) -> None:
    response = TestClient(_app()).get("/health", headers={REQUEST_ID_HEADER: unsafe,
                                                          CORRELATION_ID_HEADER: "corr-1"})
    assert GENERATED.match(response.headers[REQUEST_ID_HEADER])
    assert response.headers[CORRELATION_ID_HEADER] == "corr-1"


# --- error responses ----------------------------------------------------------------------------------------------

def test_an_authorization_refusal_keeps_detail_and_adds_the_envelope() -> None:
    response = TestClient(_app()).get("/memory/search?project_id=p&query=q", headers={REQUEST_ID_HEADER: "req-auth"})
    assert response.status_code in (401, 403)
    body = response.json()
    assert "detail" in body  # unchanged contract for existing clients
    error = body["error"]
    assert error["schema_version"] == "pados.error.v1"
    assert error["category"] == "AUTHORIZATION" and error["retryable"] is False
    assert error["request_id"] == "req-auth" and error["correlation_id"] == "req-auth"
    assert error["write_occurred"] == "NO"  # a GET never writes
    assert re.match(r"^[A-Z][A-Z0-9]*(_[A-Z0-9]+)*$", error["code"])


def test_a_code_shaped_detail_becomes_the_error_code() -> None:
    from pados.operability.http import envelope_for_http_error

    envelope = envelope_for_http_error(409, "APPROVAL_NOT_REVOCABLE", method="POST")
    assert (envelope.code, envelope.category, envelope.write_occurred) == (
        "APPROVAL_NOT_REVOCABLE", ErrorCategory.CONFLICT, WriteOutcome.UNKNOWN)
    prefixed = envelope_for_http_error(403, "DEFAULT_CONTROL_TOKEN_REFUSED: run `pados init`", method="GET")
    assert prefixed.code == "DEFAULT_CONTROL_TOKEN_REFUSED"
    prose = envelope_for_http_error(409, "approval is not pending", method="POST")
    assert prose.code == "CONFLICT" and "approval is not pending" not in prose.message


def test_a_request_validation_failure_is_a_validation_envelope() -> None:
    response = TestClient(_app()).post("/control/work-requests", json={"unexpected": True},
                                       headers={"X-PADOS-Control-Token": CONTROL_VALUE})
    assert response.status_code == 422
    body = response.json()
    assert isinstance(body["detail"], list)
    assert (body["error"]["code"], body["error"]["category"]) == ("VALIDATION_FAILED", "VALIDATION")


def test_an_unhandled_exception_is_a_safe_internal_envelope_and_is_logged(capsys: pytest.CaptureFixture[str]) -> None:
    app = _app()

    @app.get("/__op001_boom")
    def boom() -> None:
        raise RuntimeError("secret-shaped internal detail INTERNAL-DETAIL-MUST-NOT-LEAK")

    response = TestClient(app, raise_server_exceptions=False).get("/__op001_boom", headers={REQUEST_ID_HEADER: "req-500"})
    assert response.status_code == 500
    error = response.json()["error"]
    assert (error["code"], error["category"], error["retryable"]) == ("INTERNAL_ERROR", "INTERNAL", False)
    assert "INTERNAL-DETAIL-MUST-NOT-LEAK" not in response.text
    assert response.headers[REQUEST_ID_HEADER] == "req-500"
    logged = [line for line in _log_lines(capsys.readouterr().out) if line.get("request_id") == "req-500"]
    assert any(line.get("error_code") == "INTERNAL_ERROR" for line in logged)
    assert all("INTERNAL-DETAIL-MUST-NOT-LEAK" not in json.dumps(line) for line in logged)


def test_a_declared_pados_error_carries_its_own_semantics() -> None:
    app = _app()

    @app.post("/__op001_declared")
    def declared() -> None:
        raise PadosError("NODE_TIMEOUT", ErrorCategory.TIMEOUT, message="The compute node did not answer in time.",
                         retryable=True, write_occurred=WriteOutcome.NO, details={"dependency": "compute_node"})

    response = TestClient(app).post("/__op001_declared")
    assert response.status_code == 504
    error = response.json()["error"]
    assert (error["code"], error["retryable"], error["write_occurred"]) == ("NODE_TIMEOUT", True, "NO")
    assert error["details"] == {"dependency": "compute_node"}


# --- one context: response, structured log and downstream call ----------------------------------------------------

def test_the_same_context_reaches_response_log_and_downstream_call(capsys: pytest.CaptureFixture[str]) -> None:
    from pados.compute_node.registry import NodeClient, NodeRegistration

    seen: list[httpx.Request] = []

    def node(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"status": "ok"})

    registration = NodeRegistration.model_validate({
        "node_id": "lcn-00000000000000000001", "node_name": "op001", "endpoint": "http://127.0.0.1:8765",
        "auth_ref": "secretref://env/OP001_NODE", "authorized": True, "allowed_capabilities": ["LOCAL_INFERENCE"],
        "max_classification": "INTERNAL", "project_ids": ["*"], "allowed_models": [],
        "security_profile": "LOCAL_SINGLE_USER"})
    app = _app()

    @app.get("/__op001_downstream")
    def downstream() -> dict[str, Any]:
        client = NodeClient(registration, "op001-credential",
                            http=lambda endpoint: httpx.Client(base_url=endpoint, transport=httpx.MockTransport(node)))
        return {"node": client._request("GET", "/health"), "context": current_context().request_id}

    response = TestClient(app).get("/__op001_downstream", headers={REQUEST_ID_HEADER: "req-flow",
                                                                    CORRELATION_ID_HEADER: "corr-flow"})
    assert response.status_code == 200 and response.json()["context"] == "req-flow"
    assert response.headers[REQUEST_ID_HEADER] == "req-flow"
    assert seen and seen[0].headers[REQUEST_ID_HEADER] == "req-flow"
    assert seen[0].headers[CORRELATION_ID_HEADER] == "corr-flow"
    finished = [line for line in _log_lines(capsys.readouterr().out)
                if line.get("event") == "http.request.finished" and line.get("request_id") == "req-flow"]
    assert len(finished) == 1
    record = finished[0]
    assert record["correlation_id"] == "corr-flow" and record["component"] == "api"
    assert record["outcome"] == "completed" and record["status_code"] == 200
    assert isinstance(record["duration_ms"], int | float)
    assert "op001-credential" not in json.dumps(record)


def test_outside_a_request_no_context_is_invented_for_downstream_calls() -> None:
    assert current_context() is None
    assert downstream_headers() == {}
    with bind_context(OperationContext(request_id="r1", correlation_id="c1", component="task_loop")):
        assert downstream_headers() == {REQUEST_ID_HEADER: "r1", CORRELATION_ID_HEADER: "c1"}
    assert current_context() is None

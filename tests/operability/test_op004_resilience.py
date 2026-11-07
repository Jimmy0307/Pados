# SPDX-License-Identifier: AGPL-3.0-only
"""OP-004: parent deadlines bound child calls; retries are bounded, jittered and never retry refusals.

Exercised at the existing boundaries: the compute-node client (status reads retried, mutations never), the worker's
model call and the API's ``X-Request-Deadline-Ms`` header.
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from pados.operability.errors import ErrorCategory, PadosError
from pados.operability.resilience import (
    DEADLINE_HEADER,
    Deadline,
    DeadlineExceeded,
    RetryPolicy,
    current_deadline,
    deadline_from_header,
    deadline_scope,
    retry_call,
)

CONTROL_VALUE = "pados-test-control-" + "value"


class Clock:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


class Transient(Exception):
    retryable = True


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from pados.config import get_settings

    monkeypatch.setenv("PADOS_CONTROL_API_TOKEN_HASH", hashlib.sha256(CONTROL_VALUE.encode()).hexdigest().upper())
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


# --- deadlines ----------------------------------------------------------------------------------------------------

def test_a_child_timeout_never_exceeds_the_remaining_budget_and_a_spent_budget_refuses() -> None:
    clock = Clock()
    deadline = Deadline.after(2.0, clock)
    assert deadline.cap(30.0) == 2.0 and deadline.cap(0.5) == 0.5
    clock.now += 2.5
    with pytest.raises(DeadlineExceeded) as refused:
        deadline.cap(30.0)
    assert (refused.value.code, refused.value.category, refused.value.retryable) == (
        "DEADLINE_EXCEEDED", ErrorCategory.TIMEOUT, False)


def test_an_inner_deadline_never_extends_the_outer_one() -> None:
    clock = Clock()
    outer, longer = Deadline.after(1.0, clock), Deadline.after(60.0, clock)
    with deadline_scope(outer), deadline_scope(longer) as effective:
        assert effective is outer and current_deadline() is outer
    assert current_deadline() is None


@pytest.mark.parametrize(("header", "valid"), [("1500", True), ("1", True), ("600000", True), ("0", False),
                                               ("600001", False), ("-5", False), ("soon", False), (None, False)])
def test_the_deadline_header_is_bounded(header: str | None, valid: bool) -> None:
    assert (deadline_from_header(header) is not None) is valid


def test_the_api_binds_the_request_deadline_for_the_handler() -> None:
    from pados.api.app import create_app

    app = create_app()

    @app.get("/__op004_deadline")
    def probe() -> dict[str, Any]:
        deadline = current_deadline()
        return {"remaining": None if deadline is None else deadline.remaining()}

    client = TestClient(app)
    assert client.get("/__op004_deadline").json() == {"remaining": None}
    remaining = client.get("/__op004_deadline", headers={DEADLINE_HEADER: "2000"}).json()["remaining"]
    assert 0 < remaining <= 2.0


# --- retries ------------------------------------------------------------------------------------------------------

def test_a_transient_failure_is_retried_with_jittered_backoff_then_succeeds() -> None:
    calls, sleeps = [], []

    def flaky() -> str:
        calls.append(1)
        if len(calls) < 3:
            raise Transient()
        return "ok"

    result = retry_call(flaky, operation="op004", policy=RetryPolicy(max_attempts=3, base_delay_s=0.2),
                        sleep=sleeps.append, rand=lambda: 0.5)
    assert result == "ok" and len(calls) == 3
    assert sleeps == [pytest.approx(0.2 * 0.75), pytest.approx(0.4 * 0.75)]


def test_retries_stop_at_the_attempt_limit() -> None:
    calls: list[int] = []

    def always() -> None:
        calls.append(1)
        raise Transient()

    with pytest.raises(Transient):
        retry_call(always, operation="op004", policy=RetryPolicy(max_attempts=2), sleep=lambda _s: None)
    assert len(calls) == 2


@pytest.mark.parametrize("refusal", [
    PermissionError("APPROVAL_SCOPE_MISMATCH"),
    ValueError("WORK_REQUEST_IDEMPOTENCY_CONTENT_CONFLICT"),
    PadosError("SCHEMA_INVALID", ErrorCategory.VALIDATION, message="m"),
    PadosError("STALE_PLAN", ErrorCategory.AUTHORITY, message="m"),
    PadosError("CONTENT_CONFLICT", ErrorCategory.CONFLICT, message="m"),
    RuntimeError("unclassified"),
])
def test_refusals_and_unclassified_failures_are_never_retried(refusal: Exception) -> None:
    calls: list[int] = []

    def refused() -> None:
        calls.append(1)
        raise refusal

    with pytest.raises(type(refusal)):
        retry_call(refused, operation="op004", sleep=lambda _s: None)
    assert len(calls) == 1


def test_retries_stop_when_the_parent_budget_cannot_cover_the_next_backoff() -> None:
    clock = Clock()
    calls: list[int] = []

    def always() -> None:
        calls.append(1)
        raise Transient()

    with deadline_scope(Deadline.after(0.05, clock)), pytest.raises(Transient):
        retry_call(always, operation="op004", policy=RetryPolicy(max_attempts=5, base_delay_s=0.2),
                   sleep=lambda _s: None, rand=lambda: 0.0)
    assert len(calls) == 1


# --- the compute-node client --------------------------------------------------------------------------------------

def _node_client(handler: Any) -> Any:
    from pados.compute_node.registry import NodeClient, NodeRegistration

    registration = NodeRegistration.model_validate({
        "node_id": "lcn-00000000000000000004", "node_name": "op004", "endpoint": "http://127.0.0.1:8765",
        "auth_ref": "secretref://env/OP004_NODE", "authorized": True, "allowed_capabilities": ["LOCAL_INFERENCE"],
        "max_classification": "INTERNAL", "project_ids": ["*"], "allowed_models": [],
        "security_profile": "LOCAL_SINGLE_USER"})
    return NodeClient(registration, "op004-credential", timeout=3.0,
                      http=lambda endpoint: httpx.Client(base_url=endpoint, transport=httpx.MockTransport(handler)))


def test_a_spent_budget_refuses_a_node_call_before_any_network_and_a_live_one_caps_the_timeout() -> None:
    from pados.compute_node.registry import NodeClientError

    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"job_id": "j", "state": "SUCCEEDED"})

    client, clock = _node_client(handler), Clock()
    with deadline_scope(Deadline.after(1.0, clock)):
        client.job("j")
        assert seen[-1].extensions["timeout"]["read"] == pytest.approx(1.0)
        clock.now += 5
        with pytest.raises(NodeClientError, match="NODE_DEADLINE_EXCEEDED"):
            client.job("j")
    assert len(seen) == 1


def test_a_node_status_read_is_retried_once_but_an_execution_never_is() -> None:
    from pados.compute_node.registry import NodeClientError

    attempts: dict[str, int] = {"GET": 0, "POST": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts[request.method] += 1
        if request.method == "GET" and attempts["GET"] == 1:
            raise httpx.ConnectError("node restarting")
        if request.method == "POST":
            raise httpx.ConnectError("node restarting")
        return httpx.Response(200, json={"job_id": "j", "state": "RUNNING"})

    client = _node_client(handler)
    assert client.job("j")["state"] == "RUNNING" and attempts["GET"] == 2
    with pytest.raises(NodeClientError, match="NODE_OFFLINE"):
        client.execute({"request": "x"})
    assert attempts["POST"] == 1


# --- the worker's model call --------------------------------------------------------------------------------------

def test_a_spent_parent_budget_closes_the_model_job_without_calling_the_model(tmp_path: Path) -> None:
    from pados.worker import service as worker_service

    class Provider:
        calls = 0

        def chat(self, *args: Any, **kwargs: Any) -> Any:
            Provider.calls += 1
            raise AssertionError("the model must not be called")

    call = worker_service._ModelCall(provider=Provider(), intent=None, timeout=30)  # type: ignore[arg-type]
    clock = Clock()
    with deadline_scope(Deadline.after(0.5, clock)):
        result = worker_service._call_model(call, job={}, handoff_path=tmp_path / "missing.json",
                                            artifact_dir=tmp_path)
    assert (result["status"], result["error_code"]) == ("TIMED_OUT", "PARENT_DEADLINE_EXCEEDED")
    assert Provider.calls == 0 and not (tmp_path / "stdout.txt").exists()

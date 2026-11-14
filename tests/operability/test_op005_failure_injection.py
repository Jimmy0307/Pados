# SPDX-License-Identifier: AGPL-3.0-only
"""OP-005: deterministic, local failure injection with the outcome each recovery runbook relies on.

Each case injects one failure at an existing boundary and asserts the stable code, the retry and write semantics, and
that no failure text, credential or partial result leaks or is kept as evidence. The recovery steps are in
docs/operations/RECOVERY_RUNBOOKS.md.
"""
from __future__ import annotations

import hashlib
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy.exc import OperationalError

CONTROL_VALUE = "pados-test-control-" + "value"
LEAK = "pass" + "word=hunter2 host=db.internal.example"


@pytest.fixture(autouse=True)
def _settings(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    from pados.config import get_settings

    monkeypatch.setenv("PADOS_CONTROL_API_TOKEN_HASH", hashlib.sha256(CONTROL_VALUE.encode()).hexdigest().upper())
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


def _disconnect(*_args: Any, **_kwargs: Any) -> Any:
    raise OperationalError("SELECT 1", {}, Exception(f"server closed the connection unexpectedly {LEAK}"))


@pytest.mark.failure_injection(component="core_api")
@pytest.mark.parametrize(("method", "path", "body", "retryable", "write"), [
    ("get", "/control/approvals", None, True, "NO"),
    ("post", "/control/work-requests", {"project_id": "00000000-0000-0000-0000-000000000000", "title": "t",
                                        "body": "b", "idempotency_key": "k"}, False, "UNKNOWN"),
])
def test_database_disconnect_is_a_dependency_failure_never_an_internal_error(
        monkeypatch: pytest.MonkeyPatch, method: str, path: str, body: Any, retryable: bool, write: str) -> None:
    from pados.api import app as app_module

    monkeypatch.setattr(app_module, "session_scope", _disconnect)
    client = TestClient(app_module.create_app(), raise_server_exceptions=False)
    kwargs: dict[str, Any] = {"headers": {"X-PADOS-Control-Token": CONTROL_VALUE}}
    if body is not None:
        kwargs["json"] = body
    response = getattr(client, method)(path, **kwargs)
    assert response.status_code == 503
    error = response.json()["error"]
    assert (error["code"], error["category"], error["retryable"], error["write_occurred"]) == (
        "DATABASE_UNAVAILABLE", "DEPENDENCY", retryable, write)
    assert "hunter2" not in response.text and "db.internal" not in response.text


def _model_call(provider: Any) -> Any:
    from pados.worker import service as worker_service

    return worker_service._ModelCall(provider=provider, intent=None, timeout=5)  # type: ignore[arg-type]


def _handoff(tmp_path: Path) -> Path:
    path = tmp_path / "handoff.json"
    path.write_text('{"request": {"title": "t"}}', encoding="utf-8")
    return path


@pytest.mark.failure_injection(component="worker_provider")
def test_provider_timeout_closes_the_job_and_keeps_no_partial_output(tmp_path: Path,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    from pados.worker import service as worker_service

    def timeout(*_args: Any, **_kwargs: Any) -> str:
        raise TimeoutError

    monkeypatch.setattr(worker_service, "_run_model_chat", timeout)
    result = worker_service._call_model(_model_call(object()), job={}, handoff_path=_handoff(tmp_path),
                                        artifact_dir=tmp_path)
    assert (result["status"], result["error_code"]) == ("TIMED_OUT", "TIMED_OUT")
    assert "stdout_path" not in result and not (tmp_path / "stdout.txt").exists()


@pytest.mark.failure_injection(component="worker_provider")
def test_a_malformed_empty_model_answer_fails_the_output_contract(tmp_path: Path,
                                                                  monkeypatch: pytest.MonkeyPatch) -> None:
    from pados.worker import service as worker_service

    monkeypatch.setattr(worker_service, "_run_model_chat", lambda *_a, **_k: "   ")
    result = worker_service._call_model(_model_call(object()), job={}, handoff_path=_handoff(tmp_path),
                                        artifact_dir=tmp_path)
    assert (result["status"], result["error_code"]) == ("FAILED", "OUTPUT_CONTRACT_FAILED")


@pytest.mark.failure_injection(component="worker_provider")
def test_a_provider_exception_becomes_a_code_and_never_its_text(tmp_path: Path,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    from pados.worker import service as worker_service

    def broken(*_args: Any, **_kwargs: Any) -> str:
        raise RuntimeError(LEAK)

    monkeypatch.setattr(worker_service, "_run_model_chat", broken)
    result = worker_service._call_model(_model_call(object()), job={}, handoff_path=_handoff(tmp_path),
                                        artifact_dir=tmp_path)
    assert (result["status"], result["error_code"]) == ("FAILED", "MODEL_PROVIDER_FAILED")
    assert "hunter2" not in str(result)


def _node(handler: Any) -> Any:
    from pados.compute_node.registry import NodeClient, NodeRegistration

    registration = NodeRegistration.model_validate({
        "node_id": "lcn-00000000000000000005", "node_name": "op005", "endpoint": "http://127.0.0.1:8765",
        "auth_ref": "secretref://env/OP005_NODE", "authorized": True, "allowed_capabilities": ["LOCAL_INFERENCE"],
        "max_classification": "INTERNAL", "project_ids": ["*"], "allowed_models": [],
        "security_profile": "LOCAL_SINGLE_USER"})
    return NodeClient(registration, "op005-credential",
                      http=lambda endpoint: httpx.Client(base_url=endpoint, transport=httpx.MockTransport(handler)))


@pytest.mark.failure_injection(component="compute_node")
def test_a_malformed_node_response_is_refused_as_schema_invalid() -> None:
    from pados.compute_node.registry import NodeClientError

    client = _node(lambda _request: httpx.Response(200, content=b"<html>proxy error</html>"))
    with pytest.raises(NodeClientError, match="NODE_SCHEMA_INVALID"):
        client.job("j")


@pytest.mark.failure_injection(component="compute_node")
def test_an_offline_node_is_reported_after_one_bounded_status_retry() -> None:
    from pados.compute_node.registry import NodeClientError

    attempts: list[int] = []

    def offline(_request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        raise httpx.ConnectError("refused")

    with pytest.raises(NodeClientError, match="NODE_OFFLINE"):
        _node(offline).job("j")
    assert len(attempts) == 2


@pytest.mark.failure_injection(component="evidence_cas")
def test_partial_or_altered_evidence_never_verifies() -> None:
    from pados.execution.receipts import execution_receipt, verify_execution_receipt

    contract = {"work_contract_id": "c", "plan_id": "p", "sequence": 1, "capability": "READ_REPOSITORY"}
    artifacts = [{"path": "stdout.txt", "sha256": "a" * 64, "size_bytes": 10}]
    receipt = execution_receipt(run_id="r", attempt=1, contract=contract, artifacts=artifacts, provider_id="p",
                                evidence_kind="DETERMINISTIC_HOST", ok=True)
    assert verify_execution_receipt(receipt, run_id="r", attempt=1, contract=contract, artifacts=artifacts)
    altered = [{**artifacts[0], "sha256": "b" * 64}]
    assert not verify_execution_receipt(receipt, run_id="r", attempt=1, contract=contract, artifacts=altered)
    assert not verify_execution_receipt(receipt, run_id="r", attempt=1, contract=contract, artifacts=[])


@pytest.mark.failure_injection(component="task_loop")
def test_an_expired_parent_deadline_refuses_a_node_call_without_sending_it() -> None:
    from pados.compute_node.registry import NodeClientError
    from pados.operability.resilience import Deadline, deadline_scope

    sent: list[int] = []
    client = _node(lambda _request: sent.append(1) or httpx.Response(200, json={}))
    with deadline_scope(Deadline(expires_at=0.0)), pytest.raises(NodeClientError, match="NODE_DEADLINE_EXCEEDED"):
        client.job("j")
    assert sent == []

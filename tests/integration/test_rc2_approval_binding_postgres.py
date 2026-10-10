"""RC2 D1: control approvals bind an immutable WORK_ROUTE subject at the routing choke point.

Needs PADOS_RC2_APPROVAL_BINDING_TEST_DATABASE_URL naming a disposable loopback database
(name ending in ``_test``) migrated to the operational head. Every test runs in one
transaction that is rolled back; only the independently committed ROUTE_REFUSED audit
rows remain, which is exactly the behaviour under test.
"""
from __future__ import annotations

import json
import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from pados.control import service as control_service
from pados.control.approval_subject import WorkRouteSubject, subject_hash
from pados.loop import scope_authority
from pados.loop import service as loop_service
from pados.loop.schemas import LoopCreate, PlanStepCreate, StepType, TaskGoalCreate, TaskPlanCreate
from pados.memory.service import bootstrap_project
from pados.worker import router as worker_router
from pados.worker import service as worker_service
from pados.worker.schemas import (
    CapabilityId,
    RiskLevel,
    RouteRequest,
    WorkerAvailability,
    WorkerSpec,
    WorkerType,
)

_TEST_DATABASE_ENV = "PADOS_RC2_APPROVAL_BINDING_TEST_DATABASE_URL"
RC1_OPERATIONS = (
    {"step_sequence": 1, "capability_id": "RUN_STATIC_ANALYSIS", "command_id": "python_version",
     "max_risk_level": "LOW"},
    {"step_sequence": 2, "capability_id": "SUMMARIZE", "command_id": None, "max_risk_level": "LOW"},
)
API_STATIC = {"step_sequence": None, "capability_id": "RUN_STATIC_ANALYSIS", "command_id": "python_version",
              "max_risk_level": "LOW"}


@pytest.fixture
def session() -> Iterator[Session]:
    database_url = os.environ.get(_TEST_DATABASE_ENV)
    if not database_url:
        pytest.skip(f"set {_TEST_DATABASE_ENV} to a dedicated local disposable test database")
    parsed = make_url(database_url)
    if not str(parsed.database or "").endswith("_test") or parsed.host not in {"localhost", "127.0.0.1", "::1"}:
        pytest.fail("approval binding integration tests require a disposable loopback *_test database")
    engine = create_engine(database_url, pool_pre_ping=True, connect_args={"connect_timeout": 2})
    db_session = Session(engine, expire_on_commit=False)
    try:
        yield db_session
    finally:
        db_session.rollback()
        db_session.close()
        engine.dispose()


def _spec(worker_id: str, worker_type: WorkerType, capabilities: list[CapabilityId],
          ceiling: RiskLevel = RiskLevel.CRITICAL) -> WorkerSpec:
    return WorkerSpec(
        worker_id=worker_id, worker_type=worker_type, display_name=worker_id, version="test",
        availability=WorkerAvailability.AVAILABLE, capabilities=capabilities, risk_ceiling=ceiling,
        supports_structured_output=True, supports_file_changes=worker_type != WorkerType.DETERMINISTIC_TOOL,
        supports_tool_calls=True, supports_streaming=False, max_context_tokens=1000, default_timeout_seconds=30,
    )


@dataclass
class Router:
    calls: list[dict[str, Any]] = field(default_factory=list)


@pytest.fixture
def router(monkeypatch: pytest.MonkeyPatch) -> Router:
    """Deterministic, Ollama and an available cloud worker; select_worker calls are recorded."""
    workers = {
        "deterministic-tool": _spec("deterministic-tool", WorkerType.DETERMINISTIC_TOOL,
                                    [CapabilityId.RUN_STATIC_ANALYSIS, CapabilityId.BUILD_RELEASE], RiskLevel.MEDIUM),
        "ollama-local": _spec("ollama-local", WorkerType.OLLAMA, [CapabilityId.SUMMARIZE], RiskLevel.LOW),
        "claude-code-runtime": _spec("claude-code-runtime", WorkerType.CLAUDE_CODE,
                                     [CapabilityId.MODIFY_CODE, CapabilityId.BUILD_RELEASE,
                                      CapabilityId.SUMMARIZE]),
    }
    recorded = Router()

    def select(*args: Any, **kwargs: Any) -> Any:
        recorded.calls.append(dict(kwargs))
        return worker_router.select_worker(*args, **kwargs)

    def run_command(_invocation: object, *, project_root: Path) -> dict[str, object]:
        del project_root
        return {"stdout": "Python 3.12", "stderr": "", "status": "SUCCEEDED", "exit_code": 0, "error_code": None}

    monkeypatch.setattr(worker_service, "discover_workers", lambda _settings: workers)
    monkeypatch.setattr(worker_service, "select_worker", select)
    monkeypatch.setattr(worker_service, "run_registered_command", run_command)
    return recorded


def _project(session: Session, root: Path) -> str:
    root.mkdir(parents=True, exist_ok=True)
    return bootstrap_project(session, name=f"rc2-d1-{uuid4()}", root_path=root, classification="internal")


def _request(session: Session, project_id: str) -> str:
    row = control_service.create_work_request(session, {
        "project_id": project_id, "title": "rc2 approval binding", "body": "bounded inspection",
        "idempotency_key": f"rc2-request-{uuid4()}"})
    return str(row["id"])


def _subject(project_id: str, request_id: str, operations: tuple[dict[str, Any], ...] | list[dict[str, Any]], *,
             surfaces: tuple[str, ...] = ("CONTROL_API", "TASK_LOOP"),
             worker_types: tuple[str, ...] = ("DETERMINISTIC_TOOL", "OLLAMA"),
             scope_version_ref: str | None = None) -> dict[str, Any]:
    return WorkRouteSubject.model_validate({
        "kind": "WORK_ROUTE", "schema_version": "pados.approval-subject.v1", "project_id": project_id,
        "request_id": request_id, "actor": {"surfaces": list(surfaces), "worker_types": list(worker_types)},
        "operations": list(operations), "scope_version_ref": scope_version_ref, "task_envelope_hash": None,
    }).model_dump(mode="json")


def _approval(session: Session, project_id: str, subject: dict[str, Any] | None, *,
              status: str = "APPROVED") -> str:
    payload: dict[str, Any] = {"project_id": project_id, "proposal_summary": "rc2 d1",
                               "idempotency_key": f"rc2-approval-{uuid4()}", "subject": subject}
    if subject is None:
        payload["proposal_hash"] = "a" * 64
    row = control_service.create_approval(session, payload)
    if status == "PENDING":
        return str(row["id"])
    decided = control_service.decide_approval(session, str(row["id"]), version=1, status=status,
                                              reason="rc2 d1", idempotency_key=f"rc2-decision-{uuid4()}")
    return str(decided["id"])


def _route(approval_id: str, capability: str, *, command_id: str | None = None, risk: str = "LOW",
           surface: str = "CONTROL_API", step_sequence: int | None = None) -> RouteRequest:
    return RouteRequest(approval_id=approval_id, capability_id=CapabilityId(capability),
                        risk_level=RiskLevel(risk), context_query="rc2 approval binding",
                        command_id=command_id, surface=surface,  # type: ignore[arg-type]
                        step_sequence=step_sequence, idempotency_key=f"rc2-route-{uuid4()}")


def _do_route(session: Session, request_id: str, route: RouteRequest, tmp_path: Path) -> dict[str, Any]:
    return worker_service.route_work_request(session, request_id=request_id, route=route, settings=None,
                                             artifact_root=tmp_path / "artifacts")


def _jobs(session: Session, request_id: str) -> int:
    return int(session.execute(text("SELECT count(*) FROM worker_jobs WHERE request_id = CAST(:id AS uuid)"),
                               {"id": request_id}).scalar_one())


def _refusals(session: Session, request_id: str) -> list[dict[str, Any]]:
    rows = session.execute(text(
        "SELECT project_id, payload_json FROM control_sync_events "
        "WHERE event_type = 'ROUTE_REFUSED' AND payload_json->>'request_id' = :id ORDER BY sequence_number"),
        {"id": request_id}).mappings().all()
    return [dict(row["payload_json"]) for row in rows]


def _assert_refused(session: Session, router: Router, tmp_path: Path, request_id: str, route: RouteRequest,
                    code: str) -> dict[str, Any]:
    jobs_before = _jobs(session, request_id)
    calls_before = len(router.calls)
    with pytest.raises(PermissionError) as refused:
        _do_route(session, request_id, route, tmp_path)
    assert str(refused.value) == code
    assert _jobs(session, request_id) == jobs_before
    assert len(router.calls) == calls_before, "select_worker must not run for a refused route"
    events = [event for event in _refusals(session, request_id) if event["code"] == code]
    assert events, f"no ROUTE_REFUSED audit row for {code}"
    return events[-1]


def test_rc1_exploit_is_refused_before_routing_with_audit(
    session: Session, router: Router, tmp_path: Path
) -> None:
    project_id = _project(session, tmp_path / "ws")
    request_id = _request(session, project_id)
    subject = _subject(project_id, request_id, RC1_OPERATIONS)
    approval_id = _approval(session, project_id, subject)
    for capability, risk in (("BUILD_RELEASE", "MEDIUM"), ("MODIFY_CODE", "MEDIUM"), ("BUILD_RELEASE", "LOW")):
        event = _assert_refused(session, router, tmp_path, request_id, _route(approval_id, capability, risk=risk),
                                "APPROVAL_SCOPE_MISMATCH")
        assert event["approval_id"] == approval_id
        assert event["capability_id"] == capability
        assert event["surface"] == "CONTROL_API"
        assert event["step_sequence"] is None
        assert event["project_id"] == project_id
        assert event["subject_hash"] == subject_hash(WorkRouteSubject.model_validate(subject))
    assert router.calls == []
    assert _jobs(session, request_id) == 0


def test_refusal_audit_survives_the_callers_rollback(session: Session, router: Router, tmp_path: Path) -> None:
    project_id = _project(session, tmp_path / "ws")
    request_id = _request(session, project_id)
    approval_id = _approval(session, project_id, _subject(project_id, request_id, RC1_OPERATIONS))
    _assert_refused(session, router, tmp_path, request_id, _route(approval_id, "BUILD_RELEASE"),
                    "APPROVAL_SCOPE_MISMATCH")
    session.rollback()
    survived = session.execute(text(
        "SELECT count(*) FROM control_sync_events WHERE event_type = 'ROUTE_REFUSED' "
        "AND payload_json->>'request_id' = :id"), {"id": request_id}).scalar_one()
    assert int(survived) == 1


def test_wrong_project_and_wrong_request(session: Session, router: Router, tmp_path: Path) -> None:
    project_a = _project(session, tmp_path / "a")
    project_b = _project(session, tmp_path / "b")
    request_a = _request(session, project_a)
    other_request_a = _request(session, project_a)
    request_b = _request(session, project_b)
    approval_b = _approval(session, project_b, _subject(project_b, request_b, [API_STATIC]))
    _assert_refused(session, router, tmp_path, request_a,
                    _route(approval_b, "RUN_STATIC_ANALYSIS", command_id="python_version"),
                    "APPROVAL_PROJECT_MISMATCH")
    approval_other = _approval(session, project_a, _subject(project_a, other_request_a, [API_STATIC]))
    _assert_refused(session, router, tmp_path, request_a,
                    _route(approval_other, "RUN_STATIC_ANALYSIS", command_id="python_version"),
                    "APPROVAL_REQUEST_MISMATCH")


def test_subject_creation_rejects_foreign_request_and_hash_mismatch(session: Session, tmp_path: Path) -> None:
    project_a = _project(session, tmp_path / "a")
    project_b = _project(session, tmp_path / "b")
    request_b = _request(session, project_b)
    with pytest.raises(PermissionError, match="APPROVAL_REQUEST_MISMATCH"):
        _approval(session, project_a, _subject(project_a, request_b, [API_STATIC]))
    with pytest.raises(PermissionError, match="APPROVAL_PROJECT_MISMATCH"):
        _approval(session, project_a, _subject(project_b, request_b, [API_STATIC]))
    request_a = _request(session, project_a)
    with pytest.raises(ValueError, match="APPROVAL_SUBJECT_HASH_MISMATCH"):
        control_service.create_approval(session, {
            "project_id": project_a, "proposal_summary": "x", "idempotency_key": f"k-{uuid4()}",
            "proposal_hash": "b" * 64, "subject": _subject(project_a, request_a, [API_STATIC])})


def test_wrong_surface_step_command_and_risk(session: Session, router: Router, tmp_path: Path) -> None:
    project_id = _project(session, tmp_path / "ws")
    request_id = _request(session, project_id)
    loop_only = _approval(session, project_id,
                          _subject(project_id, request_id, RC1_OPERATIONS, surfaces=("TASK_LOOP",)))
    _assert_refused(session, router, tmp_path, request_id,
                    _route(loop_only, "RUN_STATIC_ANALYSIS", command_id="python_version", step_sequence=1),
                    "APPROVAL_ACTOR_MISMATCH")
    both = _approval(session, project_id, _subject(project_id, request_id, RC1_OPERATIONS))
    for route in (
        _route(both, "SUMMARIZE", surface="TASK_LOOP", step_sequence=1),  # step 2 operation used for step 1
        _route(both, "RUN_STATIC_ANALYSIS", command_id="ruff_check", surface="TASK_LOOP", step_sequence=1),
        _route(both, "RUN_STATIC_ANALYSIS", command_id="python_version", surface="TASK_LOOP", step_sequence=3),
        _route(both, "RUN_STATIC_ANALYSIS", command_id="python_version", risk="MEDIUM", surface="TASK_LOOP",
               step_sequence=1),
    ):
        _assert_refused(session, router, tmp_path, request_id, route, "APPROVAL_SCOPE_MISMATCH")


def test_unbound_and_non_approved_approvals_fail_closed(session: Session, router: Router, tmp_path: Path) -> None:
    project_id = _project(session, tmp_path / "ws")
    request_id = _request(session, project_id)
    legacy = _approval(session, project_id, None)
    _assert_refused(session, router, tmp_path, request_id,
                    _route(legacy, "RUN_STATIC_ANALYSIS", command_id="python_version"), "APPROVAL_SUBJECT_UNBOUND")
    subject = _subject(project_id, request_id, [API_STATIC])
    for status in ("REJECTED", "REVISION_REQUESTED", "PENDING"):
        approval_id = _approval(session, project_id, subject, status=status)
        _assert_refused(session, router, tmp_path, request_id,
                        _route(approval_id, "RUN_STATIC_ANALYSIS", command_id="python_version"),
                        "APPROVAL_NOT_APPROVED")


def test_tampered_subject_is_blocked_by_trigger_and_hash(session: Session, router: Router, tmp_path: Path) -> None:
    project_id = _project(session, tmp_path / "ws")
    request_id = _request(session, project_id)
    approval_id = _approval(session, project_id, _subject(project_id, request_id, [API_STATIC]))
    widened = _subject(project_id, request_id, [API_STATIC, {**API_STATIC, "capability_id": "BUILD_RELEASE",
                                                              "command_id": None, "max_risk_level": "CRITICAL"}])
    for column, value in (("subject_json", json.dumps(widened)), ("proposal_hash", "c" * 64),
                          ("subject_kind", None)):
        nested = session.begin_nested()
        with pytest.raises(DBAPIError, match="APPROVAL_SUBJECT_IMMUTABLE|check"):
            session.execute(text(f"UPDATE control_approvals SET {column} = :value WHERE id = CAST(:id AS uuid)"),
                            {"value": value, "id": approval_id})
        nested.rollback()
    other_project = _project(session, tmp_path / "other")
    nested = session.begin_nested()
    with pytest.raises(DBAPIError, match="APPROVAL_SUBJECT_IMMUTABLE"):
        session.execute(text("UPDATE control_approvals SET project_id = CAST(:p AS uuid) WHERE id = CAST(:id AS uuid)"),
                        {"p": other_project, "id": approval_id})
    nested.rollback()
    forged = session.execute(text(
        """
        INSERT INTO control_approvals(project_id, proposal_hash, proposal_summary, status, idempotency_key,
                                      subject_kind, subject_json)
        VALUES (CAST(:project_id AS uuid), :hash, 'forged', 'APPROVED', :key, 'WORK_ROUTE', CAST(:subject AS jsonb))
        RETURNING id::text
        """), {"project_id": project_id, "hash": subject_hash(WorkRouteSubject.model_validate(
            _subject(project_id, request_id, [API_STATIC]))), "key": f"forged-{uuid4()}",
            "subject": json.dumps(widened)}).scalar_one()
    _assert_refused(session, router, tmp_path, request_id, _route(forged, "BUILD_RELEASE", risk="CRITICAL"),
                    "APPROVAL_SUBJECT_TAMPERED")


def test_stale_or_unreadable_scope_version(
    session: Session, router: Router, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project_id = _project(session, tmp_path / "ws")
    request_id = _request(session, project_id)
    approval_id = _approval(session, project_id,
                            _subject(project_id, request_id, [API_STATIC], scope_version_ref="scope:1"))
    for head in (2, None):
        monkeypatch.setattr(scope_authority, "read_current_scope_version", lambda _s, _p, head=head: head)
        _assert_refused(session, router, tmp_path, request_id,
                        _route(approval_id, "RUN_STATIC_ANALYSIS", command_id="python_version"),
                        "APPROVAL_SCOPE_VERSION_STALE")
    monkeypatch.setattr(scope_authority, "read_current_scope_version", lambda _s, _p: 1)
    routed = _do_route(session, request_id, _route(approval_id, "RUN_STATIC_ANALYSIS", command_id="python_version"),
                       tmp_path)
    assert routed["job"]["status"] == "SUCCEEDED"


def test_unapproved_cloud_worker_type_is_never_a_candidate(
    session: Session, router: Router, tmp_path: Path
) -> None:
    project_id = _project(session, tmp_path / "ws")
    request_id = _request(session, project_id)
    operation = {"step_sequence": None, "capability_id": "MODIFY_CODE", "command_id": None,
                 "max_risk_level": "MEDIUM"}
    approval_id = _approval(session, project_id, _subject(project_id, request_id, [operation],
                                                          worker_types=("DETERMINISTIC_TOOL",)))
    with pytest.raises(PermissionError, match="NO_ELIGIBLE_WORKER"):
        _do_route(session, request_id, _route(approval_id, "MODIFY_CODE", risk="MEDIUM"), tmp_path)
    assert router.calls[-1]["allowed_worker_types"] == frozenset({WorkerType.DETERMINISTIC_TOOL})
    assert _jobs(session, request_id) == 0


def test_busy_project_still_reports_scope_mismatch(session: Session, router: Router, tmp_path: Path) -> None:
    project_id = _project(session, tmp_path / "ws")
    request_id = _request(session, project_id)
    summarize = {"step_sequence": None, "capability_id": "SUMMARIZE", "command_id": None, "max_risk_level": "LOW"}
    approval_id = _approval(session, project_id, _subject(project_id, request_id, [summarize, API_STATIC]))
    pending = _do_route(session, request_id, _route(approval_id, "SUMMARIZE"), tmp_path)
    assert pending["job"]["status"] == "ROUTED"  # no model executor: the project is now busy
    _assert_refused(session, router, tmp_path, request_id, _route(approval_id, "BUILD_RELEASE", risk="MEDIUM"),
                    "APPROVAL_SCOPE_MISMATCH")
    with pytest.raises(PermissionError, match="BLOCKED_PROJECT_BUSY"):
        _do_route(session, request_id, _route(approval_id, "RUN_STATIC_ANALYSIS", command_id="python_version"),
                  tmp_path)


def test_happy_path_and_idempotency_is_bound_to_the_operation(
    session: Session, router: Router, tmp_path: Path
) -> None:
    project_id = _project(session, tmp_path / "ws")
    request_id = _request(session, project_id)
    approval_id = _approval(session, project_id, _subject(project_id, request_id, [API_STATIC]))
    route = _route(approval_id, "RUN_STATIC_ANALYSIS", command_id="python_version")
    routed = _do_route(session, request_id, route, tmp_path)
    assert routed["job"]["status"] == "SUCCEEDED"
    assert routed["job"]["worker_type"] == WorkerType.DETERMINISTIC_TOOL.value
    assert router.calls[-1]["allowed_worker_types"] == frozenset(
        {WorkerType.DETERMINISTIC_TOOL, WorkerType.OLLAMA})
    replay = _do_route(session, request_id, route, tmp_path)
    assert replay["routing"] == {"status": "idempotent"}
    assert str(replay["job"]["id"]) == str(routed["job"]["id"])
    hijack = route.model_copy(update={"capability_id": CapabilityId.BUILD_RELEASE, "command_id": None})
    _assert_refused(session, router, tmp_path, request_id, hijack, "APPROVAL_SCOPE_MISMATCH")
    assert _jobs(session, request_id) == 1


def test_missing_approval_is_refused_with_the_precise_diagnostic(
    session: Session, router: Router, tmp_path: Path
) -> None:
    project_id = _project(session, tmp_path / "ws")
    request_id = _request(session, project_id)
    event = _assert_refused(session, router, tmp_path, request_id,
                            _route(str(uuid4()), "RUN_STATIC_ANALYSIS", command_id="python_version"),
                            "APPROVAL_REF_NOT_FOUND")
    assert event["subject_hash"] is None


def test_subjectless_approval_on_schema_0018_never_authorizes_routing(
    session: Session, router: Router, tmp_path: Path
) -> None:
    versions = set(session.execute(text("SELECT version_num FROM alembic_version")).scalars())
    assert "20261010_0018" in versions
    columns = set(session.execute(text(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'control_approvals'")).scalars())
    assert {"subject_kind", "subject_json"} <= columns
    project_id = _project(session, tmp_path / "ws")
    request_id = _request(session, project_id)
    legacy = _approval(session, project_id, None)
    stored = session.execute(text("SELECT subject_kind, subject_json FROM control_approvals WHERE id::text = :id"),
                             {"id": legacy}).one()
    assert tuple(stored) == (None, None)
    for capability, command, risk, surface, step in (
        ("RUN_STATIC_ANALYSIS", "python_version", "LOW", "CONTROL_API", None),
        ("SUMMARIZE", None, "LOW", "CONTROL_API", None),
        ("BUILD_RELEASE", None, "MEDIUM", "CONTROL_API", None),
        ("MODIFY_CODE", None, "MEDIUM", "CONTROL_API", None),
        ("RUN_STATIC_ANALYSIS", "python_version", "LOW", "TASK_LOOP", 1),
    ):
        event = _assert_refused(
            session, router, tmp_path, request_id,
            _route(legacy, capability, command_id=command, risk=risk, surface=surface, step_sequence=step),
            "APPROVAL_SUBJECT_UNBOUND")
        assert event["project_id"] == project_id
        assert event["subject_hash"] == "a" * 64
    assert router.calls == []
    assert _jobs(session, request_id) == 0
    goal = loop_service.create_task_goal(session, TaskGoalCreate(
        project_id=project_id, request_id=request_id, objective="subjectless", success_criteria=["none"],
        approval_id=legacy, idempotency_key=f"rc2-goal-{uuid4()}"))
    with pytest.raises(PermissionError, match="APPROVAL_SUBJECT_UNBOUND"):
        loop_service.create_task_plan(session, _plan(str(goal["id"]), legacy, sequences=(1,)))


def test_idempotency_key_reuse_with_a_different_subject_is_refused(
    session: Session, router: Router, tmp_path: Path
) -> None:
    project_id = _project(session, tmp_path / "ws")
    request_id = _request(session, project_id)
    key = f"rc2-approval-{uuid4()}"
    first = control_service.create_approval(session, {
        "project_id": project_id, "proposal_summary": "s", "idempotency_key": key,
        "subject": _subject(project_id, request_id, [API_STATIC])})
    replay = control_service.create_approval(session, {
        "project_id": project_id, "proposal_summary": "s", "idempotency_key": key,
        "subject": _subject(project_id, request_id, [API_STATIC])})
    assert replay["id"] == first["id"]
    widened = [API_STATIC, {**API_STATIC, "capability_id": "BUILD_RELEASE", "command_id": None}]
    with pytest.raises(ValueError, match="APPROVAL_IDEMPOTENCY_CONTENT_CONFLICT"):
        control_service.create_approval(session, {
            "project_id": project_id, "proposal_summary": "s", "idempotency_key": key,
            "subject": _subject(project_id, request_id, widened)})
    approved = control_service.decide_approval(session, str(first["id"]), version=1, status="APPROVED",
                                               reason="rc2", idempotency_key=f"rc2-decision-{uuid4()}")
    route = _route(str(approved["id"]), "RUN_STATIC_ANALYSIS", command_id="python_version")
    assert _do_route(session, request_id, route, tmp_path)["job"]["status"] == "SUCCEEDED"
    # Reusing the route idempotency key under another approval never returns the existing job.
    other_request = _request(session, project_id)
    foreign = _approval(session, project_id, _subject(project_id, other_request, [API_STATIC]))
    _assert_refused(session, router, tmp_path, request_id, route.model_copy(update={"approval_id": foreign}),
                    "APPROVAL_REQUEST_MISMATCH")
    legacy = _approval(session, project_id, None)
    _assert_refused(session, router, tmp_path, request_id, route.model_copy(update={"approval_id": legacy}),
                    "APPROVAL_SUBJECT_UNBOUND")
    assert _jobs(session, request_id) == 1


def _plan(goal_id: str, approval_id: str, *, sequences: tuple[int, ...]) -> TaskPlanCreate:
    return TaskPlanCreate(
        task_goal_id=goal_id, approval_id=approval_id, idempotency_key=f"rc2-plan-{uuid4()}",
        steps=[PlanStepCreate(sequence=sequence, title=f"step {sequence}", step_type=StepType.INSPECT,
                              required_capability=CapabilityId.READ_REPOSITORY, command_id="python_version",
                              dependencies=[] if sequence == 1 else [sequence - 1])
               for sequence in sequences])


def _loop_op(sequence: int) -> dict[str, Any]:
    return {"step_sequence": sequence, "capability_id": "READ_REPOSITORY", "command_id": "python_version",
            "max_risk_level": "LOW"}


def test_wrong_task_plan_is_refused_at_plan_creation(session: Session, tmp_path: Path) -> None:
    project_id = _project(session, tmp_path / "ws")
    request_id = _request(session, project_id)
    other_request = _request(session, project_id)
    step_one = _approval(session, project_id, _subject(project_id, request_id, [_loop_op(1)]))
    goal = loop_service.create_task_goal(session, TaskGoalCreate(
        project_id=project_id, request_id=request_id, objective="plan binding", success_criteria=["bound"],
        approval_id=step_one, idempotency_key=f"rc2-goal-{uuid4()}"))
    with pytest.raises(PermissionError, match="APPROVAL_SCOPE_MISMATCH"):
        loop_service.create_task_plan(session, _plan(str(goal["id"]), step_one, sequences=(1, 2)))
    foreign = _approval(session, project_id, _subject(project_id, other_request, [_loop_op(1)]))
    with pytest.raises(PermissionError, match="APPROVAL_REQUEST_MISMATCH"):
        loop_service.create_task_plan(session, _plan(str(goal["id"]), foreign, sequences=(1,)))
    api_only = _approval(session, project_id,
                         _subject(project_id, request_id, [_loop_op(1)], surfaces=("CONTROL_API",)))
    with pytest.raises(PermissionError, match="APPROVAL_ACTOR_MISMATCH"):
        loop_service.create_task_plan(session, _plan(str(goal["id"]), api_only, sequences=(1,)))
    plan = loop_service.create_task_plan(session, _plan(str(goal["id"]), step_one, sequences=(1,)))
    with pytest.raises(PermissionError, match="APPROVAL_ACTOR_MISMATCH"):
        loop_service.create_loop(session, LoopCreate(task_goal_id=str(goal["id"]), active_plan_id=str(plan["id"]),
                                                     approval_id=api_only, idempotency_key=f"rc2-loop-{uuid4()}"))


def test_wrong_task_plan_step_is_refused_at_the_loop_route(
    session: Session, router: Router, tmp_path: Path
) -> None:
    project_id = _project(session, tmp_path / "ws")
    request_id = _request(session, project_id)
    step_one = _approval(session, project_id, _subject(project_id, request_id, [_loop_op(1)]))
    step_two_only = _approval(session, project_id, _subject(project_id, request_id, [_loop_op(2)]))
    goal = loop_service.create_task_goal(session, TaskGoalCreate(
        project_id=project_id, request_id=request_id, objective="step binding", success_criteria=["bound"],
        approval_id=step_one, idempotency_key=f"rc2-goal-{uuid4()}"))
    plan = loop_service.create_task_plan(session, _plan(str(goal["id"]), step_one, sequences=(1,)))
    loop = loop_service.create_loop(session, LoopCreate(
        task_goal_id=str(goal["id"]), active_plan_id=str(plan["id"]), approval_id=step_two_only,
        idempotency_key=f"rc2-loop-{uuid4()}"))
    loop_service.start_loop(session, str(loop["id"]), owner_instance="rc2-d1")
    with pytest.raises(PermissionError, match="APPROVAL_SCOPE_MISMATCH"):
        loop_service.run_one_iteration(session, loop_id=str(loop["id"]), settings=None,
                                       artifact_root=tmp_path / "artifacts", idempotency_key=f"rc2-it-{uuid4()}")
    assert router.calls == []
    assert _jobs(session, request_id) == 0
    event = _refusals(session, request_id)[-1]
    assert (event["code"], event["surface"], event["step_sequence"], event["approval_id"]) == (
        "APPROVAL_SCOPE_MISMATCH", "TASK_LOOP", 1, step_two_only)

# SPDX-License-Identifier: AGPL-3.0-only
# ruff: noqa: F811 - the D1 fixtures are imported by name and requested as test parameters
"""RC2 SEC-4: WORK_ROUTE approvals expire, can be revoked, and are consumed; request and project status are checked.

Uses the D1 fixtures (PADOS_RC2_APPROVAL_BINDING_TEST_DATABASE_URL, a disposable loopback ``*_test`` database
migrated to the operational head). Every refusal happens before a worker is selected and is audited.
"""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from pados.control import service as control_service
from pados.loop import service as loop_service
from pados.loop.schemas import LoopCreate, TaskGoalCreate
from tests.integration.test_rc2_approval_binding_postgres import (  # noqa: F401 - fixtures are used by name
    API_STATIC,
    Router,
    _approval,
    _assert_refused,
    _do_route,
    _jobs,
    _loop_op,
    _plan,
    _project,
    _request,
    _route,
    _subject,
    router,
    session,
)

STATIC = {"capability": "RUN_STATIC_ANALYSIS", "command_id": "python_version"}


def _static(approval_id: str) -> object:
    return _route(approval_id, STATIC["capability"], command_id=STATIC["command_id"])


def _row(session: Session, approval_id: str) -> dict[str, object]:
    return dict(control_service.get_approval(session, approval_id))


def _revoke(session: Session, approval_id: str, key: str = "") -> dict[str, object]:
    current = _row(session, approval_id)
    return control_service.revoke_approval(session, approval_id, version=int(str(current["version"])),
                                           reason="no longer wanted", idempotency_key=key or f"revoke-{uuid4()}")


def _loop(session: Session, project_id: str, request_id: str, approval_id: str) -> dict[str, object]:
    goal = loop_service.create_task_goal(session, TaskGoalCreate(
        project_id=project_id, request_id=request_id, objective="lifecycle", success_criteria=["bound"],
        approval_id=approval_id, idempotency_key=f"sec4-goal-{uuid4()}"))
    plan = loop_service.create_task_plan(session, _plan(str(goal["id"]), approval_id, sequences=(1,)))
    return loop_service.create_loop(session, LoopCreate(
        task_goal_id=str(goal["id"]), active_plan_id=str(plan["id"]), approval_id=approval_id,
        idempotency_key=f"sec4-loop-{uuid4()}"))


def test_approving_a_work_route_approval_sets_an_expiry(session: Session, tmp_path: Path) -> None:
    project_id = _project(session, tmp_path / "ws")
    request_id = _request(session, project_id)
    approval_id = _approval(session, project_id, _subject(project_id, request_id, [API_STATIC]))
    remaining = session.execute(text(
        "SELECT extract(epoch FROM expires_at - now()) FROM control_approvals WHERE id = CAST(:id AS uuid)"),
        {"id": approval_id}).scalar_one()
    assert remaining is not None and 0 < float(remaining) <= 86400
    unbound = _approval(session, project_id, None)
    assert _row(session, unbound)["expires_at"] is None  # subject-less approvals are outside this lifecycle


def test_expired_approval_is_refused(session: Session, router: Router, tmp_path: Path) -> None:
    project_id = _project(session, tmp_path / "ws")
    request_id = _request(session, project_id)
    approval_id = _approval(session, project_id, _subject(project_id, request_id, [API_STATIC]))
    session.execute(text("UPDATE control_approvals SET expires_at = now() - interval '1 second' "
                         "WHERE id = CAST(:id AS uuid)"), {"id": approval_id})
    _assert_refused(session, router, tmp_path, request_id, _static(approval_id), "APPROVAL_EXPIRED")
    with pytest.raises(PermissionError, match="APPROVAL_EXPIRED"):
        loop_service.create_task_goal(session, TaskGoalCreate(
            project_id=project_id, request_id=request_id, objective="expired", success_criteria=["refused"],
            approval_id=approval_id, idempotency_key=f"sec4-goal-{uuid4()}"))


def test_revoked_approval_is_refused_and_revocation_is_idempotent(
    session: Session, router: Router, tmp_path: Path
) -> None:
    project_id = _project(session, tmp_path / "ws")
    request_id = _request(session, project_id)
    approval_id = _approval(session, project_id, _subject(project_id, request_id, [API_STATIC]))
    revoked = _revoke(session, approval_id, "revoke-once")
    assert revoked["status"] == "CANCELLED"
    assert _revoke(session, approval_id, "revoke-once")["version"] == revoked["version"]
    with pytest.raises(ValueError, match="APPROVAL_NOT_REVOCABLE"):
        _revoke(session, approval_id)
    _assert_refused(session, router, tmp_path, request_id, _static(approval_id), "APPROVAL_REVOKED")
    unbound = _approval(session, project_id, None)
    with pytest.raises(ValueError, match="APPROVAL_NOT_REVOCABLE"):
        _revoke(session, unbound)  # result-candidate and other subject-less approvals keep their own lifecycle


def test_a_direct_operation_is_routed_once(session: Session, router: Router, tmp_path: Path) -> None:
    project_id = _project(session, tmp_path / "ws")
    request_id = _request(session, project_id)
    approval_id = _approval(session, project_id, _subject(project_id, request_id, [API_STATIC]))
    first = _do_route(session, request_id, _static(approval_id), tmp_path)
    assert first["job"]["status"] == "SUCCEEDED"
    _assert_refused(session, router, tmp_path, request_id, _static(approval_id), "APPROVAL_CONSUMED")
    assert _jobs(session, request_id) == 1


def test_cancelled_request_and_inactive_project_are_not_routable(
    session: Session, router: Router, tmp_path: Path
) -> None:
    project_id = _project(session, tmp_path / "ws")
    request_id = _request(session, project_id)
    approval_id = _approval(session, project_id, _subject(project_id, request_id, [API_STATIC]))
    session.execute(text("UPDATE control_work_requests SET status = 'CANCELLED' WHERE id = CAST(:id AS uuid)"),
                    {"id": request_id})
    _assert_refused(session, router, tmp_path, request_id, _static(approval_id), "REQUEST_NOT_ROUTABLE")
    other_request = _request(session, project_id)
    other = _approval(session, project_id, _subject(project_id, other_request, [API_STATIC]))
    session.execute(text("UPDATE projects SET status = 'archived' WHERE id = CAST(:id AS uuid)"), {"id": project_id})
    _assert_refused(session, router, tmp_path, other_request, _static(other), "PROJECT_NOT_ACTIVE")


def test_one_work_route_approval_backs_exactly_one_loop(session: Session, router: Router, tmp_path: Path) -> None:
    project_id = _project(session, tmp_path / "ws")
    request_id = _request(session, project_id)
    approval_id = _approval(session, project_id, _subject(project_id, request_id, [_loop_op(1), API_STATIC]))
    loop = _loop(session, project_id, request_id, approval_id)
    assert str(_row(session, approval_id)["consumed_by_loop_id"]) == str(loop["id"])
    loop_service.cancel_loop(session, str(loop["id"]))  # one mutable loop per project; free the slot first
    with pytest.raises(PermissionError, match="APPROVAL_CONSUMED"):
        _loop(session, project_id, request_id, approval_id)
    _assert_refused(session, router, tmp_path, request_id, _static(approval_id), "APPROVAL_CONSUMED")
    with pytest.raises(Exception, match="APPROVAL_CONSUMED"):
        session.execute(text("UPDATE control_approvals SET consumed_by_loop_id = gen_random_uuid() "
                             "WHERE id = CAST(:id AS uuid)"), {"id": approval_id})


def test_revoking_mid_loop_stops_before_routing_and_waits_for_a_human(
    session: Session, router: Router, tmp_path: Path
) -> None:
    project_id = _project(session, tmp_path / "ws")
    request_id = _request(session, project_id)
    approval_id = _approval(session, project_id, _subject(project_id, request_id, [_loop_op(1)]))
    loop = _loop(session, project_id, request_id, approval_id)
    loop_service.start_loop(session, str(loop["id"]), owner_instance="sec4")
    _revoke(session, approval_id)
    result = loop_service.run_one_iteration(session, loop_id=str(loop["id"]), settings=None,
                                            artifact_root=tmp_path / "artifacts", idempotency_key=f"sec4-{uuid4()}")
    after = loop_service.get_loop(session, str(loop["id"]))
    assert after["status"] == "WAITING_HUMAN"
    assert "APPROVAL_REVOKED" in str(after["final_result"]) or "APPROVAL_REVOKED" in str(result)
    assert router.calls == [] and _jobs(session, request_id) == 0

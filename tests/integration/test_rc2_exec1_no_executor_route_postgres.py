# SPDX-License-Identifier: AGPL-3.0-only
# ruff: noqa: F811 - the D1 fixtures are imported by name and requested as test parameters
"""RC2 EXEC-1: a route whose selected worker has no executor is refused at the routing entry, never left ROUTED.

PADOS executes a deterministic tool only with a registered command, and a local Ollama model. A route to a
Codex/Claude runtime, or a deterministic route with no command, used to create a job that nothing would ever run;
it stayed ROUTED until its deadline and looked like work in progress. Now the routing entry refuses it with
ROUTE_HAS_NO_EXECUTOR and an audit row, before any job, context packet or handoff exists.

Uses the D1 fixtures (PADOS_RC2_APPROVAL_BINDING_TEST_DATABASE_URL).
"""
from __future__ import annotations

from pathlib import Path

from sqlalchemy.orm import Session

from tests.integration.test_rc2_approval_binding_postgres import (  # noqa: F401 - fixtures are used by name
    Router,
    _approval,
    _do_route,
    _jobs,
    _project,
    _refusals,
    _request,
    _route,
    _subject,
    router,
    session,
)

NO_EXECUTOR = "ROUTE_HAS_NO_EXECUTOR"


def _op(capability: str, *, command_id: str | None = None, risk: str = "MEDIUM") -> dict[str, object]:
    return {"step_sequence": None, "capability_id": capability, "command_id": command_id, "max_risk_level": risk}


def _assert_no_executor(session: Session, tmp_path: Path, request_id: str, route: object) -> None:
    try:
        _do_route(session, request_id, route, tmp_path)  # type: ignore[arg-type]
    except PermissionError as refused:
        assert str(refused) == NO_EXECUTOR
    else:  # pragma: no cover - the defect: a job nothing will run
        raise AssertionError("a route with no executor was accepted")
    assert _jobs(session, request_id) == 0
    assert not (tmp_path / "artifacts" / "worker_jobs").exists() or not any(
        (tmp_path / "artifacts" / "worker_jobs").iterdir())
    assert [event for event in _refusals(session, request_id) if event["code"] == NO_EXECUTOR]


def test_a_route_to_an_approved_cloud_runtime_is_refused_not_left_routed(
    session: Session, router: Router, tmp_path: Path
) -> None:
    project_id = _project(session, tmp_path / "ws")
    request_id = _request(session, project_id)
    approval_id = _approval(session, project_id, _subject(project_id, request_id, [_op("MODIFY_CODE")],
                                                          worker_types=("CLAUDE_CODE_RUNTIME",)))
    _assert_no_executor(session, tmp_path, request_id, _route(approval_id, "MODIFY_CODE", risk="MEDIUM"))


def test_a_deterministic_route_without_a_command_is_refused_not_left_routed(
    session: Session, router: Router, tmp_path: Path
) -> None:
    project_id = _project(session, tmp_path / "ws")
    request_id = _request(session, project_id)
    approval_id = _approval(session, project_id, _subject(project_id, request_id, [_op("BUILD_RELEASE")],
                                                          worker_types=("DETERMINISTIC_TOOL",)))
    _assert_no_executor(session, tmp_path, request_id, _route(approval_id, "BUILD_RELEASE", risk="MEDIUM"))


def test_executable_routes_still_run(session: Session, router: Router, tmp_path: Path) -> None:
    project_id = _project(session, tmp_path / "ws")
    request_id = _request(session, project_id)
    approval_id = _approval(session, project_id, _subject(
        project_id, request_id, [_op("RUN_STATIC_ANALYSIS", command_id="python_version", risk="LOW")],
        worker_types=("DETERMINISTIC_TOOL",)))
    routed = _do_route(session, request_id, _route(approval_id, "RUN_STATIC_ANALYSIS", command_id="python_version"),
                       tmp_path)
    assert routed["job"]["status"] == "SUCCEEDED"
    assert routed["job"]["worker_type"] == "DETERMINISTIC_TOOL"

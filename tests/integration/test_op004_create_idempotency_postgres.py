# SPDX-License-Identifier: AGPL-3.0-only
# ruff: noqa: F811 - the residual fixture is imported by name and requested as a parameter
"""OP-004: replaying a task goal or loop create under its key returns the same record; different content conflicts.

Uses the residual amendment database (PADOS_RESIDUAL_AMENDMENT_TEST_DATABASE_URL).
"""
from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from pados.loop import scope_authority
from pados.loop import service as loop_service
from pados.loop.schemas import LoopCreate, TaskGoalCreate
from tests.integration.test_residual_plan_amendment_postgres import (  # noqa: F401 - fixture used by name
    _seed_runtime,
    disposable_session,
)

# The seed's goal content (tests/integration/test_residual_plan_amendment_postgres.py::_seed_runtime).
SEED_OBJECTIVE = "Execute the approved task plan and report discrete goal outcomes."
SEED_CRITERIA = ["each goal has a normalized result"]


@pytest.fixture
def seeded(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
           disposable_session: Session) -> tuple[Session, dict[str, Any], dict[str, Any]]:
    monkeypatch.setattr(scope_authority, "read_current_scope_version", lambda _settings, _project_id: 0)
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _, _, _, _, loop_id, _ = _seed_runtime(disposable_session, workspace, child_capability="READ_REPOSITORY")
    loop = dict(disposable_session.execute(text("SELECT * FROM task_loops WHERE id = CAST(:l AS uuid)"),
                                           {"l": loop_id}).mappings().one())
    goal = dict(disposable_session.execute(text("SELECT * FROM task_goals WHERE id = :g"),
                                           {"g": loop["task_goal_id"]}).mappings().one())
    return disposable_session, goal, loop


def _goal_payload(goal: dict[str, Any], **change: Any) -> TaskGoalCreate:
    return TaskGoalCreate(project_id=str(goal["project_id"]), request_id=str(goal["request_id"]),
                          objective=change.get("objective", SEED_OBJECTIVE), success_criteria=SEED_CRITERIA,
                          approval_id=str(goal["approval_id"]), idempotency_key=goal["idempotency_key"],
                          **{k: v for k, v in change.items() if k != "objective"})


def _loop_payload(loop: dict[str, Any], **change: Any) -> LoopCreate:
    return LoopCreate(task_goal_id=str(loop["task_goal_id"]), active_plan_id=str(loop["active_plan_id"]),
                      approval_id=str(loop["approval_id"]), max_iterations=change.get("max_iterations", 6),
                      idempotency_key=loop["idempotency_key"])


def test_a_task_goal_replay_returns_the_same_goal(seeded: tuple[Session, dict[str, Any], dict[str, Any]]) -> None:
    session, goal, _ = seeded
    assert str(loop_service.create_task_goal(session, _goal_payload(goal))["id"]) == str(goal["id"])


def test_a_task_goal_key_reused_for_different_content_conflicts(
        seeded: tuple[Session, dict[str, Any], dict[str, Any]]) -> None:
    session, goal, _ = seeded
    with pytest.raises(ValueError, match="TASK_GOAL_IDEMPOTENCY_CONTENT_CONFLICT"):
        loop_service.create_task_goal(session, _goal_payload(goal, objective="Something else entirely."))
    with pytest.raises(ValueError, match="TASK_GOAL_IDEMPOTENCY_CONTENT_CONFLICT"):
        loop_service.create_task_goal(session, _goal_payload(goal, scope_paths=["src/"]))


def test_a_loop_key_reused_for_different_bounds_conflicts_before_anything_is_bound(
        seeded: tuple[Session, dict[str, Any], dict[str, Any]]) -> None:
    session, _, loop = seeded
    with pytest.raises(ValueError, match="LOOP_IDEMPOTENCY_CONTENT_CONFLICT"):
        loop_service.create_loop(session, _loop_payload(loop, max_iterations=2))
    unchanged = session.execute(text("SELECT max_iterations FROM task_loops WHERE id = :l"),
                                {"l": loop["id"]}).scalar_one()
    assert unchanged == loop["max_iterations"] == 6

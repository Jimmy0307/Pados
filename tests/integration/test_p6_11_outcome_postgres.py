from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text

from pados.control import service as control_service
from pados.core.task_service import create_project
from pados.db import session_scope
from pados.loop import service as loop_service
from pados.project_goal import repository
from pados.project_goal.schemas import ProjectGoalCreate, ProjectPlanCreate, ProjectTaskCreate
from pados.project_goal.service import (
    ProjectControllerService,
    ProjectGoalService,
    ProjectOutcomeAggregationService,
    ProjectPlanService,
    ProjectTaskService,
)
from tests.integration.approval_subjects import work_route_subject


def _approval(session: object, project_id: str, key: str, request_id: str | None = None) -> str:
    # A task approval that launches P6.10 must name the step it authorizes (RC2 D1).
    binding = {"proposal_hash": "a" * 64} if request_id is None else {"subject": work_route_subject(project_id, request_id)}
    row = control_service.create_approval(session, {"project_id": project_id, "proposal_summary": key, "idempotency_key": key, **binding})  # type: ignore[arg-type]
    approved = control_service.decide_approval(session, str(row["id"]), version=1, status="APPROVED", reason="integration", idempotency_key=f"decide-{key}")  # type: ignore[arg-type]
    return str(approved["id"])


def _active_task_with_lineage(session: object) -> tuple[str, str, str, str, str, str]:
    project_id = create_project(session, name=f"p611-outcome-{uuid4()}", root_path=f"C:/PADOS/runtime/p611/{uuid4()}")  # type: ignore[arg-type]
    request = control_service.create_work_request(session, {"project_id": project_id, "title": "outcome request", "body": "bounded deterministic inspection", "idempotency_key": f"request-{uuid4()}"})  # type: ignore[arg-type]
    goal = ProjectGoalService().create(session, ProjectGoalCreate(project_id=project_id, objective="Aggregate one governed task", desired_outcomes=["verified completion"], success_criteria=["loop completed"], scope_paths=["src"], idempotency_key=f"goal-{uuid4()}"))  # type: ignore[arg-type]
    ProjectGoalService().approve(session, str(goal["id"]), project_id=project_id, approval_id=_approval(session, project_id, f"goal-{uuid4()}"), idempotency_key=f"goal-approve-{uuid4()}")  # type: ignore[arg-type]
    ProjectGoalService().activate(session, str(goal["id"]), project_id=project_id, idempotency_key=f"goal-active-{uuid4()}")  # type: ignore[arg-type]
    plan = ProjectPlanService().create(session, ProjectPlanCreate(project_goal_id=str(goal["id"]), planning_horizon="one cycle", strategy_summary="aggregate one deterministic task", idempotency_key=f"plan-{uuid4()}"))  # type: ignore[arg-type]
    ProjectPlanService().approve(session, str(plan["id"]), approval_id=_approval(session, project_id, f"plan-{uuid4()}"), idempotency_key=f"plan-approve-{uuid4()}")  # type: ignore[arg-type]
    task = ProjectTaskService().propose(session, ProjectTaskCreate(project_plan_id=str(plan["id"]), project_goal_id=str(goal["id"]), project_id=project_id, title="Inspect", objective="Inspect repository", success_criteria=["command passes"], scope_paths=["src"], priority=1, risk_level="LOW", estimated_cost=1, estimated_duration_seconds=60, task_goal_template={"request_id": str(request["id"])}, task_plan_template={"steps": [{"sequence": 1, "title": "Version", "step_type": "INSPECT", "required_capability": "READ_REPOSITORY", "command_id": "python_version"}]}, idempotency_key=f"task-{uuid4()}"))  # type: ignore[arg-type]
    ProjectTaskService().approve(session, str(task["id"]), project_id=project_id, approval_id=_approval(session, project_id, f"task-{uuid4()}", str(request["id"])), idempotency_key=f"task-approve-{uuid4()}")  # type: ignore[arg-type]
    ProjectControllerService().run_cycle(session, goal_id=str(goal["id"]), project_id=project_id, owner_instance="integration", idempotency_key=f"controller-{uuid4()}")  # type: ignore[arg-type]
    linked = repository.get_task(session, str(task["id"]), project_id=project_id)  # type: ignore[arg-type]
    return project_id, str(goal["id"]), str(plan["id"]), str(linked["id"]), str(linked["task_plan_id"]), str(linked["task_loop_id"])


def _persist_passed_evidence_and_complete(session: object, *, task_plan_id: str, loop_id: str) -> None:
    session.execute(text("UPDATE task_plan_steps SET status = 'PASSED' WHERE task_plan_id = CAST(:id AS uuid)"), {"id": task_plan_id})  # type: ignore[attr-defined]
    session.execute(text("INSERT INTO task_progress_snapshots(loop_id, acceptance_criteria_total, acceptance_criteria_met, tests_total, tests_passed, tests_failed, blocker_count, artifact_count, validation_status, scope_hash, state_hash) VALUES (CAST(:loop_id AS uuid), 1, 1, 1, 1, 0, 0, 0, 'PASSED', 'p611-outcome', 'p611-outcome')"), {"loop_id": loop_id})  # type: ignore[attr-defined]
    loop_service.start_loop(session, loop_id, owner_instance="integration")  # type: ignore[arg-type]
    result = loop_service.run_one_iteration(session, loop_id=loop_id, settings=None, artifact_root=None, idempotency_key=f"complete-{uuid4()}")  # type: ignore[arg-type]
    assert result["loop"]["status"] == "COMPLETED"


def test_outcome_aggregation_completes_once_and_replays_idempotently() -> None:
    with session_scope() as session:
        project_id, goal_id, project_plan_id, task_id, task_plan_id, loop_id = _active_task_with_lineage(session)
        _persist_passed_evidence_and_complete(session, task_plan_id=task_plan_id, loop_id=loop_id)
        aggregator = ProjectOutcomeAggregationService()
        first = aggregator.aggregate(session, task_id=task_id, project_id=project_id, idempotency_key="aggregate-once")
        replay = aggregator.aggregate(session, task_id=task_id, project_id=project_id, idempotency_key="aggregate-once")
        assert first["status"] == replay["status"] == "COMPLETED"
        counts = session.execute(text("SELECT (SELECT count(*) FROM project_goal_events WHERE project_goal_id = CAST(:goal_id AS uuid) AND event_type = 'P6_10_OUTCOME_VERIFIED'), (SELECT count(*) FROM project_tasks WHERE id = CAST(:task_id AS uuid) AND status = 'COMPLETED')"), {"goal_id": goal_id, "task_id": task_id}).one()
        assert tuple(int(value) for value in counts) == (1, 1)
        task = repository.get_task(session, task_id, project_id=project_id)
        assert (str(task["task_plan_id"]), str(task["task_loop_id"]), str(task["project_plan_id"])) == (task_plan_id, loop_id, project_plan_id)


def test_outcome_aggregation_rejects_non_terminal_loop_without_completing_task() -> None:
    with session_scope() as session:
        project_id, goal_id, _project_plan_id, task_id, _task_plan_id, _loop_id = _active_task_with_lineage(session)
        with pytest.raises(ValueError, match="non-terminal"):
            ProjectOutcomeAggregationService().aggregate(session, task_id=task_id, project_id=project_id, idempotency_key="aggregate-reject")
        task = repository.get_task(session, task_id, project_id=project_id)
        assert task["status"] == "ACTIVE"
        assert session.execute(text("SELECT count(*) FROM project_goal_events WHERE project_goal_id = CAST(:goal_id AS uuid) AND event_type = 'P6_10_OUTCOME_VERIFIED'"), {"goal_id": goal_id}).scalar_one() == 0

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import text

from pados.control import service as control_service
from pados.core.task_service import create_project
from pados.db import session_scope
from pados.project_goal.schemas import ProjectGoalCreate, ProjectPlanCreate, ProjectTaskCreate
from pados.project_goal.service import (
    ProjectControllerService,
    ProjectGoalService,
    ProjectPlanService,
    ProjectTaskService,
)


def approval(session: object, project_id: str, key: str) -> str:
    row = control_service.create_approval(session, {"project_id": project_id, "proposal_hash": "a" * 64, "proposal_summary": key, "idempotency_key": key})  # type: ignore[arg-type]
    approved = control_service.decide_approval(session, str(row["id"]), version=1, status="APPROVED", reason="integration", idempotency_key=f"decide-{key}")  # type: ignore[arg-type]
    return str(approved["id"])


def test_controller_creates_one_p6_10_lineage_and_replays_idempotently() -> None:
    with session_scope() as session:
        project_id = create_project(session, name=f"p611-controller-{uuid4()}", root_path=f"C:/PADOS/runtime/p611/{uuid4()}")
        request = control_service.create_work_request(session, {"project_id": project_id, "title": "controller request", "body": "bounded deterministic inspection", "idempotency_key": f"request-{uuid4()}"})
        goal_approval = approval(session, project_id, f"goal-{uuid4()}")
        goal = ProjectGoalService().create(session, ProjectGoalCreate(project_id=project_id, objective="Launch one governed task", desired_outcomes=["lineage"], success_criteria=["loop exists"], scope_paths=["src"], idempotency_key=f"goal-{uuid4()}"))
        ProjectGoalService().approve(session, str(goal["id"]), project_id=project_id, approval_id=goal_approval, idempotency_key=f"goal-approve-{uuid4()}")
        ProjectGoalService().activate(session, str(goal["id"]), project_id=project_id, idempotency_key=f"goal-active-{uuid4()}")
        plan_approval = approval(session, project_id, f"plan-{uuid4()}")
        plan = ProjectPlanService().create(session, ProjectPlanCreate(project_goal_id=str(goal["id"]), planning_horizon="one cycle", strategy_summary="launch exactly one deterministic inspection", idempotency_key=f"plan-{uuid4()}"))
        ProjectPlanService().approve(session, str(plan["id"]), approval_id=plan_approval, idempotency_key=f"plan-approve-{uuid4()}")
        task_approval = approval(session, project_id, f"task-{uuid4()}")
        task = ProjectTaskService().propose(session, ProjectTaskCreate(project_plan_id=str(plan["id"]), project_goal_id=str(goal["id"]), project_id=project_id, title="Inspect", objective="Inspect repository", success_criteria=["command passes"], scope_paths=["src"], priority=1, risk_level="LOW", estimated_cost=1, estimated_duration_seconds=60, task_goal_template={"request_id": str(request["id"])}, task_plan_template={"steps": [{"sequence": 1, "title": "Version", "step_type": "INSPECT", "required_capability": "READ_REPOSITORY", "command_id": "python_version"}]}, idempotency_key=f"task-{uuid4()}"))
        ProjectTaskService().approve(session, str(task["id"]), project_id=project_id, approval_id=task_approval, idempotency_key=f"task-approve-{uuid4()}")
        controller = ProjectControllerService()
        first = controller.run_cycle(session, goal_id=str(goal["id"]), project_id=project_id, owner_instance="integration", idempotency_key="controller-cycle")
        replay = controller.run_cycle(session, goal_id=str(goal["id"]), project_id=project_id, owner_instance="integration", idempotency_key="controller-cycle")
        assert first["activated_task_id"] == str(task["id"])
        assert replay["activated_task_id"] == str(task["id"])
        counts = session.execute(text("SELECT (SELECT count(*) FROM task_goals WHERE project_id = CAST(:project_id AS uuid)), (SELECT count(*) FROM task_plans tp JOIN task_goals tg ON tg.id = tp.task_goal_id WHERE tg.project_id = CAST(:project_id AS uuid)), (SELECT count(*) FROM task_loops WHERE project_id = CAST(:project_id AS uuid)), (SELECT count(*) FROM project_progress_snapshots WHERE project_goal_id = CAST(:goal_id AS uuid))"), {"project_id": project_id, "goal_id": str(goal["id"])}).one()
        assert tuple(int(value) for value in counts) == (1, 1, 1, 1)

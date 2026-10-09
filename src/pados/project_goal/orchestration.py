# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from pados.loop import service as loop_service
from pados.loop.schemas import (
    LoopCreate,
    PlanStepCreate,
    StopPolicy,
    TaskGoalCreate,
    TaskPlanCreate,
)
from pados.project_goal import repository
from pados.worker.schemas import RiskLevel


class P610OrchestrationAdapter:
    """Translate one approved P6.11 Project Task into governed P6.10 records."""

    def launch(
        self,
        session: Session,
        task_id: str,
        *,
        project_id: str,
        launch_key: str,
    ) -> dict[str, Any]:
        task = repository.get_task(session, task_id, project_id=project_id, lock=True)
        if task["status"] not in {"APPROVED", "READY", "ACTIVE"}:
            raise PermissionError("only an approved Project Task may launch P6.10")
        existing_key = task.get("launch_idempotency_key")
        if existing_key is not None:
            if existing_key != launch_key:
                raise ValueError("Project Task already launched with another idempotency key")
            if all(task.get(name) for name in ("task_goal_id", "task_plan_id", "task_loop_id")):
                return task
            raise RuntimeError("incomplete P6.10 lineage requires governed recovery")
        approval_id = str(task.get("approval_id") or "")
        if not approval_id:
            raise PermissionError("Project Task approval is missing")
        goal_template = dict(task["task_goal_template"])
        plan_template = dict(task["task_plan_template"])
        request_id = str(goal_template.get("request_id") or "")
        if not request_id:
            raise ValueError("P6.10 Task Goal template requires a governed request_id")
        risk = RiskLevel(str(task["risk_level"]))
        goal = loop_service.create_task_goal(
            session,
            TaskGoalCreate(
                project_id=project_id,
                request_id=request_id,
                objective=str(task["objective"]),
                success_criteria=list(task["success_criteria"]),
                constraints=list(task["constraints_json"]),
                scope_paths=list(task["scope_paths"]),
                prohibited_actions=list(
                    goal_template.get("prohibited_actions", ["merge", "push", "deploy"])
                ),
                risk_level=risk,
                approval_id=approval_id,
                idempotency_key=f"p611:{launch_key}:goal",
            ),
        )
        steps = [PlanStepCreate(**item) for item in plan_template.get("steps", [])]
        if not steps:
            raise ValueError("P6.10 Task Plan template requires at least one bounded step")
        plan = loop_service.create_task_plan(
            session,
            TaskPlanCreate(
                task_goal_id=str(goal["id"]),
                approval_id=approval_id,
                assumptions=list(plan_template.get("assumptions", [])),
                steps=steps,
                stop_policy=StopPolicy(**plan_template.get("stop_policy", {})),
                validation_policy=dict(plan_template.get("validation_policy", {})),
                rollback_policy=dict(plan_template.get("rollback_policy", {})),
                idempotency_key=f"p611:{launch_key}:plan",
            ),
        )
        loop = loop_service.create_loop(
            session,
            LoopCreate(
                task_goal_id=str(goal["id"]),
                active_plan_id=str(plan["id"]),
                approval_id=approval_id,
                idempotency_key=f"p611:{launch_key}:loop",
            ),
        )
        linked = repository.link_p6_10(
            session,
            task_id=task_id,
            launch_key=launch_key,
            task_goal_id=str(goal["id"]),
            task_plan_id=str(plan["id"]),
            task_loop_id=str(loop["id"]),
        )
        repository.append_event(
            session,
            project_id=project_id,
            goal_id=str(task["project_goal_id"]),
            event_type="P6_10_LOOP_LINKED",
            payload={
                "project_task_id": task_id,
                "task_goal_id": str(goal["id"]),
                "task_plan_id": str(plan["id"]),
                "task_loop_id": str(loop["id"]),
            },
            idempotency_key=f"p610-linked:{launch_key}",
        )
        return linked

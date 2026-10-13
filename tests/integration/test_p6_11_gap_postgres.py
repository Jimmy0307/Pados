from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import NoResultFound
from sqlalchemy.orm import Session

from pados.control import service as control_service
from pados.core.task_service import create_project
from pados.db import session_scope
from pados.project_goal import repository
from pados.project_goal.schemas import ProjectGoalCreate, ProjectPlanCreate, ProjectTaskCreate
from pados.project_goal.service import (
    ProjectGapService,
    ProjectGoalService,
    ProjectPlanService,
    ProjectTaskService,
)


def _approval(session: Session, project_id: str, key: str) -> str:
    row = control_service.create_approval(session, {"project_id": project_id, "proposal_hash": "a" * 64, "proposal_summary": key, "idempotency_key": key})
    return str(control_service.decide_approval(session, str(row["id"]), version=1, status="APPROVED", reason="native", idempotency_key=f"decide-{key}")["id"])


def _scope(gate: dict[str, Any]) -> dict[str, Any]:
    return {"project_id": str(gate["project_id"]), "goal_id": str(gate["project_goal_id"]), "goal_version": int(gate["project_goal_version"]), "plan_id": str(gate["project_plan_id"]), "plan_version": int(gate["project_plan_version"]), "task_id": str(gate["project_task_id"]), "gate_type": str(gate["gate_type"]), "risk_delta": int(gate["risk_delta"]), "scope_delta": list(gate["scope_delta"]), "budget_delta": int(gate["budget_delta"]), "time_horizon_delta": int(gate["time_horizon_delta"]), "evidence_hash": str(gate["evidence_hash"]), "valid_from": gate["valid_from"], "valid_until": gate["valid_until"], "required_action": str(gate["required_action"])}


def _lineage(session: Session) -> tuple[str, dict[str, Any], dict[str, Any], dict[str, Any]]:
    project_id = create_project(session, name=f"p611-gap-{uuid4()}", root_path=f"C:/PADOS/runtime/p611-gap/{uuid4()}")
    goal = ProjectGoalService().create(session, ProjectGoalCreate(project_id=project_id, objective="verify gaps", desired_outcomes=["governed gaps"], success_criteria=["validation"], scope_paths=["src"], idempotency_key=f"goal-{uuid4()}"))
    ProjectGoalService().approve(session, str(goal["id"]), project_id=project_id, approval_id=_approval(session, project_id, f"goal-{uuid4()}"), idempotency_key=f"goal-approve-{uuid4()}")
    ProjectGoalService().activate(session, str(goal["id"]), project_id=project_id, idempotency_key=f"goal-active-{uuid4()}")
    plan = ProjectPlanService().create(session, ProjectPlanCreate(project_goal_id=str(goal["id"]), planning_horizon="one", strategy_summary="verify gaps", idempotency_key=f"plan-{uuid4()}"))
    ProjectPlanService().approve(session, str(plan["id"]), approval_id=_approval(session, project_id, f"plan-{uuid4()}"), idempotency_key=f"plan-approve-{uuid4()}")
    task = ProjectTaskService().propose(session, ProjectTaskCreate(project_plan_id=str(plan["id"]), project_goal_id=str(goal["id"]), project_id=project_id, title="Validate", objective="Validate evidence", success_criteria=["passed"], scope_paths=["src"], priority=1, risk_level="LOW", estimated_cost=1, estimated_duration_seconds=60, task_goal_template={}, task_plan_template={}, idempotency_key=f"task-{uuid4()}"))
    return project_id, goal, plan, task


def test_native_gap_lifecycle_deduplicates_supersedes_and_reappears(tmp_path: Path) -> None:
    with session_scope() as session:
        project_id, goal, plan, task = _lineage(session)
        gaps = ProjectGapService()
        common: dict[str, Any] = {"project_id": project_id, "goal_id": str(goal["id"]), "plan_id": str(plan["id"]), "task_id": str(task["id"]), "criterion_id": "", "gap_type": "FAILED_VALIDATION", "description": "validation failed", "required_state": {"validation": "PASSED"}, "severity": "CRITICAL", "confidence": 1.0}
        opened = gaps.open(session, **common, observed_state={"validation": "FAILED-A"}, idempotency_key="open-a")
        replay = gaps.open(session, **common, observed_state={"validation": "FAILED-A"}, idempotency_key="open-a")
        assert str(opened["id"]) == str(replay["id"])
        assert opened["status"] == "OPEN" and opened["required_state_hash"] and opened["observed_state_hash"]
        with pytest.raises(PermissionError, match="scoped Human Gate"):
            gaps.transition(session, gap_id=str(opened["id"]), project_id=project_id, desired="WAIVED", actor="human:owner", reason_code="WAIVER", idempotency_key="no-gate")
        acknowledged = gaps.transition(session, gap_id=str(opened["id"]), project_id=project_id, desired="ACKNOWLEDGED", actor="human:owner", reason_code="ACK", idempotency_key="ack")
        resolving = gaps.transition(session, gap_id=str(opened["id"]), project_id=project_id, desired="RESOLVING", actor="controller", reason_code="REMEDIATION", idempotency_key="resolving")
        with pytest.raises(ValueError, match="evidence"):
            gaps.transition(session, gap_id=str(opened["id"]), project_id=project_id, desired="RESOLVED", actor="controller", reason_code="INVALID", idempotency_key="no-evidence", evidence={"resolved": True})
        resolved = gaps.transition(session, gap_id=str(opened["id"]), project_id=project_id, desired="RESOLVED", actor="controller", reason_code="VALIDATED", idempotency_key="resolved", evidence={"validation_status": "PASSED", "evidence_ids": ["validation:current"]})
        assert acknowledged["status"] == "ACKNOWLEDGED" and resolving["status"] == "RESOLVING" and resolved["status"] == "RESOLVED"
        reappeared = gaps.open(session, **common, observed_state={"validation": "FAILED-A"}, idempotency_key="reappeared")
        changed = gaps.open(session, **common, observed_state={"validation": "FAILED-B"}, idempotency_key="open-b")
        assert reappeared["status"] == "OPEN" and int(reappeared["occurrence_number"]) == 2
        assert repository.get_gap(session, str(reappeared["id"]), project_id=project_id)["status"] == "SUPERSEDED"
        assert changed["status"] == "OPEN"
        gap_count = session.execute(text("SELECT count(*) FROM project_gaps WHERE project_id = CAST(:project_id AS uuid)"), {"project_id": project_id}).scalar_one()
        event_counts = session.execute(text("SELECT count(*) FILTER (WHERE event_type = 'GAP_OPENED'), count(*) FILTER (WHERE event_type = 'GAP_SUPERSEDED'), count(*) FILTER (WHERE event_type = 'GAP_REAPPEARED') FROM project_goal_events WHERE project_goal_id = CAST(:goal_id AS uuid)"), {"goal_id": str(goal["id"])}).one()
        assert int(gap_count) == 3 and tuple(int(value) for value in event_counts) == (3, 1, 1)
        with pytest.raises(NoResultFound):
            repository.get_gap(session, str(changed["id"]), project_id=str(uuid4()))
        artifact = tmp_path / "project_goals/p6_11_gap_checkpoint/db_acceptance.json"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(json.dumps({"demonstration_id": f"p611-gap-{goal['id']}", "project_id": project_id, "goal_id": str(goal["id"]), "goal_version": int(goal["version"]), "plan_id": str(plan["id"]), "plan_version": int(plan["version"]), "task_id": str(task["id"]), "gap_ids": [str(opened["id"]), str(reappeared["id"]), str(changed["id"])], "transitions": ["OPEN", "ACKNOWLEDGED", "RESOLVING", "RESOLVED", "REAPPEARED", "SUPERSEDED"], "resolution_evidence_ids": ["validation:current"], "gap_rows": int(gap_count), "gap_opened_events": int(event_counts[0]), "gap_superseded_events": int(event_counts[1]), "gap_reappeared_events": int(event_counts[2]), "duplicate_gaps": 0, "waiver_without_gate_rejected": True, "alembic_head": "20260711_0009"}, indent=2, sort_keys=True), encoding="utf-8")

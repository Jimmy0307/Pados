from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.orm import Session

from pados.control import service as control_service
from pados.core.task_service import create_project
from pados.db import session_scope
from pados.project_goal import repository
from pados.project_goal.engine import evaluate_task_risk, stable_hash
from pados.project_goal.schemas import ProjectGoalCreate, ProjectPlanCreate, ProjectTaskCreate
from pados.project_goal.service import (
    ProjectControllerService,
    ProjectGoalService,
    ProjectHumanGateService,
    ProjectPlanService,
    ProjectTaskService,
)
from tests.integration.approval_subjects import work_route_subject


def _approval(session: object, project_id: str, key: str, request_id: str | None = None) -> str:
    # A task approval that launches P6.10 must name the step it authorizes (RC2 D1).
    binding = {"proposal_hash": "a" * 64} if request_id is None else {"subject": work_route_subject(project_id, request_id)}
    row = control_service.create_approval(session, {"project_id": project_id, "proposal_summary": key, "idempotency_key": key, **binding})  # type: ignore[arg-type]
    return str(control_service.decide_approval(session, str(row["id"]), version=1, status="APPROVED", reason="integration", idempotency_key=f"decide-{key}")["id"])  # type: ignore[arg-type]


def _scope(gate: dict[str, object]) -> dict[str, object]:
    valid_from = gate["valid_from"]
    return {
        "project_id": str(gate["project_id"]), "goal_id": str(gate["project_goal_id"]),
        "goal_version": gate["project_goal_version"], "plan_id": str(gate["project_plan_id"]),
        "plan_version": gate["project_plan_version"], "task_id": str(gate["project_task_id"]),
        "gate_type": gate["gate_type"], "risk_delta": gate["risk_delta"],
        "scope_delta": gate["scope_delta"], "budget_delta": gate["budget_delta"],
        "time_horizon_delta": gate["time_horizon_delta"], "evidence_hash": gate["evidence_hash"],
        "valid_from": valid_from.isoformat() if hasattr(valid_from, "isoformat") else valid_from,
        "valid_until": gate["valid_until"].isoformat() if hasattr(gate["valid_until"], "isoformat") else gate["valid_until"], "required_action": gate["required_action"],
    }


def _approved_gate_scenario(session: Session, *, suffix: str, valid_until: str | None = None) -> dict[str, Any]:
    project_id = create_project(session, name=f"p611-{suffix}-{uuid4()}", root_path=f"C:/PADOS/runtime/p611/{uuid4()}")
    request = control_service.create_work_request(session, {"project_id": project_id, "title": suffix, "body": "bounded", "idempotency_key": f"request-{uuid4()}"})
    goal = ProjectGoalService().create(session, ProjectGoalCreate(project_id=project_id, objective=suffix, desired_outcomes=["lineage"], success_criteria=["loop"], scope_paths=["src", "tests"], risk_budget={"max_risk_score": 40}, idempotency_key=f"goal-{uuid4()}"))
    ProjectGoalService().approve(session, str(goal["id"]), project_id=project_id, approval_id=_approval(session, project_id, f"goal-{uuid4()}"), idempotency_key=f"goal-approve-{uuid4()}")
    ProjectGoalService().activate(session, str(goal["id"]), project_id=project_id, idempotency_key=f"goal-active-{uuid4()}")
    plan = ProjectPlanService().create(session, ProjectPlanCreate(project_goal_id=str(goal["id"]), planning_horizon="one", strategy_summary=suffix, idempotency_key=f"plan-{uuid4()}"))
    ProjectPlanService().approve(session, str(plan["id"]), approval_id=_approval(session, project_id, f"plan-{uuid4()}"), idempotency_key=f"plan-approve-{uuid4()}")
    task = ProjectTaskService().propose(session, ProjectTaskCreate(project_plan_id=str(plan["id"]), project_goal_id=str(goal["id"]), project_id=project_id, title=suffix, objective=suffix, success_criteria=["pass"], scope_paths=["src"], priority=1, risk_level="HIGH", estimated_cost=1, estimated_duration_seconds=60, task_goal_template={"request_id": str(request["id"])}, task_plan_template={"steps": [{"sequence": 1, "title": "version", "step_type": "INSPECT", "required_capability": "READ_REPOSITORY", "command_id": "python_version"}]}, idempotency_key=f"task-{uuid4()}"))
    ProjectTaskService().approve(session, str(task["id"]), project_id=project_id, approval_id=_approval(session, project_id, f"task-{uuid4()}", str(request["id"])), idempotency_key=f"task-approve-{uuid4()}")
    persisted_task = repository.get_task(session, str(task["id"]), project_id=project_id)
    risk = evaluate_task_risk(task=persisted_task, goal_risk_budget=dict(goal["risk_budget"]), plan_risk_controls=dict(plan.get("risk_controls", {})), active_tasks=[])
    evidence = stable_hash({"task": str(task["id"]), "goal": int(goal["version"]), "plan": int(plan["version"]), "scope": persisted_task["scope_paths"], "risk": risk.risk_score})
    condition = stable_hash({"task": str(task["id"]), "gate_type": risk.gate_type, "risk": risk.risk_score, "reasons": risk.reason_codes, "evidence": evidence})
    gate = ProjectHumanGateService().open(session, project_id=project_id, goal_id=str(goal["id"]), plan_id=str(plan["id"]), task_id=str(task["id"]), gate_type=str(risk.gate_type), reason_codes=list(risk.reason_codes), risk_delta=risk.risk_score, scope_delta=list(risk.affected_scope_paths), evidence_hash=evidence, condition_hash=condition, idempotency_key=f"gate-{suffix}", valid_until=valid_until)
    ProjectHumanGateService().approve(session, gate_id=str(gate["id"]), project_id=project_id, approval_id=_approval(session, project_id, f"gate-{uuid4()}"), approver="human:reviewer", task_id=str(task["id"]), idempotency_key=f"approve-{suffix}", approval_scope=_scope(gate))
    return {"project_id": project_id, "goal": goal, "plan": plan, "task": task, "gate": gate, "evidence": evidence}


def test_expired_approved_gate_is_replaced_without_p610_launch() -> None:
    with session_scope() as session:
        scenario = _approved_gate_scenario(session, suffix="expired", valid_until=(datetime.now(UTC) + timedelta(minutes=5)).isoformat())
        session.execute(text("UPDATE project_human_gates SET valid_until = now() - interval '1 second' WHERE id = CAST(:id AS uuid)"), {"id": str(scenario["gate"]["id"])})
        controller = ProjectControllerService()
        blocked = controller.run_cycle(session, goal_id=str(scenario["goal"]["id"]), project_id=str(scenario["project_id"]), owner_instance="integration", idempotency_key="expired")
        replay = controller.run_cycle(session, goal_id=str(scenario["goal"]["id"]), project_id=str(scenario["project_id"]), owner_instance="integration", idempotency_key="expired")
        assert blocked["activated_task_id"] is replay["activated_task_id"] is None
        statuses = session.execute(text("SELECT status FROM project_human_gates WHERE project_goal_id = CAST(:id AS uuid) ORDER BY created_at, id"), {"id": str(scenario["goal"]["id"])}).scalars().all()
        events = session.execute(text("SELECT event_type FROM project_goal_events WHERE project_goal_id = CAST(:id AS uuid)"), {"id": str(scenario["goal"]["id"])}).scalars().all()
        assert statuses.count("EXPIRED") == 1 and statuses.count("PENDING_APPROVAL") == 1
        assert events.count("HUMAN_GATE_EXPIRED") == events.count("TASK_ACTIVATION_BLOCKED") == events.count("HUMAN_ACTION_REQUIRED") == 1
        assert session.execute(text("SELECT count(*) FROM task_goals WHERE project_id = CAST(:id AS uuid)"), {"id": str(scenario["project_id"])}).scalar_one() == 0


def test_evidence_change_supersedes_gate_without_p610_launch() -> None:
    with session_scope() as session:
        scenario = _approved_gate_scenario(session, suffix="evidence")
        session.execute(text("UPDATE project_tasks SET scope_paths = CAST(:paths AS jsonb) WHERE id = CAST(:id AS uuid)"), {"id": str(scenario["task"]["id"]), "paths": json.dumps(["src", "tests"])})
        controller = ProjectControllerService()
        blocked = controller.run_cycle(session, goal_id=str(scenario["goal"]["id"]), project_id=str(scenario["project_id"]), owner_instance="integration", idempotency_key="evidence")
        replay = controller.run_cycle(session, goal_id=str(scenario["goal"]["id"]), project_id=str(scenario["project_id"]), owner_instance="integration", idempotency_key="evidence")
        assert blocked["activated_task_id"] is replay["activated_task_id"] is None
        gates = session.execute(text("SELECT status, evidence_hash FROM project_human_gates WHERE project_goal_id = CAST(:id AS uuid) ORDER BY created_at, id"), {"id": str(scenario["goal"]["id"])}).mappings().all()
        events = session.execute(text("SELECT event_type FROM project_goal_events WHERE project_goal_id = CAST(:id AS uuid)"), {"id": str(scenario["goal"]["id"])}).scalars().all()
        assert [row["status"] for row in gates].count("SUPERSEDED") == 1 and [row["status"] for row in gates].count("PENDING_APPROVAL") == 1
        assert gates[0]["evidence_hash"] != gates[1]["evidence_hash"]
        assert events.count("HUMAN_GATE_SUPERSEDED") == events.count("TASK_ACTIVATION_BLOCKED") == events.count("HUMAN_ACTION_REQUIRED") == 1
        assert session.execute(text("SELECT count(*) FROM task_goals WHERE project_id = CAST(:id AS uuid)"), {"id": str(scenario["project_id"])}).scalar_one() == 0


def test_human_gate_blocks_wrong_approval_then_launches_once_and_replays(tmp_path: Path) -> None:
    with session_scope() as session:
        project_id = create_project(session, name=f"p611-human-{uuid4()}", root_path=f"C:/PADOS/runtime/p611/{uuid4()}")
        request = control_service.create_work_request(session, {"project_id": project_id, "title": "human gate", "body": "bounded deterministic inspection", "idempotency_key": f"request-{uuid4()}"})
        goal = ProjectGoalService().create(session, ProjectGoalCreate(project_id=project_id, objective="Gate one high risk task", desired_outcomes=["lineage"], success_criteria=["loop exists"], scope_paths=["src"], risk_budget={"max_risk_score": 40}, idempotency_key=f"goal-{uuid4()}"))
        ProjectGoalService().approve(session, str(goal["id"]), project_id=project_id, approval_id=_approval(session, project_id, f"goal-{uuid4()}"), idempotency_key=f"goal-approve-{uuid4()}")
        ProjectGoalService().activate(session, str(goal["id"]), project_id=project_id, idempotency_key=f"goal-active-{uuid4()}")
        plan = ProjectPlanService().create(session, ProjectPlanCreate(project_goal_id=str(goal["id"]), planning_horizon="one cycle", strategy_summary="gate then launch", idempotency_key=f"plan-{uuid4()}"))
        ProjectPlanService().approve(session, str(plan["id"]), approval_id=_approval(session, project_id, f"plan-{uuid4()}"), idempotency_key=f"plan-approve-{uuid4()}")
        task = ProjectTaskService().propose(session, ProjectTaskCreate(project_plan_id=str(plan["id"]), project_goal_id=str(goal["id"]), project_id=project_id, title="Inspect", objective="Inspect repository", success_criteria=["command passes"], scope_paths=["src"], priority=1, risk_level="HIGH", estimated_cost=1, estimated_duration_seconds=60, task_goal_template={"request_id": str(request["id"])}, task_plan_template={"steps": [{"sequence": 1, "title": "Version", "step_type": "INSPECT", "required_capability": "READ_REPOSITORY", "command_id": "python_version"}]}, idempotency_key=f"task-{uuid4()}"))
        ProjectTaskService().approve(session, str(task["id"]), project_id=project_id, approval_id=_approval(session, project_id, f"task-{uuid4()}", str(request["id"])), idempotency_key=f"task-approve-{uuid4()}")
        controller = ProjectControllerService()
        blocked = controller.run_cycle(session, goal_id=str(goal["id"]), project_id=project_id, owner_instance="integration", idempotency_key="blocked")
        assert blocked["activated_task_id"] is None
        gate = dict(session.execute(text("SELECT * FROM project_human_gates WHERE project_goal_id = CAST(:id AS uuid)"), {"id": str(goal["id"])}).mappings().one())
        assert session.execute(text("SELECT count(*) FROM task_goals WHERE project_id = CAST(:id AS uuid)"), {"id": project_id}).scalar_one() == 0
        try:
            ProjectHumanGateService().approve(session, gate_id=str(gate["id"]), project_id=project_id, approval_id=_approval(session, project_id, f"wrong-{uuid4()}"), approver="human:reviewer", task_id="00000000-0000-0000-0000-000000000000", idempotency_key="wrong", approval_scope=_scope(gate))
        except PermissionError:
            pass
        else:
            raise AssertionError("wrong task approval must fail")
        approval_id = _approval(session, project_id, f"gate-{uuid4()}")
        approved = ProjectHumanGateService().approve(session, gate_id=str(gate["id"]), project_id=project_id, approval_id=approval_id, approver="human:reviewer", task_id=str(task["id"]), idempotency_key="approve", approval_scope=_scope(gate))
        replay = ProjectHumanGateService().approve(session, gate_id=str(gate["id"]), project_id=project_id, approval_id=approval_id, approver="human:reviewer", task_id=str(task["id"]), idempotency_key="approve", approval_scope=_scope(gate))
        assert approved["status"] == replay["status"] == "APPROVED"
        persisted_gate = repository.get_human_gate(session, str(gate["id"]), project_id=project_id)
        persisted_task = repository.get_task(session, str(task["id"]), project_id=project_id)
        risk = evaluate_task_risk(task=persisted_task, goal_risk_budget=dict(goal["risk_budget"]), plan_risk_controls=dict(plan.get("risk_controls", {})), active_tasks=[])
        expected_evidence_hash = stable_hash({"task": str(persisted_task["id"]), "goal": int(goal["version"]), "plan": int(plan["version"]), "scope": persisted_task["scope_paths"], "risk": risk.risk_score})
        assert persisted_gate["evidence_hash"] == expected_evidence_hash
        assert ProjectHumanGateService().authorizes(persisted_gate, goal=goal, plan=plan, evidence_hash=expected_evidence_hash)
        expected_condition_hash = stable_hash({"task": str(persisted_task["id"]), "gate_type": risk.gate_type, "risk": risk.risk_score, "reasons": risk.reason_codes, "evidence": expected_evidence_hash})
        reevaluation_gate = repository.find_human_gate(session, project_id=project_id, goal_id=str(goal["id"]), task_id=str(task["id"]), gate_type=risk.gate_type or "RISK_BUDGET", condition_hash=expected_condition_hash)
        assert reevaluation_gate is not None
        assert ProjectHumanGateService().authorizes(reevaluation_gate, goal=goal, plan=plan, evidence_hash=expected_evidence_hash)
        launched = controller.run_cycle(session, goal_id=str(goal["id"]), project_id=project_id, owner_instance="integration", idempotency_key="launched")
        repeated = controller.run_cycle(session, goal_id=str(goal["id"]), project_id=project_id, owner_instance="integration", idempotency_key="launched")
        assert launched["activated_task_id"] == repeated["activated_task_id"] == str(task["id"])
        counts = session.execute(text("SELECT (SELECT count(*) FROM task_goals WHERE project_id = CAST(:id AS uuid)), (SELECT count(*) FROM project_human_gates WHERE project_goal_id = CAST(:goal AS uuid)), (SELECT count(*) FROM project_goal_events WHERE project_goal_id = CAST(:goal AS uuid) AND event_type = 'HUMAN_GATE_APPROVED')"), {"id": project_id, "goal": str(goal["id"])}).one()
        assert tuple(int(value) for value in counts) == (1, 1, 1)
        artifact = tmp_path / "project_goals/p6_11_human_gate_checkpoint/db_acceptance.json"
        artifact.parent.mkdir(parents=True, exist_ok=True)
        artifact.write_text(json.dumps({"project_id": project_id, "goal_id": str(goal["id"]), "task_id": str(task["id"]), "gate_id": str(gate["id"]), "approval_id": approval_id, "risk_blocked": True, "invalid_approval_rejected": True, "correct_approval": approved["status"], "approval_replay_duplicate_events": 0, "controller_replay_duplicate_lineage": 0, "task_goals": int(counts[0]), "human_gates": int(counts[1]), "approval_events": int(counts[2])}, indent=2, sort_keys=True), encoding="utf-8")

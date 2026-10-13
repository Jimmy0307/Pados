# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from pados.loop import service as loop_service
from pados.project_goal import repository
from pados.project_goal.engine import (
    ProjectDecisionInput,
    assert_gap_transition,
    assert_goal_transition,
    assert_human_gate_scope,
    assert_human_gate_transition,
    assert_plan_transition,
    budget_exhaustion,
    decide_project_action,
    evaluate_task_risk,
    gap_identity,
    stable_hash,
    validate_gap_resolution,
)
from pados.project_goal.orchestration import P610OrchestrationAdapter
from pados.project_goal.schemas import (
    GoalStatus,
    PlanStatus,
    ProjectGoalCreate,
    ProjectPlanCreate,
    ProjectProgressSnapshot,
    ProjectTaskCreate,
)


def _json_values(value: dict[str, Any], names: tuple[str, ...]) -> dict[str, Any]:
    result = dict(value)
    for name in names:
        result[name] = repository._json(result[name])
    return result


def _approved_approval(session: Session, approval_id: str, project_id: str) -> dict[str, Any]:
    row = session.execute(
        text("SELECT * FROM control_approvals WHERE id = CAST(:id AS uuid)"),
        {"id": approval_id},
    ).mappings().one()
    if str(row["project_id"]) != project_id:
        raise PermissionError("cross-project approval rejected")
    if row["status"] != "APPROVED":
        raise PermissionError("approval is not approved")
    return dict(row)


class ProjectGoalService:
    def create(self, session: Session, payload: ProjectGoalCreate, *, supersedes_goal_id: str | None = None) -> dict[str, Any]:
        previous = repository.get_goal(session, supersedes_goal_id, project_id=payload.project_id, lock=True) if supersedes_goal_id else None
        if previous and previous["status"] not in {"APPROVED", "ACTIVE", "AT_RISK", "WAITING_HUMAN", "BLOCKED"}:
            raise ValueError("only a governed goal may be revised")
        versions = repository.list_goals(session, payload.project_id)
        content = payload.model_dump(mode="json", exclude={"idempotency_key"})
        values = payload.model_dump(mode="json") | {
            "version": (int(previous["version"]) + 1) if previous else len(versions) + 1,
            "content_hash": stable_hash(content),
            "supersedes_goal_id": supersedes_goal_id,
        }
        values = _json_values(values, ("desired_outcomes", "success_criteria", "constraints", "scope_paths", "prohibited_actions", "assumptions", "risk_budget", "execution_budget"))
        row = repository.insert_goal(session, values)
        repository.append_event(session, project_id=payload.project_id, goal_id=str(row["id"]), event_type="PROJECT_GOAL_CREATED", payload={"version": row["version"]}, idempotency_key=f"goal-created:{payload.idempotency_key}")
        return row

    def approve(self, session: Session, goal_id: str, *, project_id: str, approval_id: str, idempotency_key: str) -> dict[str, Any]:
        goal = repository.get_goal(session, goal_id, project_id=project_id, lock=True)
        _approved_approval(session, approval_id, project_id)
        if goal["status"] == "APPROVED" and str(goal["approval_id"]) == approval_id:
            return goal
        assert_goal_transition(GoalStatus(str(goal["status"])), GoalStatus.PENDING_APPROVAL)
        repository.transition(session, "project_goals", goal_id, "DRAFT", "PENDING_APPROVAL")
        row = repository.transition(session, "project_goals", goal_id, "PENDING_APPROVAL", "APPROVED", approval_id=approval_id)
        repository.append_event(session, project_id=project_id, goal_id=goal_id, event_type="PROJECT_GOAL_APPROVED", payload={"approval_id": approval_id}, idempotency_key=idempotency_key)
        return row

    def activate(self, session: Session, goal_id: str, *, project_id: str, idempotency_key: str) -> dict[str, Any]:
        goal = repository.get_goal(session, goal_id, project_id=project_id, lock=True)
        if goal["status"] == "ACTIVE":
            return goal
        assert_goal_transition(GoalStatus(str(goal["status"])), GoalStatus.ACTIVE)
        row = repository.transition(session, "project_goals", goal_id, "APPROVED", "ACTIVE")
        repository.append_event(session, project_id=project_id, goal_id=goal_id, event_type="PROJECT_GOAL_ACTIVATED", payload={}, idempotency_key=idempotency_key)
        return row


class ProjectPlanService:
    def create(self, session: Session, payload: ProjectPlanCreate, *, supersedes_plan_id: str | None = None, plan_diff: dict[str, Any] | None = None, evidence_ids: list[str] | None = None) -> dict[str, Any]:
        goal = repository.get_goal(session, payload.project_goal_id, lock=True)
        if goal["status"] not in {"APPROVED", "ACTIVE", "AT_RISK", "WAITING_HUMAN", "BLOCKED"}:
            raise PermissionError("project goal is not governed")
        previous = repository.get_plan(session, supersedes_plan_id, lock=True) if supersedes_plan_id else None
        if previous and str(previous["project_goal_id"]) != payload.project_goal_id:
            raise PermissionError("cross-goal plan revision rejected")
        version = int(previous["version"]) + 1 if previous else 1
        content = payload.model_dump(mode="json", exclude={"idempotency_key"})
        values = payload.model_dump(mode="json") | {"version": version, "content_hash": stable_hash(content), "supersedes_plan_id": supersedes_plan_id, "plan_diff": plan_diff or {}, "supporting_evidence_ids": evidence_ids or []}
        values = _json_values(values, ("assumptions", "priorities", "task_graph", "dependency_graph", "critical_path", "review_policy", "replan_policy", "stop_policy", "risk_controls", "resource_limits", "plan_diff", "supporting_evidence_ids"))
        row = repository.insert_plan(session, values)
        repository.append_event(session, project_id=str(goal["project_id"]), goal_id=payload.project_goal_id, event_type="PROJECT_PLAN_CREATED", payload={"plan_id": str(row["id"]), "version": version}, idempotency_key=f"plan-created:{payload.idempotency_key}")
        return row

    def approve(self, session: Session, plan_id: str, *, approval_id: str, idempotency_key: str) -> dict[str, Any]:
        plan = repository.get_plan(session, plan_id, lock=True)
        goal = repository.get_goal(session, str(plan["project_goal_id"]))
        _approved_approval(session, approval_id, str(goal["project_id"]))
        if plan["status"] == "APPROVED" and str(plan["approval_id"]) == approval_id:
            return plan
        assert_plan_transition(PlanStatus(str(plan["status"])), PlanStatus.PENDING_APPROVAL)
        repository.transition(session, "project_plans", plan_id, "DRAFT", "PENDING_APPROVAL")
        row = repository.transition(session, "project_plans", plan_id, "PENDING_APPROVAL", "APPROVED", approval_id=approval_id)
        repository.append_event(session, project_id=str(goal["project_id"]), goal_id=str(goal["id"]), event_type="PROJECT_PLAN_APPROVED", payload={"plan_id": plan_id}, idempotency_key=idempotency_key)
        return row


class ProjectTaskService:
    def propose(self, session: Session, payload: ProjectTaskCreate) -> dict[str, Any]:
        goal = repository.get_goal(session, payload.project_goal_id)
        plan = repository.get_plan(session, payload.project_plan_id)
        if str(goal["project_id"]) != payload.project_id or str(plan["project_goal_id"]) != payload.project_goal_id:
            raise PermissionError("cross-project task rejected")
        if plan["status"] not in {"APPROVED", "ACTIVE"}:
            raise PermissionError("task requires an approved project plan")
        goal_scope = set(goal["scope_paths"])
        if not set(payload.scope_paths).issubset(goal_scope):
            raise PermissionError("task scope exceeds project goal")
        values = payload.model_dump(mode="json")
        values |= {
            "goal_hash": stable_hash(payload.objective),
            "scope_hash": stable_hash(sorted(payload.scope_paths)),
            "success_criteria_hash": stable_hash(payload.success_criteria),
            "dependency_hash": stable_hash([]),
            "deduplication_key": stable_hash({"goal": payload.objective, "scope": sorted(payload.scope_paths), "criteria": payload.success_criteria, "dependencies": []}),
        }
        values = _json_values(values, ("success_criteria", "constraints", "scope_paths", "required_capabilities", "task_goal_template", "task_plan_template"))
        row = repository.insert_task(session, values)
        repository.append_event(session, project_id=payload.project_id, goal_id=payload.project_goal_id, event_type="PROJECT_TASK_PROPOSED", payload={"task_id": str(row["id"])}, idempotency_key=f"task-proposed:{payload.idempotency_key}")
        return row

    def approve(self, session: Session, task_id: str, *, project_id: str, approval_id: str, idempotency_key: str) -> dict[str, Any]:
        task = repository.get_task(session, task_id, project_id=project_id, lock=True)
        _approved_approval(session, approval_id, project_id)
        if task["status"] in {"APPROVED", "READY", "ACTIVE"} and str(task["approval_id"]) == approval_id:
            return task
        if task["status"] == "PROPOSED":
            repository.transition(session, "project_tasks", task_id, "PROPOSED", "PENDING_APPROVAL")
        row = repository.transition(session, "project_tasks", task_id, "PENDING_APPROVAL", "APPROVED", approval_id=approval_id)
        repository.append_event(session, project_id=project_id, goal_id=str(row["project_goal_id"]), event_type="PROJECT_TASK_APPROVED", payload={"task_id": task_id, "approval_id": approval_id}, idempotency_key=idempotency_key)
        return row


class ProjectControllerService:
    """One deterministic, lease-protected P6.11 controller cycle per invocation."""

    def __init__(self, adapter: P610OrchestrationAdapter | None = None) -> None:
        self._adapter = adapter or P610OrchestrationAdapter()

    def run_cycle(
        self,
        session: Session,
        *,
        goal_id: str,
        project_id: str,
        owner_instance: str,
        idempotency_key: str,
    ) -> dict[str, Any]:
        goal = repository.get_goal(session, goal_id, project_id=project_id, lock=True)
        if goal["status"] != "ACTIVE":
            raise PermissionError("controller requires an active Project Goal")
        plan = repository.active_plan(session, goal_id)
        lease = repository.acquire_lease(session, goal_id=goal_id, owner=owner_instance, lease_seconds=300)
        try:
            loop = repository.get_or_create_loop(session, goal_id=goal_id, plan_id=str(plan["id"]), project_id=project_id, stop_policy=dict(plan["stop_policy"]), idempotency_key=f"loop:{goal_id}")
            exhausted = budget_exhaustion(dict(goal["execution_budget"]), {"max_project_cycles": int(loop["current_cycle"])})
            if exhausted:
                repository.append_event(session, project_id=project_id, goal_id=goal_id, event_type="PROJECT_BUDGET_EXHAUSTED", payload={"limit": exhausted}, idempotency_key=f"budget:{idempotency_key}", loop_id=str(loop["id"]))
                raise PermissionError(f"project budget exhausted: {exhausted}")
            cycle = repository.create_cycle(session, loop_id=str(loop["id"]), goal_id=goal_id, cycle_number=int(loop["current_cycle"]) + 1, input_hash=stable_hash({"goal": goal_id, "plan": str(plan["id"])}), idempotency_key=idempotency_key)
            if cycle["status"] == "COMPLETED":
                tasks = repository.list_tasks(session, goal_id=goal_id, plan_id=str(plan["id"]))
                active_task = next((str(task["id"]) for task in tasks if task["status"] == "ACTIVE"), None)
                return {"loop": loop, "cycle": cycle, "decision": cycle["decision"], "reason": cycle["decision_reason"], "activated_task_id": active_task, "lease": lease}
            tasks = repository.list_tasks(session, goal_id=goal_id, plan_id=str(plan["id"]))
            activated = self._activate_one_eligible_task(session, goal=goal, plan=plan, tasks=tasks, project_id=project_id, cycle_id=str(cycle["id"]))
            metrics = self._metrics(goal=goal, plan=plan, tasks=repository.list_tasks(session, goal_id=goal_id, plan_id=str(plan["id"])))
            snapshot_model = ProjectProgressSnapshot(**metrics)
            snapshot = repository.insert_snapshot(session, goal_id=goal_id, plan_id=str(plan["id"]), cycle_id=str(cycle["id"]), metrics=snapshot_model.model_dump(mode="json"), scope_hash=snapshot_model.scope_hash, state_hash=snapshot_model.state_hash, evidence_hash=snapshot_model.evidence_hash, idempotency_key=f"snapshot:{idempotency_key}")
            decision_input = ProjectDecisionInput(
                mandatory_criteria_pass=metrics["success_criteria_met"] == metrics["success_criteria_total"],
                required_tasks_complete=metrics["task_total"] > 0 and metrics["task_completed"] == metrics["task_total"],
                required_artifacts_exist=metrics["validated_artifacts"] >= 0,
                required_validations_pass=metrics["task_failed"] == 0,
                unresolved_critical_blockers=metrics["unresolved_blockers"],
                risk_budget_violated=metrics["open_risks"] > int(goal["risk_budget"].get("max_risk_score", 100)),
                ready_tasks=metrics["task_approved"],
                active_tasks=metrics["task_active"],
            )
            decision, reason, _status = decide_project_action(decision_input)
            repository.append_event(session, project_id=project_id, goal_id=goal_id, event_type="PROJECT_DECISION", payload={"decision": decision.value, "reason": reason, "snapshot_id": str(snapshot["id"]), "activated_task_id": activated}, idempotency_key=f"decision:{idempotency_key}", loop_id=str(loop["id"]), cycle_id=str(cycle["id"]))
            repository.complete_cycle(session, cycle_id=str(cycle["id"]), decision=decision.value, reason=reason)
            return {"loop": loop, "cycle": cycle, "snapshot": snapshot, "decision": decision.value, "reason": reason, "activated_task_id": activated, "lease": lease}
        finally:
            repository.release_lease(session, goal_id=goal_id, owner=owner_instance)

    def _activate_one_eligible_task(self, session: Session, *, goal: dict[str, Any], plan: dict[str, Any], tasks: list[dict[str, Any]], project_id: str, cycle_id: str) -> str | None:
        budget = dict(goal["execution_budget"])
        active = [task for task in tasks if task["status"] == "ACTIVE"]
        if budget_exhaustion(budget, {"max_active_tasks": len(active)}):
            return None
        for task in tasks:
            if task["status"] != "APPROVED" or not self._dependencies_satisfied(session, task, project_id):
                continue
            if not set(task["scope_paths"]).issubset(set(goal["scope_paths"])):
                raise PermissionError("task scope exceeds active Project Goal")
            risk = evaluate_task_risk(
                task=task,
                goal_risk_budget=dict(goal["risk_budget"]),
                plan_risk_controls=dict(plan.get("risk_controls", {})),
                active_tasks=active,
            )
            evidence_hash = stable_hash({"task": str(task["id"]), "goal": int(goal["version"]), "plan": int(plan["version"]), "scope": task["scope_paths"], "risk": risk.risk_score})
            condition_hash = stable_hash({"task": str(task["id"]), "gate_type": risk.gate_type, "risk": risk.risk_score, "reasons": risk.reason_codes, "evidence": evidence_hash})
            repository.append_event(session, project_id=project_id, goal_id=str(goal["id"]), event_type="RISK_EVALUATED", payload={"task_id": str(task["id"]), "risk_score": risk.risk_score, "categories": risk.categories, "reason_codes": risk.reason_codes, "evidence_hash": evidence_hash}, idempotency_key=f"risk-evaluated:{cycle_id}:{task['id']}", cycle_id=cycle_id)
            if not risk.allowed:
                gate_service = ProjectHumanGateService()
                gate_type = risk.gate_type or "RISK_BUDGET"
                gate = repository.find_human_gate(session, project_id=project_id, goal_id=str(goal["id"]), task_id=str(task["id"]), gate_type=gate_type, condition_hash=condition_hash)
                if gate and gate["status"] == "APPROVED" and not gate_service.authorizes(gate, goal=goal, plan=plan, evidence_hash=evidence_hash):
                    gate = gate_service.expire(session, gate=gate) if isinstance(gate.get("valid_until"), datetime) and datetime.now(UTC) >= gate["valid_until"] else gate_service.supersede(session, gate=gate, reason="APPROVAL_EVIDENCE_CHANGED")
                if not gate:
                    previous = repository.find_latest_human_gate(session, project_id=project_id, goal_id=str(goal["id"]), task_id=str(task["id"]), gate_type=gate_type)
                    if previous and previous["status"] == "APPROVED":
                        gate_service.supersede(session, gate=previous, reason="APPROVAL_EVIDENCE_CHANGED")
                if not gate or not gate_service.authorizes(gate, goal=goal, plan=plan, evidence_hash=evidence_hash):
                    if gate and gate["status"] == "APPROVED":
                        gate_service.supersede(session, gate=gate, reason="APPROVAL_EVIDENCE_CHANGED")
                    replacement_key = f"risk-gate:{goal['id']}:{plan['id']}:{task['id']}:{condition_hash}" + (f":{gate['id']}" if gate else "")
                    gate = gate_service.open(session, project_id=project_id, goal_id=str(goal["id"]), plan_id=str(plan["id"]), task_id=str(task["id"]), gate_type=gate_type, reason_codes=list(risk.reason_codes), risk_delta=risk.risk_score, scope_delta=list(risk.affected_scope_paths), evidence_hash=evidence_hash, condition_hash=condition_hash, idempotency_key=replacement_key)
                    ProjectGapService().open(session, project_id=project_id, goal_id=str(goal["id"]), plan_id=str(plan["id"]), task_id=str(task["id"]), criterion_id="", gap_type="RISK_BUDGET_EXCEEDED", description="Task activation requires governed human approval", required_state={"max_risk_score": goal["risk_budget"].get("max_risk_score")}, observed_state={"risk_score": risk.risk_score, "gate_id": str(gate["id"])}, severity="CRITICAL", confidence=1.0, idempotency_key=f"risk-gap:{goal['id']}:{plan['id']}:{task['id']}:{condition_hash}")
                    repository.append_event(session, project_id=project_id, goal_id=str(goal["id"]), event_type="TASK_ACTIVATION_BLOCKED", payload={"task_id": str(task["id"]), "gate_id": str(gate["id"]), "reason": "HUMAN_ACTION_REQUIRED"}, idempotency_key=f"task-blocked:{cycle_id}:{task['id']}", cycle_id=cycle_id)
                    repository.append_event(session, project_id=project_id, goal_id=str(goal["id"]), event_type="HUMAN_ACTION_REQUIRED", payload={"gate_id": str(gate["id"])}, idempotency_key=f"human-action:{cycle_id}:{task['id']}", cycle_id=cycle_id)
                    repository.append_event(session, project_id=project_id, goal_id=str(goal["id"]), event_type="PROJECT_DECISION_WAITING_HUMAN", payload={"task_id": str(task["id"])}, idempotency_key=f"waiting-human:{cycle_id}:{task['id']}", cycle_id=cycle_id)
                    repository.wait_for_human(session, goal_id=str(goal["id"]))
                    if not gate_service.authorizes(gate, goal=goal, plan=plan, evidence_hash=evidence_hash):
                        return None
            repository.append_event(session, project_id=project_id, goal_id=str(goal["id"]), event_type="TASK_REEVALUATED", payload={"task_id": str(task["id"])}, idempotency_key=f"task-reevaluated:{cycle_id}:{task['id']}", cycle_id=cycle_id)
            ready = repository.transition(session, "project_tasks", str(task["id"]), "APPROVED", "READY")
            launched = self._adapter.launch(session, str(ready["id"]), project_id=project_id, launch_key=f"cycle:{cycle_id}:task:{ready['id']}")
            repository.append_event(session, project_id=project_id, goal_id=str(goal["id"]), event_type="P6_10_LAUNCH_AUTHORIZED", payload={"task_id": str(launched["id"])}, idempotency_key=f"task-activated:{cycle_id}:{ready['id']}", cycle_id=cycle_id)
            return str(launched["id"])
        return None

    def _dependencies_satisfied(self, session: Session, task: dict[str, Any], project_id: str) -> bool:
        for dependency in repository.dependencies(session, str(task["id"])):
            if str(dependency["dependency_project_id"]) != project_id:
                raise PermissionError("cross-project dependency rejected")
            if dependency["dependency_type"] == "OPTIONAL":
                continue
            if dependency["dependency_status"] != "COMPLETED":
                return False
        return True

    def _metrics(self, *, goal: dict[str, Any], plan: dict[str, Any], tasks: list[dict[str, Any]]) -> dict[str, Any]:
        def count(status: str) -> int:
            return sum(task["status"] == status for task in tasks)

        criteria_total = len(goal["success_criteria"])
        completed = count("COMPLETED")
        return {
            "success_criteria_total": criteria_total, "success_criteria_met": criteria_total if tasks and completed == len(tasks) else 0,
            "success_criteria_partially_met": 0, "task_total": len(tasks), "task_proposed": count("PROPOSED"),
            "task_approved": count("APPROVED") + count("READY"), "task_active": count("ACTIVE"), "task_completed": completed,
            "task_failed": count("FAILED"), "task_blocked": count("BLOCKED"), "loops_active": count("ACTIVE"),
            "loops_completed": completed, "loops_failed": count("FAILED"), "validated_artifacts": 0,
            "unresolved_blockers": count("BLOCKED"), "open_risks": 0, "budget_consumed": len(tasks) - count("PROPOSED"),
            "budget_remaining": max(0, int(goal["execution_budget"]["max_project_cycles"]) - 1), "time_consumed_seconds": 0,
            "time_remaining_seconds": int(goal["time_horizon_seconds"]), "plan_version": int(plan["version"]), "goal_version": int(goal["version"]),
            "scope_hash": stable_hash(goal["scope_paths"]), "state_hash": stable_hash([(str(task["id"]), task["status"]) for task in tasks]),
            "evidence_hash": stable_hash([(str(task["id"]), task.get("task_loop_id")) for task in tasks]),
        }


class ProjectGapService:
    def open(
        self,
        session: Session,
        *,
        project_id: str,
        goal_id: str,
        plan_id: str,
        criterion_id: str = "",
        task_id: str | None = None,
        gap_type: str,
        description: str,
        required_state: dict[str, Any],
        observed_state: dict[str, Any],
        severity: str,
        confidence: float,
        idempotency_key: str,
    ) -> dict[str, Any]:
        goal = repository.get_goal(session, goal_id, project_id=project_id)
        plan = repository.get_plan(session, plan_id)
        if str(plan["project_goal_id"]) != goal_id or (task_id and str(repository.get_task(session, task_id, project_id=project_id)["project_plan_id"]) != plan_id):
            raise PermissionError("cross-goal Gap rejected")
        subject_id = task_id or criterion_id
        if not subject_id:
            raise ValueError("Gap requires task or success criterion")
        identity = gap_identity(project_id=project_id, goal_id=goal_id, goal_version=int(goal["version"]), plan_id=plan_id, plan_version=int(plan["version"]), subject_id=subject_id, gap_type=gap_type, required_state=required_state, observed_state=observed_state)
        existing = repository.find_active_gap(session, project_id=project_id, identity_hash=identity)
        if existing:
            repository.append_event(session, project_id=project_id, goal_id=goal_id, event_type="GAP_REUSED", payload={"gap_id": str(existing["id"]), "gap_type": gap_type, "actor": "controller"}, idempotency_key=f"gap-reused:{idempotency_key}")
            return existing
        persisted_criterion = criterion_id or task_id or ""
        prior = repository.find_historical_gap(session, project_id=project_id, identity_hash=identity)
        related = repository.find_related_active_gap(session, project_id=project_id, goal_id=goal_id, plan_id=plan_id, task_id=task_id, criterion_id=persisted_criterion, gap_type=gap_type, required_state_hash=stable_hash(required_state))
        row = repository.insert_gap(session, project_id=project_id, goal_id=goal_id, plan_id=plan_id, task_id=task_id, criterion_id=persisted_criterion, gap_type=gap_type, description=description, required_state=required_state, observed_state=observed_state, severity=severity, confidence=confidence, identity_hash=identity, idempotency_key=idempotency_key, occurrence_number=int(prior["occurrence_number"]) + 1 if prior else 1, reappeared_from_gap_id=str(prior["id"]) if prior else None)
        if related:
            repository.transition_gap(session, gap_id=str(related["id"]), project_id=project_id, current=str(related["status"]), desired="SUPERSEDED", actor="controller", reason_code="OBSERVED_STATE_CHANGED", idempotency_key=f"gap-superseded:{idempotency_key}", superseded_by_gap_id=str(row["id"]))
            repository.append_event(session, project_id=project_id, goal_id=goal_id, event_type="GAP_SUPERSEDED", payload={"gap_id": str(related["id"]), "superseded_by_gap_id": str(row["id"]), "reason_code": "OBSERVED_STATE_CHANGED"}, idempotency_key=f"gap-superseded:{idempotency_key}")
        repository.append_event(session, project_id=project_id, goal_id=goal_id, event_type="GAP_OPENED", payload={"gap_id": str(row["id"]), "gap_type": gap_type, "actor": "controller", "idempotency_key": idempotency_key}, idempotency_key=f"gap-open:{idempotency_key}")
        if prior:
            repository.append_event(session, project_id=project_id, goal_id=goal_id, event_type="GAP_REAPPEARED", payload={"gap_id": str(row["id"]), "prior_gap_id": str(prior["id"]), "occurrence_number": row["occurrence_number"]}, idempotency_key=f"gap-reappeared:{idempotency_key}")
        return row

    def transition(self, session: Session, *, gap_id: str, project_id: str, desired: str, actor: str, reason_code: str, idempotency_key: str, evidence: dict[str, Any] | None = None, human_gate_id: str | None = None, superseding_gap_id: str | None = None) -> dict[str, Any]:
        gap = repository.get_gap(session, gap_id, project_id=project_id, lock=True)
        if gap["status"] == desired and gap.get("idempotency_key") == idempotency_key:
            return gap
        approved = False
        if desired == "WAIVED":
            if not human_gate_id:
                raise PermissionError("Gap waiver requires scoped Human Gate")
            gate = repository.get_human_gate(session, human_gate_id, project_id=project_id)
            approved = gate["status"] == "APPROVED" and gate["gate_type"] == "GAP_WAIVER" and str(gate.get("project_task_id") or "") == str(gap.get("project_task_id") or "") and str(gate["project_goal_id"]) == str(gap["project_goal_id"]) and int(gate["project_goal_version"]) == int(gap["project_goal_version"]) and int(gate["project_plan_version"] or 0) == int(gap["project_plan_version"])
        if desired == "RESOLVED":
            validate_gap_resolution(evidence or {}, gap_type=str(gap["gap_type"]))
        assert_gap_transition(str(gap["status"]), desired, human_gate_approved=approved)
        event = {"ACKNOWLEDGED": "GAP_ACKNOWLEDGED", "RESOLVING": "GAP_RESOLVING", "RESOLVED": "GAP_RESOLVED", "WAIVED": "GAP_WAIVED", "SUPERSEDED": "GAP_SUPERSEDED"}[desired]
        row = repository.transition_gap(session, gap_id=gap_id, project_id=project_id, current=str(gap["status"]), desired=desired, actor=actor, reason_code=reason_code, idempotency_key=idempotency_key, evidence_ids=list((evidence or {}).get("evidence_ids", [])), resolution_state_hash=stable_hash(evidence) if evidence else None, human_gate_id=human_gate_id, superseded_by_gap_id=superseding_gap_id)
        repository.append_event(session, project_id=project_id, goal_id=str(gap["project_goal_id"]), event_type=event, payload={"gap_id": gap_id, "actor": actor, "reason_code": reason_code, "evidence_ids": (evidence or {}).get("evidence_ids", []), "human_gate_id": human_gate_id, "idempotency_key": idempotency_key}, idempotency_key=f"gap-transition:{idempotency_key}")
        return row


class ProjectHumanGateService:
    def open(self, session: Session, *, project_id: str, goal_id: str, plan_id: str | None, task_id: str | None, gate_type: str, reason_codes: list[str], risk_delta: int, scope_delta: list[str], evidence_hash: str, condition_hash: str, idempotency_key: str, valid_from: str | None = None, valid_until: str | None = None) -> dict[str, Any]:
        goal = repository.get_goal(session, goal_id, project_id=project_id)
        valid_from = valid_from or datetime.now(UTC).replace(microsecond=0).isoformat()
        row = repository.insert_human_gate(session, project_id=project_id, goal_id=goal_id, plan_id=plan_id, task_id=task_id, gate_type=gate_type, reason_codes=reason_codes, risk_delta=risk_delta, scope_delta=scope_delta, budget_delta=0, time_horizon_delta=0, required_action="explicit scoped human approval", evidence_hash=evidence_hash, condition_hash=condition_hash, valid_from=valid_from, valid_until=valid_until, requested_by="controller", idempotency_key=idempotency_key)
        repository.append_event(session, project_id=project_id, goal_id=goal_id, event_type="HUMAN_GATE_CREATED", payload={"gate_id": str(row["id"]), "gate_type": gate_type, "reason_codes": reason_codes, "goal_version": goal["version"], "approval_scope_hash": row["approval_scope_hash"]}, idempotency_key=f"human-gate-created:{idempotency_key}")
        repository.append_event(session, project_id=project_id, goal_id=goal_id, event_type="HUMAN_GATE_SUBMITTED", payload={"gate_id": str(row["id"])}, idempotency_key=f"human-gate-submitted:{idempotency_key}")
        return row

    def approve(self, session: Session, *, gate_id: str, project_id: str, approval_id: str, approver: str, task_id: str | None, idempotency_key: str, approval_scope: dict[str, object] | None = None, decision_reason: str = "governed human approval") -> dict[str, Any]:
        gate = repository.get_human_gate(session, gate_id, project_id=project_id)
        if gate["status"] == "APPROVED":
            if gate["decision_identity"] == idempotency_key and str(gate["approval_id"]) == approval_id and str(gate["resolved_by"]) == approver:
                repository.append_event(session, project_id=project_id, goal_id=str(gate["project_goal_id"]), event_type="HUMAN_GATE_REPLAYED", payload={"gate_id": gate_id, "decision": "APPROVED"}, idempotency_key=f"human-gate-replayed:{idempotency_key}")
                return gate
            raise PermissionError("IDEMPOTENCY_CONFLICT")
        if gate["status"] != "PENDING_APPROVAL":
            raise PermissionError("Human Gate is not pending approval")
        now = datetime.now(UTC)
        if isinstance(gate.get("valid_from"), datetime) and now < gate["valid_from"]:
            repository.append_event(session, project_id=project_id, goal_id=str(gate["project_goal_id"]), event_type="APPROVAL_NOT_YET_VALID", payload={"gate_id": gate_id}, idempotency_key=f"human-gate-not-yet-valid:{idempotency_key}")
            raise PermissionError("Human Gate approval is not yet valid")
        if isinstance(gate.get("valid_until"), datetime) and now >= gate["valid_until"]:
            assert_human_gate_transition("PENDING_APPROVAL", "EXPIRED")
            repository.resolve_human_gate(session, gate_id=gate_id, current="PENDING_APPROVAL", desired="EXPIRED", actor="controller", decision_reason="APPROVAL_EXPIRED", approval_id=None, idempotency_key=f"expired:{idempotency_key}")
            repository.append_event(session, project_id=project_id, goal_id=str(gate["project_goal_id"]), event_type="HUMAN_GATE_EXPIRED", payload={"gate_id": gate_id, "reason": "APPROVAL_EXPIRED"}, idempotency_key=f"human-gate-expired:{idempotency_key}")
            raise PermissionError("Human Gate approval expired")
        if str(gate["project_task_id"] or "") != str(task_id or ""):
            raise PermissionError("Task approval scope mismatch")
        if not approver or approver == gate["requested_by"] or approver.lower() in {"controller", "worker", "task-loop", "p6.10", "automated"} or not approver.startswith("human:"):
            raise PermissionError("automated actor cannot approve Human Gate")
        if approval_scope is None:
            raise PermissionError("Human Gate approval scope is required")
        try:
            assert_human_gate_scope(approval_scope, {"approval_scope_hash": gate["approval_scope_hash"]})
        except PermissionError:
            repository.append_event(session, project_id=project_id, goal_id=str(gate["project_goal_id"]), event_type="HUMAN_GATE_SCOPE_REJECTED", payload={"gate_id": gate_id}, idempotency_key=f"human-gate-scope-rejected:{idempotency_key}")
            raise
        _approved_approval(session, approval_id, project_id)
        assert_human_gate_transition("PENDING_APPROVAL", "APPROVED")
        row = repository.resolve_human_gate(session, gate_id=gate_id, current="PENDING_APPROVAL", desired="APPROVED", actor=approver, decision_reason=decision_reason, approval_id=approval_id, idempotency_key=idempotency_key)
        repository.append_event(session, project_id=project_id, goal_id=str(gate["project_goal_id"]), event_type="HUMAN_GATE_APPROVED", payload={"gate_id": gate_id, "task_id": task_id, "actor": approver, "approval_scope_hash": gate["approval_scope_hash"], "idempotency_key": idempotency_key}, idempotency_key=f"human-gate-approved:{idempotency_key}")
        return row

    def reject(self, session: Session, *, gate_id: str, project_id: str, approver: str, idempotency_key: str, decision_reason: str) -> dict[str, Any]:
        gate = repository.get_human_gate(session, gate_id, project_id=project_id)
        if gate["status"] == "REJECTED" and gate["decision_identity"] == idempotency_key:
            return gate
        if gate["status"] != "PENDING_APPROVAL" or not approver.startswith("human:") or approver == gate["requested_by"]:
            raise PermissionError("Human Gate rejection is not authorized")
        assert_human_gate_transition("PENDING_APPROVAL", "REJECTED")
        row = repository.resolve_human_gate(session, gate_id=gate_id, current="PENDING_APPROVAL", desired="REJECTED", actor=approver, decision_reason=decision_reason, approval_id=None, idempotency_key=idempotency_key)
        repository.append_event(session, project_id=project_id, goal_id=str(gate["project_goal_id"]), event_type="HUMAN_GATE_REJECTED", payload={"gate_id": gate_id, "actor": approver}, idempotency_key=f"human-gate-rejected:{idempotency_key}")
        return row

    def supersede(self, session: Session, *, gate: dict[str, Any], reason: str) -> dict[str, Any]:
        if gate["status"] not in {"OPEN", "PENDING_APPROVAL", "APPROVED"}:
            return gate
        row = repository.resolve_human_gate(session, gate_id=str(gate["id"]), current=str(gate["status"]), desired="SUPERSEDED", actor="controller", decision_reason=reason, approval_id=None, idempotency_key=f"supersede:{gate['id']}:{reason}")
        repository.append_event(session, project_id=str(gate["project_id"]), goal_id=str(gate["project_goal_id"]), event_type="HUMAN_GATE_SUPERSEDED", payload={"gate_id": str(gate["id"]), "reason": reason}, idempotency_key=f"human-gate-superseded:{gate['id']}:{reason}")
        return row

    def expire(self, session: Session, *, gate: dict[str, Any]) -> dict[str, Any]:
        assert_human_gate_transition(str(gate["status"]), "EXPIRED")
        row = repository.resolve_human_gate(session, gate_id=str(gate["id"]), current=str(gate["status"]), desired="EXPIRED", actor="controller", decision_reason="APPROVAL_EXPIRED", approval_id=None, idempotency_key=f"expired:{gate['id']}")
        repository.append_event(session, project_id=str(gate["project_id"]), goal_id=str(gate["project_goal_id"]), event_type="HUMAN_GATE_EXPIRED", payload={"gate_id": str(gate["id"]), "reason": "APPROVAL_EXPIRED"}, idempotency_key=f"human-gate-expired:{gate['id']}")
        return row

    def authorizes(self, gate: dict[str, Any], *, goal: dict[str, Any], plan: dict[str, Any], evidence_hash: str) -> bool:
        if gate["status"] != "APPROVED" or int(gate["project_goal_version"]) != int(goal["version"]) or int(gate["project_plan_version"] or 0) != int(plan["version"]) or gate["evidence_hash"] != evidence_hash:
            return False
        now = datetime.now(UTC)
        valid_from = gate.get("valid_from")
        valid_until = gate.get("valid_until")
        if isinstance(valid_from, datetime) and now < valid_from:
            return False
        return not (isinstance(valid_until, datetime) and now >= valid_until)


class ProjectOutcomeAggregationService:
    """Fail-closed aggregation of persisted P6.10 execution evidence."""

    def aggregate(self, session: Session, *, task_id: str, project_id: str, idempotency_key: str) -> dict[str, Any]:
        task = repository.get_task(session, task_id, project_id=project_id, lock=True)
        if task["status"] in {"COMPLETED", "FAILED", "PARTIALLY_COMPLETED"}:
            return task
        if task["status"] != "ACTIVE" or not all(task.get(name) for name in ("task_goal_id", "task_plan_id", "task_loop_id")):
            raise PermissionError("active Project Task with complete P6.10 lineage required")
        goal = loop_service.get_task_goal(session, str(task["task_goal_id"]))
        plan = loop_service.get_task_plan(session, str(task["task_plan_id"]))
        loop = loop_service.get_loop(session, str(task["task_loop_id"]))
        if str(goal["project_id"]) != project_id or str(plan["task_goal_id"]) != str(goal["id"]):
            return self._block(session, task, "LINEAGE_MISMATCH", idempotency_key)
        if loop["status"] == "COMPLETED":
            steps = session.execute(text("SELECT status FROM task_plan_steps WHERE task_plan_id = CAST(:id AS uuid)"), {"id": str(plan["id"])}).scalars().all()
            snapshots = session.execute(text("SELECT validation_status FROM task_progress_snapshots WHERE loop_id = CAST(:id AS uuid) ORDER BY created_at DESC LIMIT 1"), {"id": str(loop["id"])}).scalars().all()
            if steps and all(str(status) == "PASSED" for status in steps) and snapshots == ["PASSED"]:
                row = repository.task_outcome(session, task_id=task_id, desired="COMPLETED")
                repository.append_event(session, project_id=project_id, goal_id=str(task["project_goal_id"]), event_type="P6_10_OUTCOME_VERIFIED", payload={"task_id": task_id, "loop_id": str(loop["id"]), "outcome": "COMPLETED"}, idempotency_key=f"aggregate:{idempotency_key}")
                return row
            return self._block(session, task, "VALIDATION_EVIDENCE_MISSING", idempotency_key)
        if loop["status"] in {"WAITING_HUMAN", "FAILED_RECOVERABLE", "FAILED_TERMINAL", "BLOCKED", "TIMED_OUT"}:
            return self._block(session, task, f"P6_10_{loop['status']}", idempotency_key)
        raise ValueError("non-terminal P6.10 Loop cannot be aggregated")

    def _block(self, session: Session, task: dict[str, Any], reason: str, idempotency_key: str) -> dict[str, Any]:
        row = repository.task_outcome(session, task_id=str(task["id"]), desired="BLOCKED")
        plan = repository.active_plan(session, str(task["project_goal_id"]))
        gap_type = "FAILED_VALIDATION" if reason == "VALIDATION_EVIDENCE_MISSING" else "INSUFFICIENT_EVIDENCE" if reason == "LINEAGE_MISMATCH" else "BLOCKED_DEPENDENCY"
        ProjectGapService().open(session, project_id=str(task["project_id"]), goal_id=str(task["project_goal_id"]), plan_id=str(plan["id"]), task_id=str(task["id"]), criterion_id="", gap_type=gap_type, description=reason, required_state={"p6_10": "VERIFIED"}, observed_state={"reason": reason}, severity="CRITICAL", confidence=1.0, idempotency_key=f"aggregate-gap:{idempotency_key}")
        repository.append_event(session, project_id=str(task["project_id"]), goal_id=str(task["project_goal_id"]), event_type="P6_10_EVIDENCE_REJECTED", payload={"task_id": str(task["id"]), "reason": reason}, idempotency_key=f"aggregate:{idempotency_key}")
        return row

    def resolve(self, session: Session, *, goal_id: str, gap_id: str, evidence: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        goal = repository.get_goal(session, goal_id)
        row = repository.resolve_gap(session, gap_id=gap_id, goal_id=goal_id, resolution_evidence=evidence)
        repository.append_event(session, project_id=str(goal["project_id"]), goal_id=goal_id, event_type="PROJECT_GAP_RESOLVED", payload={"gap_id": gap_id}, idempotency_key=f"gap-resolve:{idempotency_key}")
        return row

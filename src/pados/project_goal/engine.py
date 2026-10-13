# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import hashlib
import json
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum

from pydantic import BaseModel

from pados.project_goal.schemas import (
    DeltaClassification,
    GoalStatus,
    PlanStatus,
    ProjectDecision,
    ProjectLoopStatus,
    ProjectProgressDelta,
    ProjectProgressSnapshot,
    ProjectTaskStatus,
    ReplanHistoryEntry,
)

GOAL_TRANSITIONS: dict[GoalStatus, set[GoalStatus]] = {
    GoalStatus.DRAFT: {GoalStatus.PENDING_APPROVAL, GoalStatus.CANCELLED},
    GoalStatus.PENDING_APPROVAL: {GoalStatus.APPROVED, GoalStatus.CANCELLED},
    GoalStatus.APPROVED: {GoalStatus.ACTIVE, GoalStatus.SUPERSEDED, GoalStatus.CANCELLED},
    GoalStatus.ACTIVE: {
        GoalStatus.AT_RISK, GoalStatus.WAITING_HUMAN, GoalStatus.ACHIEVED,
        GoalStatus.PARTIALLY_ACHIEVED, GoalStatus.BLOCKED, GoalStatus.FAILED,
        GoalStatus.CANCELLED, GoalStatus.SUPERSEDED,
    },
    GoalStatus.AT_RISK: {GoalStatus.ACTIVE, GoalStatus.WAITING_HUMAN, GoalStatus.BLOCKED, GoalStatus.FAILED},
    GoalStatus.WAITING_HUMAN: {GoalStatus.ACTIVE, GoalStatus.BLOCKED, GoalStatus.CANCELLED},
    GoalStatus.BLOCKED: {GoalStatus.WAITING_HUMAN, GoalStatus.PARTIALLY_ACHIEVED, GoalStatus.FAILED, GoalStatus.CANCELLED},
}
TERMINAL_GOALS = {GoalStatus.ACHIEVED, GoalStatus.PARTIALLY_ACHIEVED, GoalStatus.FAILED, GoalStatus.CANCELLED, GoalStatus.SUPERSEDED}

PLAN_TRANSITIONS: dict[PlanStatus, set[PlanStatus]] = {
    PlanStatus.DRAFT: {PlanStatus.PENDING_APPROVAL, PlanStatus.CANCELLED},
    PlanStatus.PENDING_APPROVAL: {PlanStatus.APPROVED, PlanStatus.CANCELLED},
    PlanStatus.APPROVED: {PlanStatus.ACTIVE, PlanStatus.SUPERSEDED, PlanStatus.CANCELLED},
    PlanStatus.ACTIVE: {PlanStatus.AT_RISK, PlanStatus.COMPLETED, PlanStatus.FAILED, PlanStatus.CANCELLED, PlanStatus.SUPERSEDED},
    PlanStatus.AT_RISK: {PlanStatus.ACTIVE, PlanStatus.FAILED, PlanStatus.SUPERSEDED},
}
TERMINAL_PLANS = {PlanStatus.COMPLETED, PlanStatus.FAILED, PlanStatus.CANCELLED, PlanStatus.SUPERSEDED}

LOOP_TRANSITIONS: dict[ProjectLoopStatus, set[ProjectLoopStatus]] = {
    ProjectLoopStatus.DRAFT: {ProjectLoopStatus.PENDING_APPROVAL, ProjectLoopStatus.CANCELLED},
    ProjectLoopStatus.PENDING_APPROVAL: {ProjectLoopStatus.READY, ProjectLoopStatus.CANCELLED},
    ProjectLoopStatus.READY: {ProjectLoopStatus.ACTIVE, ProjectLoopStatus.CANCELLED},
    ProjectLoopStatus.ACTIVE: {ProjectLoopStatus.EVALUATING, ProjectLoopStatus.AT_RISK, ProjectLoopStatus.BLOCKED, ProjectLoopStatus.PAUSED, ProjectLoopStatus.CANCELLED, ProjectLoopStatus.TIMED_OUT},
    ProjectLoopStatus.EVALUATING: {ProjectLoopStatus.ACTIVE, ProjectLoopStatus.PLANNING, ProjectLoopStatus.ACHIEVED, ProjectLoopStatus.PARTIALLY_ACHIEVED, ProjectLoopStatus.FAILED_RECOVERABLE, ProjectLoopStatus.FAILED_TERMINAL},
    ProjectLoopStatus.PLANNING: {ProjectLoopStatus.WAITING_HUMAN},
    ProjectLoopStatus.WAITING_HUMAN: {ProjectLoopStatus.ACTIVE, ProjectLoopStatus.PAUSED, ProjectLoopStatus.CANCELLED},
    ProjectLoopStatus.PAUSED: {ProjectLoopStatus.ACTIVE, ProjectLoopStatus.CANCELLED},
    ProjectLoopStatus.AT_RISK: {ProjectLoopStatus.EVALUATING, ProjectLoopStatus.WAITING_HUMAN},
    ProjectLoopStatus.BLOCKED: {ProjectLoopStatus.WAITING_HUMAN},
    ProjectLoopStatus.FAILED_RECOVERABLE: {ProjectLoopStatus.WAITING_HUMAN, ProjectLoopStatus.CANCELLED},
}
TERMINAL_LOOPS = {ProjectLoopStatus.ACHIEVED, ProjectLoopStatus.FAILED_TERMINAL, ProjectLoopStatus.CANCELLED, ProjectLoopStatus.TIMED_OUT}


HUMAN_GATE_TRANSITIONS: dict[str, set[str]] = {
    "OPEN": {"PENDING_APPROVAL", "EXPIRED", "CANCELLED", "SUPERSEDED"},
    "PENDING_APPROVAL": {"APPROVED", "REJECTED", "EXPIRED", "CANCELLED", "SUPERSEDED"},
    "APPROVED": {"EXPIRED", "SUPERSEDED"},
}

GAP_TRANSITIONS: dict[str, set[str]] = {
    "OPEN": {"ACKNOWLEDGED", "WAIVED", "SUPERSEDED"},
    "ACKNOWLEDGED": {"RESOLVING", "WAIVED", "SUPERSEDED"},
    "RESOLVING": {"RESOLVED", "SUPERSEDED"},
}

GAP_TYPES = {
    "MISSING_OUTCOME", "FAILED_VALIDATION", "MISSING_ARTIFACT", "BLOCKED_DEPENDENCY",
    "INSUFFICIENT_EVIDENCE", "SCOPE_CONFLICT", "RISK_BUDGET_EXCEEDED",
    "EXECUTION_BUDGET_EXCEEDED", "TIME_HORIZON_EXCEEDED", "PLAN_STALE",
}


@dataclass(frozen=True)
class ProjectRiskResult:
    allowed: bool
    risk_score: int
    categories: tuple[str, ...]
    approval_required: bool
    reason_codes: tuple[str, ...]
    affected_scope_paths: tuple[str, ...]
    gate_type: str | None


def evaluate_task_risk(
    *,
    task: dict[str, object],
    goal_risk_budget: dict[str, object],
    plan_risk_controls: dict[str, object],
    active_tasks: list[dict[str, object]],
    external_side_effect: bool = False,
    production_modification: bool = False,
    release_or_deployment: bool = False,
    destructive_operation: bool = False,
    secret_operation: bool = False,
    migration_outside_validation_database: bool = False,
    new_external_system: bool = False,
    new_worker_capability: bool = False,
) -> ProjectRiskResult:
    """Produce a deterministic, server-authoritative activation decision."""
    levels = {"LOW": 10, "MEDIUM": 25, "HIGH": 50, "CRITICAL": 100}
    level = str(task.get("risk_level", "LOW"))
    if level not in levels:
        raise ValueError("unrecognized task risk level")
    ceiling = goal_risk_budget.get("max_risk_score")
    if not isinstance(ceiling, int) or isinstance(ceiling, bool) or ceiling <= 0:
        raise ValueError("max_risk_score must be a finite positive integer")
    categories: list[str] = [f"TASK_{level}"]
    score = levels[level]
    flags = (
        (external_side_effect, "EXTERNAL_SIDE_EFFECT", 30),
        (production_modification, "PRODUCTION_MODIFICATION", 40),
        (release_or_deployment, "RELEASE_OR_DEPLOYMENT", 40),
        (destructive_operation, "DESTRUCTIVE_OPERATION", 50),
        (secret_operation, "SECRET_OPERATION", 50),
        (migration_outside_validation_database, "EXTERNAL_MIGRATION", 50),
        (new_external_system, "NEW_EXTERNAL_SYSTEM", 35),
        (new_worker_capability, "NEW_WORKER_CAPABILITY", 35),
    )
    for enabled, category, weight in flags:
        if enabled:
            categories.append(category)
            score += weight
    active_score = sum(levels.get(str(item.get("risk_level", "")), 0) for item in active_tasks)
    score += active_score
    reasons: list[str] = []
    gated = any(enabled for enabled, _category, _weight in flags)
    if score > ceiling:
        reasons.append("RISK_CEILING_EXCEEDED")
        gated = True
    if external_side_effect and not bool(plan_risk_controls.get("allow_external_side_effects", False)):
        reasons.append("EXTERNAL_SIDE_EFFECT_NOT_ALLOWED")
        gated = True
    if active_score and score > ceiling:
        reasons.append("COMBINED_ACTIVE_TASK_RISK_EXCEEDED")
    raw_paths = task.get("scope_paths", [])
    paths = tuple(sorted(item for item in raw_paths if isinstance(item, str))) if isinstance(raw_paths, list) else ()
    return ProjectRiskResult(
        allowed=not gated,
        risk_score=score,
        categories=tuple(categories),
        approval_required=gated,
        reason_codes=tuple(reasons),
        affected_scope_paths=paths,
        gate_type="RISK_BUDGET" if gated else None,
    )


def _assert_transition[StatusT: StrEnum](current: StatusT, desired: StatusT, transitions: dict[StatusT, set[StatusT]], terminal: set[StatusT], label: str) -> None:
    if current in terminal:
        raise ValueError(f"terminal {label} state cannot transition")
    if desired not in transitions.get(current, set()):
        current_value = getattr(current, "value", str(current))
        desired_value = getattr(desired, "value", str(desired))
        raise ValueError(f"invalid {label} transition {current_value}->{desired_value}")


def assert_goal_transition(current: GoalStatus, desired: GoalStatus) -> None:
    _assert_transition(current, desired, GOAL_TRANSITIONS, TERMINAL_GOALS, "goal")


def assert_plan_transition(current: PlanStatus, desired: PlanStatus) -> None:
    _assert_transition(current, desired, PLAN_TRANSITIONS, TERMINAL_PLANS, "plan")


def assert_loop_transition(current: ProjectLoopStatus, desired: ProjectLoopStatus) -> None:
    _assert_transition(current, desired, LOOP_TRANSITIONS, TERMINAL_LOOPS, "project loop")


def stable_hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True, default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def human_gate_scope_hash(scope: dict[str, object]) -> str:
    """Server-side hash of the immutable, structured Human Gate authority."""
    required = {
        "project_id", "goal_id", "goal_version", "plan_id", "plan_version", "task_id",
        "gate_type", "risk_delta", "scope_delta", "budget_delta", "time_horizon_delta",
        "evidence_hash", "valid_from", "valid_until", "required_action",
    }
    if set(scope) != required:
        raise ValueError("Human Gate approval scope is incomplete")
    canonical_scope = dict(scope)
    for field in ("valid_from", "valid_until"):
        value = canonical_scope[field]
        if value is None:
            continue
        try:
            timestamp = value if isinstance(value, datetime) else datetime.fromisoformat(str(value))
        except ValueError:
            continue
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        canonical_scope[field] = timestamp.astimezone(UTC).isoformat()
    return stable_hash(canonical_scope)


def assert_human_gate_transition(current: str, desired: str) -> None:
    """Keep Human Gate lifecycle changes in the governed service layer."""
    if desired not in HUMAN_GATE_TRANSITIONS.get(current, set()):
        raise ValueError(f"invalid human gate transition {current}->{desired}")


def gap_identity(*, project_id: str, goal_id: str, goal_version: int, plan_id: str, plan_version: int, subject_id: str, gap_type: str, required_state: dict[str, object], observed_state: dict[str, object]) -> str:
    if gap_type not in GAP_TYPES:
        raise ValueError("unsupported project gap type")
    return stable_hash({"project_id": project_id, "project_goal_id": goal_id, "project_goal_version": goal_version, "project_plan_id": plan_id, "project_plan_version": plan_version, "subject_id": subject_id, "gap_type": gap_type, "required_state_hash": stable_hash(required_state), "observed_state_hash": stable_hash(observed_state)})


def assert_gap_transition(current: str, desired: str, *, human_gate_approved: bool = False) -> None:
    if desired == "WAIVED" and current in {"OPEN", "ACKNOWLEDGED"}:
        if not human_gate_approved:
            raise ValueError("Gap waiver requires approved Human Gate")
        return
    if desired not in GAP_TRANSITIONS.get(current, set()):
        raise ValueError(f"invalid project gap transition {current}->{desired}")


def validate_gap_resolution(evidence: dict[str, object], *, gap_type: str) -> None:
    if gap_type not in GAP_TYPES or not evidence.get("evidence_ids"):
        raise ValueError("Gap resolution requires server-verified evidence")
    required = {
        "FAILED_VALIDATION": ("validation_status", "PASSED"),
        "MISSING_ARTIFACT": ("artifact_registered", True),
        "BLOCKED_DEPENDENCY": ("dependency_status", "COMPLETED"),
        "INSUFFICIENT_EVIDENCE": ("lineage_valid", True),
        "RISK_BUDGET_EXCEEDED": ("risk_within_authority", True),
        "EXECUTION_BUDGET_EXCEEDED": ("budget_permits_progress", True),
    }
    requirement = required.get(gap_type)
    if requirement and evidence.get(requirement[0]) != requirement[1]:
        raise ValueError("Gap resolution requires current verified evidence")


def validate_replan_scope(goal: dict[str, object], proposal: dict[str, object]) -> None:
    delta = proposal.get("scope_delta", [])
    scope_paths = goal.get("scope_paths", [])
    if not isinstance(delta, list) or not isinstance(scope_paths, list) or not set(delta).issubset(set(scope_paths)):
        raise PermissionError("replan scope exceeds Project Goal authority")
    values: tuple[object, object, object] = (proposal.get("risk_delta", 0), proposal.get("budget_delta", 0), proposal.get("time_horizon_delta", 0))
    if any(not isinstance(value, int) or isinstance(value, bool) for value in values):
        raise ValueError("replan deltas must be integers")
    risk_delta, budget_delta, time_horizon_delta = values
    assert isinstance(risk_delta, int) and not isinstance(risk_delta, bool)
    assert isinstance(budget_delta, int) and not isinstance(budget_delta, bool)
    assert isinstance(time_horizon_delta, int) and not isinstance(time_horizon_delta, bool)
    if risk_delta > 0 or budget_delta > 0 or time_horizon_delta > 0:
        raise PermissionError("replan exceeds Project Goal authority")


def assert_human_gate_scope(submitted: dict[str, object], persisted: dict[str, object]) -> None:
    """Fail closed for every immutable authorization field."""
    required = {
        "project_id", "goal_id", "goal_version", "plan_id", "plan_version", "task_id",
        "gate_type", "risk_delta", "scope_delta", "budget_delta", "time_horizon_delta",
        "evidence_hash", "valid_from", "valid_until", "required_action",
    }
    if set(submitted) != required:
        raise PermissionError("Human Gate approval scope is incomplete")
    if human_gate_scope_hash(submitted) != str(persisted["approval_scope_hash"]):
        raise PermissionError("Human Gate approval scope mismatch")


def validate_task_graph(statuses: dict[str, ProjectTaskStatus], edges: list[tuple[str, str]]) -> None:
    graph: dict[str, list[str]] = defaultdict(list)
    for task_id, dependency_id in edges:
        if task_id == dependency_id:
            raise ValueError("self-dependency is forbidden")
        if task_id not in statuses or dependency_id not in statuses:
            raise ValueError("missing dependency node")
        if statuses[dependency_id] == ProjectTaskStatus.CANCELLED:
            raise ValueError("cancelled dependency is forbidden")
        graph[task_id].append(dependency_id)
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise ValueError("circular dependency is forbidden")
        if node in visited:
            return
        visiting.add(node)
        for dependency in graph[node]:
            visit(dependency)
        visiting.remove(node)
        visited.add(node)

    for node in statuses:
        visit(node)


def calculate_project_delta(before: ProjectProgressSnapshot, after: ProjectProgressSnapshot) -> ProjectProgressDelta:
    criteria = after.success_criteria_met - before.success_criteria_met
    completed = after.task_completed - before.task_completed
    failed = after.task_failed - before.task_failed
    blockers = after.unresolved_blockers - before.unresolved_blockers
    risks = after.open_risks - before.open_risks
    budget = after.budget_consumed - before.budget_consumed
    artifacts = after.validated_artifacts - before.validated_artifacts
    scope_changed = before.scope_hash != after.scope_hash
    plan_changed = before.plan_version != after.plan_version
    goal_changed = before.goal_version != after.goal_version
    score = criteria * 5 + completed * 3 + artifacts - failed * 4 - blockers * 4 - risks * 2
    classification = DeltaClassification.INCOMPARABLE if (scope_changed or goal_changed) else (DeltaClassification.POSITIVE if score > 0 else DeltaClassification.NEGATIVE if score < 0 else DeltaClassification.NEUTRAL)
    return ProjectProgressDelta(criteria_met_delta=criteria, task_completed_delta=completed, task_failed_delta=failed, blocker_delta=blockers, risk_delta=risks, budget_delta=budget, artifact_delta=artifacts, scope_changed=scope_changed, plan_changed=plan_changed, goal_changed=goal_changed, classification=classification)


class ProjectDecisionInput(BaseModel):
    mandatory_criteria_pass: bool = False
    required_tasks_complete: bool = False
    required_artifacts_exist: bool = False
    required_validations_pass: bool = False
    unresolved_critical_blockers: int = 0
    risk_budget_violated: bool = False
    ready_tasks: int = 0
    active_tasks: int = 0
    verified_gap_requires_replan: bool = False
    plan_is_stale: bool = False
    scope_change_required: bool = False
    budget_exceeded: bool = False
    time_horizon_exceeded: bool = False
    terminal_failure: bool = False


def decide_project_action(value: ProjectDecisionInput) -> tuple[ProjectDecision, str, ProjectLoopStatus]:
    if value.terminal_failure:
        return ProjectDecision.FAIL_TERMINAL, "terminal failure verified", ProjectLoopStatus.FAILED_TERMINAL
    if value.scope_change_required:
        return ProjectDecision.WAIT_FOR_HUMAN, "scope change requires approval", ProjectLoopStatus.WAITING_HUMAN
    if value.budget_exceeded or value.time_horizon_exceeded:
        return ProjectDecision.MARK_BLOCKED, "bounded execution limit exceeded", ProjectLoopStatus.BLOCKED
    if value.risk_budget_violated:
        return ProjectDecision.MARK_AT_RISK, "risk budget violated", ProjectLoopStatus.AT_RISK
    achieved = value.mandatory_criteria_pass and value.required_tasks_complete and value.required_artifacts_exist and value.required_validations_pass and value.unresolved_critical_blockers == 0
    if achieved:
        return ProjectDecision.ACHIEVE, "all governed success conditions verified", ProjectLoopStatus.ACHIEVED
    if value.verified_gap_requires_replan or value.plan_is_stale:
        return ProjectDecision.PROPOSE_REPLAN, "verified evidence invalidates active plan", ProjectLoopStatus.PLANNING
    if value.active_tasks:
        return ProjectDecision.CONTINUE_ACTIVE_TASKS, "active governed tasks remain", ProjectLoopStatus.ACTIVE
    if value.ready_tasks:
        return ProjectDecision.ACTIVATE_NEXT_TASK, "dependency-safe task is ready", ProjectLoopStatus.ACTIVE
    return ProjectDecision.WAIT_FOR_HUMAN, "no governed executable task available", ProjectLoopStatus.WAITING_HUMAN


def detect_replan_oscillation(history: list[ReplanHistoryEntry], proposal: ReplanHistoryEntry, *, current_cycle: int, minimum_cooldown_cycles: int = 2, max_replans: int = 4) -> str | None:
    if len(history) >= max_replans:
        return "max-replans"
    if any(item.plan_hash == proposal.plan_hash for item in history):
        return "same-plan-hash"
    if proposal.inverse_plan_hash and any(item.plan_hash == proposal.inverse_plan_hash for item in history):
        return "inverse-plan"
    if history and current_cycle - history[-1].cycle < minimum_cooldown_cycles:
        return "replan-cooldown"
    recent = history[-2:]
    if len(recent) == 2 and proposal.task_set_hash == recent[0].task_set_hash == recent[1].task_set_hash and proposal.dependency_hash == recent[0].dependency_hash and proposal.priority_hash == recent[0].priority_hash:
        return "dependency-priority-oscillation"
    return None


def budget_exhaustion(budget: dict[str, int], usage: dict[str, int]) -> str | None:
    """Return the first finite ceiling reached; reject malformed runtime budget state."""
    for key, limit in budget.items():
        if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0 or limit > 1_000_000_000:
            raise ValueError("budget limits must be finite positive integers")
        used = usage.get(key, 0)
        if not isinstance(used, int) or isinstance(used, bool) or used < 0:
            raise ValueError("budget usage must be a non-negative integer")
        if used >= limit:
            return key
    return None

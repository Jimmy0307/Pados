from __future__ import annotations

from typing import Any

import pytest
from pydantic import ValidationError

from pados.project_goal.engine import (
    ProjectDecisionInput,
    assert_goal_transition,
    assert_loop_transition,
    assert_plan_transition,
    calculate_project_delta,
    decide_project_action,
    detect_replan_oscillation,
    stable_hash,
    validate_task_graph,
)
from pados.project_goal.schemas import (
    GoalStatus,
    PlanStatus,
    ProjectBudget,
    ProjectDecision,
    ProjectLoopStatus,
    ProjectProgressSnapshot,
    ProjectTaskStatus,
    ReplanHistoryEntry,
)


def snapshot(**overrides: object) -> ProjectProgressSnapshot:
    values: dict[str, Any] = {
        "success_criteria_total": 3,
        "success_criteria_met": 1,
        "success_criteria_partially_met": 0,
        "task_total": 3,
        "task_proposed": 0,
        "task_approved": 3,
        "task_active": 1,
        "task_completed": 1,
        "task_failed": 0,
        "task_blocked": 0,
        "loops_active": 1,
        "loops_completed": 1,
        "loops_failed": 0,
        "validated_artifacts": 1,
        "unresolved_blockers": 0,
        "open_risks": 0,
        "budget_consumed": 1,
        "budget_remaining": 49,
        "time_consumed_seconds": 10,
        "time_remaining_seconds": 3590,
        "plan_version": 1,
        "goal_version": 1,
        "scope_hash": "scope-a",
        "state_hash": "state-a",
        "evidence_hash": "evidence-a",
    }
    values.update(overrides)
    return ProjectProgressSnapshot(**values)


def test_budget_is_positive_and_never_unlimited() -> None:
    budget = ProjectBudget()
    assert budget.max_active_tasks == 2
    assert budget.max_total_tasks == 20
    for value in budget.model_dump().values():
        assert isinstance(value, int) and value > 0
    for invalid in (0, -1):
        with pytest.raises(ValidationError):
            ProjectBudget(max_project_cycles=invalid)


def test_goal_plan_and_loop_transitions_reject_illegal_or_terminal_moves() -> None:
    assert_goal_transition(GoalStatus.DRAFT, GoalStatus.PENDING_APPROVAL)
    assert_plan_transition(PlanStatus.APPROVED, PlanStatus.ACTIVE)
    assert_loop_transition(ProjectLoopStatus.EVALUATING, ProjectLoopStatus.PLANNING)
    with pytest.raises(ValueError, match="invalid goal transition"):
        assert_goal_transition(GoalStatus.DRAFT, GoalStatus.ACTIVE)
    with pytest.raises(ValueError, match="terminal goal"):
        assert_goal_transition(GoalStatus.ACHIEVED, GoalStatus.ACTIVE)
    with pytest.raises(ValueError, match="terminal project loop"):
        assert_loop_transition(ProjectLoopStatus.ACHIEVED, ProjectLoopStatus.ACTIVE)


def test_task_graph_rejects_self_missing_cancelled_and_cycles() -> None:
    statuses = {"a": ProjectTaskStatus.READY, "b": ProjectTaskStatus.READY}
    validate_task_graph(statuses, [("a", "b")])
    with pytest.raises(ValueError, match="self-dependency"):
        validate_task_graph(statuses, [("a", "a")])
    with pytest.raises(ValueError, match="missing dependency"):
        validate_task_graph(statuses, [("a", "missing")])
    with pytest.raises(ValueError, match="circular dependency"):
        validate_task_graph(statuses, [("a", "b"), ("b", "a")])
    with pytest.raises(ValueError, match="cancelled dependency"):
        validate_task_graph({**statuses, "b": ProjectTaskStatus.CANCELLED}, [("a", "b")])


def test_progress_is_service_calculated_and_scope_changes_are_incomparable() -> None:
    delta = calculate_project_delta(
        snapshot(),
        snapshot(success_criteria_met=2, task_completed=2, validated_artifacts=2, state_hash="state-b"),
    )
    assert delta.criteria_met_delta == 1
    assert delta.task_completed_delta == 1
    assert delta.classification.value == "POSITIVE"
    changed = calculate_project_delta(snapshot(), snapshot(scope_hash="scope-b"))
    assert changed.scope_changed is True
    assert changed.classification.value == "INCOMPARABLE"


def test_decision_table_requires_complete_verified_evidence_for_achieve() -> None:
    complete = ProjectDecisionInput(
        mandatory_criteria_pass=True,
        required_tasks_complete=True,
        required_artifacts_exist=True,
        required_validations_pass=True,
        unresolved_critical_blockers=0,
        risk_budget_violated=False,
        ready_tasks=0,
        active_tasks=0,
    )
    assert decide_project_action(complete)[0] == ProjectDecision.ACHIEVE
    assert decide_project_action(complete.model_copy(update={"required_artifacts_exist": False}))[0] != ProjectDecision.ACHIEVE
    assert decide_project_action(complete.model_copy(update={"scope_change_required": True}))[0] == ProjectDecision.WAIT_FOR_HUMAN
    assert decide_project_action(complete.model_copy(update={"budget_exceeded": True}))[0] == ProjectDecision.MARK_BLOCKED


def test_replan_oscillation_detects_equivalent_inverse_and_priority_flip() -> None:
    history = [
        ReplanHistoryEntry(cycle=1, plan_hash="one", task_set_hash="tasks", dependency_hash="a", priority_hash="p1"),
        ReplanHistoryEntry(cycle=3, plan_hash="two", task_set_hash="tasks", dependency_hash="b", priority_hash="p2"),
    ]
    assert detect_replan_oscillation(history, history[-1], current_cycle=4) == "same-plan-hash"
    inverse = ReplanHistoryEntry(
        cycle=5,
        plan_hash="three",
        inverse_plan_hash="one",
        task_set_hash="tasks",
        dependency_hash="a",
        priority_hash="p1",
    )
    assert detect_replan_oscillation(history, inverse, current_cycle=5) == "inverse-plan"
    assert stable_hash({"b": 2, "a": 1}) == stable_hash({"a": 1, "b": 2})

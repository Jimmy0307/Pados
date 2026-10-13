from __future__ import annotations

from pados.project_goal.engine import ReplanHistoryEntry, detect_replan_oscillation


def test_same_plan_hash_is_persistently_rejected() -> None:
    entry = ReplanHistoryEntry(cycle=1, plan_hash="plan", task_set_hash="tasks", dependency_hash="deps", priority_hash="priority")
    assert detect_replan_oscillation([entry], entry, current_cycle=3) == "same-plan-hash"

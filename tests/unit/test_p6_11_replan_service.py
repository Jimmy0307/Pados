from __future__ import annotations

import pytest

from pados.project_goal.engine import validate_replan_scope


def test_replan_scope_rejects_goal_authority_expansion() -> None:
    goal = {"scope_paths": ["src"], "desired_outcomes": ["pass"], "success_criteria": ["tests"], "risk_budget": {"max_risk_score": 50}, "execution_budget": {"max_replans": 4}, "time_horizon_seconds": 60}
    validate_replan_scope(goal, {"scope_delta": ["src"], "risk_delta": 0, "budget_delta": 0, "time_horizon_delta": 0})
    with pytest.raises(PermissionError, match="scope"):
        validate_replan_scope(goal, {"scope_delta": ["src", "deploy"], "risk_delta": 0, "budget_delta": 0, "time_horizon_delta": 0})

from __future__ import annotations

import pytest

from pados.project_goal.engine import budget_exhaustion, evaluate_task_risk


def test_risk_evaluator_requires_human_gate_for_external_side_effect_over_ceiling() -> None:
    result = evaluate_task_risk(
        task={"risk_level": "HIGH", "scope_paths": ["src"], "required_capabilities": ["WRITE_REPOSITORY"]},
        goal_risk_budget={"max_risk_score": 40},
        plan_risk_controls={"allow_external_side_effects": False},
        active_tasks=[],
        external_side_effect=True,
    )
    assert result.allowed is False
    assert result.approval_required is True
    assert result.gate_type == "RISK_BUDGET"
    assert "EXTERNAL_SIDE_EFFECT" in result.categories
    assert "RISK_CEILING_EXCEEDED" in result.reason_codes


def test_budget_allows_below_boundary_and_blocks_at_boundary() -> None:
    budget = {"max_active_tasks": 2, "max_task_loops": 3}
    assert budget_exhaustion(budget, {"max_active_tasks": 1, "max_task_loops": 2}) is None
    assert budget_exhaustion(budget, {"max_active_tasks": 2, "max_task_loops": 2}) == "max_active_tasks"
    assert budget_exhaustion(budget, {"max_active_tasks": 3, "max_task_loops": 2}) == "max_active_tasks"


@pytest.mark.parametrize("limit", [0, -1, 1_000_000_001])
def test_budget_rejects_unbounded_or_overflow_like_limit(limit: int) -> None:
    with pytest.raises(ValueError, match="finite positive"):
        budget_exhaustion({"max_active_tasks": limit}, {})


def test_budget_rejects_negative_usage() -> None:
    with pytest.raises(ValueError, match="non-negative"):
        budget_exhaustion({"max_active_tasks": 1}, {"max_active_tasks": -1})

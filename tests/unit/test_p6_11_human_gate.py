from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from pados.project_goal import repository
from pados.project_goal.engine import (
    assert_human_gate_transition,
    human_gate_scope_hash,
)
from pados.project_goal.service import ProjectHumanGateService


def _scope() -> dict[str, object]:
    return {"project_id": "project", "goal_id": "goal", "goal_version": 1, "plan_id": "plan", "plan_version": 1, "task_id": "task", "gate_type": "RISK_BUDGET_INCREASE", "risk_delta": 10, "scope_delta": ["src"], "budget_delta": 0, "time_horizon_delta": 0, "evidence_hash": "evidence", "valid_from": "2026-07-11T00:00:00+00:00", "valid_until": None, "required_action": "human approval"}


def test_human_gate_scope_hash_is_canonical_and_changes_with_scoped_data() -> None:
    scope = _scope()
    assert human_gate_scope_hash(scope) == human_gate_scope_hash(dict(reversed(list(scope.items()))))
    assert human_gate_scope_hash(scope) != human_gate_scope_hash(scope | {"task_id": "other-task"})
    assert human_gate_scope_hash(scope) != human_gate_scope_hash(scope | {"evidence_hash": "changed"})


def test_human_gate_scope_hash_normalizes_equivalent_timestamp_offsets() -> None:
    scope = _scope()
    equivalent_timestamp = datetime(2026, 7, 11, 8, tzinfo=timezone(timedelta(hours=8)))
    assert human_gate_scope_hash(scope) == human_gate_scope_hash(scope | {"valid_from": equivalent_timestamp})


@pytest.mark.parametrize("field", sorted(_scope()))
def test_human_gate_scope_hash_changes_for_every_governed_field(field: str) -> None:
    scope = _scope()
    replacement: object = ["changed"] if field == "scope_delta" else "changed"
    if field in {"goal_version", "plan_version", "risk_delta", "budget_delta", "time_horizon_delta"}:
        replacement = 99
    assert human_gate_scope_hash(scope) != human_gate_scope_hash(scope | {field: replacement})


@pytest.mark.parametrize(
    ("current", "desired"),
    [("OPEN", "PENDING_APPROVAL"), ("PENDING_APPROVAL", "APPROVED"), ("PENDING_APPROVAL", "REJECTED"), ("OPEN", "EXPIRED")],
)
def test_human_gate_legal_lifecycle(current: str, desired: str) -> None:
    assert_human_gate_transition(current, desired)


@pytest.mark.parametrize("current, desired", [("OPEN", "APPROVED"), ("REJECTED", "APPROVED"), ("APPROVED", "PENDING_APPROVAL")])
def test_human_gate_illegal_lifecycle(current: str, desired: str) -> None:
    with pytest.raises(ValueError, match="invalid human gate transition"):
        assert_human_gate_transition(current, desired)


def test_human_gate_approval_rejects_a_wrong_task_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    gate = {
        "id": "gate",
        "project_id": "project",
        "project_goal_id": "goal",
        "project_goal_version": 1,
        "project_plan_id": "plan",
        "project_plan_version": 1,
        "project_task_id": "task",
        "gate_type": "RISK_BUDGET_INCREASE",
        "status": "PENDING_APPROVAL",
        "risk_delta": 20,
        "scope_delta": [],
        "budget_delta": 0,
        "time_horizon_delta": 0,
        "supporting_evidence_ids": [],
        "approval_scope_hash": "scope",
    }
    monkeypatch.setattr(repository, "get_human_gate", lambda *args, **kwargs: gate)
    with pytest.raises(PermissionError, match="Task"):
        ProjectHumanGateService().approve(
            object(), gate_id="gate", project_id="project", approval_id="approval",  # type: ignore[arg-type]
            approver="human", task_id="different-task", idempotency_key="approve",
        )

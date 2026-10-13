from __future__ import annotations

import pytest

from pados.project_goal import repository
from pados.project_goal.engine import (
    assert_gap_transition,
    gap_identity,
    validate_gap_resolution,
)
from pados.project_goal.service import ProjectGapService

GOAL = {"id": "goal", "project_id": "project", "version": 1}
PLAN = {"id": "plan", "project_goal_id": "goal", "version": 1}
GAP = {"id": "gap", "project_id": "project", "project_goal_id": "goal", "project_goal_version": 1, "project_plan_version": 1, "project_task_id": "task", "gap_type": "FAILED_VALIDATION", "status": "OPEN", "idempotency_key": None}


def test_gap_identity_is_stable_and_observed_state_creates_new_occurrence() -> None:
    first = gap_identity(project_id="project", goal_id="goal", goal_version=1, plan_id="plan", plan_version=1, subject_id="task", gap_type="FAILED_VALIDATION", required_state={"validation": "PASSED"}, observed_state={"validation": "FAILED"})
    assert first == gap_identity(project_id="project", goal_id="goal", goal_version=1, plan_id="plan", plan_version=1, subject_id="task", gap_type="FAILED_VALIDATION", required_state={"validation": "PASSED"}, observed_state={"validation": "FAILED"})
    assert first != gap_identity(project_id="project", goal_id="goal", goal_version=1, plan_id="plan", plan_version=1, subject_id="task", gap_type="FAILED_VALIDATION", required_state={"validation": "PASSED"}, observed_state={"validation": "BLOCKED"})


def test_gap_lifecycle_requires_evidence_for_resolution_and_gate_for_waiver() -> None:
    assert_gap_transition("OPEN", "ACKNOWLEDGED")
    assert_gap_transition("ACKNOWLEDGED", "RESOLVING")
    with pytest.raises(ValueError, match="evidence"):
        validate_gap_resolution({}, gap_type="FAILED_VALIDATION")
    with pytest.raises(ValueError, match="Human Gate"):
        assert_gap_transition("OPEN", "WAIVED", human_gate_approved=False)
    validate_gap_resolution({"validation_status": "PASSED", "evidence_ids": ["evidence"]}, gap_type="FAILED_VALIDATION")


def test_gap_open_reuses_active_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    monkeypatch.setattr(repository, "get_goal", lambda *args, **kwargs: GOAL)
    monkeypatch.setattr(repository, "get_plan", lambda *args, **kwargs: PLAN)
    monkeypatch.setattr(repository, "get_task", lambda *args, **kwargs: {"project_plan_id": "plan"})
    monkeypatch.setattr(repository, "find_active_gap", lambda *args, **kwargs: GAP)
    monkeypatch.setattr(repository, "append_event", lambda *args, **kwargs: events.append(kwargs["event_type"]))
    row = ProjectGapService().open(object(), project_id="project", goal_id="goal", plan_id="plan", task_id="task", criterion_id="", gap_type="FAILED_VALIDATION", description="failed", required_state={"validation": "PASSED"}, observed_state={"validation": "FAILED"}, severity="CRITICAL", confidence=1.0, idempotency_key="same")  # type: ignore[arg-type]
    assert row == GAP
    assert events == ["GAP_REUSED"]


def test_gap_transition_requires_scoped_gate_and_verified_resolution(monkeypatch: pytest.MonkeyPatch) -> None:
    events: list[str] = []
    monkeypatch.setattr(repository, "get_gap", lambda *args, **kwargs: GAP)
    monkeypatch.setattr(repository, "append_event", lambda *args, **kwargs: events.append(kwargs["event_type"]))
    monkeypatch.setattr(repository, "transition_gap", lambda *args, **kwargs: GAP | {"status": kwargs["desired"]})
    with pytest.raises(PermissionError, match="scoped Human Gate"):
        ProjectGapService().transition(object(), gap_id="gap", project_id="project", desired="WAIVED", actor="human:owner", reason_code="waiver", idempotency_key="waive")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="evidence"):
        ProjectGapService().transition(object(), gap_id="gap", project_id="project", desired="RESOLVED", actor="controller", reason_code="verified", idempotency_key="resolve")  # type: ignore[arg-type]
    monkeypatch.setattr(repository, "get_gap", lambda *args, **kwargs: GAP | {"status": "RESOLVING"})
    row = ProjectGapService().transition(object(), gap_id="gap", project_id="project", desired="RESOLVED", actor="controller", reason_code="verified", idempotency_key="resolve", evidence={"validation_status": "PASSED", "evidence_ids": ["validation:1"]})  # type: ignore[arg-type]
    assert row["status"] == "RESOLVED"
    assert events == ["GAP_RESOLVED"]


def test_gap_waiver_rejects_wrong_gate_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(repository, "get_gap", lambda *args, **kwargs: GAP)
    monkeypatch.setattr(repository, "get_human_gate", lambda *args, **kwargs: {"status": "APPROVED", "gate_type": "GAP_WAIVER", "project_goal_id": "other", "project_goal_version": 1, "project_plan_version": 1, "project_task_id": "task"})
    with pytest.raises(ValueError, match="Human Gate"):
        ProjectGapService().transition(object(), gap_id="gap", project_id="project", desired="WAIVED", actor="human:owner", reason_code="waiver", idempotency_key="waive", human_gate_id="wrong")  # type: ignore[arg-type]

from __future__ import annotations

from typing import Any

import pytest

from pados.loop import service as loop_service
from pados.project_goal import repository
from pados.project_goal import service as project_goal_service
from pados.project_goal.service import ProjectOutcomeAggregationService


def task() -> dict[str, Any]:
    return {"id": "task", "project_id": "project", "project_goal_id": "goal", "status": "ACTIVE", "task_goal_id": "p610-goal", "task_plan_id": "p610-plan", "task_loop_id": "p610-loop"}


def wire(monkeypatch: pytest.MonkeyPatch, status: str) -> list[str]:
    events: list[str] = []
    monkeypatch.setattr(repository, "get_task", lambda *args, **kwargs: task())
    monkeypatch.setattr(loop_service, "get_task_goal", lambda *args, **kwargs: {"id": "p610-goal", "project_id": "project"})
    monkeypatch.setattr(loop_service, "get_task_plan", lambda *args, **kwargs: {"id": "p610-plan", "task_goal_id": "p610-goal"})
    monkeypatch.setattr(loop_service, "get_loop", lambda *args, **kwargs: {"id": "p610-loop", "status": status})
    monkeypatch.setattr(repository, "task_outcome", lambda session, **kwargs: task() | {"status": kwargs["desired"]})
    monkeypatch.setattr(repository, "active_plan", lambda *args, **kwargs: {"id": "plan"})
    def append_event(*args: object, **kwargs: object) -> dict[str, object]:
        event_type = kwargs["event_type"]
        assert isinstance(event_type, str)
        events.append(event_type)
        return {}

    monkeypatch.setattr(repository, "append_event", append_event)
    monkeypatch.setattr(project_goal_service.ProjectGapService, "open", lambda *args, **kwargs: {"id": "gap"})
    return events


def test_non_terminal_p6_10_loop_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    wire(monkeypatch, "RUNNING")
    with pytest.raises(ValueError, match="non-terminal"):
        ProjectOutcomeAggregationService().aggregate(object(), task_id="task", project_id="project", idempotency_key="one")  # type: ignore[arg-type]


def test_failed_p6_10_loop_blocks_project_task(monkeypatch: pytest.MonkeyPatch) -> None:
    events = wire(monkeypatch, "FAILED_TERMINAL")
    result = ProjectOutcomeAggregationService().aggregate(object(), task_id="task", project_id="project", idempotency_key="one")  # type: ignore[arg-type]
    assert result["status"] == "BLOCKED"
    assert events == ["P6_10_EVIDENCE_REJECTED"]

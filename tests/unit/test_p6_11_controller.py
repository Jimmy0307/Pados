from __future__ import annotations

from typing import Any

import pytest

from pados.project_goal import repository
from pados.project_goal.service import ProjectControllerService

GOAL: dict[str, Any] = {
    "id": "goal", "project_id": "project", "status": "ACTIVE", "version": 1,
    "success_criteria": ["done"], "scope_paths": ["src"],
    "risk_budget": {"max_risk_score": 100},
    "execution_budget": {"max_active_tasks": 1, "max_project_cycles": 10},
    "time_horizon_seconds": 60,
}
PLAN: dict[str, Any] = {"id": "plan", "version": 1, "stop_policy": {}}
TASK: dict[str, Any] = {"id": "task", "status": "APPROVED", "scope_paths": ["src"]}


class Adapter:
    def __init__(self) -> None:
        self.calls = 0

    def launch(self, session: object, task_id: str, *, project_id: str, launch_key: str) -> dict[str, Any]:
        self.calls += 1
        return TASK | {"status": "ACTIVE", "task_loop_id": "p610-loop"}


def wire(monkeypatch: pytest.MonkeyPatch, *, goal: dict[str, Any] = GOAL, dependencies: list[dict[str, Any]] | None = None) -> list[str]:
    calls: list[str] = []
    monkeypatch.setattr(repository, "get_goal", lambda *args, **kwargs: goal)
    monkeypatch.setattr(repository, "active_plan", lambda *args, **kwargs: PLAN)
    monkeypatch.setattr(repository, "acquire_lease", lambda *args, **kwargs: {"owner_instance": "owner"})
    monkeypatch.setattr(repository, "get_or_create_loop", lambda *args, **kwargs: {"id": "loop", "current_cycle": 0})
    monkeypatch.setattr(repository, "create_cycle", lambda *args, **kwargs: {"id": "cycle", "status": "RUNNING"})
    monkeypatch.setattr(repository, "list_tasks", lambda *args, **kwargs: [TASK])
    monkeypatch.setattr(repository, "dependencies", lambda *args, **kwargs: dependencies or [])
    monkeypatch.setattr(repository, "transition", lambda *args, **kwargs: TASK | {"status": "READY"})
    monkeypatch.setattr(repository, "insert_snapshot", lambda *args, **kwargs: {"id": "snapshot"})
    monkeypatch.setattr(repository, "append_event", lambda *args, **kwargs: {})

    def complete(*args: object, **kwargs: object) -> dict[str, object]:
        calls.append("complete")
        return {}

    monkeypatch.setattr(repository, "complete_cycle", complete)
    monkeypatch.setattr(repository, "release_lease", lambda *args, **kwargs: calls.append("release"))
    return calls


def test_controller_requires_active_goal(monkeypatch: pytest.MonkeyPatch) -> None:
    wire(monkeypatch, goal=GOAL | {"status": "APPROVED"})
    with pytest.raises(PermissionError, match="active Project Goal"):
        ProjectControllerService(Adapter()).run_cycle(object(), goal_id="goal", project_id="project", owner_instance="owner", idempotency_key="cycle")  # type: ignore[arg-type]


def test_controller_runs_one_bounded_cycle_and_releases_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = wire(monkeypatch)
    adapter = Adapter()
    result = ProjectControllerService(adapter).run_cycle(object(), goal_id="goal", project_id="project", owner_instance="owner", idempotency_key="cycle")  # type: ignore[arg-type]
    assert result["activated_task_id"] == "task"
    assert adapter.calls == 1
    assert calls == ["complete", "release"]


def test_controller_does_not_activate_unsatisfied_dependency(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = wire(monkeypatch, dependencies=[{"dependency_project_id": "project", "dependency_type": "REQUIRES_COMPLETION", "dependency_status": "BLOCKED"}])
    adapter = Adapter()
    result = ProjectControllerService(adapter).run_cycle(object(), goal_id="goal", project_id="project", owner_instance="owner", idempotency_key="cycle")  # type: ignore[arg-type]
    assert result["activated_task_id"] is None
    assert adapter.calls == 0
    assert calls == ["complete", "release"]

from __future__ import annotations

from typing import Any

import pytest

from pados.loop import service as p610_loop_service
from pados.project_goal import repository
from pados.project_goal.orchestration import P610OrchestrationAdapter


def approved_task() -> dict[str, Any]:
    return {
        "id": "task-id",
        "project_id": "project-id",
        "project_goal_id": "goal-id",
        "status": "APPROVED",
        "approval_id": "approval-id",
        "objective": "Inspect repository",
        "success_criteria": ["validation passes"],
        "constraints_json": [],
        "scope_paths": ["src"],
        "risk_level": "LOW",
        "task_goal_template": {"request_id": "request-id"},
        "task_plan_template": {
            "steps": [
                {
                    "sequence": 1,
                    "title": "Inspect",
                    "step_type": "INSPECT",
                    "required_capability": "READ_REPOSITORY",
                    "command_id": "python_version",
                }
            ]
        },
        "launch_idempotency_key": None,
        "task_goal_id": None,
        "task_plan_id": None,
        "task_loop_id": None,
    }


def test_adapter_rejects_unapproved_task(monkeypatch: pytest.MonkeyPatch) -> None:
    task = approved_task() | {"status": "PROPOSED"}
    monkeypatch.setattr(repository, "get_task", lambda *args, **kwargs: task)
    with pytest.raises(PermissionError, match="approved Project Task"):
        P610OrchestrationAdapter().launch(object(), "task-id", project_id="project-id", launch_key="launch")  # type: ignore[arg-type]


def test_adapter_calls_p6_10_services_and_persists_lineage_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = approved_task()
    calls: list[str] = []

    def record(name: str, result: dict[str, str]) -> dict[str, str]:
        calls.append(name)
        return result

    monkeypatch.setattr(repository, "get_task", lambda *args, **kwargs: task)
    monkeypatch.setattr(
        p610_loop_service,
        "create_task_goal",
        lambda session, payload: record("goal", {"id": "p610-goal"}),
    )
    monkeypatch.setattr(
        p610_loop_service,
        "create_task_plan",
        lambda session, payload: record("plan", {"id": "p610-plan"}),
    )
    monkeypatch.setattr(
        p610_loop_service,
        "create_loop",
        lambda session, payload: record("loop", {"id": "p610-loop"}),
    )

    def link(*args: object, **kwargs: object) -> dict[str, Any]:
        calls.append("link")
        return task | {"task_loop_id": "p610-loop"}

    monkeypatch.setattr(repository, "link_p6_10", link)
    monkeypatch.setattr(repository, "append_event", lambda *args, **kwargs: {})
    result = P610OrchestrationAdapter().launch(object(), "task-id", project_id="project-id", launch_key="launch")  # type: ignore[arg-type]
    assert result["task_loop_id"] == "p610-loop"
    assert calls == ["goal", "plan", "loop", "link"]


def test_adapter_idempotent_replay_does_not_relaunch(monkeypatch: pytest.MonkeyPatch) -> None:
    task = approved_task() | {
        "status": "ACTIVE",
        "launch_idempotency_key": "launch",
        "task_goal_id": "p610-goal",
        "task_plan_id": "p610-plan",
        "task_loop_id": "p610-loop",
    }
    monkeypatch.setattr(repository, "get_task", lambda *args, **kwargs: task)
    result = P610OrchestrationAdapter().launch(object(), "task-id", project_id="project-id", launch_key="launch")  # type: ignore[arg-type]
    assert result == task

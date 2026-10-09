from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, NoResultFound

from pados.core.task_service import create_project
from pados.db import session_scope
from pados.project_goal import repository
from pados.project_goal.schemas import ProjectGoalCreate
from pados.project_goal.service import ProjectGoalService


def make_project() -> str:
    with session_scope() as session:
        return create_project(
            session,
            name=f"p611-repository-{uuid4()}",
            root_path=f"C:/PADOS/runtime/p611/{uuid4()}",
        )


def goal_payload(project_id: str, key: str) -> ProjectGoalCreate:
    return ProjectGoalCreate(
        project_id=project_id,
        objective="Verify P6.11 repository governance",
        desired_outcomes=["persistent goal"],
        success_criteria=["repository checks pass"],
        scope_paths=["src"],
        idempotency_key=key,
    )


def test_goal_create_is_idempotent_and_cross_project_read_fails_closed() -> None:
    project_id = make_project()
    other_project_id = make_project()
    service = ProjectGoalService()
    with session_scope() as session:
        first = service.create(session, goal_payload(project_id, "goal-create"))
        replay = service.create(session, goal_payload(project_id, "goal-create"))
        assert first["id"] == replay["id"]
        assert len(repository.events(session, str(first["id"]))) == 1
        with pytest.raises(NoResultFound):
            repository.get_goal(
                session,
                str(first["id"]),
                project_id=other_project_id,
            )


def test_goal_version_and_event_append_only_constraints() -> None:
    project_id = make_project()
    with session_scope() as session:
        goal = ProjectGoalService().create(session, goal_payload(project_id, "goal-version"))
        with pytest.raises(DBAPIError), session.begin_nested():
            session.execute(
                text(
                    "UPDATE project_goal_events SET event_type = 'TAMPERED' "
                    "WHERE project_goal_id = CAST(:id AS uuid)"
                ),
                {"id": str(goal["id"])},
            )


def test_controller_lease_is_owner_safe_and_reentrant() -> None:
    project_id = make_project()
    with session_scope() as session:
        goal = ProjectGoalService().create(session, goal_payload(project_id, "goal-lease"))
        goal_id = str(goal["id"])
        first = repository.acquire_lease(
            session,
            goal_id=goal_id,
            owner="owner-a",
            lease_seconds=60,
        )
        replay = repository.acquire_lease(
            session,
            goal_id=goal_id,
            owner="owner-a",
            lease_seconds=60,
        )
        assert first["project_goal_id"] == replay["project_goal_id"]
        with pytest.raises(NoResultFound):
            repository.acquire_lease(
                session,
                goal_id=goal_id,
                owner="owner-b",
                lease_seconds=60,
            )
        with pytest.raises(PermissionError):
            repository.release_lease(session, goal_id=goal_id, owner="owner-b")
        repository.release_lease(session, goal_id=goal_id, owner="owner-a")

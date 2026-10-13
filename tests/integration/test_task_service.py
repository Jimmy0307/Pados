import pytest
from sqlalchemy import text

from pados.core.task_service import (
    OptimisticLockError,
    claim_next_task,
    create_checkpoint,
    create_project,
    create_task,
    get_task,
    recover_stale_claims,
    release_for_retry,
    transition_task,
)
from pados.db import session_scope


def test_task_lifecycle_and_audit() -> None:
    with session_scope() as session:
        project_id = create_project(session, name="p1-project", root_path=".")
        task_id = create_task(session, project_id=project_id, title="P1", objective="control plane")
        task = get_task(session, task_id)
        scoped = transition_task(
            session,
            task_id=task.id,
            from_version=task.version,
            new_status="PLANNED",
            new_gate="G3",
            actor="planner",
        )
        claimed = claim_next_task(session, agent_role="builder", project_id=project_id)
        assert claimed is not None
        assert claimed.id == scoped.id
        assert claimed.status == "IMPLEMENTING"
        retry = release_for_retry(session, task_id=claimed.id)
        assert retry.status == "FIXING"
        checkpoint_id = create_checkpoint(
            session,
            task_id=retry.id,
            git_commit="test",
            database_revision="20260710_0002",
            snapshot={"status": retry.status},
        )
        assert checkpoint_id


def test_optimistic_lock_rejects_stale_version() -> None:
    with session_scope() as session:
        project_id = create_project(session, name="p1-lock-project", root_path=".")
        task_id = create_task(session, project_id=project_id, title="Lock", objective="version")
        task = get_task(session, task_id)
        transition_task(
            session,
            task_id=task.id,
            from_version=task.version,
            new_status="SCOPED",
            new_gate="G0",
            actor="orchestrator",
        )
        with pytest.raises(OptimisticLockError):
            transition_task(
                session,
                task_id=task.id,
                from_version=task.version,
                new_status="PLANNED",
                new_gate="G3",
                actor="planner",
            )


def test_task_state_machine_rejects_illegal_release_jump() -> None:
    with session_scope() as session:
        project_id = create_project(session, name="p6-6-state-machine-project", root_path=".")
        task_id = create_task(session, project_id=project_id, title="State", objective="illegal release")
        task = get_task(session, task_id)
        with pytest.raises(ValueError, match="ILLEGAL_TASK_TRANSITION"):
            transition_task(
                session,
                task_id=task.id,
                from_version=task.version,
                new_status="RELEASED",
                new_gate="G8",
                actor="release",
            )


def test_worker_does_not_double_claim_project_queue() -> None:
    with session_scope() as session:
        project_id = create_project(session, name="p1-queue-project", root_path=".")
        task_id = create_task(session, project_id=project_id, title="Queue", objective="claim once")
        task = get_task(session, task_id)
        transition_task(
            session,
            task_id=task.id,
            from_version=task.version,
            new_status="PLANNED",
            new_gate="G3",
            actor="planner",
        )
        first_claim = claim_next_task(session, agent_role="builder", project_id=project_id)
        second_claim = claim_next_task(session, agent_role="builder", project_id=project_id)
        assert first_claim is not None
        assert second_claim is None


def test_recover_stale_claim_returns_task_to_queue() -> None:
    with session_scope() as session:
        project_id = create_project(session, name="p1-stale-project", root_path=".")
        task_id = create_task(session, project_id=project_id, title="Stale", objective="recover")
        task = get_task(session, task_id)
        transition_task(
            session,
            task_id=task.id,
            from_version=task.version,
            new_status="PLANNED",
            new_gate="G3",
            actor="planner",
        )
        claimed = claim_next_task(session, agent_role="builder", project_id=project_id)
        assert claimed is not None
        session.execute(
            text("UPDATE tasks SET updated_at = now() - interval '2 hours' WHERE id = :task_id"),
            {"task_id": claimed.id},
        )
        recovered = recover_stale_claims(session, older_than_minutes=30)
        assert recovered >= 1
        reclaimed = claim_next_task(session, agent_role="builder", project_id=project_id)
        assert reclaimed is not None
        assert reclaimed.id == claimed.id

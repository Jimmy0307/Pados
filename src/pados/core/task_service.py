# SPDX-License-Identifier: AGPL-3.0-only
import json
from dataclasses import dataclass
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from pados.core.state_machine import validate_transition


class OptimisticLockError(RuntimeError):
    pass


@dataclass(frozen=True)
class TaskRecord:
    id: str
    status: str
    version: int
    retry_count: int


def _audit(
    session: Session,
    *,
    entity_type: str,
    entity_id: str,
    event_type: str,
    actor: str,
    before_state: dict[str, Any] | None = None,
    after_state: dict[str, Any] | None = None,
) -> None:
    session.execute(
        text(
            """
            INSERT INTO audit_events
              (entity_type, entity_id, event_type, actor, before_state, after_state)
            VALUES
              (:entity_type, :entity_id, :event_type, :actor,
               CAST(:before_state AS jsonb), CAST(:after_state AS jsonb))
            """
        ),
        {
            "entity_type": entity_type,
            "entity_id": entity_id,
            "event_type": event_type,
            "actor": actor,
            "before_state": json.dumps(before_state or {}, sort_keys=True),
            "after_state": json.dumps(after_state or {}, sort_keys=True),
        },
    )


def create_project(
    session: Session,
    *,
    name: str,
    root_path: str,
    default_branch: str = "main",
    classification: str = "internal",
    actor: str = "system",
) -> str:
    project_id = session.execute(
        text(
            """
            INSERT INTO projects (name, root_path, default_branch, status, classification)
            VALUES (:name, :root_path, :default_branch, 'active', :classification)
            RETURNING id
            """
        ),
        {
            "name": name,
            "root_path": root_path,
            "default_branch": default_branch,
            "classification": classification,
        },
    ).scalar_one()
    project_id_text = str(project_id)
    _audit(
        session,
        entity_type="project",
        entity_id=project_id_text,
        event_type="created",
        actor=actor,
        after_state={
            "name": name,
            "root_path": root_path,
            "default_branch": default_branch,
            "classification": classification,
        },
    )
    return project_id_text


def create_task(
    session: Session,
    *,
    project_id: str,
    title: str,
    objective: str,
    actor: str = "orchestrator",
) -> str:
    task_id = session.execute(
        text(
            """
            INSERT INTO tasks (project_id, title, objective, status, current_gate)
            VALUES (:project_id, :title, :objective, 'NEW', 'G0')
            RETURNING id
            """
        ),
        {"project_id": project_id, "title": title, "objective": objective},
    ).scalar_one()
    task_id_text = str(task_id)
    _audit(
        session,
        entity_type="task",
        entity_id=task_id_text,
        event_type="created",
        actor=actor,
        after_state={"status": "NEW", "current_gate": "G0"},
    )
    return task_id_text


def get_task(session: Session, task_id: str) -> TaskRecord:
    row = session.execute(
        text("SELECT id, status, version, retry_count FROM tasks WHERE id = :task_id"),
        {"task_id": task_id},
    ).mappings().one()
    return TaskRecord(
        id=str(row["id"]),
        status=str(row["status"]),
        version=int(row["version"]),
        retry_count=int(row["retry_count"]),
    )


def transition_task(
    session: Session,
    *,
    task_id: str,
    from_version: int,
    new_status: str,
    new_gate: str,
    actor: str,
) -> TaskRecord:
    before = get_task(session, task_id)
    validate_transition(before.status, new_status, new_gate)
    row = session.execute(
        text(
            """
            UPDATE tasks
            SET status = :new_status,
                current_gate = :new_gate,
                version = version + 1,
                updated_at = now(),
                completed_at = CASE
                    WHEN :new_status IN ('READY_FOR_RELEASE', 'RELEASED', 'FAILED', 'CANCELLED')
                    THEN now()
                    ELSE completed_at
                END
            WHERE id = :task_id AND version = :from_version
            RETURNING id, status, version, retry_count
            """
        ),
        {
            "task_id": task_id,
            "from_version": from_version,
            "new_status": new_status,
            "new_gate": new_gate,
        },
    ).mappings().first()
    if row is None:
        raise OptimisticLockError(f"task {task_id} version changed")
    after = TaskRecord(
        id=str(row["id"]),
        status=str(row["status"]),
        version=int(row["version"]),
        retry_count=int(row["retry_count"]),
    )
    _audit(
        session,
        entity_type="task",
        entity_id=task_id,
        event_type="transitioned",
        actor=actor,
        before_state={"status": before.status, "version": before.version},
        after_state={"status": after.status, "version": after.version, "gate": new_gate},
    )
    return after


def claim_next_task(
    session: Session,
    *,
    agent_role: str,
    actor: str = "worker",
    project_id: str | None = None,
) -> TaskRecord | None:
    project_filter = "AND project_id = CAST(:project_id AS uuid)" if project_id is not None else ""
    row = session.execute(
        text(
            f"""
            WITH candidate AS (
                SELECT id
                FROM tasks
                WHERE status IN ('PLANNED', 'FIXING')
                  {project_filter}
                  AND retry_count < max_retries
                ORDER BY priority ASC, created_at ASC
                FOR UPDATE SKIP LOCKED
                LIMIT 1
            )
            UPDATE tasks
            SET status = 'IMPLEMENTING',
                current_gate = 'G4',
                assigned_agent_role = :agent_role,
                version = version + 1,
                updated_at = now()
            WHERE id IN (SELECT id FROM candidate)
            RETURNING id, status, version, retry_count
            """
        ),
        {"agent_role": agent_role, "project_id": project_id},
    ).mappings().first()
    if row is None:
        return None
    task = TaskRecord(
        id=str(row["id"]),
        status=str(row["status"]),
        version=int(row["version"]),
        retry_count=int(row["retry_count"]),
    )
    _audit(
        session,
        entity_type="task",
        entity_id=task.id,
        event_type="claimed",
        actor=actor,
        after_state={
            "status": task.status,
            "agent_role": agent_role,
            "version": task.version,
            "project_id": project_id,
        },
    )
    return task


def release_for_retry(session: Session, *, task_id: str, actor: str = "worker") -> TaskRecord:
    row = session.execute(
        text(
            """
            UPDATE tasks
            SET status = CASE
                    WHEN retry_count + 1 >= max_retries THEN 'BLOCKED'
                    ELSE 'FIXING'
                END,
                current_gate = 'G7',
                retry_count = retry_count + 1,
                assigned_agent_role = NULL,
                version = version + 1,
                updated_at = now()
            WHERE id = :task_id
            RETURNING id, status, version, retry_count
            """
        ),
        {"task_id": task_id},
    ).mappings().one()
    task = TaskRecord(
        id=str(row["id"]),
        status=str(row["status"]),
        version=int(row["version"]),
        retry_count=int(row["retry_count"]),
    )
    _audit(
        session,
        entity_type="task",
        entity_id=task.id,
        event_type="released_for_retry",
        actor=actor,
        after_state={"status": task.status, "retry_count": task.retry_count},
    )
    return task


def recover_stale_claims(
    session: Session,
    *,
    older_than_minutes: int,
    actor: str = "orchestrator",
) -> int:
    rows = session.execute(
        text(
            """
            UPDATE tasks
            SET status = 'PLANNED',
                current_gate = 'G3',
                assigned_agent_role = NULL,
                version = version + 1,
                updated_at = now()
            WHERE status = 'IMPLEMENTING'
              AND updated_at < now() - (:older_than_minutes * interval '1 minute')
            RETURNING id
            """
        ),
        {"older_than_minutes": older_than_minutes},
    ).scalars().all()
    for task_id in rows:
        _audit(
            session,
            entity_type="task",
            entity_id=str(task_id),
            event_type="stale_claim_recovered",
            actor=actor,
            after_state={"status": "PLANNED", "gate": "G3"},
        )
    return len(rows)


def create_checkpoint(
    session: Session,
    *,
    task_id: str,
    git_commit: str,
    database_revision: str,
    snapshot: dict[str, Any],
) -> str:
    checkpoint_id = session.execute(
        text(
            """
            INSERT INTO checkpoints (task_id, state_snapshot, git_commit, database_revision)
            VALUES (:task_id, CAST(:snapshot AS jsonb), :git_commit, :database_revision)
            RETURNING id
            """
        ),
        {
            "task_id": task_id,
            "snapshot": json.dumps(snapshot, sort_keys=True),
            "git_commit": git_commit,
            "database_revision": database_revision,
        },
    ).scalar_one()
    return str(checkpoint_id)

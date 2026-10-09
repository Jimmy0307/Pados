# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session


def list_projects(session: Session) -> list[dict[str, object]]:
    rows = session.execute(
        text(
            """
            SELECT id, name, root_path, status, updated_at
            FROM projects
            ORDER BY updated_at DESC, name ASC
            """
        )
    ).mappings().all()
    return [dict(row) for row in rows]


def get_project(session: Session, project_id: str) -> dict[str, object]:
    row = session.execute(
        text("SELECT id, name, root_path, status, configuration, updated_at FROM projects WHERE id = :id"),
        {"id": project_id},
    ).mappings().one()
    return dict(row)


def project_activity(session: Session, project_id: str) -> list[dict[str, object]]:
    rows = session.execute(
        text(
            """
            SELECT id, event_type, event_time, payload_json
            FROM run_events
            WHERE project_id = :project_id
            ORDER BY event_time DESC
            LIMIT 25
            """
        ),
        {"project_id": project_id},
    ).mappings().all()
    return [dict(row) for row in rows]


def context_summary(session: Session, project_id: str) -> dict[str, object]:
    rows = session.execute(
        text(
            """
            SELECT verification_status, freshness_status, count(*) AS count
            FROM memory_records
            WHERE project_id = :project_id
            GROUP BY verification_status, freshness_status
            """
        ),
        {"project_id": project_id},
    ).mappings().all()
    return {"project_id": project_id, "memory_counts": [dict(row) for row in rows]}


def add_message(session: Session, payload: Mapping[str, object]) -> dict[str, object]:
    row = session.execute(
        text(
            """
            INSERT INTO control_messages(project_id, body, author, idempotency_key)
            VALUES (:project_id, :body, :author, :idempotency_key)
            ON CONFLICT (project_id, idempotency_key) DO UPDATE SET body = control_messages.body
            RETURNING id, project_id, body, author, idempotency_key, created_at
            """
        ),
        dict(payload),
    ).mappings().one()
    append_sync_event(session, str(row["project_id"]), "CONTROL_MESSAGE_CAPTURED", {"message_id": str(row["id"])})
    return dict(row)


def list_messages(session: Session, project_id: str) -> list[dict[str, object]]:
    rows = session.execute(
        text(
            """
            SELECT id, project_id, body, author, idempotency_key, created_at
            FROM control_messages
            WHERE project_id = :project_id
            ORDER BY created_at ASC
            """
        ),
        {"project_id": project_id},
    ).mappings().all()
    return [dict(row) for row in rows]


def create_work_request(session: Session, payload: Mapping[str, object]) -> dict[str, object]:
    values = dict(payload)
    values["status"] = "PENDING_WORKER_ROUTER"
    row = session.execute(
        text(
            """
            INSERT INTO control_work_requests(project_id, title, body, status, idempotency_key)
            VALUES (:project_id, :title, :body, :status, :idempotency_key)
            ON CONFLICT (project_id, idempotency_key) DO UPDATE SET title = control_work_requests.title
            RETURNING id, project_id, title, body, status, idempotency_key, created_at, updated_at
            """
        ),
        values,
    ).mappings().one()
    append_sync_event(session, str(row["project_id"]), "WORK_REQUEST_CAPTURED", {"request_id": str(row["id"])})
    return dict(row)


def get_work_request(session: Session, request_id: str) -> dict[str, object]:
    row = session.execute(
        text("SELECT * FROM control_work_requests WHERE id = :id"),
        {"id": request_id},
    ).mappings().one()
    return dict(row)


def create_approval(session: Session, payload: Mapping[str, object]) -> dict[str, object]:
    row = session.execute(
        text(
            """
            INSERT INTO control_approvals(project_id, proposal_hash, proposal_summary, idempotency_key)
            VALUES (:project_id, :proposal_hash, :proposal_summary, :idempotency_key)
            ON CONFLICT (project_id, idempotency_key) DO UPDATE SET proposal_hash = control_approvals.proposal_hash
            RETURNING *
            """
        ),
        dict(payload),
    ).mappings().one()
    append_sync_event(session, str(row["project_id"]), "APPROVAL_CREATED", {"approval_id": str(row["id"])})
    return dict(row)


def list_approvals(session: Session) -> list[dict[str, object]]:
    rows = session.execute(text("SELECT * FROM control_approvals ORDER BY created_at DESC")).mappings().all()
    return [dict(row) for row in rows]


def get_approval(session: Session, approval_id: str) -> dict[str, object]:
    row = session.execute(text("SELECT * FROM control_approvals WHERE id = :id"), {"id": approval_id}).mappings().one()
    return dict(row)


def decide_approval(
    session: Session,
    approval_id: str,
    *,
    version: int,
    status: str,
    reason: str,
    idempotency_key: str,
) -> dict[str, object]:
    current = get_approval(session, approval_id)
    if current["status"] != "PENDING":
        if current.get("decision_reason") == idempotency_key:
            return current
        raise ValueError("approval is not pending")
    current_version = current["version"]
    if not isinstance(current_version, int):
        current_version = int(str(current_version))
    if current_version != version:
        raise ValueError("stale approval version")
    row = session.execute(
        text(
            """
            UPDATE control_approvals
            SET status = :status,
                version = version + 1,
                decision_reason = :decision_reason,
                decided_at = now(),
                updated_at = now()
            WHERE id = :id AND version = :version AND status = 'PENDING'
            RETURNING *
            """
        ),
        {
            "id": approval_id,
            "version": version,
            "status": status,
            "decision_reason": idempotency_key if not reason else f"{idempotency_key}: {reason}",
        },
    ).mappings().one()
    append_sync_event(session, str(row["project_id"]), f"APPROVAL_{status}", {"approval_id": approval_id})
    return dict(row)


def append_sync_event(
    session: Session,
    project_id: str | None,
    event_type: str,
    payload_json: dict[str, Any],
) -> None:
    session.execute(
        text(
            """
            INSERT INTO control_sync_events(project_id, event_type, payload_json)
            VALUES (:project_id, :event_type, CAST(:payload_json AS jsonb))
            """
        ),
        {"project_id": project_id, "event_type": event_type, "payload_json": __import__("json").dumps(payload_json)},
    )


def sync_events(session: Session, cursor: int) -> list[dict[str, object]]:
    rows = session.execute(
        text(
            """
            SELECT sequence_number, project_id, event_type, payload_json, event_time
            FROM control_sync_events
            WHERE sequence_number > :cursor
            ORDER BY sequence_number ASC
            LIMIT 100
            """
        ),
        {"cursor": cursor},
    ).mappings().all()
    return [dict(row) for row in rows]

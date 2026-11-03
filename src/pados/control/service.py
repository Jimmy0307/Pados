# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from pados.control.approval_subject import (
    APPROVAL_PROJECT_MISMATCH,
    APPROVAL_REQUEST_MISMATCH,
    APPROVAL_SUBJECT_HASH_MISMATCH,
    WorkRouteSubject,
    subject_hash,
)
from pados.logging_config import get_logger

_LOG = get_logger("pados.control")


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


def _same_content(row: Any, values: Mapping[str, Any], fields: tuple[str, ...], code: str) -> None:
    """OP-004: a reused idempotency key is a replay only when the content is the same; otherwise a conflict."""
    if any(str(row.get(field)) != str(values.get(field)) for field in fields):
        raise ValueError(code)


def _captured(session: Session, row: Any, event_type: str, payload: dict[str, Any]) -> dict[str, object]:
    """Record the capture event only when the row was just inserted; a replay changes nothing and records nothing.

    ``inserted`` comes from ``RETURNING (xmax = 0)``: zero on a fresh insert, the updating transaction on the
    ``ON CONFLICT DO UPDATE`` replay path. It is dropped from the returned record, so a replay returns the same shape.
    """
    record = {key: value for key, value in row.items() if key != "inserted"}
    if row["inserted"]:
        append_sync_event(session, str(record["project_id"]), event_type, payload)
    else:
        _LOG.info("control.idempotent_replay", outcome="replayed", event_type=event_type)
    return record


def add_message(session: Session, payload: Mapping[str, object]) -> dict[str, object]:
    row = session.execute(
        text(
            """
            INSERT INTO control_messages(project_id, body, author, idempotency_key)
            VALUES (:project_id, :body, :author, :idempotency_key)
            ON CONFLICT (project_id, idempotency_key) DO UPDATE SET body = control_messages.body
            RETURNING id, project_id, body, author, idempotency_key, created_at, (xmax = 0) AS inserted
            """
        ),
        dict(payload),
    ).mappings().one()
    _same_content(row, payload, ("body", "author"), "MESSAGE_IDEMPOTENCY_CONTENT_CONFLICT")
    return _captured(session, row, "CONTROL_MESSAGE_CAPTURED", {"message_id": str(row["id"])})


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
            RETURNING id, project_id, title, body, status, idempotency_key, created_at, updated_at,
                      (xmax = 0) AS inserted
            """
        ),
        values,
    ).mappings().one()
    _same_content(row, values, ("title", "body"), "WORK_REQUEST_IDEMPOTENCY_CONTENT_CONFLICT")
    return _captured(session, row, "WORK_REQUEST_CAPTURED", {"request_id": str(row["id"])})


def get_work_request(session: Session, request_id: str) -> dict[str, object]:
    row = session.execute(
        text("SELECT * FROM control_work_requests WHERE id = :id"),
        {"id": request_id},
    ).mappings().one()
    return dict(row)


_INSERT_UNBOUND_APPROVAL = """
    INSERT INTO control_approvals(project_id, proposal_hash, proposal_summary, idempotency_key)
    VALUES (:project_id, :proposal_hash, :proposal_summary, :idempotency_key)
    ON CONFLICT (project_id, idempotency_key) DO UPDATE SET proposal_hash = control_approvals.proposal_hash
    RETURNING *, (xmax = 0) AS inserted
"""

_INSERT_BOUND_APPROVAL = """
    INSERT INTO control_approvals(
      project_id, proposal_hash, proposal_summary, idempotency_key, subject_kind, subject_json
    )
    VALUES (
      :project_id, :proposal_hash, :proposal_summary, :idempotency_key, :subject_kind,
      CAST(:subject_json AS jsonb)
    )
    ON CONFLICT (project_id, idempotency_key) DO UPDATE SET proposal_hash = control_approvals.proposal_hash
    RETURNING *, (xmax = 0) AS inserted
"""


_DECIDE_UNBOUND_APPROVAL = """
    UPDATE control_approvals
    SET status = :status, version = version + 1, decision_reason = :decision_reason,
        decided_at = now(), updated_at = now()
    WHERE id = :id AND version = :version AND status = 'PENDING'
    RETURNING *
"""

_DECIDE_WORK_ROUTE_APPROVAL = """
    UPDATE control_approvals
    SET status = :status, version = version + 1, decision_reason = :decision_reason,
        decided_at = now(),
        expires_at = CASE WHEN :status = 'APPROVED' THEN now() + make_interval(secs => :ttl_seconds) END,
        updated_at = now()
    WHERE id = :id AND version = :version AND status = 'PENDING' AND subject_kind = 'WORK_ROUTE'
    RETURNING *
"""


def _bound_subject_values(session: Session, values: dict[str, Any], raw_subject: object) -> dict[str, Any]:
    """Validate a WORK_ROUTE subject and derive proposal_hash from it (RC2 D1)."""
    subject = WorkRouteSubject.model_validate(raw_subject)
    derived = subject_hash(subject)
    supplied = values.get("proposal_hash")
    if supplied is not None and str(supplied).lower() != derived:
        raise ValueError(APPROVAL_SUBJECT_HASH_MISMATCH)
    if subject.project_id != str(values["project_id"]):
        raise PermissionError(APPROVAL_PROJECT_MISMATCH)
    request = session.execute(
        text(
            """
            SELECT 1 FROM control_work_requests
            WHERE id::text = :request_id AND project_id::text = :project_id
            """
        ),
        {"request_id": subject.request_id, "project_id": subject.project_id},
    ).first()
    if request is None:
        raise PermissionError(APPROVAL_REQUEST_MISMATCH)
    return {
        **values,
        "proposal_hash": derived,
        "subject_kind": subject.kind,
        "subject_json": json.dumps(subject.model_dump(mode="json"), sort_keys=True),
    }


def create_approval(session: Session, payload: Mapping[str, object]) -> dict[str, object]:
    """Create an approval; one with a WORK_ROUTE subject is the only kind that can route work."""
    values: dict[str, Any] = {key: value for key, value in payload.items() if key != "subject"}
    raw_subject = payload.get("subject")
    if raw_subject is None:
        row = session.execute(text(_INSERT_UNBOUND_APPROVAL), values).mappings().one()
        _same_content(row, values, ("proposal_hash", "proposal_summary"), "APPROVAL_IDEMPOTENCY_CONTENT_CONFLICT")
    else:
        values = _bound_subject_values(session, values, raw_subject)
        row = session.execute(text(_INSERT_BOUND_APPROVAL), values).mappings().one()
        _same_content(row, values, ("proposal_hash", "proposal_summary"), "APPROVAL_IDEMPOTENCY_CONTENT_CONFLICT")
    return _captured(session, row, "APPROVAL_CREATED", {"approval_id": str(row["id"])})


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
    ttl_seconds: int | None = None,
) -> dict[str, object]:
    """Decide a PENDING approval. An approved WORK_ROUTE approval authorizes new work only until ``expires_at``."""
    if ttl_seconds is None:
        from pados.config import get_settings

        ttl_seconds = get_settings().approval_ttl_seconds
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
    # Only a WORK_ROUTE approval has a lifecycle; a subject-less approval is decided with the columns every schema
    # has, as D1 inserts it, so the proof lane (main heads 0011/0013) keeps working.
    lifecycle = current.get("subject_kind") == "WORK_ROUTE"
    row = session.execute(
        text(_DECIDE_WORK_ROUTE_APPROVAL if lifecycle else _DECIDE_UNBOUND_APPROVAL),
        {
            "id": approval_id,
            "version": version,
            "status": status,
            "decision_reason": idempotency_key if not reason else f"{idempotency_key}: {reason}",
            **({"ttl_seconds": ttl_seconds} if lifecycle else {}),
        },
    ).mappings().one()
    append_sync_event(session, str(row["project_id"]), f"APPROVAL_{status}", {"approval_id": approval_id})
    return dict(row)


def revoke_approval(
    session: Session,
    approval_id: str,
    *,
    version: int,
    reason: str,
    idempotency_key: str,
) -> dict[str, object]:
    """Revoke a PENDING or APPROVED WORK_ROUTE approval (status CANCELLED); no new work can route under it.

    Subject-less approvals (ResultCandidate and remote decisions) keep their own governed lifecycle and are refused.
    A loop already running under the approval stops before its next route and waits for a human.
    """
    current = get_approval(session, approval_id)
    marker = f"revoked:{idempotency_key}"
    if current["status"] == "CANCELLED" and str(current.get("decision_reason") or "").startswith(marker):
        return current
    if current.get("subject_kind") != "WORK_ROUTE" or current["status"] not in {"PENDING", "APPROVED"}:
        raise ValueError("APPROVAL_NOT_REVOCABLE")
    if int(str(current["version"])) != version:
        raise ValueError("stale approval version")
    row = session.execute(
        text(
            """
            UPDATE control_approvals
            SET status = 'CANCELLED', version = version + 1, decision_reason = :decision_reason,
                decided_at = now(), updated_at = now()
            WHERE id = :id AND version = :version AND status IN ('PENDING', 'APPROVED')
              AND subject_kind = 'WORK_ROUTE'
            RETURNING *
            """
        ),
        {"id": approval_id, "version": version, "decision_reason": f"{marker}: {reason}" if reason else marker},
    ).mappings().one_or_none()
    if row is None:
        raise ValueError("stale approval version")
    append_sync_event(session, str(row["project_id"]), "APPROVAL_REVOKED", {"approval_id": approval_id})
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
        {"project_id": project_id, "event_type": event_type, "payload_json": json.dumps(payload_json)},
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

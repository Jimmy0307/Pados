# SPDX-License-Identifier: AGPL-3.0-only
"""Capture a TaskEnvelope for a work request created through the local control API.

A plan whose steps bind product goals (the ResultCandidate path) needs the request body to carry a hashed
TaskEnvelope. The Remote console captures one server-side (``remote.live.task_envelope_for_intent``); this module
does the same for the local Control Center, through the same intake helper, when the caller names the capability
the work needs (``requested_capability``). Capture records the stated intent and one pending root goal; it does not
decompose, grants nothing (approval, routing and policy still decide), and never runs on requests without a
capability, so plain requests and client-built envelope bodies are stored exactly as before.

Idempotency: the envelope carries its capture time, so a retry with the same key is answered from the stored row
(same title, text and capability: the same record; anything else: ``WORK_REQUEST_IDEMPOTENCY_CONTENT_CONFLICT``)
instead of recapturing; a concurrent first submission that loses the insert is answered the same way. The stored
envelope keeps the ScopeVersion it was captured at: if the scope moves before planning, the plan is refused as stale
and the work needs a new request (a new key), as for a Remote console task.
"""
from __future__ import annotations

import json
from datetime import datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from pados.control import service as control_service
from pados.control.schemas import MAX_WORK_REQUEST_BODY_CHARS, WorkRequestCreate
from pados.enterprise.classification import DataClassification, parse_classification
from pados.execution.context import canonical_hash
from pados.work_protocol.intake import capture_task_envelope
from pados.work_protocol.models import TaskPriority

CAPTURED_BY = "local:control-operator"  # whoever holds the local control token; not an authenticated person
MAX_CAPTURED_INTENT_CHARS = 2_000  # Goal.objective's limit: the intent becomes the root goal's objective
_CONFLICT = "WORK_REQUEST_IDEMPOTENCY_CONTENT_CONFLICT"


def captured_task_id(project_id: str, idempotency_key: str) -> str:
    """Stable per request: the same project and key always name the same task."""
    return "task:" + canonical_hash({"project_id": project_id, "idempotency_key": idempotency_key})[:40]


def captured_request_body(
    *,
    project_id: str,
    idempotency_key: str,
    title: str,
    request_text: str,
    capability: str,
    classification: DataClassification,
    scope_version: int,
    created_at: datetime,
) -> str:
    """The stored body: the operator's text and capability, plus the hashed envelope captured from them."""
    intent = f"{title.strip()}\n\n{request_text.strip()}"
    if len(intent) > MAX_CAPTURED_INTENT_CHARS:
        raise ValueError("TASK_INTENT_TOO_LONG")
    envelope = capture_task_envelope(
        task_id=captured_task_id(project_id, idempotency_key),
        original_intent=intent,
        project_ref=project_id,
        classification=classification,
        priority=TaskPriority.NORMAL,
        deadline=None,
        scope_version_ref=f"scope:{scope_version}",
        created_by=CAPTURED_BY,
        created_at=created_at,
        capability_hint=capability,
    )
    body = json.dumps(
        {
            "request_text": request_text,
            "requested_capability": capability,
            "task_envelope": envelope.model_dump(mode="json"),
            "task_envelope_hash": envelope.content_hash,
        },
        sort_keys=True,
        ensure_ascii=False,
    )
    if len(body) > MAX_WORK_REQUEST_BODY_CHARS:
        raise ValueError("TASK_ENVELOPE_STORAGE_SIZE_EXCEEDED")
    return body


def _stored_body(session: Session, project_id: str, idempotency_key: str) -> tuple[str, str] | None:
    row = session.execute(
        text(
            "SELECT title, body FROM control_work_requests "
            "WHERE project_id = CAST(:project_id AS uuid) AND idempotency_key = :key"
        ),
        {"project_id": project_id, "key": idempotency_key},
    ).mappings().first()
    return None if row is None else (str(row["title"]), str(row["body"]))


def _same_capture(stored_title: str, stored_body: str, payload: WorkRequestCreate) -> bool:
    try:
        body = json.loads(stored_body)
    except ValueError:
        return False
    return (
        isinstance(body, dict)
        and stored_title == payload.title
        and body.get("request_text") == payload.body
        and body.get("requested_capability") == str(payload.requested_capability)
        and isinstance(body.get("task_envelope"), dict)
    )


def create_captured_work_request(
    session: Session,
    payload: WorkRequestCreate,
    *,
    scope_version: int | None,
    now: datetime,
) -> dict[str, Any]:
    """Create (or replay) a work request whose body carries a server-captured TaskEnvelope."""
    if payload.requested_capability is None:
        raise ValueError("TASK_CAPABILITY_REQUIRED")
    record = {"project_id": payload.project_id, "title": payload.title, "idempotency_key": payload.idempotency_key}
    stored = _stored_body(session, payload.project_id, payload.idempotency_key)
    if stored is not None:
        if not _same_capture(*stored, payload):
            raise ValueError(_CONFLICT)
        return control_service.create_work_request(session, {**record, "body": stored[1]})
    if scope_version is None:
        raise ValueError("TASK_ENVELOPE_SCOPE_VERSION_UNAVAILABLE")
    classification = session.execute(
        text("SELECT classification FROM projects WHERE id = CAST(:id AS uuid)"), {"id": payload.project_id}
    ).scalar_one()
    body = captured_request_body(
        project_id=payload.project_id,
        idempotency_key=payload.idempotency_key,
        title=payload.title,
        request_text=payload.body,
        capability=str(payload.requested_capability),
        classification=parse_classification(classification),
        scope_version=scope_version,
        created_at=now,
    )
    try:
        return control_service.create_work_request(session, {**record, "body": body})
    except ValueError as exc:
        # A concurrent first submission with this key committed its own capture (another capture time): the same
        # request is a replay of that row; anything else stays a conflict.
        stored = _stored_body(session, payload.project_id, payload.idempotency_key)
        if str(exc) != _CONFLICT or stored is None or not _same_capture(*stored, payload):
            raise
        return control_service.create_work_request(session, {**record, "body": stored[1]})

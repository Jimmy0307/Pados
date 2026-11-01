# SPDX-License-Identifier: AGPL-3.0-only
# ruff: noqa: F811 - the D1 fixtures are imported by name and requested as test parameters
"""OP-004: a reused idempotency key returns the same record; with different content it is a conflict, never a
silent success that hands back what was stored before.

Uses the D1 fixtures (PADOS_RC2_APPROVAL_BINDING_TEST_DATABASE_URL).
"""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy.orm import Session

from pados.control import service as control_service
from tests.integration.test_rc2_approval_binding_postgres import _project, session  # noqa: F401


def _project_id(session: Session, tmp_path: Path) -> str:
    return _project(session, tmp_path / "ws")


def test_a_duplicate_work_request_returns_the_same_record_and_different_content_conflicts(
        session: Session, tmp_path: Path) -> None:
    project_id = _project_id(session, tmp_path)
    key = f"op004-request-{uuid4()}"
    first = control_service.create_work_request(session, {"project_id": project_id, "title": "t", "body": "b",
                                                          "idempotency_key": key})
    again = control_service.create_work_request(session, {"project_id": project_id, "title": "t", "body": "b",
                                                          "idempotency_key": key})
    assert again["id"] == first["id"]
    with pytest.raises(ValueError, match="WORK_REQUEST_IDEMPOTENCY_CONTENT_CONFLICT"):
        control_service.create_work_request(session, {"project_id": project_id, "title": "t", "body": "changed",
                                                      "idempotency_key": key})


def test_a_duplicate_message_returns_the_same_record_and_different_content_conflicts(
        session: Session, tmp_path: Path) -> None:
    project_id = _project_id(session, tmp_path)
    key = f"op004-message-{uuid4()}"
    payload = {"project_id": project_id, "body": "hello", "author": "owner", "idempotency_key": key}
    assert control_service.add_message(session, payload)["id"] == control_service.add_message(session, payload)["id"]
    with pytest.raises(ValueError, match="MESSAGE_IDEMPOTENCY_CONTENT_CONFLICT"):
        control_service.add_message(session, {**payload, "body": "different"})


def test_a_duplicate_unbound_approval_returns_the_same_record_and_different_content_conflicts(
        session: Session, tmp_path: Path) -> None:
    project_id = _project_id(session, tmp_path)
    key = f"op004-approval-{uuid4()}"
    payload = {"project_id": project_id, "proposal_hash": "a" * 64, "proposal_summary": "s", "idempotency_key": key}
    first = control_service.create_approval(session, payload)
    assert control_service.create_approval(session, payload)["id"] == first["id"]
    for change in ({"proposal_hash": "b" * 64}, {"proposal_summary": "other"}):
        with pytest.raises(ValueError, match="APPROVAL_IDEMPOTENCY_CONTENT_CONFLICT"):
            control_service.create_approval(session, {**payload, **change})

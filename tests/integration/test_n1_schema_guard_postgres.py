# SPDX-License-Identifier: AGPL-3.0-only
# ruff: noqa: F811 - the D1 session fixture is imported by name and requested as a test parameter
"""N-1: the result-acceptance step-up guard holds in the default ``public`` schema and in a non-default schema.

Before the fix the guard probed ``to_regclass('public.result_acceptance_candidates')`` but read the table unqualified.
With PADOS installed in another schema (first on the ``search_path``) and no copy in ``public``, the probe missed it,
and the control token could decide an approval that backs a ResultCandidate. Each test runs inside the fixture's
transaction, which is rolled back: any ``public`` copy is hidden by a transactional rename, and the scratch schema and
tables disappear with the rollback.

Uses the D1 fixtures (PADOS_RC2_APPROVAL_BINDING_TEST_DATABASE_URL).
"""
from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from pados.control import service as control_service
from pados.db.relations import relation_visible
from tests.integration.test_rc2_approval_binding_postgres import session  # noqa: F401

TABLE = "result_acceptance_candidates"


def _hide_public_copy(session: Session) -> None:
    """Rename any real ``public`` table out of the way for this transaction only (DDL rolls back in PostgreSQL)."""
    hidden = f"{TABLE}_n1_{uuid4().hex[:8]}"
    # The rename takes an ACCESS EXCLUSIVE lock until the rollback: fail fast rather than wait on another session.
    session.execute(text("SET LOCAL lock_timeout = '5s'"))
    session.execute(text(f"ALTER TABLE IF EXISTS public.{TABLE} RENAME TO {hidden}"))


def _install_into(session: Session, schema: str, approval_id: str) -> None:
    """A minimal stand-in carrying the one column the guard reads, created unqualified as PADOS creates its tables."""
    session.execute(text(f"CREATE SCHEMA IF NOT EXISTS {schema}"))
    session.execute(text(f"SET LOCAL search_path TO {schema}"))
    session.execute(text(f"CREATE TABLE {TABLE} (approval_id uuid)"))
    session.execute(text(f"INSERT INTO {TABLE} (approval_id) VALUES (CAST(:a AS uuid))"), {"a": approval_id})


def _pending(approval_id: str) -> dict[str, object]:
    return {"id": approval_id, "status": "PENDING", "version": 1, "decision_reason": None, "subject_kind": None}


@pytest.mark.parametrize("schema", ["public", "pados_n1_alt"])
def test_the_guard_sees_the_table_in_the_schema_it_was_installed_into(session: Session, schema: str) -> None:
    approval_id = str(uuid4())
    _hide_public_copy(session)
    _install_into(session, schema, approval_id)
    assert relation_visible(session, TABLE)
    assert control_service._backs_a_result_candidate(session, approval_id) is True
    assert control_service._backs_a_result_candidate(session, str(uuid4())) is False


@pytest.mark.parametrize("schema", ["public", "pados_n1_alt"])
def test_the_control_token_cannot_decide_a_result_acceptance_in_either_schema(
        session: Session, monkeypatch: pytest.MonkeyPatch, schema: str) -> None:
    """End to end through ``decide_approval``: refused with RESULT_ACCEPTANCE_REQUIRES_STEP_UP, nothing written."""
    approval_id = str(uuid4())
    _hide_public_copy(session)
    _install_into(session, schema, approval_id)
    monkeypatch.setattr(control_service, "get_approval", lambda _s, _id: _pending(approval_id))
    with pytest.raises(PermissionError, match="^RESULT_ACCEPTANCE_REQUIRES_STEP_UP$"):
        control_service.decide_approval(session, approval_id, version=1, status="APPROVED", reason="",
                                        idempotency_key=f"n1-{uuid4().hex[:8]}", ttl_seconds=60)


def test_a_public_table_off_the_search_path_is_neither_probed_nor_queried(session: Session) -> None:
    """The opposite mismatch: a ``public`` copy the unqualified query cannot see must not be reported as present
    (before the fix the probe found it and the query that followed failed with "relation does not exist")."""
    session.execute(text("CREATE SCHEMA IF NOT EXISTS pados_n1_empty"))
    session.execute(text("SET LOCAL search_path TO pados_n1_empty"))
    session.execute(text(f"CREATE TABLE IF NOT EXISTS public.{TABLE} (approval_id uuid)"))
    assert relation_visible(session, TABLE) is False
    assert control_service._backs_a_result_candidate(session, str(uuid4())) is False

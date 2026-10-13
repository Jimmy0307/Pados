from __future__ import annotations

from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any, Self
from uuid import UUID

import pytest

from pados.corpus.project_context import (
    PROJECT_CONTEXT_PROJECTION_ABSENT_CREATABLE,
    PROJECT_CONTEXT_PROJECTION_PRESENT,
    PROJECT_CONTEXT_PROJECTION_STALE_REFRESHABLE,
    ProjectContextProjectionBlocked,
    ensure_project_context_projection,
    inspect_project_context_projection,
)

PROJECT_ID = "a45eed63-f747-49f7-9f48-05aaa6eb3e72"
OPERATIONAL_DATABASE = "pados_local_runtime"
CORPUS_DATABASE = "pados_corpus_runtime"
SOURCE_CREATED = datetime(2026, 8, 1, 0, 0, tzinfo=UTC)
SOURCE_UPDATED = datetime(2026, 8, 2, 0, 0, tzinfo=UTC)


def _source_row(*, name: str = "PADOS", updated_at: datetime = SOURCE_UPDATED) -> tuple[Any, ...]:
    return (UUID(PROJECT_ID), name, "C:/PADOS", "main", "active", "internal", SOURCE_CREATED, updated_at)


def _local_row(
    *,
    name: str = "PADOS",
    updated_at: datetime = SOURCE_UPDATED,
    configuration: dict[str, Any] | None = None,
) -> tuple[Any, ...]:
    return (UUID(PROJECT_ID), name, "C:/PADOS", "main", "active", "internal", configuration or {}, SOURCE_CREATED, updated_at)


class FakeResult:
    def __init__(self, row: tuple[Any, ...] | None) -> None:
        self.row = row

    def fetchone(self) -> tuple[Any, ...] | None:
        return self.row


class FakeTransaction:
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


class FakeConnection:
    def __init__(self, database: str, *, source: tuple[Any, ...] | None, local: tuple[Any, ...] | None) -> None:
        self.database = database
        self.source = source
        self.local = local
        self.writes: list[str] = []
        self.statements: list[str] = []
        self.updated_local: tuple[Any, ...] | None = None

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def transaction(self) -> FakeTransaction:
        return FakeTransaction()

    def execute(self, sql: str, params: tuple[Any, ...] = ()) -> FakeResult:
        normalized = " ".join(sql.split()).upper()
        self.statements.append(normalized)
        if normalized.startswith("INSERT INTO PROJECTS"):
            self.writes.append("INSERT")
            jsonb = params[6]
            configuration = getattr(jsonb, "obj", jsonb)
            self.local = _local_row(configuration=dict(configuration))
            return FakeResult(None)
        if normalized.startswith("UPDATE PROJECTS"):
            self.writes.append("UPDATE")
            jsonb = params[5]
            configuration = getattr(jsonb, "obj", jsonb)
            self.local = _local_row(name=str(params[0]), configuration=dict(configuration))
            return FakeResult(None)
        if normalized == "SELECT CURRENT_DATABASE()":
            return FakeResult((self.database,))
        if normalized == "SET TRANSACTION READ ONLY":
            return FakeResult(None)
        if normalized.startswith("SELECT ID, NAME, ROOT_PATH") and self.database == OPERATIONAL_DATABASE:
            return FakeResult(self.source)
        if normalized.startswith("SELECT ID, NAME, ROOT_PATH") and self.database == CORPUS_DATABASE:
            return FakeResult(self.local)
        if normalized == "SELECT ID FROM PROJECTS WHERE ID = %S":
            return FakeResult((UUID(PROJECT_ID),) if self.local is not None else None)
        raise AssertionError(f"unexpected SQL: {sql}")


def _factory(connections: dict[str, FakeConnection]):
    @contextmanager
    def connect(database: str):
        yield connections[database]

    return connect


def _projection_metadata() -> dict[str, str]:
    return {
        "contract": "P6_15_PROJECT_CONTEXT_PROJECTION",
        "source_database": OPERATIONAL_DATABASE,
        "source_project_id": PROJECT_ID,
        "source_record_timestamp": SOURCE_UPDATED.isoformat(),
        "source_record_version": SOURCE_UPDATED.isoformat(),
        "source_revision": "20260711_0009",
        "synchronized_at": "2026-08-02T00:00:00+00:00",
    }


def test_doctor_reports_absent_projection_without_writing() -> None:
    connections = {
        OPERATIONAL_DATABASE: FakeConnection(OPERATIONAL_DATABASE, source=_source_row(), local=None),
        CORPUS_DATABASE: FakeConnection(CORPUS_DATABASE, source=None, local=None),
    }
    result = inspect_project_context_projection(
        project_id=PROJECT_ID,
        operational_database=OPERATIONAL_DATABASE,
        corpus_database=CORPUS_DATABASE,
        operational_revision="20260711_0009",
        connect=_factory(connections),
    )
    assert result["status"] == PROJECT_CONTEXT_PROJECTION_ABSENT_CREATABLE
    assert not connections[OPERATIONAL_DATABASE].writes
    assert not connections[CORPUS_DATABASE].writes


def test_projection_created_then_unchanged_and_operational_read_stays_read_only() -> None:
    connections = {
        OPERATIONAL_DATABASE: FakeConnection(OPERATIONAL_DATABASE, source=_source_row(), local=None),
        CORPUS_DATABASE: FakeConnection(CORPUS_DATABASE, source=None, local=None),
    }
    first = ensure_project_context_projection(
        project_id=PROJECT_ID,
        operational_database=OPERATIONAL_DATABASE,
        corpus_database=CORPUS_DATABASE,
        operational_revision="20260711_0009",
        connect=_factory(connections),
    )
    assert first["status"] == "CREATED"
    second = ensure_project_context_projection(
        project_id=PROJECT_ID,
        operational_database=OPERATIONAL_DATABASE,
        corpus_database=CORPUS_DATABASE,
        operational_revision="20260711_0009",
        connect=_factory(connections),
    )
    assert second["status"] == "UNCHANGED"
    assert connections[OPERATIONAL_DATABASE].writes == []
    assert connections[CORPUS_DATABASE].writes == ["INSERT"]
    assert not any(statement.startswith(("INSERT", "UPDATE", "DELETE")) for statement in connections[OPERATIONAL_DATABASE].statements)
    assert "SELECT ID FROM PROJECTS WHERE ID = %S" in connections[CORPUS_DATABASE].statements


def test_projection_refreshes_after_authoritative_context_change() -> None:
    connections = {
        OPERATIONAL_DATABASE: FakeConnection(OPERATIONAL_DATABASE, source=_source_row(), local=_local_row()),
        CORPUS_DATABASE: FakeConnection(CORPUS_DATABASE, source=None, local=_local_row()),
    }
    result = ensure_project_context_projection(
        project_id=PROJECT_ID,
        operational_database=OPERATIONAL_DATABASE,
        corpus_database=CORPUS_DATABASE,
        operational_revision="20260711_0009",
        connect=_factory(connections),
    )
    assert result["status"] == "REFRESHED"
    assert connections[CORPUS_DATABASE].writes == ["UPDATE"]


def test_projection_inspection_classifies_present_and_stale() -> None:
    metadata = _projection_metadata()
    present_local = _local_row(configuration={"_p6_15_project_context_projection": metadata})
    connections = {
        OPERATIONAL_DATABASE: FakeConnection(OPERATIONAL_DATABASE, source=_source_row(), local=None),
        CORPUS_DATABASE: FakeConnection(CORPUS_DATABASE, source=None, local=present_local),
    }
    present = inspect_project_context_projection(
        project_id=PROJECT_ID,
        operational_database=OPERATIONAL_DATABASE,
        corpus_database=CORPUS_DATABASE,
        operational_revision="20260711_0009",
        connect=_factory(connections),
    )
    assert present["status"] == PROJECT_CONTEXT_PROJECTION_PRESENT
    connections[CORPUS_DATABASE].local = _local_row(
        name="old PADOS", configuration={"_p6_15_project_context_projection": metadata}
    )
    stale = inspect_project_context_projection(
        project_id=PROJECT_ID,
        operational_database=OPERATIONAL_DATABASE,
        corpus_database=CORPUS_DATABASE,
        operational_revision="20260711_0009",
        connect=_factory(connections),
    )
    assert stale["status"] == PROJECT_CONTEXT_PROJECTION_STALE_REFRESHABLE


def test_missing_operational_project_and_distinct_database_guard_fail_closed() -> None:
    connections = {
        OPERATIONAL_DATABASE: FakeConnection(OPERATIONAL_DATABASE, source=None, local=None),
        CORPUS_DATABASE: FakeConnection(CORPUS_DATABASE, source=None, local=None),
    }
    with pytest.raises(ProjectContextProjectionBlocked, match="BLOCKED_OPERATIONAL_PROJECT_NOT_FOUND"):
        ensure_project_context_projection(
            project_id=PROJECT_ID,
            operational_database=OPERATIONAL_DATABASE,
            corpus_database=CORPUS_DATABASE,
            operational_revision="20260711_0009",
            connect=_factory(connections),
        )


def test_incomplete_operational_context_fails_closed_without_corpus_write() -> None:
    connections = {
        OPERATIONAL_DATABASE: FakeConnection(OPERATIONAL_DATABASE, source=_source_row(name=""), local=None),
        CORPUS_DATABASE: FakeConnection(CORPUS_DATABASE, source=None, local=None),
    }
    with pytest.raises(ProjectContextProjectionBlocked, match="BLOCKED_PROJECT_CONTEXT_INCOMPLETE"):
        ensure_project_context_projection(
            project_id=PROJECT_ID,
            operational_database=OPERATIONAL_DATABASE,
            corpus_database=CORPUS_DATABASE,
            operational_revision="20260711_0009",
            connect=_factory(connections),
        )
    assert connections[CORPUS_DATABASE].writes == []
    with pytest.raises(ProjectContextProjectionBlocked, match="BLOCKED_DATABASE_IDENTITY_INVALID"):
        ensure_project_context_projection(
            project_id=PROJECT_ID,
            operational_database=OPERATIONAL_DATABASE,
            corpus_database=OPERATIONAL_DATABASE,
            operational_revision="20260711_0009",
            connect=_factory(connections),
        )

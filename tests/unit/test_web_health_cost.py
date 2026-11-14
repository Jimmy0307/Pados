# SPDX-License-Identifier: AGPL-3.0-only
"""/api/health answers in milliseconds of database work, not seconds, with the same payload.

One answer used to take 2-3.5 s on the validation hosts (finding U8): every call built an extra engine in
``check_database`` that was never disposed, and ``metrics`` re-read the table list and reflected each of its 14
tables just to count rows. The counts, statuses and payload are unchanged; only the work behind them is.
"""
from __future__ import annotations

import contextlib
from collections.abc import Iterator
from typing import Any

import pytest

from pados.web import service


class _Result:
    def __init__(self, value: int) -> None:
        self.value = value

    def scalar_one(self) -> int:
        return self.value


class _Session:
    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: Any) -> _Result:
        self.statements.append(str(statement.compile(compile_kwargs={"literal_binds": True})))
        return _Result(3)


def test_metrics_reads_the_table_list_once_and_reflects_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    reads: list[object] = []

    def tables(engine: object) -> set[str]:
        reads.append(engine)
        return {"projects", "memory_records", "task_goals", "task_loops", "audit_events", "control_sync_events"}

    def no_reflection(*_args: object) -> None:
        raise AssertionError("metrics must not reflect tables to count their rows")

    monkeypatch.setattr(service, "_public_tables", tables)
    monkeypatch.setattr(service, "_reflect_table", no_reflection)
    session = _Session()
    counts = service.metrics(session, object())  # type: ignore[arg-type]
    assert counts == {"projects": 3, "memories": 3, "tasks": 3, "runs": 3, "events": 6}
    assert len(reads) == 1
    assert sorted(session.statements) == sorted(
        f"SELECT count(*) AS count_1 \nFROM public.{name}"
        for name in ("projects", "memory_records", "task_goals", "task_loops", "audit_events", "control_sync_events"))


def test_metrics_refuses_a_table_name_that_is_not_an_identifier(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "_public_tables", lambda _engine: {"projects"})
    with pytest.raises(service.UnknownTableError):
        service._count_rows(_Session(), "projects; drop table x")  # type: ignore[arg-type]


class _Engine:
    def __init__(self) -> None:
        self.disposed = 0

    def dispose(self) -> None:
        self.disposed += 1


def test_one_domain_check_uses_one_engine_and_disposes_it(monkeypatch: pytest.MonkeyPatch) -> None:
    from pados.config import Settings

    created: list[_Engine] = []
    seen_by_check: list[object] = []

    def get_engine(_settings: object) -> _Engine:
        created.append(_Engine())
        return created[-1]

    def check_database(_settings: object, *, engine: object = None) -> dict[str, str]:
        seen_by_check.append(engine)
        return {"version": "PostgreSQL 16", "pgvector": "true"}

    @contextlib.contextmanager
    def session(_engine: object) -> Iterator[_Session]:
        yield _Session()

    monkeypatch.setattr(service, "get_engine", get_engine)
    monkeypatch.setattr(service, "check_database", check_database)
    monkeypatch.setattr(service, "_migration_status", lambda *_args: {"status": "OK"})
    monkeypatch.setattr(service, "Session", session)
    monkeypatch.setattr(service, "metrics", lambda _session, _engine: {"projects": 1})
    result = service._domain_health(Settings(), database_name="pados_x", expected_revision="r", include_metrics=True)
    assert result["readiness"] == "READY" and result["metrics"] == {"projects": 1}
    assert len(created) == 1 and seen_by_check == created and created[0].disposed == 1


def test_check_database_disposes_the_engine_it_creates_but_not_one_it_is_given(
        monkeypatch: pytest.MonkeyPatch) -> None:
    from pados.config import Settings
    from pados.db import engine as engine_module

    class _Connection:
        def execute(self, statement: object) -> _Result:
            return _Result(1)

    class _ConnectingEngine(_Engine):
        @contextlib.contextmanager
        def connect(self) -> Iterator[_Connection]:
            yield _Connection()

    created: list[_ConnectingEngine] = []

    def get_engine(_settings: object) -> _ConnectingEngine:
        created.append(_ConnectingEngine())
        return created[-1]

    monkeypatch.setattr(engine_module, "get_engine", get_engine)
    own = engine_module.check_database(Settings())
    assert own["status"] == "ok" and len(created) == 1 and created[0].disposed == 1
    given = _ConnectingEngine()
    engine_module.check_database(Settings(), engine=given)  # type: ignore[arg-type]
    assert len(created) == 1 and given.disposed == 0

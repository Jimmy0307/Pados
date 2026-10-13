from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Self

import pytest

from pados.config import OPERATIONAL_REVISION_DEFAULT, Settings
from pados.corpus import operator
from pados.corpus.operator_config import OperatorConfiguration, resolve_operator_configuration
from pados.web import service

OPERATIONAL_DATABASE = "pados_local_runtime"
CORPUS_DATABASE = "pados_corpus_runtime"
OPERATIONAL_REVISION = OPERATIONAL_REVISION_DEFAULT
CORPUS_REVISION = "20260730_0011"
EXCLUDED_REVISION = "20260730_0012"
PROJECT_ID = "325f8168-efec-4818-99ac-b00a03d6accd"


class _FakeConnection:
    def __init__(self, revision: str) -> None:
        self.revision = revision

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def execute(self, _statement: object) -> _FakeResult:
        return _FakeResult(self.revision)


class _FakeResult:
    def __init__(self, revision: str) -> None:
        self.revision = revision

    def scalars(self) -> _FakeResult:
        return self

    def __iter__(self) -> Iterator[str]:
        return iter((self.revision,))


class _FakeEngine:
    def __init__(self, revision: str) -> None:
        self.revision = revision

    def connect(self) -> _FakeConnection:
        return _FakeConnection(self.revision)

    def dispose(self) -> None:
        return None


class _FakeSession:
    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def test_corpus_bound_process_keeps_explicit_operational_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PADOS_DB_NAME", CORPUS_DATABASE)
    monkeypatch.setenv("PADOS_CORPUS_DATABASE_NAME", CORPUS_DATABASE)
    monkeypatch.delenv("PADOS_CORPUS_OPERATIONAL_DATABASE", raising=False)

    config = resolve_operator_configuration(Settings.model_validate({}))

    assert config.operational_database == OPERATIONAL_DATABASE
    assert config.corpus_database == CORPUS_DATABASE


def test_health_reports_independent_operational_and_corpus_domains(monkeypatch: pytest.MonkeyPatch) -> None:
    settings = Settings.model_validate(
        {
            "db_name": CORPUS_DATABASE,
            "operational_database_name": OPERATIONAL_DATABASE,
            "corpus_database_name": CORPUS_DATABASE,
            "operational_revision": OPERATIONAL_REVISION,
            "corpus_revision": CORPUS_REVISION,
            "excluded_revision": EXCLUDED_REVISION,
        }
    )
    seen: list[str] = []
    revisions = {
        OPERATIONAL_DATABASE: OPERATIONAL_REVISION,
        CORPUS_DATABASE: CORPUS_REVISION,
    }

    def fake_engine(active: Settings) -> _FakeEngine:
        seen.append(active.db_name)
        return _FakeEngine(revisions[active.db_name])

    monkeypatch.setattr(service, "get_engine", fake_engine)
    monkeypatch.setattr(
        service,
        "check_database",
        lambda active: {"version": "PostgreSQL", "pgvector": "true"},
    )
    monkeypatch.setattr(service, "Session", lambda _engine: _FakeSession())
    monkeypatch.setattr(
        service,
        "metrics",
        lambda _session, _engine: {"projects": 1, "memories": 0, "tasks": 0, "runs": 0, "events": 0},
    )

    payload = service.health(settings)

    assert payload["status"] == "OK"
    assert payload["operational"]["readiness"] == "READY"
    assert payload["operational"]["database"]["name"] == OPERATIONAL_DATABASE
    assert payload["operational"]["migration"]["current_revision"] == OPERATIONAL_REVISION
    assert payload["corpus"]["readiness"] == "READY"
    assert payload["corpus"]["database"]["name"] == CORPUS_DATABASE
    assert payload["corpus"]["migration"]["current_revision"] == CORPUS_REVISION
    assert payload["runtime_contract"]["excluded_revision_applied"] is False
    assert seen == [OPERATIONAL_DATABASE, CORPUS_DATABASE]


def test_project_overview_reads_identity_operationally_and_counts_from_corpus(monkeypatch: pytest.MonkeyPatch) -> None:
    config = OperatorConfiguration(
        repository_root=None,  # type: ignore[arg-type]
        operational_database=OPERATIONAL_DATABASE,
        corpus_database=CORPUS_DATABASE,
        database_host="127.0.0.1",
        database_port=5432,
        database_user="pados",
        database_password="test-only",
        artifact_root=None,  # type: ignore[arg-type]
        staging_root=None,  # type: ignore[arg-type]
        operator_name="test-operator",
    )
    seen: list[str] = []

    @contextmanager
    def fake_read_only_session(active: Settings) -> Iterator[_FakeSession]:
        seen.append(active.db_name)
        yield _FakeSession()

    monkeypatch.setattr(operator, "configuration", lambda: config)
    monkeypatch.setattr(operator, "_read_only_session", fake_read_only_session)
    monkeypatch.setattr(
        operator.control_service,  # type: ignore[attr-defined]
        "list_projects",
        lambda _session: [{"id": PROJECT_ID, "name": "Demo Project"}],
    )
    monkeypatch.setattr(
        operator,
        "_project_corpus_status",
        lambda _config, project_id: {
            "status": "READY",
            "database": CORPUS_DATABASE,
            "project_id": project_id,
            "counts": {"documents": 6, "evidence_spans": 247},
        },
    )

    payload = operator.list_projects(include_corpus_status=True)

    assert payload["projects"][0]["name"] == "Demo Project"
    assert payload["projects"][0]["corpus_status"]["database"] == CORPUS_DATABASE
    assert payload["projects"][0]["corpus_status"]["counts"] == {"documents": 6, "evidence_spans": 247}
    assert seen == [OPERATIONAL_DATABASE]

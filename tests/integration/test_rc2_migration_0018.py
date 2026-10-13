"""Migration 20261010_0018 (RC2 D1): 0017 -> 0018 with live data, subject immutability, downgrade, re-upgrade.

Uses the dedicated disposable database named by PADOS_FC_MIGRATION_TEST_DATABASE (loopback only),
the same database the 0017 migration test uses.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError

from pados.config import get_settings, lane_settings

DATABASE = os.environ.get("PADOS_FC_MIGRATION_TEST_DATABASE", "")
pytestmark = pytest.mark.skipif(not DATABASE, reason="set PADOS_FC_MIGRATION_TEST_DATABASE")
ROOT = Path(__file__).resolve().parents[2]
PREVIOUS, HEAD = "20261007_0017", "20261010_0018"
SUBJECT = {"kind": "WORK_ROUTE", "schema_version": "pados.approval-subject.v1"}


def _alembic(*args: str) -> str:
    env = {**os.environ, "PADOS_DB_NAME": DATABASE}
    done = subprocess.run([sys.executable, "-m", "alembic", *args], cwd=ROOT, env=env,
                          capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr[-2000:]
    return done.stdout


@pytest.fixture
def engine() -> Iterator[Engine]:
    settings = get_settings()
    if settings.db_host not in {"127.0.0.1", "localhost"} or not DATABASE.startswith("pados_fc_migration"):
        pytest.fail("migration test requires a dedicated loopback pados_fc_migration_* database")
    engine = create_engine(lane_settings(settings, DATABASE).database_url)
    yield engine
    engine.dispose()


def _main_version(engine: Engine) -> str:
    """The operational-chain revision (ignores the separate corpus head when present)."""
    with engine.connect() as connection:
        versions = connection.execute(text("SELECT version_num FROM alembic_version")).scalars().all()
    chain = [version for version in versions if version in {PREVIOUS, HEAD}]
    assert len(chain) == 1, versions
    return chain[0]


def _columns(engine: Engine) -> set[str]:
    with engine.connect() as connection:
        return set(connection.execute(text(
            "SELECT column_name FROM information_schema.columns WHERE table_name = 'control_approvals'")).scalars())


def _project(engine: Engine) -> str:
    project_id = str(uuid4())
    with engine.begin() as connection:
        connection.execute(text(
            "INSERT INTO projects(id, name, root_path, classification) VALUES "
            "(CAST(:id AS uuid), :name, :root, 'internal')"),
            {"id": project_id, "name": f"migration-0018-{project_id}", "root": f"/tmp/{project_id}"})
    return project_id


def _insert_bound(engine: Engine, project_id: str, subject: dict[str, object] | None = None,
                  kind: str | None = "WORK_ROUTE") -> str:
    with engine.begin() as connection:
        return str(connection.execute(text(
            "INSERT INTO control_approvals(project_id, proposal_hash, proposal_summary, idempotency_key, "
            "subject_kind, subject_json) VALUES (CAST(:p AS uuid), :h, 'bound', :k, :kind, CAST(:s AS jsonb)) "
            "RETURNING id::text"),
            {"p": project_id, "h": "b" * 64, "k": f"bound-{uuid4()}", "kind": kind,
             "s": None if subject is None else json.dumps(subject)}).scalar_one())


def test_0017_to_0018_with_data_trigger_downgrade_and_reupgrade(engine: Engine) -> None:
    _alembic("upgrade", PREVIOUS)
    if _main_version(engine) != PREVIOUS:
        _alembic("downgrade", PREVIOUS)  # dedicated disposable DB: start from the previous head
    assert _main_version(engine) == PREVIOUS
    assert not {"subject_kind", "subject_json"} & _columns(engine)
    project_id = _project(engine)
    with engine.begin() as connection:
        legacy = str(connection.execute(text(
            "INSERT INTO control_approvals(project_id, proposal_hash, proposal_summary, idempotency_key) "
            "VALUES (CAST(:p AS uuid), :h, 'pre-existing', :k) RETURNING id::text"),
            {"p": project_id, "h": "a" * 64, "k": f"pre-{project_id}"}).scalar_one())

    _alembic("upgrade", HEAD)
    assert _main_version(engine) == HEAD
    assert {"subject_kind", "subject_json"} <= _columns(engine)
    with engine.connect() as connection:
        row = connection.execute(text("SELECT subject_kind, subject_json, proposal_hash FROM control_approvals "
                                      "WHERE id::text = :id"), {"id": legacy}).one()
    assert tuple(row) == (None, None, "a" * 64)  # existing approvals stay subjectless (cannot route)

    bound = _insert_bound(engine, project_id, SUBJECT)
    other_project = _project(engine)
    for assignment, value in (("subject_json = CAST(:v AS jsonb)", json.dumps({**SUBJECT, "x": 1})),
                              ("proposal_hash = :v", "c" * 64),
                              ("subject_kind = :v", None),
                              ("project_id = CAST(:v AS uuid)", other_project)):
        with pytest.raises(DBAPIError), engine.begin() as connection:
            connection.execute(text(f"UPDATE control_approvals SET {assignment} WHERE id::text = :id"),
                               {"v": value, "id": bound})
    with pytest.raises(DBAPIError), engine.begin() as connection:  # a legacy approval cannot be bound later
        connection.execute(text("UPDATE control_approvals SET subject_kind = 'WORK_ROUTE', "
                                "subject_json = CAST(:s AS jsonb) WHERE id::text = :id"),
                           {"s": json.dumps(SUBJECT), "id": legacy})
    with engine.begin() as connection:  # the decision lifecycle is unaffected
        connection.execute(text("UPDATE control_approvals SET status = 'APPROVED', version = version + 1 "
                                "WHERE id::text = :id"), {"id": bound})
    with pytest.raises(DBAPIError):  # both-or-neither
        _insert_bound(engine, project_id, None)
    with pytest.raises(DBAPIError):  # only WORK_ROUTE subjects exist
        _insert_bound(engine, project_id, SUBJECT, kind="ANYTHING")

    _alembic("downgrade", PREVIOUS)
    assert _main_version(engine) == PREVIOUS
    assert not {"subject_kind", "subject_json"} & _columns(engine)
    with engine.connect() as connection:
        triggers = set(connection.execute(text(
            "SELECT tgname FROM pg_trigger WHERE tgrelid = 'control_approvals'::regclass")).scalars())
        kept = connection.execute(text("SELECT count(*) FROM control_approvals WHERE id::text IN (:a, :b)"),
                                  {"a": legacy, "b": bound}).scalar_one()
    assert "trg_control_approvals_subject_immutable" not in triggers
    assert kept == 2

    _alembic("upgrade", HEAD)
    assert _main_version(engine) == HEAD
    with engine.connect() as connection:
        triggers = set(connection.execute(text(
            "SELECT tgname FROM pg_trigger WHERE tgrelid = 'control_approvals'::regclass")).scalars())
    assert "trg_control_approvals_subject_immutable" in triggers


def test_offline_sql_generation_for_0018() -> None:
    sql = _alembic("upgrade", f"{PREVIOUS}:{HEAD}", "--sql")
    assert "ADD COLUMN subject_kind" in sql
    assert "ck_control_approvals_subject_pair" in sql
    assert "trg_control_approvals_subject_immutable" in sql

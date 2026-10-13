import os
import re
from pathlib import Path

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from pados.cli.main import _database_failure_details
from pados.core.task_service import create_project
from pados.db import session_scope
from pados.memory.service import bootstrap_project


@pytest.fixture(autouse=True)
def require_explicit_disposable_database() -> None:
    database_name = os.environ.get("PADOS_DB_NAME", "")
    disposable_intent = os.environ.get("PADOS_PROJECT_CREATE_DISPOSABLE_DB")
    if disposable_intent != "1" or re.fullmatch(r"pados_project_create_diag_\d{8}_\d{3}", database_name) is None:
        pytest.fail(
            "project-create integration tests require "
            "PADOS_PROJECT_CREATE_DISPOSABLE_DB=1 and a named disposable database"
        )


def test_project_create_applies_defaults_and_persists_related_rows_once(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()

    with session_scope() as session:
        savepoint = session.begin_nested()
        project_id = bootstrap_project(
            session,
            name="project-create-defaults-contract",
            root_path=root,
            classification="internal",
            default_branch="main",
        )
        project = session.execute(
            text(
                """
                SELECT name, root_path, default_branch, status, configuration::text, classification
                FROM projects
                WHERE id = CAST(:project_id AS uuid)
                """
            ),
            {"project_id": project_id},
        ).one()
        assert tuple(project) == (
            "project-create-defaults-contract",
            str(root.resolve()),
            "main",
            "active",
            "{}",
            "internal",
        )
        assert session.execute(
            text("SELECT count(*) FROM context_project_registry WHERE project_id = CAST(:id AS uuid)"),
            {"id": project_id},
        ).scalar_one() == 1
        assert session.execute(
            text(
                "SELECT count(*) FROM context_sources "
                "WHERE project_id = CAST(:id AS uuid) AND source_uri = :source_uri"
            ),
            {"id": project_id, "source_uri": f"project:{project_id}"},
        ).scalar_one() == 1
        assert session.execute(
            text(
                "SELECT count(*) FROM audit_events "
                "WHERE entity_type = 'project' AND entity_id = CAST(:id AS uuid) "
                "AND event_type = 'created'"
            ),
            {"id": project_id},
        ).scalar_one() == 1
        savepoint.rollback()


def test_project_create_omitted_status_and_configuration_use_contract_defaults(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()

    with session_scope() as session:
        savepoint = session.begin_nested()
        project_id = bootstrap_project(session, name="project-create-omitted-defaults", root_path=root)
        row = session.execute(
            text(
                "SELECT default_branch, status, configuration::text, classification "
                "FROM projects WHERE id = CAST(:id AS uuid)"
            ),
            {"id": project_id},
        ).one()
        assert tuple(row) == ("main", "active", "{}", "internal")
        savepoint.rollback()


def test_project_create_rejects_missing_root_before_persistence(tmp_path: Path) -> None:
    with session_scope() as session:
        savepoint = session.begin_nested()
        with pytest.raises(ValueError, match="root_path must exist and be a directory"):
            bootstrap_project(session, name="project-create-missing-root", root_path=tmp_path / "missing")
        assert session.execute(
            text("SELECT count(*) FROM projects WHERE name = 'project-create-missing-root'")
        ).scalar_one() == 0
        savepoint.rollback()


def test_repository_not_null_failure_is_distinguishable_from_duplicate_rejection(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()

    with session_scope() as session:
        savepoint = session.begin_nested()
        bootstrap_project(session, name="project-create-duplicate", root_path=root)
        with pytest.raises(ValueError, match="duplicate project identity rejected"):
            bootstrap_project(session, name="project-create-duplicate", root_path=root)
        savepoint.rollback()

    with session_scope() as session:
        savepoint = session.begin_nested()
        with pytest.raises(IntegrityError) as caught:
            create_project(
                session,
                name=None,  # type: ignore[arg-type]
                root_path=str(root),
            )
        original = caught.value.orig
        assert getattr(original, "sqlstate", None) == "23502"
        assert getattr(getattr(original, "diag", None), "column_name", None) == "name"
        assert _database_failure_details(caught.value)["constraint_name"] == "projects_name_not_null"
        savepoint.rollback()

    with session_scope() as session:
        assert session.execute(
            text("SELECT count(*) FROM projects WHERE name IS NULL")
        ).scalar_one() == 0

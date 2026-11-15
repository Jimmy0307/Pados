# SPDX-License-Identifier: AGPL-3.0-only
# ruff: noqa: F811 - the D1 fixture is imported by name and requested as a test parameter
"""Control Center project creation and Markdown documents against PostgreSQL (the CLI's rows, audit and replay).

Uses the D1 fixture (PADOS_RC2_APPROVAL_BINDING_TEST_DATABASE_URL, a disposable loopback ``*_test`` database migrated
to the operational head); every test runs in one transaction that is rolled back. Files go to a temporary workspace.
"""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from pados.control import product_projects, product_routes, work_projection
from pados.control.product_errors import ProductControlError
from pados.control.product_projects import ProjectCreate
from tests.integration.test_rc2_approval_binding_postgres import session  # noqa: F401


def _settings(workspace: Path) -> Any:
    return SimpleNamespace(product_workspace_root=str(workspace), remote_config="")


def _events(session: Session, project_id: str, kind: str) -> list[dict[str, Any]]:
    rows = session.execute(text("SELECT payload_json FROM control_sync_events WHERE project_id = CAST(:p AS uuid) "
                                "AND event_type = :k ORDER BY sequence_number"), {"p": project_id, "k": kind})
    return [dict(row) for row in rows.scalars().all()]


def test_create_project_writes_the_cli_rows_folder_readme_and_audit_and_replays(session: Session,
                                                                                tmp_path: Path) -> None:
    settings = _settings(tmp_path / "workspace")
    name = f"文件檢查 {uuid4().hex[:8]}"
    payload = ProjectCreate(name=name, description="Synthetic demo project.", idempotency_key=f"k-{uuid4()}")
    created = product_projects.create_project(session, settings, payload)
    assert created["replayed"] is False
    project_id = created["project_id"]
    row = session.execute(text("SELECT name, root_path, classification, configuration FROM projects "
                               "WHERE id = CAST(:id AS uuid)"), {"id": project_id}).mappings().one()
    folder = Path(row["root_path"])
    assert row["name"] == name and row["classification"] == "internal"
    assert folder.parent == (tmp_path / "workspace").resolve()
    assert folder.name == product_projects.project_folder_name(name)
    assert row["configuration"]["description"] == "Synthetic demo project."
    assert (folder / "README.md").read_text(encoding="utf-8").startswith(f"# {name}")
    registry = session.execute(text("SELECT canonical_root FROM context_project_registry "
                                    "WHERE project_id = CAST(:id AS uuid)"), {"id": project_id}).scalar_one()
    assert Path(registry) == folder
    audit = session.execute(text("SELECT actor FROM audit_events WHERE entity_type = 'project' "
                                 "AND entity_id = :id"), {"id": project_id}).scalars().all()
    assert audit == [product_projects.ACTOR]
    (event,) = _events(session, project_id, "PROJECT_CREATED")
    assert event["idempotency_key"] == payload.idempotency_key and event["folder"] == folder.name

    again = product_projects.create_project(session, settings, payload)
    assert again == {"project_id": project_id, "replayed": True}
    assert len(_events(session, project_id, "PROJECT_CREATED")) == 1
    with pytest.raises(ProductControlError) as conflict:
        product_projects.create_project(session, settings, payload.model_copy(update={"description": "other"}))
    assert conflict.value.code == "PROJECT_IDEMPOTENCY_CONTENT_CONFLICT"
    with pytest.raises(ProductControlError) as taken:
        product_projects.create_project(session, settings, ProjectCreate(name=name.upper(),
                                                                         idempotency_key=f"k-{uuid4()}"))
    assert taken.value.code == "PROJECT_NAME_TAKEN"
    view = product_projects.project_view(session, project_id)
    assert view["folder"] == folder.name and view["description"] == "Synthetic demo project."


def test_documents_are_written_audited_replayed_and_projected(session: Session, tmp_path: Path) -> None:
    settings = _settings(tmp_path / "workspace")
    created = product_projects.create_project(session, settings, ProjectCreate(
        name=f"docs-{uuid4().hex[:8]}", idempotency_key=f"k-{uuid4()}"))
    project_id = created["project_id"]
    body = product_routes.DocumentWrite(path="guide.md", content="[ok](README.md) [broken](missing.md)\n",
                                        idempotency_key="doc-1")
    first = product_routes.write_document_entry(session, settings, project_id, body)
    assert first["replayed"] is False and first["replaced"] is False
    replay = product_routes.write_document_entry(session, settings, project_id, body)
    assert replay["replayed"] is True and replay["sha256"] == first["sha256"]
    with pytest.raises(ProductControlError) as conflict:
        product_routes.write_document_entry(session, settings, project_id,
                                            body.model_copy(update={"content": "changed\n"}))
    assert conflict.value.code == "DOCUMENT_IDEMPOTENCY_CONTENT_CONFLICT"
    fixed = product_routes.write_document_entry(session, settings, project_id, product_routes.DocumentWrite(
        path="guide.md", content="[ok](README.md)\n", replace=True, expected_sha256=first["sha256"],
        idempotency_key="doc-2"))
    assert fixed["previous_sha256"] == first["sha256"]
    written = _events(session, project_id, "PROJECT_DOCUMENT_WRITTEN")
    assert [e["idempotency_key"] for e in written] == ["doc-1", "doc-2"]

    work = work_projection.project_work(session, project_id)
    assert work["authority"] == "PROJECTION_ONLY"
    assert [e["event_type"] for e in work["events"]][:3] == ["PROJECT_DOCUMENT_WRITTEN", "PROJECT_DOCUMENT_WRITTEN",
                                                             "PROJECT_CREATED"]
    assert all("request_fingerprint" not in (e["payload_json"] or {}) for e in work["events"])
    assert work["loops"] == [] and work["candidates"] == []


def test_documents_of_a_cli_project_outside_the_workspace_are_read_only(session: Session, tmp_path: Path) -> None:
    from pados.memory.service import bootstrap_project

    outside = tmp_path / "user-repo"
    outside.mkdir()
    project_id = bootstrap_project(session, name=f"cli-{uuid4()}", root_path=outside)
    with pytest.raises(ProductControlError) as excinfo:
        product_routes.write_document_entry(session, _settings(tmp_path / "workspace"), project_id,
                                            product_routes.DocumentWrite(path="x.md", content="x",
                                                                         idempotency_key="d"))
    assert excinfo.value.code == "PROJECT_ROOT_OUTSIDE_WORKSPACE"
    assert not (outside / "x.md").exists()
    assert _events(session, project_id, "PROJECT_DOCUMENT_WRITTEN") == []

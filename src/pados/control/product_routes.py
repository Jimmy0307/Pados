# SPDX-License-Identifier: AGPL-3.0-only
"""Control routes for the Control Center's product work loop (projects, Markdown documents, read projections).

Every route here sits behind the single ``control_auth`` dependency of ``pados.api.app`` (the control token header, or a
paired browser session with CSRF and Origin), so it grants exactly the control plane's authority and nothing more.
None of them schedules, routes, approves or accepts: work still goes through the existing work-request, approval,
task-goal, task-plan, TaskLoop and ResultCandidate routes, and result acceptance only through the Remote WebAuthn
step-up.

SESSION SCOPES: a session scope allowlist is being added on another branch (``src/pados/identity/scopes.py``). When the
branches are integrated, each route below must be added there as allowed for sessions; the comment at each route says
so again.
"""
# No ``from __future__ import annotations``: FastAPI must see the route annotations built from the local
# ``control_auth`` dependency at definition time.
import hashlib
from collections.abc import Callable
from contextlib import AbstractContextManager
from typing import Annotated, Any

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import text
from sqlalchemy.exc import DataError, NoResultFound
from sqlalchemy.orm import Session

from pados.config import artifact_root
from pados.control import project_documents as documents
from pados.control import product_projects, resource_summary, work_projection
from pados.control import service as control_service
from pados.control.product_errors import ProductControlError, require_uuid
from pados.control.project_setup import ensure_local_proof_scope
from pados.logging_config import get_logger

DOCUMENT_EVENT = "PROJECT_DOCUMENT_WRITTEN"
_LOG = get_logger("pados.control.product")

SessionFactory = Callable[[Any], AbstractContextManager[Session]]


class DocumentWrite(BaseModel):
    """Lengths are checked by the service (bytes, not characters) so a refusal carries a closed code."""

    path: str = Field(max_length=400)
    content: str = Field(max_length=300_000)
    replace: bool = False
    expected_sha256: str | None = Field(default=None, max_length=80)
    idempotency_key: str = Field(min_length=1, max_length=120)


def _lock_key(namespace: str, value: str) -> int:
    return int.from_bytes(hashlib.sha256(f"{namespace}:{value}".encode()).digest()[:8], "big", signed=True)


def project_root_path(session: Session, project_id: str) -> str:
    """The registered root of a project (``NoResultFound`` for an unknown project)."""
    return str(session.execute(text("SELECT root_path FROM projects WHERE id = CAST(:id AS uuid)"),
                               {"id": project_id}).scalar_one())


def prior_document_write(session: Session, project_id: str, idempotency_key: str) -> dict[str, Any] | None:
    row = session.execute(
        text(
            """
            SELECT payload_json FROM control_sync_events
            WHERE project_id = CAST(:project_id AS uuid) AND event_type = :event_type
              AND payload_json->>'idempotency_key' = :key
            ORDER BY sequence_number ASC LIMIT 1
            """
        ),
        {"project_id": project_id, "event_type": DOCUMENT_EVENT, "key": idempotency_key},
    ).scalar_one_or_none()
    return dict(row) if isinstance(row, dict) else None


def write_document_entry(session: Session, settings: Any, project_id: str, payload: DocumentWrite) -> dict[str, Any]:
    """Validate, serialize per project, replay by key, write atomically, then audit the write as a sync event."""
    relative = documents.validate_document_path(payload.path)
    data = documents.encode_content(payload.content)
    expected = documents.expected_sha(payload.expected_sha256)
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _lock_key("pados.document", project_id)})
    root_path = project_root_path(session, project_id)
    prior = prior_document_write(session, project_id, payload.idempotency_key)
    if prior is not None:
        same = (prior.get("path") == relative.as_posix() and prior.get("sha256") == documents.sha256_hex(data)
                and bool(prior.get("replace")) == payload.replace)
        if not same:
            raise ProductControlError("DOCUMENT_IDEMPOTENCY_CONTENT_CONFLICT", 409)
        return {**{k: prior.get(k) for k in ("path", "size", "sha256", "replaced", "previous_sha256")},
                "replayed": True}
    root = documents.resolve_project_root(root_path, str(getattr(settings, "product_workspace_root", "") or ""))
    receipt = documents.write_document(root, relative, data, replace=payload.replace, expected_sha256=expected)
    # The file is written before its audit row: a failed commit after a successful write leaves the file without the
    # event (logged below by the caller's error path); a failed write never records an event.
    control_service.append_sync_event(session, project_id, DOCUMENT_EVENT, {
        **receipt, "replace": payload.replace, "idempotency_key": payload.idempotency_key})
    _LOG.info("control.document.written", outcome="written", replaced=receipt["replaced"])
    return {**receipt, "replayed": False}


def register_product_control_routes(app: FastAPI, settings: Any, *, control_auth: Any,
                                    session_scope: SessionFactory) -> None:
    """Mount the product routes behind ``control_auth``: the ``Depends(...)`` every /control route uses."""
    ControlAuth = Annotated[None, control_auth]

    def run(handler: Callable[[], Any]) -> Any:
        try:
            return handler()
        except ProductControlError as exc:
            raise HTTPException(status_code=exc.status, detail=exc.code) from exc
        except NoResultFound as exc:
            raise HTTPException(status_code=404, detail="NOT_FOUND") from exc
        except DataError as exc:
            raise HTTPException(status_code=422, detail="INVALID_IDENTIFIER") from exc

    # SESSION SCOPES: add to src/pados/identity/scopes.py (allowed for sessions) when branches are integrated.
    @app.post("/control/projects")
    def control_create_project(payload: product_projects.ProjectCreate, _: ControlAuth) -> object:
        """Create a project in a folder derived from its name inside the approved workspace root (CLI setup path)."""
        def _create() -> dict[str, Any]:
            with session_scope(settings) as session:
                created = product_projects.create_project(session, settings, payload)
            try:
                scope_version = ensure_local_proof_scope(settings, created["project_id"])
            except Exception as exc:  # noqa: BLE001 - any proof-lane failure is one closed, retryable refusal
                _LOG.warning("control.project.proof_scope_failed", error=type(exc).__name__)
                raise ProductControlError("PROJECT_PROOF_SCOPE_UNAVAILABLE", 503) from exc
            with session_scope(settings) as session:
                project = product_projects.project_view(session, created["project_id"])
            return {"status": "ok", "project": project, "replayed": created["replayed"],
                    "scope_version": scope_version,
                    "proof_scope": "READY" if scope_version is not None else "NOT_CONFIGURED"}

        return run(_create)

    # SESSION SCOPES: add to src/pados/identity/scopes.py (allowed for sessions) when branches are integrated.
    @app.get("/control/projects/{project_id}/documents")
    def control_list_documents(project_id: str, _: ControlAuth) -> object:
        def _list() -> dict[str, Any]:
            checked = require_uuid(project_id)
            with session_scope(settings) as session:
                root_path = project_root_path(session, checked)
            root = documents.resolve_project_root(root_path, str(settings.product_workspace_root or ""))
            return {"status": "ok", "project_id": checked, **documents.list_documents(root)}

        return run(_list)

    # SESSION SCOPES: add to src/pados/identity/scopes.py (allowed for sessions) when branches are integrated.
    @app.get("/control/projects/{project_id}/documents/content")
    def control_read_document(project_id: str, _: ControlAuth,
                              path: Annotated[str, Query(max_length=400)] = "") -> object:
        def _read() -> dict[str, Any]:
            checked = require_uuid(project_id)
            relative = documents.validate_document_path(path)
            with session_scope(settings) as session:
                root_path = project_root_path(session, checked)
            root = documents.resolve_project_root(root_path, str(settings.product_workspace_root or ""))
            return {"status": "ok", "project_id": checked, "document": documents.read_document(root, relative)}

        return run(_read)

    # SESSION SCOPES: add to src/pados/identity/scopes.py (allowed for sessions) when branches are integrated.
    @app.post("/control/projects/{project_id}/documents")
    def control_write_document(project_id: str, payload: DocumentWrite, _: ControlAuth) -> object:
        def _write() -> dict[str, Any]:
            checked = require_uuid(project_id)
            with session_scope(settings) as session:
                receipt = write_document_entry(session, settings, checked, payload)
            return {"status": "ok", "project_id": checked, "document": receipt}

        return run(_write)

    # PROJECTION ONLY. SESSION SCOPES: add to src/pados/identity/scopes.py (allowed for sessions) when integrated.
    @app.get("/control/projects/{project_id}/work")
    def control_project_work(project_id: str, _: ControlAuth) -> object:
        def _work() -> dict[str, Any]:
            checked = require_uuid(project_id)
            with session_scope(settings) as session:
                project = product_projects.project_view(session, checked)
                work = work_projection.project_work(session, checked)
            return {"status": "ok", "project": project, **work,
                    "step_up": work_projection.step_up_readiness(settings, checked)}

        return run(_work)

    # PROJECTION ONLY (工作心智圖 / Work Map): the project's work projection plus its iterations, jobs and artifact
    # receipts, read in one transaction. Never a second store and never an action: creating work from a node goes
    # through POST /control/work-requests and the approval routes, exactly as the work-request page does.
    # SESSION SCOPES: add to src/pados/identity/scopes.py (allowed for sessions) when integrated.
    @app.get("/control/projects/{project_id}/work-map")
    def control_project_work_map(project_id: str, _: ControlAuth) -> object:
        def _map() -> dict[str, Any]:
            checked = require_uuid(project_id)
            with session_scope(settings) as session:
                project = product_projects.project_view(session, checked)
                work = work_projection.project_work(session, checked)
                execution = work_projection.project_execution(session, checked)
            return {"status": "ok", "project": project, **work, "projection": "pados.control.work-map.v1",
                    "execution": execution, "step_up": work_projection.step_up_readiness(settings, checked)}

        return run(_map)

    # PROJECTION ONLY (資源與儲存 / Resources): measurements with source, unit, time and measured/UNKNOWN, the storage
    # budget and a cleanup plan PREVIEW (never executed; evidence, decisions, audit and unique copies are never
    # candidates). SESSION SCOPES: add to src/pados/identity/scopes.py (allowed for sessions) when integrated.
    @app.get("/control/resources")
    def control_resources(_: ControlAuth) -> object:
        return run(lambda: {"status": "ok", **resource_summary.resources_summary(settings, session_scope)})

    # PROJECTION ONLY. SESSION SCOPES: add to src/pados/identity/scopes.py (allowed for sessions) when integrated.
    @app.get("/control/jobs/{job_id}/artifacts/{artifact_type}/content")
    def control_job_artifact_content(job_id: str, artifact_type: str, _: ControlAuth) -> object:
        def _content() -> dict[str, Any]:
            checked = require_uuid(job_id)
            with session_scope(settings) as session:
                artifact = work_projection.artifact_content(session, artifact_root(settings), checked, artifact_type)
            return {"status": "ok", "artifact": artifact}

        return run(_content)

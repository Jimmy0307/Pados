# SPDX-License-Identifier: AGPL-3.0-only
"""Control routes for the Control Center's product work loop (projects, Markdown documents, read projections).

Every route here sits behind the single ``control_auth`` dependency of ``pados.api.app`` (the control token header, or a
paired browser session with CSRF and Origin), so it grants exactly the control plane's authority and nothing more.
None of them schedules, routes, approves or accepts: work still goes through the existing work-request, approval,
task-goal, task-plan, TaskLoop and ResultCandidate routes, and result acceptance only through the Remote WebAuthn
step-up.

DOCUMENT WRITES are a recoverable staged commit (intent journal, staged file, audit commit, replace) reconciled at
server start and on every document route call; see ``pados.control.project_documents`` for the crash-point table.

SESSION SCOPES: a session scope allowlist is being added on another branch (``src/pados/identity/scopes.py``). When the
branches are integrated, each route below must be added there as allowed for sessions; the comment at each route says
so again.
"""
# No ``from __future__ import annotations``: FastAPI must see the route annotations built from the local
# ``control_auth`` dependency at definition time.
import hashlib
import threading
from collections import Counter
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractContextManager, asynccontextmanager
from dataclasses import dataclass
from pathlib import PurePosixPath
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
DOCUMENT_FAILED_EVENT = "PROJECT_DOCUMENT_WRITE_FAILED"
DOCUMENT_UNAUDITED = "DOCUMENT_UNAUDITED_CHANGE"
EVENTS_MAX = 10_000
STARTUP_MAX_PROJECTS = 1_000
_LOG = get_logger("pados.control.product")
_PROJECT_LOCKS: dict[str, threading.RLock] = {}
_PROJECT_LOCKS_GUARD = threading.Lock()

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


def project_lock(project_id: str) -> threading.RLock:
    """The in-process lock of one project's documents, taken before the advisory lock.

    A write holds it across stage, commit and finalize, and every reconciliation holds it, so a reconciliation never
    races a write between its commit and its replace (the advisory lock alone ends at the commit).
    """
    with _PROJECT_LOCKS_GUARD:
        return _PROJECT_LOCKS.setdefault(project_id, threading.RLock())


def lock_project_documents(session: Session, project_id: str) -> None:
    session.execute(text("SELECT pg_advisory_xact_lock(:key)"), {"key": _lock_key("pados.document", project_id)})


def project_root_path(session: Session, project_id: str) -> str:
    """The registered root of a project (``NoResultFound`` for an unknown project)."""
    return str(session.execute(text("SELECT root_path FROM projects WHERE id = CAST(:id AS uuid)"),
                               {"id": project_id}).scalar_one())


def _workspace(settings: Any) -> str:
    return str(getattr(settings, "product_workspace_root", "") or "")


def _project_root(session: Session, settings: Any, project_id: str) -> documents.ProjectRoot:
    return documents.resolve_project_root(project_root_path(session, project_id), _workspace(settings))


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


def document_events(session: Session, project_id: str) -> list[tuple[str, dict[str, Any]]]:
    """The project's document audit trail (written and failed): the newest ``EVENTS_MAX``, returned oldest first."""
    rows = session.execute(
        text(
            """
            SELECT event_type, payload_json FROM control_sync_events
            WHERE project_id = CAST(:project_id AS uuid) AND event_type IN (:written, :failed)
            ORDER BY sequence_number DESC LIMIT :limit
            """
        ),
        {"project_id": project_id, "written": DOCUMENT_EVENT, "failed": DOCUMENT_FAILED_EVENT,
         "limit": EVENTS_MAX},
    ).all()
    return [(str(kind), dict(payload)) for kind, payload in reversed(rows) if isinstance(payload, dict)]


# --- reconciliation ---------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class Recovery:
    """What one reconciliation did; ``settle`` holds failed intents to remove once their FAILED event is committed."""

    outcomes: tuple[str, ...] = ()
    settle: tuple[documents.JournalEntry, ...] = ()

    def finish(self, root: documents.ProjectRoot) -> None:
        for entry in self.settle:
            try:
                documents.discard_entry(root.path, entry)
            except OSError as exc:  # the FAILED event is committed: the next reconciliation only closes it
                _LOG.warning("control.document.reconcile_pending", code="DOCUMENT_RECONCILE_PENDING",
                             error=type(exc).__name__)


def _settle_entry(session: Session, project_id: str, root: documents.ProjectRoot, entry: documents.JournalEntry,
                  audited: set[str], failed: set[str]) -> str:
    """Classify one open intent by the database alone (the caller holds both project locks)."""
    try:
        if entry.intent_id in failed:  # the failure was committed, the intent not yet removed
            documents.discard_entry(root.path, entry)
            return "CLOSED"
        if entry.intent_id not in audited:  # (a) no audit event: the write never happened
            documents.discard_entry(root.path, entry)
            return "ROLLED_BACK"
        outcome = documents.apply_entry(root.path, entry)  # (b) roll forward or (c) close
    except OSError as exc:  # a file held open (an editor, a scanner): retried at the next reconciliation
        _LOG.warning("control.document.reconcile_pending", code="DOCUMENT_RECONCILE_PENDING", error=type(exc).__name__)
        return "PENDING"
    if outcome == documents.APPLIED:
        return "ROLLED_FORWARD"
    if outcome == documents.ALREADY_APPLIED:
        return "CLOSED"
    control_service.append_sync_event(session, project_id, DOCUMENT_FAILED_EVENT, {
        "intent_id": entry.intent_id, "idempotency_key": entry.idempotency_key, "path": entry.path,
        "sha256": entry.sha256, "previous_sha256": entry.previous_sha256, "code": outcome})
    _LOG.warning("control.document.write_failed", code=outcome)
    return "FAILED"


def reconcile_journal(session: Session, project_id: str, root: documents.ProjectRoot) -> Recovery:
    """Settle every open write intent of a project; the caller holds ``project_lock`` and the advisory lock.

    (a) No ``PROJECT_DOCUMENT_WRITTEN`` for the intent: roll back (staged file, then intent).
    (b) Audited, target not replaced: roll forward when the staged digest is the audited one and the target still
        holds the previous content; otherwise record ``PROJECT_DOCUMENT_WRITE_FAILED`` with a closed code
        (``DOCUMENT_STAGED_MISSING``, ``DOCUMENT_STAGED_CORRUPT``, ``DOCUMENT_TARGET_CHANGED``) and remove the intent
        after that event is committed (``Recovery.finish``).
    (c) Audited and already replaced: close the intent.
    The FAILED event is recorded once: an intent whose failure is already committed is only closed.
    """
    documents.sweep_interrupted_intents(root)
    entries, unreadable = documents.open_entries(root)
    if unreadable:
        _LOG.warning("control.document.journal_unreadable", code="DOCUMENT_JOURNAL_UNREADABLE", count=unreadable)
    if not entries:
        return Recovery(outcomes=("PENDING",) * unreadable)
    events = document_events(session, project_id)
    audited = {str(p["intent_id"]) for kind, p in events if kind == DOCUMENT_EVENT and p.get("intent_id")}
    failed = {str(p["intent_id"]) for kind, p in events if kind == DOCUMENT_FAILED_EVENT and p.get("intent_id")}
    outcomes = [_settle_entry(session, project_id, root, entry, audited, failed) for entry in entries]
    for outcome in sorted(set(outcomes)):
        _LOG.info("control.document.reconciled", outcome=outcome, count=outcomes.count(outcome))
    settle = tuple(entry for entry, outcome in zip(entries, outcomes, strict=True) if outcome == "FAILED")
    return Recovery(outcomes=(*outcomes, *("PENDING",) * unreadable), settle=settle)


def reconcile_project(session_scope: SessionFactory, settings: Any, project_id: str
                      ) -> tuple[documents.ProjectRoot, list[tuple[str, dict[str, Any]]], Recovery]:
    """Reconcile one project in its own transaction, then remove the intents whose failure was committed."""
    with project_lock(project_id):
        with session_scope(settings) as session:
            lock_project_documents(session, project_id)
            root = _project_root(session, settings, project_id)
            recovery = reconcile_journal(session, project_id, root)
            events = document_events(session, project_id)
        recovery.finish(root)
    return root, events, recovery


def reconciliation_status(root: documents.ProjectRoot, events: list[tuple[str, dict[str, Any]]],
                          listed: list[dict[str, Any]]) -> dict[str, Any]:
    """Read-only: open intents, documents whose last write failed, and audited documents changed outside PADOS.

    The audited digest of a path is the newest written event's SHA-256, or the previous SHA-256 when that write's
    failure was recorded. A file that differs from it with no open intent is ``DOCUMENT_UNAUDITED_CHANGE``: reported,
    never re-audited silently.
    """
    expected: dict[str, Any] = {}
    failures: dict[str, str] = {}
    for kind, payload in events:
        path = str(payload.get("path") or "")
        if not path:
            continue
        if kind == DOCUMENT_EVENT:
            expected[path] = payload.get("sha256")
            failures.pop(path, None)
        else:
            expected[path] = payload.get("previous_sha256")
            failures[path] = str(payload.get("code") or "DOCUMENT_WRITE_FAILED")
    entries, unreadable = documents.open_entries(root)
    open_paths = {entry.path for entry in entries}
    on_disk = {str(item["path"]): item.get("sha256") for item in listed}
    unaudited = [{"path": path, "code": DOCUMENT_UNAUDITED} for path, digest in sorted(expected.items())
                 if path not in open_paths
                 and (on_disk.get(path) or documents.document_digest(root.path, path)) != digest]
    failed = [{"path": path, "code": code} for path, code in sorted(failures.items())]
    pending = len(entries) + unreadable
    state = "PENDING" if pending else "ATTENTION" if failed or unaudited else "CLEAN"
    return {"state": state, "pending": pending, "failed": failed[:documents.LIST_MAX_ENTRIES],
            "unaudited": unaudited[:documents.LIST_MAX_ENTRIES]}


def _reconcile_after_failure(session_scope: SessionFactory, settings: Any, project_id: str) -> None:
    try:
        reconcile_project(session_scope, settings, project_id)
    except Exception as exc:  # noqa: BLE001 - retried at the next document route call and at server start
        _LOG.warning("control.document.reconcile_deferred", code="DOCUMENT_RECONCILE_DEFERRED",
                     error=type(exc).__name__)


def reconcile_workspace_documents(settings: Any, session_scope: SessionFactory) -> dict[str, int]:
    """Server start: reconcile every project whose root lies inside the workspace; logs closed counts only."""
    workspace = _workspace(settings)
    totals: Counter[str] = Counter()
    if not workspace:
        return dict(totals)
    with session_scope(settings) as session:
        rows = session.execute(text("SELECT CAST(id AS text), root_path FROM projects ORDER BY id LIMIT :limit"),
                               {"limit": STARTUP_MAX_PROJECTS}).all()
    for project_id, root_path in rows:
        try:
            writable = documents.resolve_project_root(str(root_path), workspace).writable
        except ProductControlError:
            continue
        if not writable:
            continue
        try:
            root, events, recovery = reconcile_project(session_scope, settings, str(project_id))
        except Exception as exc:  # noqa: BLE001 - one project's failure never stops the others or the server
            _LOG.warning("control.document.reconcile_deferred", code="DOCUMENT_RECONCILE_DEFERRED",
                         error=type(exc).__name__)
            totals["errors"] += 1
            continue
        status = reconciliation_status(root, events, [])
        totals.update({"projects": 1, "unaudited": len(status["unaudited"]),
                       "failed_documents": len(status["failed"])})
        totals.update(outcome.lower() for outcome in recovery.outcomes)
    _LOG.info("control.document.startup_reconciled", **dict(totals))
    return dict(totals)


def _start_reconciliation(settings: Any, session_scope: SessionFactory) -> threading.Thread:
    """Reconcile at server start in the background: a slow or absent database never delays startup, and every
    document route reconciles its project first anyway."""
    def run() -> None:
        try:
            reconcile_workspace_documents(settings, session_scope)
        except Exception as exc:  # noqa: BLE001 - the routes reconcile on their next call
            _LOG.warning("control.document.reconcile_deferred", code="DOCUMENT_RECONCILE_DEFERRED",
                         error=type(exc).__name__)

    thread = threading.Thread(target=run, name="pados-document-reconcile", daemon=True)
    thread.start()
    return thread


# --- the write --------------------------------------------------------------------------------------------------------
@dataclass(frozen=True)
class _Staged:
    root: documents.ProjectRoot
    entry: documents.JournalEntry | None
    replay: dict[str, Any] | None


def _replay(session: Session, project_id: str, root: documents.ProjectRoot, prior: dict[str, Any],
            relative: PurePosixPath, data: bytes, payload: DocumentWrite) -> dict[str, Any]:
    same = (prior.get("path") == relative.as_posix() and prior.get("sha256") == documents.sha256_hex(data)
            and bool(prior.get("replace")) == payload.replace)
    if not same:
        raise ProductControlError("DOCUMENT_IDEMPOTENCY_CONTENT_CONFLICT", 409)
    intent = str(prior.get("intent_id") or "")
    if intent:
        for kind, event in document_events(session, project_id):
            if kind == DOCUMENT_FAILED_EVENT and event.get("intent_id") == intent:
                raise ProductControlError(str(event.get("code") or "DOCUMENT_WRITE_FAILED"), 409)
        if any(entry.intent_id == intent for entry in documents.open_entries(root)[0]):
            raise ProductControlError("DOCUMENT_FINALIZE_PENDING", 503)
    return {**{k: prior.get(k) for k in ("path", "size", "sha256", "replaced", "previous_sha256")},
            "replayed": True}


def _stage_and_audit(session_scope: SessionFactory, settings: Any, project_id: str, payload: DocumentWrite,
                     relative: PurePosixPath, data: bytes, expected: str | None) -> _Staged:
    """One transaction: reconcile, replay by key, record the intent and stage, append the audit event, commit."""
    root: documents.ProjectRoot | None = None
    entry: documents.JournalEntry | None = None
    replay: dict[str, Any] | None = None
    audited = False
    try:
        with session_scope(settings) as session:
            lock_project_documents(session, project_id)
            root = _project_root(session, settings, project_id)
            recovery = reconcile_journal(session, project_id, root)
            prior = prior_document_write(session, project_id, payload.idempotency_key)
            if prior is not None:
                replay = _replay(session, project_id, root, prior, relative, data, payload)
            else:
                entry = documents.stage_document(root, relative, data, replace=payload.replace,
                                                 expected_sha256=expected, idempotency_key=payload.idempotency_key)
                control_service.append_sync_event(session, project_id, DOCUMENT_EVENT, {
                    **documents.document_receipt(entry.path, entry.size, entry.sha256, entry.previous_sha256),
                    "replace": payload.replace, "idempotency_key": payload.idempotency_key,
                    "intent_id": entry.intent_id})
                audited = True  # the last statement of the block: a later exception can only be the commit's
    except Exception as exc:
        if root is not None and entry is not None and not audited:
            documents.discard_entry(root.path, entry)  # the transaction rolled back: nothing was audited
        elif audited:
            # The commit failed and may or may not have landed; only the database can tell, so reconcile now.
            _LOG.warning("control.document.audit_failed", code="DOCUMENT_AUDIT_FAILED", error=type(exc).__name__)
            _reconcile_after_failure(session_scope, settings, project_id)
            raise ProductControlError("DOCUMENT_AUDIT_FAILED", 503) from exc
        raise
    assert root is not None  # noqa: S101 - set before any statement that can succeed
    recovery.finish(root)
    return _Staged(root=root, entry=entry, replay=replay)


def _finalize(session_scope: SessionFactory, settings: Any, project_id: str, root: documents.ProjectRoot,
              entry: documents.JournalEntry) -> dict[str, Any]:
    """After the audit commit: replace the target from the staged file and close the intent."""
    try:
        outcome = documents.apply_entry(root.path, entry)
    except OSError as exc:
        _LOG.warning("control.document.finalize_pending", code="DOCUMENT_FINALIZE_PENDING", error=type(exc).__name__)
        raise ProductControlError("DOCUMENT_FINALIZE_PENDING", 503) from exc
    if outcome not in (documents.APPLIED, documents.ALREADY_APPLIED):
        _reconcile_after_failure(session_scope, settings, project_id)  # records the failure and closes the intent
        raise ProductControlError(outcome, 409)
    _LOG.info("control.document.written", outcome="written", replaced=entry.previous_sha256 is not None)
    return {**documents.document_receipt(entry.path, entry.size, entry.sha256, entry.previous_sha256),
            "replayed": False}


def write_document_entry(session_scope: SessionFactory, settings: Any, project_id: str,
                         payload: DocumentWrite) -> dict[str, Any]:
    """Validate, then stage and audit in one transaction, then replace the file (recoverable staged commit).

    The file is replaced only after its ``PROJECT_DOCUMENT_WRITTEN`` event is committed; every crash point in between
    is settled by ``reconcile_journal`` (see ``pados.control.project_documents``). A retry under the same key first
    reconciles, then replays the committed receipt, or returns the recorded closed failure code.
    """
    relative = documents.validate_document_path(payload.path)
    data = documents.encode_content(payload.content)
    expected = documents.expected_sha(payload.expected_sha256)
    with project_lock(project_id):
        staged = _stage_and_audit(session_scope, settings, project_id, payload, relative, data, expected)
        if staged.replay is not None:
            return staged.replay
        assert staged.entry is not None  # noqa: S101 - either a replay or a staged entry
        return _finalize(session_scope, settings, project_id, staged.root, staged.entry)


def register_product_control_routes(app: FastAPI, settings: Any, *, control_auth: Any,
                                    session_scope: SessionFactory) -> None:
    """Mount the product routes behind ``control_auth``: the ``Depends(...)`` every /control route uses."""
    ControlAuth = Annotated[None, control_auth]
    started = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(target: Any) -> AsyncIterator[Any]:
        _start_reconciliation(settings, session_scope)  # document write recovery at server start (background)
        async with started(target) as state:
            yield state

    app.router.lifespan_context = lifespan

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
            root, events, _recovery = reconcile_project(session_scope, settings, checked)
            listed = documents.list_documents(root)
            return {"status": "ok", "project_id": checked, **listed,
                    "reconciliation": reconciliation_status(root, events, listed["documents"])}

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
            root = documents.resolve_project_root(root_path, _workspace(settings))
            if documents.has_open_entries(root):  # cheap: one folder listing; locks only when there is work
                reconcile_project(session_scope, settings, checked)
            return {"status": "ok", "project_id": checked, "document": documents.read_document(root, relative)}

        return run(_read)

    # SESSION SCOPES: add to src/pados/identity/scopes.py (allowed for sessions) when branches are integrated.
    @app.post("/control/projects/{project_id}/documents")
    def control_write_document(project_id: str, payload: DocumentWrite, _: ControlAuth) -> object:
        def _write() -> dict[str, Any]:
            checked = require_uuid(project_id)
            receipt = write_document_entry(session_scope, settings, checked, payload)
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

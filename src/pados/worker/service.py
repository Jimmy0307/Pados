# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import hashlib
import json
import logging
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

from sqlalchemy import Engine, text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from pados.control.approval_subject import (
    APPROVAL_NOT_APPROVED,
    APPROVAL_PROJECT_MISMATCH,
    APPROVAL_REF_NOT_FOUND,
    APPROVAL_REQUEST_MISMATCH,
    APPROVAL_SCOPE_MISMATCH,
    APPROVAL_SCOPE_VERSION_STALE,
    ApprovalOperation,
    WorkRouteSubject,
    find_operation,
    load_bound_subject,
    match_route,
    scope_version_is_current,
)
from pados.loop import scope_authority
from pados.memory import packet as context_packet
from pados.memory.retrieval import RetrievalRequest
from pados.worker.command import run_registered_command
from pados.worker.handoff import build_handoff, stable_json, write_handoff_artifacts
from pados.worker.registry import capability_registry, discover_workers
from pados.worker.router import select_worker
from pados.worker.schemas import (
    CommandInvocation,
    ErrorCode,
    JobStatus,
    RouteRequest,
    WorkerAvailability,
    WorkerType,
)

LOGGER = logging.getLogger("pados.worker.service")


def _event(session: Session, job_id: str, project_id: str, event_type: str, payload: dict[str, Any]) -> None:
    session.execute(
        text(
            """
            INSERT INTO worker_job_events(job_id, project_id, event_type, payload_json)
            VALUES (CAST(:job_id AS uuid), CAST(:project_id AS uuid), :event_type, CAST(:payload_json AS jsonb))
            """
        ),
        {
            "job_id": job_id,
            "project_id": project_id,
            "event_type": event_type,
            "payload_json": json.dumps(payload, sort_keys=True),
        },
    )


def list_jobs(session: Session) -> list[dict[str, Any]]:
    rows = session.execute(text("SELECT * FROM worker_jobs ORDER BY queued_at DESC LIMIT 100")).mappings().all()
    return [dict(row) for row in rows]


def get_job(session: Session, job_id: str) -> dict[str, Any]:
    row = session.execute(text("SELECT * FROM worker_jobs WHERE id = CAST(:id AS uuid)"), {"id": job_id}).mappings().one()
    return dict(row)


def cancel_job(session: Session, job_id: str) -> dict[str, Any]:
    current = get_job(session, job_id)
    if current["status"] in {"SUCCEEDED", "FAILED", "CANCELLED", "TIMED_OUT"}:
        return current
    row = session.execute(
        text(
            """
            UPDATE worker_jobs
            SET status = 'CANCELLED', completed_at = now(), result_summary = 'cancelled by control API'
            WHERE id = CAST(:id AS uuid)
            RETURNING *
            """
        ),
        {"id": job_id},
    ).mappings().one()
    _event(session, job_id, str(row["project_id"]), "JOB_CANCELLED", {"reason": "control_api"})
    return dict(row)


@dataclass(frozen=True)
class RouteAuthorization:
    """A route the approval subject covers: the locked request and the matched operation."""

    request: dict[str, Any]
    subject: WorkRouteSubject
    operation: ApprovalOperation


def _record_route_refusal(
    session: Session,
    code: str,
    *,
    request_id: str,
    route: RouteRequest,
    project_id: str | None,
    subject_hash: str | None,
) -> None:
    """Commit a ROUTE_REFUSED audit row on an independent transaction.

    The caller's transaction is rolled back after the refusal, so the audit row
    must not depend on it. ``project_id`` is kept in the payload only: the row's
    project column stays NULL so the independent insert never waits on a project
    row that the caller's own uncommitted transaction may hold.
    """
    payload = {
        "code": code,
        "approval_id": route.approval_id,
        "request_id": request_id,
        "project_id": project_id,
        "capability_id": route.capability_id.value,
        "command_id": route.command_id,
        "surface": route.surface,
        "step_sequence": route.step_sequence,
        "subject_hash": subject_hash,
    }
    bind = session.get_bind()
    engine = bind if isinstance(bind, Engine) else bind.engine
    try:
        with engine.begin() as connection:
            connection.execute(text("SET LOCAL lock_timeout = '2s'"))
            connection.execute(
                text(
                    """
                    INSERT INTO control_sync_events(project_id, event_type, payload_json)
                    VALUES (NULL, 'ROUTE_REFUSED', CAST(:payload_json AS jsonb))
                    """
                ),
                {"payload_json": json.dumps(payload, sort_keys=True)},
            )
    except SQLAlchemyError:
        # The route is refused regardless; a lost audit row must be visible to operators.
        LOGGER.exception("ROUTE_REFUSED audit row could not be committed (code=%s)", code)


def _refuse(
    session: Session,
    code: str,
    *,
    request_id: str,
    route: RouteRequest,
    project_id: str | None,
    subject_hash: str | None,
) -> NoReturn:
    _record_route_refusal(
        session, code, request_id=request_id, route=route, project_id=project_id, subject_hash=subject_hash
    )
    raise PermissionError(code)


def _authorize_subject(
    approval: Mapping[str, Any],
    request: Mapping[str, Any],
    *,
    request_id: str,
    route: RouteRequest,
    settings: Any,
) -> tuple[WorkRouteSubject, ApprovalOperation]:
    """Checks 1-8 of the RC2 D1 binding, in order; raises PermissionError(<code>)."""
    project_id = str(request["project_id"])
    if str(approval["project_id"]) != project_id:
        raise PermissionError(APPROVAL_PROJECT_MISMATCH)
    if approval["status"] != "APPROVED":
        raise PermissionError(APPROVAL_NOT_APPROVED)
    subject = load_bound_subject(approval["subject_kind"], approval["subject_json"], approval["proposal_hash"])
    bound_approval = request.get("approval_id")
    if bound_approval is not None and str(bound_approval) != route.approval_id:
        raise PermissionError(APPROVAL_REQUEST_MISMATCH)
    code = match_route(
        subject,
        project_id=project_id,
        request_id=request_id,
        surface=route.surface,
        step_sequence=route.step_sequence,
        capability_id=route.capability_id,
        command_id=route.command_id,
        risk_level=route.risk_level,
    )
    if code is not None:
        raise PermissionError(code)
    operation = find_operation(
        subject, step_sequence=route.step_sequence, capability_id=route.capability_id, command_id=route.command_id
    )
    if operation is None:  # pragma: no cover - match_route already refused this route
        raise PermissionError(APPROVAL_SCOPE_MISMATCH)
    if subject.scope_version_ref is not None and not scope_version_is_current(
        subject, scope_authority.read_current_scope_version(settings, project_id)
    ):
        raise PermissionError(APPROVAL_SCOPE_VERSION_STALE)
    return subject, operation


def _assert_ready(
    session: Session, request_id: str, route: RouteRequest, *, settings: Any = None
) -> RouteAuthorization:
    request = session.execute(
        text(
            """
            SELECT cwr.*, p.root_path
            FROM control_work_requests cwr
            JOIN projects p ON p.id = cwr.project_id
            WHERE cwr.id = CAST(:request_id AS uuid)
            FOR UPDATE
            """
        ),
        {"request_id": request_id},
    ).mappings().one()
    approval = session.execute(
        text("SELECT * FROM control_approvals WHERE id = CAST(:approval_id AS uuid)"),
        {"approval_id": route.approval_id},
    ).mappings().first()
    project_id = str(request["project_id"])
    if approval is None:
        _refuse(session, APPROVAL_REF_NOT_FOUND, request_id=request_id, route=route,
                project_id=project_id, subject_hash=None)
    try:
        subject, operation = _authorize_subject(
            dict(approval), dict(request), request_id=request_id, route=route, settings=settings
        )
    except PermissionError as exc:
        _refuse(session, str(exc), request_id=request_id, route=route, project_id=project_id,
                subject_hash=str(approval["proposal_hash"]))
    if route.capability_id not in capability_registry():
        raise PermissionError("capability mismatch")
    # The busy check runs after every binding check, so a denied route reports its own code.
    active: Any = session.execute(
        text(
            """
            SELECT count(*)
            FROM worker_jobs
            WHERE project_id = CAST(:project_id AS uuid)
              AND status IN ('ROUTED', 'QUEUED', 'RUNNING', 'VALIDATING')
            """
        ),
        {"project_id": project_id},
    ).scalar_one()
    if int(active) > 0:
        raise PermissionError("BLOCKED_PROJECT_BUSY")
    return RouteAuthorization(request=dict(request), subject=subject, operation=operation)


def _idempotent_job(session: Session, request_id: str, route: RouteRequest) -> dict[str, Any] | None:
    """Return an existing job only when it was routed under the same approval and operation."""
    existing = session.execute(
        text(
            """
            SELECT * FROM worker_jobs
            WHERE request_id = CAST(:request_id AS uuid)
              AND idempotency_key = :idempotency_key
            """
        ),
        {"request_id": request_id, "idempotency_key": route.idempotency_key},
    ).mappings().first()
    if existing is None:
        return None
    payload = existing["route_payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    same_route = (
        str(existing["approval_id"]) == route.approval_id
        and str(existing["capability_id"]) == route.capability_id.value
        and isinstance(payload, dict)
        and payload.get("command_id") == route.command_id
    )
    return dict(existing) if same_route else None


def route_work_request(
    session: Session,
    *,
    request_id: str,
    route: RouteRequest,
    settings: Any,
    artifact_root: Path,
) -> dict[str, Any]:
    existing = _idempotent_job(session, request_id, route)
    if existing is not None:
        return {"job": existing, "routing": {"status": "idempotent"}}

    authorization = _assert_ready(session, request_id, route, settings=settings)
    request = authorization.request
    workers = discover_workers(settings)
    # Only worker types the approval names are candidates; an unapproved (cloud) worker never is.
    decision = select_worker(
        workers,
        capability_id=route.capability_id,
        risk_level=route.risk_level,
        allowed_worker_types=frozenset(authorization.subject.actor.worker_types),
    )
    if decision.selected_worker_id is None:
        session.execute(
            text("UPDATE control_work_requests SET status = 'BLOCKED', updated_at = now() WHERE id = CAST(:id AS uuid)"),
            {"id": request_id},
        )
        raise PermissionError(decision.error_code or ErrorCode.NO_ELIGIBLE_WORKER.value)

    worker = workers[decision.selected_worker_id]
    if worker.availability != WorkerAvailability.AVAILABLE:
        raise PermissionError(ErrorCode.WORKER_UNAVAILABLE.value)

    context = context_packet.assemble_context_packet(
        session,
        request=RetrievalRequest(
            project_id=str(request["project_id"]),
            query=route.context_query,
            purpose="worker-job",
            max_tokens=min(worker.max_context_tokens, 2400),
        ),
        request_id=request_id,
    )
    packet_id = context["context_packet"]["packet_id"]
    timeout = route.timeout_seconds or capability_registry()[route.capability_id].default_timeout
    row = session.execute(
        text(
            """
            INSERT INTO worker_jobs(
              request_id, project_id, worker_id, worker_type, status, capability_id,
              context_packet_id, approval_id, working_directory, isolation_type,
              attempt_number, timeout_seconds, routing_explanation, idempotency_key, route_payload
            )
            VALUES (
              CAST(:request_id AS uuid), CAST(:project_id AS uuid), :worker_id, :worker_type,
              'ROUTED', :capability_id, CAST(:context_packet_id AS uuid), CAST(:approval_id AS uuid),
              :working_directory, :isolation_type, 1, :timeout_seconds, :routing_explanation,
              :idempotency_key, CAST(:route_payload AS jsonb)
            )
            RETURNING *
            """
        ),
        {
            "request_id": request_id,
            "project_id": str(request["project_id"]),
            "worker_id": worker.worker_id,
            "worker_type": worker.worker_type.value,
            "capability_id": route.capability_id.value,
            "context_packet_id": packet_id,
            "approval_id": route.approval_id,
            "working_directory": str(Path(str(request.get("root_path", "."))).resolve()),
            "isolation_type": "GIT_WORKTREE" if capability_registry()[route.capability_id].requires_isolation else "READ_ONLY",
            "timeout_seconds": timeout,
            "routing_explanation": decision.explanation,
            "idempotency_key": route.idempotency_key,
            "route_payload": route.model_dump_json(),
        },
    ).mappings().one()
    job = dict(row)
    job_id = str(job["id"])
    _event(session, job_id, str(job["project_id"]), "JOB_CREATED", {"request_id": request_id})
    _event(session, job_id, str(job["project_id"]), "ROUTING_STARTED", {"capability": route.capability_id.value})
    _event(session, job_id, str(job["project_id"]), "WORKER_SELECTED", {"worker_id": worker.worker_id})
    handoff = build_handoff(
        job_id=job_id,
        project_id=str(job["project_id"]),
        request={"title": request["title"], "body": request["body"]},
        context_packet=context,
        allowed_actions=route.allowed_actions,
        prohibited_actions=route.prohibited_actions,
        allowed_paths=route.allowed_paths,
        validation_commands=[route.command_id] if route.command_id else [],
    )
    handoff_dir = artifact_root / "worker_jobs" / job_id
    handoff_json, handoff_md = write_handoff_artifacts(handoff, handoff_dir)
    manifest_path = handoff_dir / "result_manifest.json"
    manifest_path.write_text(
        stable_json(
            {
                "job_id": job_id,
                "status": JobStatus.ROUTED.value,
                "worker_id": worker.worker_id,
                "artifacts": [handoff_json.as_posix(), handoff_md.as_posix()],
                "error_code": None,
            }
        ),
        encoding="utf-8",
    )
    session.execute(
        text(
            """
            INSERT INTO worker_job_artifacts(job_id, project_id, artifact_type, path, sha256)
            VALUES
              (CAST(:job_id AS uuid), CAST(:project_id AS uuid), 'handoff_json', :json_path, :json_hash),
              (CAST(:job_id AS uuid), CAST(:project_id AS uuid), 'result_manifest', :manifest_path,
               :manifest_hash)
            """
        ),
        {
            "job_id": job_id,
            "project_id": str(job["project_id"]),
            "json_path": handoff_json.as_posix(),
            "json_hash": hashlib.sha256(handoff_json.read_bytes()).hexdigest(),
            "manifest_path": manifest_path.as_posix(),
            "manifest_hash": hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
        },
    )
    _event(session, job_id, str(job["project_id"]), "CONTEXT_MATERIALIZED", {"packet_id": packet_id})
    _event(session, job_id, str(job["project_id"]), "ARTIFACT_REGISTERED", {"path": handoff_json.as_posix()})
    final_job = _execute_deterministic_if_requested(
        session,
        job=job,
        command_id=route.command_id,
        project_root=Path(str(request["root_path"])).resolve(),
        artifact_dir=handoff_dir,
        timeout_seconds=timeout,
    )
    session.execute(
        text(
            """
            UPDATE control_work_requests
            SET status = 'ROUTED',
                approval_id = CAST(:approval_id AS uuid),
                capability_id = :capability_id,
                risk_level = :risk_level,
                context_packet_id = CAST(:context_packet_id AS uuid),
                updated_at = now()
            WHERE id = CAST(:request_id AS uuid)
            """
        ),
        {
            "request_id": request_id,
            "approval_id": route.approval_id,
            "capability_id": route.capability_id.value,
            "risk_level": route.risk_level.value,
            "context_packet_id": packet_id,
        },
    )
    return {"job": final_job, "routing": decision.__dict__, "handoff_path": handoff_json.as_posix()}


def _execute_deterministic_if_requested(
    session: Session,
    *,
    job: dict[str, Any],
    command_id: str | None,
    project_root: Path,
    artifact_dir: Path,
    timeout_seconds: int,
) -> dict[str, Any]:
    if job["worker_type"] != WorkerType.DETERMINISTIC_TOOL.value or command_id is None:
        return job
    job_id = str(job["id"])
    project_id = str(job["project_id"])
    _event(session, job_id, project_id, "JOB_STARTED", {"command_id": command_id})
    session.execute(
        text("UPDATE worker_jobs SET status = 'RUNNING', started_at = now() WHERE id = CAST(:id AS uuid)"),
        {"id": job_id},
    )
    result = run_registered_command(
        CommandInvocation(
            command_id=command_id,
            working_directory=project_root,
            environment={"PADOS_RUNTIME_MODE": "native", "PYTHONPATH": str(project_root / "src")},
            timeout_seconds=timeout_seconds,
        ),
        project_root=project_root,
    )
    stdout_path = artifact_dir / "stdout.txt"
    stderr_path = artifact_dir / "stderr.txt"
    stdout_path.write_text(str(result.get("stdout", "")), encoding="utf-8")
    stderr_path.write_text(str(result.get("stderr", "")), encoding="utf-8")
    session.execute(
        text(
            """
            INSERT INTO worker_job_artifacts(job_id, project_id, artifact_type, path, sha256)
            VALUES
              (CAST(:job_id AS uuid), CAST(:project_id AS uuid), 'stdout', :stdout_path, :stdout_hash),
              (CAST(:job_id AS uuid), CAST(:project_id AS uuid), 'stderr', :stderr_path, :stderr_hash)
            """
        ),
        {
            "job_id": job_id,
            "project_id": project_id,
            "stdout_path": stdout_path.as_posix(),
            "stdout_hash": hashlib.sha256(stdout_path.read_bytes()).hexdigest(),
            "stderr_path": stderr_path.as_posix(),
            "stderr_hash": hashlib.sha256(stderr_path.read_bytes()).hexdigest(),
        },
    )
    _event(session, job_id, project_id, "PROCESS_STARTED", {"command_id": command_id})
    _event(
        session,
        job_id,
        project_id,
        "OUTPUT_CAPTURED",
        {"stdout": stdout_path.as_posix(), "stderr": stderr_path.as_posix()},
    )
    status = str(result["status"])
    validation_status = "PASSED" if status == "SUCCEEDED" else "FAILED"
    event_type = "JOB_SUCCEEDED" if status == "SUCCEEDED" else "JOB_FAILED"
    if status == "TIMED_OUT":
        event_type = "JOB_TIMED_OUT"
    row = session.execute(
        text(
            """
            UPDATE worker_jobs
            SET status = :status,
                completed_at = now(),
                exit_code = :exit_code,
                result_summary = :summary,
                error_code = :error_code,
                validation_status = :validation_status,
                usage_metadata = CAST(:usage_metadata AS jsonb)
            WHERE id = CAST(:id AS uuid)
            RETURNING *
            """
        ),
        {
            "id": job_id,
            "status": status,
            "exit_code": result.get("exit_code"),
            "summary": f"registered command {command_id} completed with {status}",
            "error_code": result.get("error_code"),
            "validation_status": validation_status,
            "usage_metadata": json.dumps({"provider": "deterministic", "usage_status": "UNKNOWN"}),
        },
    ).mappings().one()
    _event(session, job_id, project_id, "VALIDATION_STARTED", {"policy": command_id})
    _event(
        session,
        job_id,
        project_id,
        "VALIDATION_PASSED" if validation_status == "PASSED" else "VALIDATION_FAILED",
        {"exit_code": result.get("exit_code")},
    )
    _event(session, job_id, project_id, event_type, {"status": status})
    return dict(row)

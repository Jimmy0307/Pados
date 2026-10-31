# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeout
from dataclasses import dataclass, replace
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
from pados.memory.security import assert_no_secret
from pados.providers.execution_binding import ExecutionBindingRefused, ExecutionIntent
from pados.providers.factory import build_chat_provider
from pados.providers.ollama import MeasuredChatProvider
from pados.resources.capability_profile import OutputConstraint
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
NON_TERMINAL_JOB_STATUSES = frozenset({"ROUTED", "QUEUED", "RUNNING", "VALIDATING"})


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


def record_job_event(
    session: Session, job_id: str, project_id: str, event_type: str, payload: dict[str, Any]
) -> None:
    """Append a worker_job_events row (for callers outside the worker service, e.g. loop cancellation)."""
    _event(session, job_id, project_id, event_type, payload)


def expire_overdue_jobs(session: Session, project_id: str) -> int:
    """Time out active project jobs whose persisted route deadline has elapsed."""
    rows = session.execute(
        text(
            """
            UPDATE worker_jobs
            SET status = 'TIMED_OUT', error_code = 'JOB_DEADLINE_EXCEEDED',
                completed_at = now(), validation_status = 'FAILED'
            WHERE project_id = CAST(:project_id AS uuid)
              AND status IN ('ROUTED', 'QUEUED', 'RUNNING', 'VALIDATING')
              AND now() > queued_at + timeout_seconds * interval '1 second'
            RETURNING id, project_id
            """
        ),
        {"project_id": project_id},
    ).mappings().all()
    for row in rows:
        _event(
            session,
            str(row["id"]),
            str(row["project_id"]),
            "JOB_TIMED_OUT",
            {"error_code": "JOB_DEADLINE_EXCEEDED"},
        )
    return len(rows)


def list_jobs(session: Session) -> list[dict[str, Any]]:
    rows = session.execute(text("SELECT * FROM worker_jobs ORDER BY queued_at DESC LIMIT 100")).mappings().all()
    return [dict(row) for row in rows]


def get_job(session: Session, job_id: str) -> dict[str, Any]:
    row = session.execute(text("SELECT * FROM worker_jobs WHERE id = CAST(:id AS uuid)"), {"id": job_id}).mappings().one()
    return dict(row)


def list_job_artifacts(session: Session, job_id: str) -> list[dict[str, Any]]:
    """The execution evidence a job recorded (type, workspace-relative path, SHA-256); read-only."""
    rows = session.execute(
        text("SELECT artifact_type, path, sha256, created_at FROM worker_job_artifacts "
             "WHERE job_id = CAST(:id AS uuid) ORDER BY created_at, id"),
        {"id": job_id},
    ).mappings().all()
    return [dict(row) for row in rows]


def cancel_job(session: Session, job_id: str) -> dict[str, Any]:
    current = get_job(session, job_id)
    if current["status"] in {"SUCCEEDED", "FAILED", "BLOCKED", "CANCELLED", "TIMED_OUT"}:
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
    expire_overdue_jobs(session, project_id)
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
    project_id = str(request["project_id"])
    workers = discover_workers(settings, project_id=project_id)
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
    if worker.worker_type == WorkerType.OLLAMA:
        final_job = _execute_model_if_requested(
            session,
            job=final_job,
            handoff_path=handoff_json,
            artifact_dir=handoff_dir,
            settings=settings,
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


def _execution_intent_for_capability(capability_id: str) -> ExecutionIntent | None:
    if capability_id == "SUMMARIZE":
        return ExecutionIntent.TEXT_SUMMARY
    if capability_id == "CLASSIFY_REQUEST":
        return ExecutionIntent.STRUCTURED_OUTPUT
    return None


def _model_stdout_contract(model_text: str) -> dict[str, Any]:
    return {
        "result_summary": model_text[:8000],
        "changed_files": [],
        "validation_results": [],
        "remaining_risks": [],
    }


def _model_prompt(handoff_path: Path, job: Mapping[str, Any]) -> str:
    handoff = json.loads(handoff_path.read_text(encoding="utf-8"))
    route_payload = job.get("route_payload", {})
    if isinstance(route_payload, str):
        route_payload = json.loads(route_payload)
    objective = str(route_payload.get("context_query") or handoff.get("request", {}).get("title", ""))
    context = {
        "success_criteria": handoff.get("success_criteria", ""),
        "relevant_facts": handoff.get("relevant_facts", []),
        "locked_decisions": handoff.get("locked_decisions", []),
        "recent_failures": handoff.get("recent_failures", []),
        "constraints": handoff.get("constraints", []),
    }
    prompt = (
        "Complete the approved model step. Do not take actions or claim changes. "
        f"\nObjective:\n{objective[:2000]}\nContext:\n{stable_json(context)[:12000]}"
    )
    assert_no_secret(prompt, field_name="worker_model_prompt")
    return prompt


def _model_response_schema(provider: Any) -> dict[str, Any] | None:
    bound = getattr(provider, "bound", None)
    execution = getattr(bound, "execution", None)
    if getattr(execution, "output_constraint", None) != OutputConstraint.JSON_SCHEMA:
        return None
    return {
        "type": "object",
        "properties": {"result_summary": {"type": "string"}},
        "required": ["result_summary"],
        "additionalProperties": False,
    }


async def _chat_once(provider: Any, prompt: str, response_schema: dict[str, Any] | None, timeout: int) -> str:
    return await asyncio.wait_for(
        provider.chat(prompt, response_schema=response_schema), timeout=max(1, timeout)
    )


def _run_model_chat(provider: Any, prompt: str, timeout: int) -> str:
    response_schema = _model_response_schema(provider)
    coroutine = _chat_once(provider, prompt, response_schema, timeout)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coroutine)
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pados-model-call")
    future = executor.submit(asyncio.run, coroutine)
    try:
        return future.result(timeout=timeout + 1)
    except FutureTimeout:
        future.cancel()
        raise TimeoutError from None
    finally:
        executor.shutdown(wait=False, cancel_futures=True)


def _finish_model_job(
    session: Session,
    *,
    job: Mapping[str, Any],
    status: str,
    error_code: str | None,
    validation_status: str,
    usage_metadata: dict[str, Any],
    result_summary: str,
    stdout_path: Path | None = None,
) -> dict[str, Any]:
    job_id = str(job["id"])
    project_id = str(job["project_id"])
    if stdout_path is not None:
        session.execute(
            text(
                """
                INSERT INTO worker_job_artifacts(job_id, project_id, artifact_type, path, sha256)
                VALUES (CAST(:job_id AS uuid), CAST(:project_id AS uuid), 'stdout', :path, :sha256)
                """
            ),
            {
                "job_id": job_id,
                "project_id": project_id,
                "path": stdout_path.as_posix(),
                "sha256": hashlib.sha256(stdout_path.read_bytes()).hexdigest(),
            },
        )
        _event(session, job_id, project_id, "OUTPUT_CAPTURED", {"stdout": stdout_path.as_posix()})
    row = session.execute(
        text(
            """
            UPDATE worker_jobs
            SET status = :status, error_code = :error_code, validation_status = :validation_status,
                result_summary = :summary, usage_metadata = CAST(:usage_metadata AS jsonb), completed_at = now()
            WHERE id = CAST(:job_id AS uuid)
            RETURNING *
            """
        ),
        {
            "job_id": job_id,
            "status": status,
            "error_code": error_code,
            "validation_status": validation_status,
            "summary": result_summary[:8000],
            "usage_metadata": json.dumps(usage_metadata, sort_keys=True),
        },
    ).mappings().one()
    event_type = {
        "SUCCEEDED": "JOB_SUCCEEDED",
        "TIMED_OUT": "JOB_TIMED_OUT",
        "BLOCKED": "JOB_BLOCKED",
    }.get(status, "JOB_FAILED")
    if validation_status in {"PASSED", "FAILED"}:
        _event(
            session,
            job_id,
            project_id,
            "VALIDATION_PASSED" if validation_status == "PASSED" else "VALIDATION_FAILED",
            {"error_code": error_code},
        )
    _event(session, job_id, project_id, event_type, {"status": status, "error_code": error_code})
    return dict(row)


def _execute_model_if_requested(
    session: Session,
    *,
    job: dict[str, Any],
    handoff_path: Path,
    artifact_dir: Path,
    settings: Any,
    timeout_seconds: int,
) -> dict[str, Any]:
    """Contain executor failures so they cannot abort the scheduler's transaction."""
    try:
        return _execute_model_once(
            session,
            job=job,
            handoff_path=handoff_path,
            artifact_dir=artifact_dir,
            settings=settings,
            timeout_seconds=timeout_seconds,
        )
    except Exception as exc:  # noqa: BLE001 - persist a bounded failure instead of propagating
        failed = dict(job)
        code = str(getattr(exc, "code", "MODEL_EXECUTOR_FAILED"))
        failed.update(
            status="FAILED",
            error_code=code,
            validation_status="FAILED",
            completed_at=None,
            usage_metadata={"measured_execution": None},
        )
        try:
            row = session.execute(
                text(
                    """
                    UPDATE worker_jobs
                    SET status = 'FAILED', error_code = :code, validation_status = 'FAILED',
                        result_summary = 'Measured model executor failed.',
                        usage_metadata = '{"measured_execution": null}'::jsonb, completed_at = now()
                    WHERE id = CAST(:id AS uuid)
                    RETURNING *
                    """
                ),
                {"id": str(job["id"]), "code": code},
            ).mappings().first()
            if row is not None:
                failed = dict(row)
                _event(session, str(job["id"]), str(job["project_id"]), "JOB_FAILED", {"error_code": code})
        except Exception:  # noqa: BLE001 - a failed database transaction cannot be repaired here
            pass
        return failed


def _execute_model_once(
    session: Session,
    *,
    job: dict[str, Any],
    handoff_path: Path,
    artifact_dir: Path,
    settings: Any,
    timeout_seconds: int,
) -> dict[str, Any]:
    """Run one measured local-model call and persist its bounded result without raising."""
    if job.get("worker_type") != WorkerType.OLLAMA.value:
        return job
    job_id = str(job["id"])
    project_id = str(job["project_id"])
    intent = _execution_intent_for_capability(str(job["capability_id"]))
    if intent is None:
        return _finish_model_job(
            session,
            job=job,
            status="BLOCKED",
            error_code="MODEL_CAPABILITY_NOT_EXECUTABLE",
            validation_status="FAILED",
            usage_metadata={
                "measured_execution_refusal": {
                    "code": "MODEL_CAPABILITY_NOT_EXECUTABLE",
                    "reasons": [],
                }
            },
            result_summary="Model capability is not executable.",
        )
    timeout = min(int(job["timeout_seconds"]), int(settings.local_inference_timeout_seconds), timeout_seconds)
    try:
        provider = build_chat_provider(settings, intent=intent, project_id=project_id)
    except ExecutionBindingRefused as exc:
        refusal = {"code": exc.refusal.code, "reasons": list(exc.refusal.reasons)}
        return _finish_model_job(
            session,
            job=job,
            status="BLOCKED",
            error_code=exc.refusal.code,
            validation_status="FAILED",
            usage_metadata={"measured_execution_refusal": refusal},
            result_summary="Measured execution binding refused.",
        )
    except Exception as exc:  # noqa: BLE001 - model execution never aborts the loop transaction
        return _finish_model_job(
            session,
            job=job,
            status="FAILED",
            error_code=str(getattr(exc, "code", "MODEL_PROVIDER_INITIALIZATION_FAILED")),
            validation_status="FAILED",
            usage_metadata={"measured_execution": None},
            result_summary="Measured provider initialization failed.",
        )
    if isinstance(provider, MeasuredChatProvider):
        provider.bound = replace(provider.bound, timeout_seconds=timeout)

    _event(session, job_id, project_id, "JOB_STARTED", {"intent": intent.value})
    session.execute(
        text("UPDATE worker_jobs SET status = 'RUNNING', started_at = now() WHERE id = CAST(:id AS uuid)"),
        {"id": job_id},
    )
    evidence: dict[str, Any] = {"measured_execution": getattr(provider, "last_evidence", None)}
    try:
        prompt = _model_prompt(handoff_path, job)
        model_text = _run_model_chat(provider, prompt, timeout)
        evidence = {"measured_execution": getattr(provider, "last_evidence", None)}
        contract = _model_stdout_contract(str(model_text))
        stdout_path = artifact_dir / "stdout.txt"
        stdout_path.write_text(stable_json(contract), encoding="utf-8")
        if not str(model_text).strip():
            return _finish_model_job(
                session,
                job=job,
                status="FAILED",
                error_code="OUTPUT_CONTRACT_FAILED",
                validation_status="FAILED",
                usage_metadata=evidence,
                result_summary="Model returned empty text.",
                stdout_path=stdout_path,
            )
        return _finish_model_job(
            session,
            job=job,
            status="SUCCEEDED",
            error_code=None,
            validation_status="PASSED",
            usage_metadata=evidence,
            result_summary=str(model_text),
            stdout_path=stdout_path,
        )
    except (TimeoutError, asyncio.TimeoutError, FutureTimeout):
        return _finish_model_job(
            session,
            job=job,
            status="TIMED_OUT",
            error_code="TIMED_OUT",
            validation_status="FAILED",
            usage_metadata={"measured_execution": getattr(provider, "last_evidence", None)},
            result_summary="Measured model call timed out.",
        )
    except ExecutionBindingRefused as exc:
        return _finish_model_job(
            session,
            job=job,
            status="FAILED",
            error_code=exc.refusal.code,
            validation_status="FAILED",
            usage_metadata={"measured_execution": getattr(provider, "last_evidence", None)},
            result_summary="Measured model call refused during execution.",
        )
    except Exception as exc:  # noqa: BLE001 - provider errors become terminal job evidence
        return _finish_model_job(
            session,
            job=job,
            status="FAILED",
            error_code=str(getattr(exc, "code", "MODEL_PROVIDER_FAILED")),
            validation_status="FAILED",
            usage_metadata={"measured_execution": getattr(provider, "last_evidence", None)},
            result_summary="Measured model call failed.",
        )

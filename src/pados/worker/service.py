# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

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


def _assert_ready(session: Session, request_id: str, route: RouteRequest) -> dict[str, Any]:
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
    ).mappings().one()
    if approval["project_id"] != request["project_id"]:
        raise PermissionError("cross-project approval rejected")
    if approval["status"] != "APPROVED":
        raise PermissionError("unapproved request rejected")
    if route.capability_id not in capability_registry():
        raise PermissionError("capability mismatch")
    active: Any = session.execute(
        text(
            """
            SELECT count(*)
            FROM worker_jobs
            WHERE project_id = CAST(:project_id AS uuid)
              AND status IN ('ROUTED', 'QUEUED', 'RUNNING', 'VALIDATING')
            """
        ),
        {"project_id": str(request["project_id"])},
    ).scalar_one()
    if int(active) > 0:
        raise PermissionError("BLOCKED_PROJECT_BUSY")
    return dict(request)


def route_work_request(
    session: Session,
    *,
    request_id: str,
    route: RouteRequest,
    settings: Any,
    artifact_root: Path,
) -> dict[str, Any]:
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
    if existing is not None:
        return {"job": dict(existing), "routing": {"status": "idempotent"}}

    request = _assert_ready(session, request_id, route)
    workers = discover_workers(settings)
    decision = select_worker(workers, capability_id=route.capability_id, risk_level=route.risk_level)
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

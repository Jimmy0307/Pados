# SPDX-License-Identifier: AGPL-3.0-only
"""Read-only projections for the Control Center's product work loop. PROJECTION ONLY.

Nothing here decides, schedules, routes, approves or accepts anything. These are bounded reads of rows the existing
authorities wrote (work requests, WORK_ROUTE approvals, task goals and plans, TaskLoops, ResultCandidates and their
decisions, sync events, job artifacts), shaped so one page can show a project's work without a second store:

- ``project_work``: the project's requests, approvals, goals, plans (with steps), loops, candidates and recent audit
  events, newest first, at most ``LIMIT`` of each;
- ``step_up_readiness``: whether the Remote console is configured and whether any approver of this project has a
  WebAuthn presence device enrolled (counts only: no actor ids, no key material);
- ``artifact_content``: the text of one recorded job artifact (stdout, stderr or the result manifest), bounded, only
  from inside the artifact root, and only when its SHA-256 still equals the recorded digest;
- ``project_execution``: the TaskLoop iterations of the project with the step each one executed, their worker jobs
  (status, timings, exit and validation codes) and the recorded job artifacts (type, SHA-256, time; never a path),
  at most ``EXECUTION_LIMIT`` iterations and jobs and ``ARTIFACT_LIMIT`` artifacts. The Work Map (工作心智圖) joins it
  with ``project_work`` in the browser; it is a rebuildable view of those rows and never a second store.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from pados.control.product_errors import ProductControlError
from pados.db.relations import relation_visible
from pados.memory.security import contains_secret

LIMIT = 50
EXECUTION_LIMIT = 200
ARTIFACT_LIMIT = 400
ARTIFACT_TEXT_TYPES = ("stdout", "stderr", "result_manifest")
ARTIFACT_MAX_BYTES = 256 * 1024


def _rows(session: Session, sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    return [dict(row) for row in session.execute(text(sql), params).mappings().all()]


def _request_view(row: dict[str, Any]) -> dict[str, Any]:
    """A work request without its stored envelope: the person's text, the capability and the root goal id."""
    try:
        body = json.loads(str(row.get("body") or ""))
    except ValueError:
        body = None
    captured = isinstance(body, dict) and isinstance(body.get("task_envelope"), dict)
    goals = body["task_envelope"].get("normalized_goals") if captured else None
    root_goal = goals[0].get("goal_id") if isinstance(goals, list) and goals and isinstance(goals[0], dict) else None
    return {"id": str(row["id"]), "title": row["title"], "status": row["status"], "created_at": row["created_at"],
            "request_text": body.get("request_text") if captured else str(row.get("body") or "")[:2000],
            "requested_capability": body.get("requested_capability") if captured else None,
            "task_envelope_captured": captured, "root_goal_id": root_goal}


def project_work(session: Session, project_id: str) -> dict[str, Any]:
    params = {"project_id": project_id, "limit": LIMIT}
    requests = _rows(session, """
        SELECT id, title, body, status, created_at FROM control_work_requests
        WHERE project_id = CAST(:project_id AS uuid) ORDER BY created_at DESC LIMIT :limit""", params)
    approvals = _rows(session, """
        SELECT id, status, version, subject_kind, subject_json, proposal_summary, decision_reason, created_at,
               decided_at, expires_at
        FROM control_approvals WHERE project_id = CAST(:project_id AS uuid) ORDER BY created_at DESC LIMIT :limit""",
        params)
    goals = _rows(session, """
        SELECT id, request_id, approval_id, objective, success_criteria, risk_level, status, created_at
        FROM task_goals WHERE project_id = CAST(:project_id AS uuid) ORDER BY created_at DESC LIMIT :limit""", params)
    plans = _rows(session, """
        SELECT p.id, p.task_goal_id, p.approval_id, p.status, p.created_at FROM task_plans p
        JOIN task_goals g ON g.id = p.task_goal_id
        WHERE g.project_id = CAST(:project_id AS uuid) ORDER BY p.created_at DESC LIMIT :limit""", params)
    steps = _rows(session, """
        SELECT s.id, s.task_plan_id, s.sequence, s.title, s.step_type, s.required_capability, s.command_id, s.risk_level,
               s.status, s.allowed_actions, s.prohibited_actions
        FROM task_plan_steps s JOIN task_plans p ON p.id = s.task_plan_id JOIN task_goals g ON g.id = p.task_goal_id
        WHERE g.project_id = CAST(:project_id AS uuid) ORDER BY s.task_plan_id, s.sequence""", params)
    loops = _rows(session, """
        SELECT id, request_id, task_goal_id, active_plan_id, approval_id, status, current_iteration, max_iterations,
               final_result, created_at, started_at, completed_at
        FROM task_loops WHERE project_id = CAST(:project_id AS uuid) ORDER BY created_at DESC LIMIT :limit""", params)
    candidates: list[dict[str, Any]] = []
    if relation_visible(session, "result_acceptance_candidates"):
        candidates = _rows(session, """
            SELECT c.candidate_id, c.loop_id, c.request_id, c.verification_status, c.approval_id, c.created_at,
                   a.status AS approval_status, d.decision, d.decided_at
            FROM result_acceptance_candidates c
            LEFT JOIN control_approvals a ON a.id = c.approval_id
            LEFT JOIN result_acceptance_decisions d ON d.candidate_id = c.candidate_id
            WHERE c.project_id = CAST(:project_id AS uuid) ORDER BY c.created_at DESC LIMIT :limit""", params)
    events = _rows(session, """
        SELECT sequence_number, event_type, payload_json, event_time FROM control_sync_events
        WHERE project_id = CAST(:project_id AS uuid) ORDER BY sequence_number DESC LIMIT :limit""", params)
    steps_by_plan: dict[str, list[dict[str, Any]]] = {}
    for step in steps:
        steps_by_plan.setdefault(str(step.pop("task_plan_id")), []).append(step)
    return {
        "projection": "pados.control.project-work.v1",
        "authority": "PROJECTION_ONLY",
        "project_id": project_id,
        "work_requests": [_request_view(row) for row in requests],
        "approvals": approvals,
        "goals": goals,
        "plans": [{**plan, "steps": steps_by_plan.get(str(plan["id"]), [])} for plan in plans],
        "loops": loops,
        "candidates": candidates,
        "events": [{**event, "payload_json": _audit_payload(event.get("payload_json"))} for event in events],
    }


_PROJECT_JOBS = """
    SELECT i.job_id FROM task_loop_iterations i JOIN task_loops l ON l.id = i.loop_id
    WHERE l.project_id = CAST(:project_id AS uuid) AND i.job_id IS NOT NULL"""


def project_execution(session: Session, project_id: str) -> dict[str, Any]:
    """Iterations, their jobs and the jobs' artifact receipts for one project (PROJECTION ONLY, bounded)."""
    params = {"project_id": project_id, "limit": EXECUTION_LIMIT, "artifact_limit": ARTIFACT_LIMIT}
    iterations = _rows(session, """
        SELECT i.id, i.loop_id, i.iteration_number, i.plan_id, i.step_id, s.sequence AS step_sequence, i.job_id,
               i.status, i.decision, i.decision_reason, i.started_at, i.completed_at
        FROM task_loop_iterations i JOIN task_loops l ON l.id = i.loop_id
        LEFT JOIN task_plan_steps s ON s.id = i.step_id
        WHERE l.project_id = CAST(:project_id AS uuid)
        ORDER BY i.started_at DESC, i.iteration_number DESC LIMIT :limit""", params)
    jobs = _rows(session, f"""
        SELECT j.id, j.request_id, j.status, j.capability_id, j.worker_type, j.exit_code, j.validation_status,
               j.error_code, j.queued_at, j.started_at, j.completed_at
        FROM worker_jobs j WHERE j.project_id = CAST(:project_id AS uuid) AND j.id IN ({_PROJECT_JOBS})
        ORDER BY j.queued_at DESC LIMIT :limit""", params)
    artifacts = _rows(session, f"""
        SELECT a.job_id, a.artifact_type, lower(a.sha256) AS sha256, a.created_at
        FROM worker_job_artifacts a WHERE a.project_id = CAST(:project_id AS uuid) AND a.job_id IN ({_PROJECT_JOBS})
        ORDER BY a.created_at DESC, a.id DESC LIMIT :artifact_limit""", params)
    return {"iterations": iterations, "jobs": jobs, "artifacts": artifacts,
            "truncated": len(iterations) >= EXECUTION_LIMIT or len(jobs) >= EXECUTION_LIMIT
            or len(artifacts) >= ARTIFACT_LIMIT}


def _audit_payload(payload: Any) -> Any:
    """Sync-event payloads as shown: the idempotency fingerprint and folder internals are not needed by a person."""
    if not isinstance(payload, dict):
        return payload
    return {key: value for key, value in payload.items() if key not in {"request_fingerprint"}}


def step_up_readiness(settings: Any, project_id: str) -> dict[str, Any]:
    """Whether a decision on this project's results can happen at all (configuration counts only)."""
    configured = str(getattr(settings, "remote_config", "") or "")
    if not configured:
        return {"remote_console": "NOT_CONFIGURED", "presence_device": "UNKNOWN", "approvers": 0}
    try:
        from pados.remote.config import RemoteConfig

        config = RemoteConfig.load(configured)
    except (OSError, ValueError, TypeError, KeyError):
        return {"remote_console": "CONFIGURATION_UNREADABLE", "presence_device": "UNKNOWN", "approvers": 0}
    approvers = [actor for actor in config.registry.actors() if actor.may_approve and project_id in actor.projects]
    enrolled = sum(1 for actor in approvers if actor.presence_credentials)
    return {
        "remote_console": "CONFIGURED",
        "presence_verifier": "CONFIGURED" if config.presence_rp_id else "NOT_CONFIGURED",
        "presence_device": "ENROLLED" if enrolled and config.presence_rp_id else "NOT_ENROLLED",
        "approvers": len(approvers),
    }


def artifact_content(session: Session, artifact_root: Path, job_id: str, artifact_type: str) -> dict[str, Any]:
    if artifact_type not in ARTIFACT_TEXT_TYPES:
        raise ProductControlError("ARTIFACT_TYPE_NOT_READABLE", 422)
    row = session.execute(
        text("SELECT path, sha256 FROM worker_job_artifacts WHERE job_id = CAST(:id AS uuid) "
             "AND artifact_type = :type ORDER BY created_at DESC, id DESC LIMIT 1"),
        {"id": job_id, "type": artifact_type},
    ).mappings().first()
    if row is None:
        raise ProductControlError("ARTIFACT_NOT_FOUND", 404)
    root = artifact_root.resolve()
    path = Path(str(row["path"])).resolve()
    if root not in path.parents:
        raise ProductControlError("ARTIFACT_PATH_OUTSIDE_ROOT", 403)
    try:
        size = path.stat().st_size
        if size > ARTIFACT_MAX_BYTES:
            raise ProductControlError("ARTIFACT_TOO_LARGE", 413)
        data = path.read_bytes()
    except OSError as exc:
        raise ProductControlError("ARTIFACT_UNAVAILABLE", 409) from exc
    if hashlib.sha256(data).hexdigest() != str(row["sha256"]).lower():
        raise ProductControlError("ARTIFACT_INTEGRITY_MISMATCH", 409)
    content = data.decode("utf-8", errors="replace")
    redacted = contains_secret(content)
    return {"job_id": job_id, "artifact_type": artifact_type, "sha256": str(row["sha256"]).lower(), "size": len(data),
            "integrity": "VERIFIED", "redacted": redacted, "content": "[REDACTED]" if redacted else content}

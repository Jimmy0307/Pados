# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from pados.control.approval_subject import (
    APPROVAL_ACTOR_MISMATCH,
    APPROVAL_REQUEST_MISMATCH,
    APPROVAL_SCOPE_MISMATCH,
    WorkRouteSubject,
    find_operation,
    load_bound_subject,
)
from pados.loop import scope_authority
from pados.loop.amendments import reconcile_residual_work, record_p6_10_execution_result
from pados.loop.engine import (
    DecisionInput,
    calculate_progress_delta,
    decide_next_action,
    ensure_stop_policy_is_bounded,
)
from pados.loop.runtime_bindings import insert_goal_runtime_binding
from pados.loop.schemas import (
    LoopCreate,
    LoopDecision,
    LoopStatus,
    ProgressDelta,
    ProgressSnapshot,
    StepStatus,
    TaskGoalCreate,
    TaskPlanCreate,
)
from pados.work_protocol.models import TaskEnvelope
from pados.worker.schemas import RISK_ORDER, RouteRequest
from pados.worker.service import (
    NON_TERMINAL_JOB_STATUSES,
    expire_overdue_jobs,
    record_job_event,
    route_work_request,
)


_MAX_RESIDUAL_DEPTH = 64


def _stable_json(payload: Any) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


def _hash(payload: Any) -> str:
    return hashlib.sha256(_stable_json(payload).encode("utf-8")).hexdigest()


def _event(session: Session, loop_id: str, project_id: str, event_type: str, payload: dict[str, Any]) -> None:
    session.execute(
        text(
            """
            INSERT INTO task_loop_events(loop_id, project_id, event_type, payload_json)
            VALUES (CAST(:loop_id AS uuid), CAST(:project_id AS uuid), :event_type, CAST(:payload AS jsonb))
            """
        ),
        {"loop_id": loop_id, "project_id": project_id, "event_type": event_type, "payload": _stable_json(payload)},
    )


def _approved_approval(session: Session, approval_id: str, project_id: str) -> dict[str, Any]:
    row = session.execute(
        text("SELECT * FROM control_approvals WHERE id = CAST(:id AS uuid)"),
        {"id": approval_id},
    ).mappings().one()
    if str(row["project_id"]) != project_id:
        raise PermissionError("cross-project approval rejected")
    if row["status"] != "APPROVED":
        raise PermissionError("approval is not approved")
    return dict(row)


def _bound_subject(approval: dict[str, Any]) -> WorkRouteSubject:
    """The approval's verified WORK_ROUTE subject; unbound or tampered approvals fail closed."""
    return load_bound_subject(approval.get("subject_kind"), approval.get("subject_json"), approval["proposal_hash"])


def _assert_plan_covered(subject: WorkRouteSubject, *, request_id: str, steps: list[Any]) -> None:
    """Every plan step must be an operation the approval subject names (defense in depth)."""
    if subject.request_id != request_id:
        raise PermissionError(APPROVAL_REQUEST_MISMATCH)
    if "TASK_LOOP" not in subject.actor.surfaces:
        raise PermissionError(APPROVAL_ACTOR_MISMATCH)
    for step in steps:
        operation = find_operation(
            subject,
            step_sequence=step.sequence,
            capability_id=step.required_capability,
            command_id=step.command_id,
        )
        if operation is None or RISK_ORDER[step.risk_level] > RISK_ORDER[operation.max_risk_level]:
            raise PermissionError(APPROVAL_SCOPE_MISMATCH)


def _root_step_sequence(session: Session, plan_id: str, step: dict[str, Any]) -> int:
    """Follow residual amendments to the root plan step; residual children inherit its operation."""
    current_id = str(step["id"])
    sequence = int(step["sequence"])
    visited = {current_id}
    for _ in range(_MAX_RESIDUAL_DEPTH):
        parent = session.execute(
            text(
                """
                SELECT s.id::text AS id, s.sequence
                FROM residual_plan_amendments a
                JOIN task_plan_steps s ON s.id = a.parent_step_id AND s.task_plan_id = a.plan_id
                WHERE a.plan_id = CAST(:plan_id AS uuid) AND a.step_id = CAST(:step_id AS uuid)
                """
            ),
            {"plan_id": plan_id, "step_id": current_id},
        ).mappings().first()
        if parent is None:
            return sequence
        current_id = str(parent["id"])
        if current_id in visited:
            raise ValueError("RESIDUAL_STEP_LINEAGE_CYCLE")
        visited.add(current_id)
        sequence = int(parent["sequence"])
    raise ValueError("RESIDUAL_STEP_LINEAGE_TOO_DEEP")


def _task_envelope_for_request(
    session: Session,
    *,
    request_id: str,
    project_id: str,
    required: bool,
) -> TaskEnvelope | None:
    request = session.execute(
        text("SELECT body FROM control_work_requests WHERE id = CAST(:id AS uuid)"),
        {"id": request_id},
    ).mappings().one()
    try:
        body = json.loads(str(request["body"]))
    except (TypeError, ValueError) as exc:
        if required:
            raise ValueError("TASK_ENVELOPE_REQUIRED_FOR_RUNTIME_BINDING") from exc
        return None
    value = body.get("task_envelope") if isinstance(body, dict) else None
    if value is None:
        if required:
            raise ValueError("TASK_ENVELOPE_REQUIRED_FOR_RUNTIME_BINDING")
        return None
    envelope = TaskEnvelope.model_validate(value)
    if body.get("task_envelope_hash") != envelope.content_hash:
        raise ValueError("TASK_ENVELOPE_HASH_MISMATCH")
    if envelope.project_ref != project_id:
        raise PermissionError("TASK_ENVELOPE_PROJECT_MISMATCH")
    return envelope


def create_task_goal(session: Session, payload: TaskGoalCreate) -> dict[str, Any]:
    request = session.execute(
        text("SELECT * FROM control_work_requests WHERE id = CAST(:id AS uuid) FOR UPDATE"),
        {"id": payload.request_id},
    ).mappings().one()
    if str(request["project_id"]) != payload.project_id:
        raise PermissionError("cross-project goal rejected")
    _approved_approval(session, payload.approval_id, payload.project_id)
    content = payload.model_dump(mode="json", exclude={"idempotency_key"})
    row = session.execute(
        text(
            """
            INSERT INTO task_goals(
              project_id, request_id, approval_id, objective, success_criteria, constraints_json,
              scope_paths, prohibited_actions, risk_level, status, content_hash, idempotency_key,
              approved_at
            )
            VALUES (
              CAST(:project_id AS uuid), CAST(:request_id AS uuid), CAST(:approval_id AS uuid),
              :objective, CAST(:success_criteria AS jsonb), CAST(:constraints AS jsonb),
              CAST(:scope_paths AS jsonb), CAST(:prohibited_actions AS jsonb), :risk_level,
              'APPROVED', :content_hash, :idempotency_key, now()
            )
            ON CONFLICT (request_id, idempotency_key) DO UPDATE SET objective = task_goals.objective
            RETURNING *
            """
        ),
        {
            "project_id": payload.project_id,
            "request_id": payload.request_id,
            "approval_id": payload.approval_id,
            "objective": payload.objective,
            "success_criteria": _stable_json(payload.success_criteria),
            "constraints": _stable_json(payload.constraints),
            "scope_paths": _stable_json(payload.scope_paths),
            "prohibited_actions": _stable_json(payload.prohibited_actions),
            "risk_level": payload.risk_level.value,
            "content_hash": _hash(content),
            "idempotency_key": payload.idempotency_key,
        },
    ).mappings().one()
    return dict(row)


def create_task_plan(
    session: Session, payload: TaskPlanCreate, *, settings: Any = None
) -> dict[str, Any]:
    goal = get_task_goal(session, payload.task_goal_id)
    if goal["status"] not in {"APPROVED", "ACTIVE"}:
        raise PermissionError("goal must be approved before planning")
    approval = _approved_approval(session, payload.approval_id, str(goal["project_id"]))
    _assert_plan_covered(_bound_subject(approval), request_id=str(goal["request_id"]), steps=payload.steps)
    ensure_stop_policy_is_bounded(payload.stop_policy)
    product_goal_ids = [step.product_goal_id for step in payload.steps if step.product_goal_id is not None]
    if len(product_goal_ids) != len(set(product_goal_ids)):
        raise ValueError("PRODUCT_GOAL_RUNTIME_BINDING_DUPLICATE")
    task_envelope = _task_envelope_for_request(
        session,
        request_id=str(goal["request_id"]),
        project_id=str(goal["project_id"]),
        required=bool(product_goal_ids),
    )
    if task_envelope is not None and product_goal_ids:
        envelope_goal_ids = {item.goal_id for item in task_envelope.normalized_goals}
        if not set(product_goal_ids).issubset(envelope_goal_ids):
            raise ValueError("PRODUCT_GOAL_RUNTIME_BINDING_UNKNOWN_GOAL")
        project = session.execute(
            text("SELECT classification FROM projects WHERE id = CAST(:id AS uuid) FOR SHARE"),
            {"id": str(goal["project_id"])},
        ).mappings().one()
        if str(project["classification"]).upper() != task_envelope.classification.value:
            raise PermissionError("TASK_ENVELOPE_CLASSIFICATION_MISMATCH")
        expected_scope_version = scope_authority.read_current_scope_version(
            settings, str(goal["project_id"])
        )
        if expected_scope_version is None or task_envelope.scope_version_ref != f"scope:{expected_scope_version}":
            raise PermissionError("TASK_ENVELOPE_SCOPE_VERSION_UNAVAILABLE_OR_STALE")
    content = payload.model_dump(mode="json", exclude={"idempotency_key"})
    content_hash = _hash(content)
    row = session.execute(
        text(
            """
            INSERT INTO task_plans(
              task_goal_id, approval_id, version, status, assumptions, stop_policy,
              validation_policy, rollback_policy, content_hash, idempotency_key, approved_at
            )
            VALUES (
              CAST(:task_goal_id AS uuid), CAST(:approval_id AS uuid), 1, 'APPROVED',
              CAST(:assumptions AS jsonb), CAST(:stop_policy AS jsonb),
              CAST(:validation_policy AS jsonb), CAST(:rollback_policy AS jsonb),
              :content_hash, :idempotency_key, now()
            )
            ON CONFLICT (task_goal_id, idempotency_key) DO UPDATE SET status = task_plans.status
            RETURNING *
            """
        ),
        {
            "task_goal_id": payload.task_goal_id,
            "approval_id": payload.approval_id,
            "assumptions": _stable_json(payload.assumptions),
            "stop_policy": payload.stop_policy.model_dump_json(),
            "validation_policy": _stable_json(payload.validation_policy),
            "rollback_policy": _stable_json(payload.rollback_policy),
            "content_hash": content_hash,
            "idempotency_key": payload.idempotency_key,
        },
    ).mappings().one()
    if str(row["content_hash"]) != content_hash:
        raise ValueError("TASK_PLAN_IDEMPOTENCY_CONTENT_CONFLICT")
    plan_id = str(row["id"])
    existing_steps: Any = session.execute(
        text("SELECT count(*) FROM task_plan_steps WHERE task_plan_id = CAST(:id AS uuid)"),
        {"id": plan_id},
    ).scalar_one()
    if int(existing_steps) == 0:
        for step in payload.steps:
            inserted_step = session.execute(
                text(
                    """
                    INSERT INTO task_plan_steps(
                      task_plan_id, sequence, title, description, step_type, required_capability,
                      preferred_worker_types, dependencies, allowed_paths, allowed_actions,
                      prohibited_actions, context_requirements, input_artifacts, output_contract,
                      validation_policy_id, command_id, success_condition, failure_policy,
                      timeout_seconds, risk_level, approval_requirement, status
                    )
                    VALUES (
                      CAST(:task_plan_id AS uuid), :sequence, :title, :description, :step_type,
                      :required_capability, CAST(:preferred_worker_types AS jsonb),
                      CAST(:dependencies AS jsonb), CAST(:allowed_paths AS jsonb),
                      CAST(:allowed_actions AS jsonb), CAST(:prohibited_actions AS jsonb),
                      CAST(:context_requirements AS jsonb), CAST(:input_artifacts AS jsonb),
                      CAST(:output_contract AS jsonb), :validation_policy_id, :command_id,
                      :success_condition, :failure_policy, :timeout_seconds, :risk_level,
                      :approval_requirement, :status
                    )
                    RETURNING id
                    """
                ),
                {
                    "task_plan_id": plan_id,
                    "sequence": step.sequence,
                    "title": step.title,
                    "description": step.description,
                    "step_type": step.step_type.value,
                    "required_capability": step.required_capability.value,
                    "preferred_worker_types": _stable_json([item.value for item in step.preferred_worker_types]),
                    "dependencies": _stable_json(step.dependencies),
                    "allowed_paths": _stable_json(step.allowed_paths),
                    "allowed_actions": _stable_json(step.allowed_actions),
                    "prohibited_actions": _stable_json(step.prohibited_actions),
                    "context_requirements": _stable_json(step.context_requirements),
                    "input_artifacts": _stable_json(step.input_artifacts),
                    "output_contract": _stable_json(step.output_contract),
                    "validation_policy_id": step.validation_policy_id,
                    "command_id": step.command_id,
                    "success_condition": step.success_condition,
                    "failure_policy": step.failure_policy,
                    "timeout_seconds": step.timeout_seconds,
                    "risk_level": step.risk_level.value,
                    "approval_requirement": step.approval_requirement,
                    "status": "READY" if not step.dependencies else "PENDING",
                },
            ).mappings().one()
            if step.product_goal_id is not None and task_envelope is not None:
                insert_goal_runtime_binding(
                    session,
                    task_id=task_envelope.task_id,
                    goal_id=step.product_goal_id,
                    plan_id=plan_id,
                    step_id=str(inserted_step["id"]),
                    provenance_hash=_hash(
                        {
                            "task_envelope_hash": task_envelope.content_hash,
                            "plan_content_hash": content_hash,
                            "product_goal_id": step.product_goal_id,
                        }
                    ),
                )
    elif task_envelope is not None and product_goal_ids:
        existing_bindings = session.execute(
            text(
                """
                SELECT b.goal_id, s.sequence
                FROM goal_runtime_bindings b
                JOIN task_plan_steps s ON s.id = b.step_id
                WHERE b.plan_id = CAST(:plan_id AS uuid)
                """
            ),
            {"plan_id": plan_id},
        ).mappings().all()
        actual = {int(item["sequence"]): str(item["goal_id"]) for item in existing_bindings}
        expected = {
            step.sequence: step.product_goal_id
            for step in payload.steps
            if step.product_goal_id is not None
        }
        if any(actual.get(sequence) != goal_id for sequence, goal_id in expected.items()):
            raise ValueError("GOAL_RUNTIME_BINDING_IDEMPOTENCY_CONFLICT")
    return dict(row)


def create_loop(session: Session, payload: LoopCreate) -> dict[str, Any]:
    goal = get_task_goal(session, payload.task_goal_id)
    plan = get_task_plan(session, payload.active_plan_id)
    if str(plan["task_goal_id"]) != str(goal["id"]):
        raise PermissionError("plan does not belong to goal")
    if goal["status"] != "APPROVED" or plan["status"] != "APPROVED":
        raise PermissionError("goal and plan must be approved")
    approval = _approved_approval(session, payload.approval_id, str(goal["project_id"]))
    if "TASK_LOOP" not in _bound_subject(approval).actor.surfaces:
        raise PermissionError(APPROVAL_ACTOR_MISMATCH)
    row = session.execute(
        text(
            """
            INSERT INTO task_loops(
              task_goal_id, active_plan_id, project_id, request_id, approval_id, status,
              max_iterations, max_replans, max_worker_failures, max_execution_seconds,
              no_progress_limit, context_token_budget, idempotency_key
            )
            VALUES (
              CAST(:task_goal_id AS uuid), CAST(:active_plan_id AS uuid), CAST(:project_id AS uuid),
              CAST(:request_id AS uuid), CAST(:approval_id AS uuid), 'READY',
              :max_iterations, :max_replans, :max_worker_failures, :max_execution_seconds,
              :no_progress_limit, :context_token_budget, :idempotency_key
            )
            ON CONFLICT (task_goal_id, idempotency_key) DO UPDATE SET status = task_loops.status
            RETURNING *
            """
        ),
        {
            "task_goal_id": payload.task_goal_id,
            "active_plan_id": payload.active_plan_id,
            "project_id": str(goal["project_id"]),
            "request_id": str(goal["request_id"]),
            "approval_id": payload.approval_id,
            "max_iterations": payload.max_iterations,
            "max_replans": payload.max_replans,
            "max_worker_failures": payload.max_worker_failures,
            "max_execution_seconds": payload.max_execution_seconds,
            "no_progress_limit": payload.no_progress_limit,
            "context_token_budget": payload.context_token_budget,
            "idempotency_key": payload.idempotency_key,
        },
    ).mappings().one()
    conflicting_bindings: Any = session.execute(
        text(
            """
            SELECT count(*) FROM goal_runtime_bindings
            WHERE plan_id = CAST(:plan_id AS uuid)
              AND loop_id IS NOT NULL
              AND loop_id <> CAST(:loop_id AS uuid)
            """
        ),
        {"plan_id": payload.active_plan_id, "loop_id": str(row["id"])},
    ).scalar_one()
    if int(conflicting_bindings) > 0:
        raise PermissionError("GOAL_RUNTIME_PLAN_ALREADY_BOUND_TO_OTHER_LOOP")
    session.execute(
        text(
            """
            UPDATE goal_runtime_bindings
            SET loop_id = CAST(:loop_id AS uuid)
            WHERE plan_id = CAST(:plan_id AS uuid) AND loop_id IS NULL
            """
        ),
        {"loop_id": str(row["id"]), "plan_id": payload.active_plan_id},
    )
    _event(session, str(row["id"]), str(row["project_id"]), "LOOP_CREATED", {"loop_id": str(row["id"])})
    return dict(row)


def start_loop(session: Session, loop_id: str, *, owner_instance: str = "pados-cli") -> dict[str, Any]:
    loop = get_loop(session, loop_id)
    if loop["status"] == "RUNNING":
        return loop
    if loop["status"] != "READY":
        raise ValueError("loop must be READY before start")
    row = session.execute(
        text(
            """
            UPDATE task_loops
            SET status = 'RUNNING', started_at = COALESCE(started_at, now())
            WHERE id = CAST(:id AS uuid) AND status = 'READY'
            RETURNING *
            """
        ),
        {"id": loop_id},
    ).mappings().one()
    session.execute(
        text(
            """
            INSERT INTO task_loop_leases(loop_id, owner_instance, lease_expires_at)
            VALUES (CAST(:loop_id AS uuid), :owner, now() + interval '5 minutes')
            ON CONFLICT (loop_id) DO UPDATE
            SET owner_instance = EXCLUDED.owner_instance,
                lease_acquired_at = now(),
                lease_expires_at = now() + interval '5 minutes',
                heartbeat_at = now()
            WHERE task_loop_leases.lease_expires_at < now()
               OR task_loop_leases.owner_instance = EXCLUDED.owner_instance
            """
        ),
        {"loop_id": loop_id, "owner": owner_instance},
    )
    _event(session, loop_id, str(row["project_id"]), "LOOP_STARTED", {"owner": owner_instance})
    return dict(row)


def run_one_iteration(
    session: Session,
    *,
    loop_id: str,
    settings: Any,
    artifact_root: Path,
    idempotency_key: str,
) -> dict[str, Any]:
    loop = get_loop_for_update(session, loop_id)
    if loop["status"] != "RUNNING":
        previous = session.execute(
            text(
                """
                SELECT * FROM task_loop_iterations
                WHERE loop_id = CAST(:loop_id AS uuid) AND idempotency_key = :idempotency_key
                """
            ),
            {"loop_id": loop_id, "idempotency_key": idempotency_key},
        ).mappings().first()
        if previous is not None and previous["status"] in {"DECIDED", "FAILED"}:
            return _replay_decided_iteration(session, loop, dict(previous))
        raise ValueError("loop must be RUNNING to step")
    active_iteration = session.execute(
        text(
            """
            SELECT * FROM task_loop_iterations
            WHERE loop_id = CAST(:loop_id AS uuid)
              AND status IN ('CREATED', 'ROUTING', 'RUNNING', 'VALIDATING')
            ORDER BY iteration_number DESC
            LIMIT 1
            FOR UPDATE
            """
        ),
        {"loop_id": loop_id},
    ).mappings().first()
    if active_iteration is not None:
        active = dict(active_iteration)
        if active["status"] == "RUNNING" and active.get("job_id") is not None:
            job_id = str(active["job_id"])
            expire_overdue_jobs(session, str(loop["project_id"]))
            job_row = session.execute(
                text("SELECT * FROM worker_jobs WHERE id = CAST(:id AS uuid)"), {"id": job_id}
            ).mappings().one()
            job = dict(job_row)
            if str(job["status"]) in NON_TERMINAL_JOB_STATUSES:
                return _waiting_for_job(session, loop, active, job)
            return _complete_routed_iteration(
                session,
                loop=loop,
                iteration=active,
                job=job,
                settings=settings,
                artifact_root=artifact_root,
            )
        raise PermissionError("duplicate active iteration rejected")
    previous = session.execute(
        text(
            """
            SELECT * FROM task_loop_iterations
            WHERE loop_id = CAST(:loop_id AS uuid) AND idempotency_key = :idempotency_key
            """
        ),
        {"loop_id": loop_id, "idempotency_key": idempotency_key},
    ).mappings().first()
    if previous is not None:
        return _replay_decided_iteration(session, loop, dict(previous))
    step = _select_ready_step(session, str(loop["active_plan_id"]))
    if step is None:
        states: Any = session.execute(
            text("SELECT status FROM task_plan_steps WHERE task_plan_id = CAST(:id AS uuid)"),
            {"id": str(loop["active_plan_id"])},
        ).scalars().all()
        if states and all(str(status) == "PASSED" for status in states):
            return _complete_loop(session, loop, "all plan steps passed")
        return _block_loop(session, loop, "no ready step; dependencies or failed work remain")
    revalidation_failure = _product_step_revalidation_failure(
        session, loop=loop, step=step, settings=settings
    )
    if revalidation_failure is not None:
        row = session.execute(
            text(
                """
                UPDATE task_loops
                SET status = 'WAITING_HUMAN',
                    final_result = CAST(:result AS jsonb)
                WHERE id = CAST(:id AS uuid)
                RETURNING *
                """
            ),
            {
                "id": loop_id,
                "result": _stable_json(
                    {"decision": "WAIT_FOR_HUMAN", "reason": revalidation_failure}
                ),
            },
        ).mappings().one()
        _event(
            session,
            loop_id,
            str(loop["project_id"]),
            "RESIDUAL_EXECUTION_REVALIDATION_FAILED",
            {"step_id": str(step["id"]), "reason": revalidation_failure},
        )
        return {
            "loop": dict(row),
            "decision": LoopDecision.WAIT_FOR_HUMAN.value,
            "decision_reason": revalidation_failure,
            "status": LoopStatus.WAITING_HUMAN.value,
        }

    iteration_number = int(loop["current_iteration"]) + 1
    before = _snapshot_from_db(session, loop, iteration_id=None)
    before_id = _insert_snapshot(session, loop_id, None, before)
    iteration = session.execute(
        text(
            """
            INSERT INTO task_loop_iterations(
              loop_id, iteration_number, plan_id, step_id, status, before_snapshot_id, idempotency_key
            )
            VALUES (
              CAST(:loop_id AS uuid), :iteration_number, CAST(:plan_id AS uuid),
              CAST(:step_id AS uuid), 'ROUTING', CAST(:before_snapshot_id AS uuid), :idempotency_key
            )
            ON CONFLICT (loop_id, idempotency_key) DO UPDATE SET status = task_loop_iterations.status
            RETURNING *
            """
        ),
        {
            "loop_id": loop_id,
            "iteration_number": iteration_number,
            "plan_id": str(loop["active_plan_id"]),
            "step_id": str(step["id"]),
            "before_snapshot_id": before_id,
            "idempotency_key": idempotency_key,
        },
    ).mappings().one()
    iteration_id = str(iteration["id"])
    _event(session, loop_id, str(loop["project_id"]), "ITERATION_CREATED", {"iteration_id": iteration_id})
    _event(session, loop_id, str(loop["project_id"]), "STEP_SELECTED", {"step_id": str(step["id"])})
    route = RouteRequest(
        approval_id=str(loop["approval_id"]),
        capability_id=step["required_capability"],
        risk_level=step["risk_level"],
        context_query=str(step["title"]),
        allowed_actions=list(step["allowed_actions"] or []),
        prohibited_actions=list(step["prohibited_actions"] or []),
        allowed_paths=list(step["allowed_paths"] or []),
        validation_policy_id=str(step["validation_policy_id"]),
        command_id=step["command_id"],
        timeout_seconds=int(step["timeout_seconds"]),
        idempotency_key=f"loop-{iteration_id}",
        surface="TASK_LOOP",
        step_sequence=_root_step_sequence(session, str(loop["active_plan_id"]), step),
    )
    session.execute(
        text("UPDATE task_plan_steps SET status = 'ROUTING', attempt_count = attempt_count + 1 WHERE id = :id"),
        {"id": step["id"]},
    )
    _event(session, loop_id, str(loop["project_id"]), "ITERATION_CONTEXT_ASSEMBLED", {"step_id": str(step["id"])})
    routed = route_work_request(
        session,
        request_id=str(loop["request_id"]),
        route=route,
        settings=settings,
        artifact_root=artifact_root,
    )
    job = routed["job"]
    job_status = str(job["status"])
    _event(session, loop_id, str(loop["project_id"]), "JOB_ROUTED", {"job_id": str(job["id"])})
    if job_status in NON_TERMINAL_JOB_STATUSES:
        session.execute(
            text("UPDATE task_plan_steps SET status = 'RUNNING' WHERE id = CAST(:id AS uuid)"),
            {"id": str(step["id"])},
        )
        session.execute(
            text(
                "UPDATE task_loop_iterations SET status = 'RUNNING', job_id = CAST(:job_id AS uuid) "
                "WHERE id = CAST(:id AS uuid)"
            ),
            {"id": iteration_id, "job_id": str(job["id"])},
        )
        _event(
            session,
            loop_id,
            str(loop["project_id"]),
            "JOB_WAITING",
            {"iteration_id": iteration_id, "job_id": str(job["id"]), "job_status": job_status},
        )
        return _waiting_for_job(session, loop, dict(iteration), job)
    return _complete_routed_iteration(
        session,
        loop=loop,
        iteration=dict(iteration),
        job=job,
        settings=settings,
        artifact_root=artifact_root,
    )


def _waiting_for_job(
    session: Session, loop: dict[str, Any], iteration: dict[str, Any], job: dict[str, Any]
) -> dict[str, Any]:
    return {
        "loop": get_loop(session, str(loop["id"])),
        "status": LoopStatus.RUNNING.value,
        "decision": "WAIT_FOR_JOB",
        "iteration_id": str(iteration["id"]),
        "job_id": str(job["id"]),
        "job_status": str(job["status"]),
    }


def _load_snapshot(session: Session, snapshot_id: str) -> ProgressSnapshot:
    row = session.execute(
        text(
            """
            SELECT acceptance_criteria_total, acceptance_criteria_met, tests_total, tests_passed,
                   tests_failed, blocker_count, artifact_count, validation_status, scope_hash, state_hash
            FROM task_progress_snapshots WHERE id = CAST(:id AS uuid)
            """
        ),
        {"id": snapshot_id},
    ).mappings().one()
    return ProgressSnapshot.model_validate(dict(row))


def _complete_routed_iteration(
    session: Session,
    *,
    loop: dict[str, Any],
    iteration: dict[str, Any],
    job: dict[str, Any],
    settings: Any,
    artifact_root: Path,
) -> dict[str, Any]:
    """Finish a terminal worker result through the normal receipt and decision path."""
    loop_id = str(loop["id"])
    iteration_id = str(iteration["id"])
    iteration_number = int(iteration["iteration_number"])
    job_id = str(job["id"])
    job_status = str(job["status"])
    step = dict(
        session.execute(
            text("SELECT * FROM task_plan_steps WHERE id = CAST(:id AS uuid)"),
            {"id": str(iteration["step_id"])},
        ).mappings().one()
    )
    step["attempt_count"] = max(0, int(step["attempt_count"]) - 1)
    step_status = {
        "SUCCEEDED": StepStatus.PASSED,
        "TIMED_OUT": StepStatus.TIMED_OUT,
        "BLOCKED": StepStatus.BLOCKED,
    }.get(job_status, StepStatus.FAILED)
    validation_status = "PASSED" if job_status == "SUCCEEDED" else "FAILED"
    session.execute(
        text("UPDATE task_plan_steps SET status = :status WHERE id = CAST(:id AS uuid)"),
        {"id": str(step["id"]), "status": step_status.value},
    )
    _event(session, loop_id, str(loop["project_id"]), "JOB_COMPLETED", {"status": job_status})
    task_envelope = _task_envelope_for_request(
        session,
        request_id=str(loop["request_id"]),
        project_id=str(loop["project_id"]),
        required=False,
    )
    capability_result = record_p6_10_execution_result(
        session,
        loop=loop,
        step=step,
        iteration=iteration,
        job=job,
        artifact_root=artifact_root,
        task_envelope=task_envelope,
    )
    amendments = (
        reconcile_residual_work(session, loop_id, settings=settings)
        if capability_result is not None
        else ()
    )
    reapproval_required = any(str(item.get("authority_decision")) == "REAPPROVAL_REQUIRED" for item in amendments)
    before_id = iteration.get("before_snapshot_id")
    if before_id is None:
        raise ValueError("active iteration is missing its before snapshot")
    before = _load_snapshot(session, str(before_id))
    after = _snapshot_from_db(session, loop, iteration_id=iteration_id, validation_status=validation_status)
    after_id = _insert_snapshot(session, loop_id, iteration_id, after)
    delta = calculate_progress_delta(before, after)
    delta_id = _insert_delta(session, iteration_id, delta)
    no_progress, negative = _current_streaks(session, loop_id, delta.classification.value)
    decision, reason, next_status = decide_next_action(
        DecisionInput(
            validation_status=validation_status,
            step_status=step_status,
            delta=delta,
            all_success_criteria_met=_all_steps_passed(session, str(loop["active_plan_id"])),
            unresolved_blockers=after.blocker_count,
            attempt_number=int(step["attempt_count"]) + 1,
            worker_failures=_worker_failures(session, loop_id),
            current_iteration=iteration_number,
            no_progress_streak=no_progress,
            negative_progress_streak=negative,
            stop_policy=_stop_policy_from_loop(loop),
            external_dependency_missing=str(step["failure_policy"]) == "external_dependency_missing",
            transient_failure=str(step["failure_policy"]) == "retry_transient",
            retry_allowed=str(step["failure_policy"]) == "retry_transient",
        )
    )
    if reapproval_required:
        decision, reason, next_status = (
            LoopDecision.WAIT_FOR_HUMAN,
            "residual plan amendment requires renewed approval",
            LoopStatus.WAITING_HUMAN,
        )
    _persist_decision(
        session,
        loop=loop,
        iteration_id=iteration_id,
        iteration_number=iteration_number,
        job_id=job_id,
        after_id=after_id,
        delta_id=delta_id,
        decision=decision,
        reason=reason,
        next_status=next_status,
    )
    return {
        "loop": get_loop(session, loop_id),
        "iteration_id": iteration_id,
        "job": job,
        "progress_delta": delta.model_dump(mode="json"),
        "decision": decision.value,
        "decision_reason": reason,
        "status": next_status.value,
    }


def _replay_decided_iteration(
    session: Session, loop: dict[str, Any], iteration: dict[str, Any]
) -> dict[str, Any]:
    decision = iteration.get("decision")
    reason = iteration.get("decision_reason")
    decision_status = {
        "CONTINUE": LoopStatus.RUNNING.value,
        "ADVANCE_STEP": LoopStatus.RUNNING.value,
        "RETRY_SAME_STEP": LoopStatus.RUNNING.value,
        "WAIT_FOR_HUMAN": LoopStatus.WAITING_HUMAN.value,
        "PAUSE": LoopStatus.PAUSED.value,
        "BLOCK": LoopStatus.BLOCKED.value,
        "COMPLETE": LoopStatus.COMPLETED.value,
        "PARTIAL_COMPLETE": LoopStatus.PARTIALLY_COMPLETED.value,
        "FAIL_RECOVERABLE": LoopStatus.FAILED_RECOVERABLE.value,
        "FAIL_TERMINAL": LoopStatus.FAILED_TERMINAL.value,
        "CANCEL": LoopStatus.CANCELLED.value,
    }.get(str(decision), str(loop["status"]))
    loop_result = get_loop(session, str(loop["id"]))
    loop_result.update(
        status=decision_status,
        current_iteration=int(iteration["iteration_number"]),
        final_result={"decision": decision, "reason": reason},
    )
    loop_result["completed_at"] = iteration.get("completed_at") if decision_status in {
        LoopStatus.COMPLETED.value,
        LoopStatus.PARTIALLY_COMPLETED.value,
        LoopStatus.CANCELLED.value,
        LoopStatus.FAILED_TERMINAL.value,
        LoopStatus.TIMED_OUT.value,
    } else None
    job = None
    if iteration.get("job_id") is not None:
        row = session.execute(
            text("SELECT * FROM worker_jobs WHERE id = CAST(:id AS uuid)"),
            {"id": str(iteration["job_id"])},
        ).mappings().first()
        job = dict(row) if row is not None else None
    delta = None
    if iteration.get("progress_delta_id") is not None:
        row = session.execute(
            text(
                """
                SELECT criteria_met_delta, tests_passed_delta, tests_failed_delta, blocker_delta,
                       artifact_delta, scope_changed, state_changed, progress_score, classification
                FROM task_progress_deltas WHERE id = CAST(:id AS uuid)
                """
            ),
            {"id": str(iteration["progress_delta_id"])},
        ).mappings().first()
        if row is not None:
            values = dict(row)
            delta = ProgressDelta(
                criteria_met_delta=values["criteria_met_delta"],
                tests_passed_delta=values["tests_passed_delta"],
                tests_failed_delta=values["tests_failed_delta"],
                blocker_delta=values["blocker_delta"],
                artifact_delta=values["artifact_delta"],
                scope_changed=values["scope_changed"],
                state_changed=values["state_changed"],
                progress_score=values["progress_score"],
                classification=values["classification"],
            ).model_dump(mode="json")
    return {
        "loop": loop_result,
        "iteration_id": str(iteration["id"]),
        "job": job,
        "progress_delta": delta,
        "decision": decision,
        "decision_reason": reason,
        "status": decision_status,
    }


def _select_ready_step(session: Session, plan_id: str) -> dict[str, Any] | None:
    rows = session.execute(
        text(
            """
            SELECT * FROM task_plan_steps
            WHERE task_plan_id = CAST(:plan_id AS uuid)
            ORDER BY sequence ASC
            """
        ),
        {"plan_id": plan_id},
    ).mappings().all()
    passed_sequences = {int(row["sequence"]) for row in rows if row["status"] == "PASSED"}
    for row in rows:
        if row["status"] not in {"READY", "PENDING"}:
            continue
        dependencies = list(row["dependencies"] or [])
        if all(int(dep) in passed_sequences for dep in dependencies):
            return dict(row)
    return None


def _product_step_revalidation_failure(
    session: Session, *, loop: dict[str, Any], step: dict[str, Any], settings: Any
) -> str | None:
    binding = session.execute(
        text(
            """
            SELECT task_id, goal_id FROM goal_runtime_bindings
            WHERE step_id = CAST(:step_id AS uuid)
              AND plan_id = CAST(:plan_id AS uuid)
              AND loop_id = CAST(:loop_id AS uuid)
            """
        ),
        {"step_id": str(step["id"]), "plan_id": str(loop["active_plan_id"]), "loop_id": str(loop["id"])},
    ).mappings().first()
    if binding is None:
        return None
    envelope = _task_envelope_for_request(
        session,
        request_id=str(loop["request_id"]),
        project_id=str(loop["project_id"]),
        required=True,
    )
    assert envelope is not None
    product_goal = next((goal for goal in envelope.normalized_goals if goal.goal_id == str(binding["goal_id"])), None)
    if str(binding["task_id"]) != envelope.task_id or product_goal is None:
        return "TASK_OR_GOAL_BINDING_INVALID"
    if str(step["required_capability"]) not in product_goal.required_capabilities:
        return "CAPABILITY_BINDING_INVALID"
    authority = session.execute(
        text(
            """
            SELECT g.approval_id::text AS goal_approval_id, p.approval_id::text AS plan_approval_id,
                   l.approval_id::text AS loop_approval_id, a.id::text AS approval_id,
                   a.project_id::text AS approval_project_id, a.status AS approval_status,
                   pr.classification, pr.status AS project_status
            FROM task_loops l
            JOIN task_goals g ON g.id = l.task_goal_id
            JOIN task_plans p ON p.id = l.active_plan_id
            JOIN control_approvals a ON a.id = l.approval_id
            JOIN projects pr ON pr.id = l.project_id
            WHERE l.id = CAST(:loop_id AS uuid)
            """
        ),
        {"loop_id": str(loop["id"])},
    ).mappings().one()
    if (
        authority["approval_status"] != "APPROVED"
        or str(authority["project_status"]).upper() != "ACTIVE"
        or str(authority["approval_id"]) != str(loop["approval_id"])
        or str(authority["goal_approval_id"]) != str(loop["approval_id"])
        or str(authority["plan_approval_id"]) != str(loop["approval_id"])
        or str(authority["loop_approval_id"]) != str(loop["approval_id"])
        or str(authority["approval_project_id"]) != str(loop["project_id"])
    ):
        return "APPROVAL_OR_PROJECT_AUTHORITY_STALE"
    classification = str(authority["classification"]).upper()
    if classification != envelope.classification.value:
        return "CLASSIFICATION_AUTHORITY_CHANGED"
    current_version = scope_authority.read_current_scope_version(settings, str(loop["project_id"]))
    if current_version is None:
        return "SCOPE_VERSION_AUTHORITY_UNAVAILABLE"
    if envelope.scope_version_ref != f"scope:{current_version}":
        return "SCOPE_VERSION_STALE"
    return None


def _snapshot_from_db(
    session: Session,
    loop: dict[str, Any],
    *,
    iteration_id: str | None,
    validation_status: str = "NOT_RUN",
) -> ProgressSnapshot:
    steps = session.execute(
        text("SELECT status FROM task_plan_steps WHERE task_plan_id = CAST(:id AS uuid)"),
        {"id": str(loop["active_plan_id"])},
    ).mappings().all()
    artifacts: Any = session.execute(
        text(
            """
            SELECT count(*) FROM worker_job_artifacts
            WHERE project_id = CAST(:project_id AS uuid)
            """
        ),
        {"project_id": str(loop["project_id"])},
    ).scalar_one()
    total = len(steps)
    passed = sum(1 for row in steps if row["status"] == "PASSED")
    failed = sum(1 for row in steps if row["status"] in {"FAILED", "BLOCKED", "TIMED_OUT"})
    scope_hash = _hash({"goal": str(loop["task_goal_id"]), "plan": str(loop["active_plan_id"])})
    state_hash = _hash({"passed": passed, "failed": failed, "iteration": iteration_id})
    return ProgressSnapshot(
        acceptance_criteria_total=total,
        acceptance_criteria_met=passed,
        tests_total=total,
        tests_passed=passed,
        tests_failed=failed,
        blocker_count=failed,
        artifact_count=int(artifacts),
        validation_status=validation_status,
        scope_hash=scope_hash,
        state_hash=state_hash,
    )


def _insert_snapshot(session: Session, loop_id: str, iteration_id: str | None, snapshot: ProgressSnapshot) -> str:
    row: Any = session.execute(
        text(
            """
            INSERT INTO task_progress_snapshots(
              loop_id, iteration_id, acceptance_criteria_total, acceptance_criteria_met,
              tests_total, tests_passed, tests_failed, blocker_count, artifact_count,
              validation_status, scope_hash, state_hash
            )
            VALUES (
              CAST(:loop_id AS uuid), CAST(:iteration_id AS uuid), :criteria_total, :criteria_met,
              :tests_total, :tests_passed, :tests_failed, :blockers, :artifacts,
              :validation_status, :scope_hash, :state_hash
            )
            RETURNING id
            """
        ),
        {
            "loop_id": loop_id,
            "iteration_id": iteration_id,
            "criteria_total": snapshot.acceptance_criteria_total,
            "criteria_met": snapshot.acceptance_criteria_met,
            "tests_total": snapshot.tests_total,
            "tests_passed": snapshot.tests_passed,
            "tests_failed": snapshot.tests_failed,
            "blockers": snapshot.blocker_count,
            "artifacts": snapshot.artifact_count,
            "validation_status": snapshot.validation_status,
            "scope_hash": snapshot.scope_hash,
            "state_hash": snapshot.state_hash,
        },
    ).scalar_one()
    return str(row)


def _insert_delta(session: Session, iteration_id: str, delta: Any) -> str:
    row: Any = session.execute(
        text(
            """
            INSERT INTO task_progress_deltas(
              iteration_id, criteria_met_delta, tests_passed_delta, tests_failed_delta,
              blocker_delta, artifact_delta, scope_changed, state_changed, progress_score,
              classification
            )
            VALUES (
              CAST(:iteration_id AS uuid), :criteria, :tests_passed, :tests_failed,
              :blocker, :artifact, :scope_changed, :state_changed, :score, :classification
            )
            RETURNING id
            """
        ),
        {
            "iteration_id": iteration_id,
            "criteria": delta.criteria_met_delta,
            "tests_passed": delta.tests_passed_delta,
            "tests_failed": delta.tests_failed_delta,
            "blocker": delta.blocker_delta,
            "artifact": delta.artifact_delta,
            "scope_changed": delta.scope_changed,
            "state_changed": delta.state_changed,
            "score": delta.progress_score,
            "classification": delta.classification.value,
        },
    ).scalar_one()
    return str(row)


def _current_streaks(session: Session, loop_id: str, latest: str) -> tuple[int, int]:
    rows: Any = session.execute(
        text(
            """
            SELECT d.classification
            FROM task_progress_deltas d
            JOIN task_loop_iterations i ON i.id = d.iteration_id
            WHERE i.loop_id = CAST(:loop_id AS uuid)
            ORDER BY d.created_at DESC
            LIMIT 20
            """
        ),
        {"loop_id": loop_id},
    ).scalars().all()
    classifications = [latest, *[str(row) for row in rows]]
    neutral = 0
    negative = 0
    for item in classifications:
        if item == "NEUTRAL":
            neutral += 1
        else:
            break
    for item in classifications:
        if item == "NEGATIVE":
            negative += 1
        else:
            break
    return neutral, negative


def _all_steps_passed(session: Session, plan_id: str) -> bool:
    remaining: Any = session.execute(
        text(
            """
            SELECT count(*) FROM task_plan_steps
            WHERE task_plan_id = CAST(:plan_id AS uuid)
              AND status <> 'PASSED'
            """
        ),
        {"plan_id": plan_id},
    ).scalar_one()
    return int(remaining) == 0


def _worker_failures(session: Session, loop_id: str) -> int:
    count: Any = session.execute(
        text(
            """
            SELECT count(*)
            FROM task_loop_iterations i
            JOIN worker_jobs j ON j.id = i.job_id
            WHERE i.loop_id = CAST(:loop_id AS uuid)
              AND j.status IN ('FAILED', 'TIMED_OUT')
            """
        ),
        {"loop_id": loop_id},
    ).scalar_one()
    return int(count)


def _stop_policy_from_loop(loop: dict[str, Any]) -> Any:
    return __import__("pados.loop.schemas", fromlist=["StopPolicy"]).StopPolicy(
        max_iterations=int(loop["max_iterations"]),
        max_worker_failures=int(loop["max_worker_failures"]),
        max_execution_seconds=int(loop["max_execution_seconds"]),
        no_progress_limit=int(loop["no_progress_limit"]),
        max_context_tokens=int(loop["context_token_budget"]),
    )


def _persist_decision(
    session: Session,
    *,
    loop: dict[str, Any],
    iteration_id: str,
    iteration_number: int,
    job_id: str,
    after_id: str,
    delta_id: str,
    decision: LoopDecision,
    reason: str,
    next_status: LoopStatus,
) -> None:
    session.execute(
        text(
            """
            UPDATE task_loop_iterations
            SET status = 'DECIDED',
                job_id = CAST(:job_id AS uuid),
                after_snapshot_id = CAST(:after_id AS uuid),
                progress_delta_id = CAST(:delta_id AS uuid),
                decision = :decision,
                decision_reason = :reason,
                completed_at = now()
            WHERE id = CAST(:iteration_id AS uuid)
            """
        ),
        {
            "iteration_id": iteration_id,
            "job_id": job_id,
            "after_id": after_id,
            "delta_id": delta_id,
            "decision": decision.value,
            "reason": reason,
        },
    )
    session.execute(
        text(
            """
            UPDATE task_loops
            SET status = :status,
                current_iteration = :iteration_number,
                completed_at = CASE WHEN :status IN ('COMPLETED', 'PARTIALLY_COMPLETED', 'FAILED_TERMINAL', 'CANCELLED', 'TIMED_OUT') THEN now() ELSE completed_at END,
                final_result = CAST(:final_result AS jsonb)
            WHERE id = CAST(:loop_id AS uuid)
            """
        ),
        {
            "loop_id": str(loop["id"]),
            "status": next_status.value,
            "iteration_number": iteration_number,
            "final_result": _stable_json({"decision": decision.value, "reason": reason}),
        },
    )
    _event(
        session,
        str(loop["id"]),
        str(loop["project_id"]),
        "LOOP_DECISION_RECORDED",
        {"decision": decision.value, "reason": reason},
    )
    if decision == LoopDecision.COMPLETE:
        _event(session, str(loop["id"]), str(loop["project_id"]), "LOOP_COMPLETED", {"iteration": iteration_number})


def _complete_loop(session: Session, loop: dict[str, Any], reason: str) -> dict[str, Any]:
    row = session.execute(
        text(
            """
            UPDATE task_loops
            SET status = 'COMPLETED', completed_at = now(), final_result = CAST(:result AS jsonb)
            WHERE id = CAST(:id AS uuid)
            RETURNING *
            """
        ),
        {"id": str(loop["id"]), "result": _stable_json({"decision": "COMPLETE", "reason": reason})},
    ).mappings().one()
    _event(session, str(row["id"]), str(row["project_id"]), "LOOP_COMPLETED", {"reason": reason})
    return {"loop": dict(row), "decision": "COMPLETE", "decision_reason": reason}


def _block_loop(session: Session, loop: dict[str, Any], reason: str) -> dict[str, Any]:
    row = session.execute(
        text(
            """
            UPDATE task_loops
            SET status = 'BLOCKED', completed_at = NULL,
                final_result = CAST(:result AS jsonb)
            WHERE id = CAST(:id AS uuid)
            RETURNING *
            """
        ),
        {"id": str(loop["id"]), "result": _stable_json({"decision": "BLOCK", "reason": reason})},
    ).mappings().one()
    _event(session, str(row["id"]), str(row["project_id"]), "LOOP_BLOCKED", {"reason": reason})
    return {"loop": dict(row), "decision": "BLOCK", "decision_reason": reason}


def get_task_goal(session: Session, goal_id: str) -> dict[str, Any]:
    row = session.execute(text("SELECT * FROM task_goals WHERE id = CAST(:id AS uuid)"), {"id": goal_id}).mappings().one()
    return dict(row)


def get_task_plan(session: Session, plan_id: str) -> dict[str, Any]:
    row = session.execute(text("SELECT * FROM task_plans WHERE id = CAST(:id AS uuid)"), {"id": plan_id}).mappings().one()
    return dict(row)


def get_loop(session: Session, loop_id: str) -> dict[str, Any]:
    row = session.execute(text("SELECT * FROM task_loops WHERE id = CAST(:id AS uuid)"), {"id": loop_id}).mappings().one()
    return dict(row)


def get_loop_for_update(session: Session, loop_id: str) -> dict[str, Any]:
    row = session.execute(
        text("SELECT * FROM task_loops WHERE id = CAST(:id AS uuid) FOR UPDATE"),
        {"id": loop_id},
    ).mappings().one()
    return dict(row)


def list_loops(session: Session) -> list[dict[str, Any]]:
    rows = session.execute(text("SELECT * FROM task_loops ORDER BY created_at DESC LIMIT 100")).mappings().all()
    return [dict(row) for row in rows]


def loop_iterations(session: Session, loop_id: str) -> list[dict[str, Any]]:
    rows = session.execute(
        text("SELECT * FROM task_loop_iterations WHERE loop_id = CAST(:id AS uuid) ORDER BY iteration_number ASC"),
        {"id": loop_id},
    ).mappings().all()
    return [dict(row) for row in rows]


def loop_events(session: Session, loop_id: str) -> list[dict[str, Any]]:
    rows = session.execute(
        text("SELECT * FROM task_loop_events WHERE loop_id = CAST(:id AS uuid) ORDER BY sequence_number ASC"),
        {"id": loop_id},
    ).mappings().all()
    return [dict(row) for row in rows]


# A finished loop is never rewritten by an operator control (RC2 SEC-3): the predicate lives in the UPDATE itself.
_TERMINAL_LOOP_SQL = "('COMPLETED', 'PARTIALLY_COMPLETED', 'FAILED_TERMINAL', 'CANCELLED', 'TIMED_OUT')"


def _transitioned(session: Session, statement: str, loop_id: str) -> dict[str, Any]:
    row = session.execute(text(statement), {"id": loop_id}).mappings().one_or_none()
    if row is None:
        raise ValueError("LOOP_TRANSITION_REFUSED: the loop is unknown or its status does not allow this action")
    return dict(row)


def pause_loop(session: Session, loop_id: str) -> dict[str, Any]:
    row = _transitioned(session, "UPDATE task_loops SET status = 'PAUSED', paused_at = now() "
                        f"WHERE id = CAST(:id AS uuid) AND status NOT IN {_TERMINAL_LOOP_SQL} "
                        "AND status <> 'PAUSED' RETURNING *", loop_id)
    _event(session, loop_id, str(row["project_id"]), "LOOP_PAUSED", {})
    return row


def resume_loop(session: Session, loop_id: str) -> dict[str, Any]:
    row = _transitioned(session, "UPDATE task_loops SET status = 'RUNNING', paused_at = NULL "
                        "WHERE id = CAST(:id AS uuid) AND status = 'PAUSED' RETURNING *", loop_id)
    _event(session, loop_id, str(row["project_id"]), "LOOP_RESUMED", {})
    return row


def cancel_loop(session: Session, loop_id: str) -> dict[str, Any]:
    row = _transitioned(session, "UPDATE task_loops SET status = 'CANCELLED', completed_at = now() "
                        f"WHERE id = CAST(:id AS uuid) AND status NOT IN {_TERMINAL_LOOP_SQL} RETURNING *", loop_id)
    cancelled_jobs = session.execute(
        text(
            """
            UPDATE worker_jobs
            SET status = 'CANCELLED', completed_at = COALESCE(completed_at, now()),
                result_summary = COALESCE(result_summary, 'cancelled with governing task loop')
            WHERE id IN (
                SELECT job_id FROM task_loop_iterations
                WHERE loop_id = CAST(:id AS uuid) AND job_id IS NOT NULL
            )
              AND status IN ('ROUTED', 'QUEUED', 'RUNNING', 'VALIDATING')
            RETURNING id, project_id
            """
        ),
        {"id": loop_id},
    ).mappings().all()
    for job in cancelled_jobs:
        record_job_event(
            session,
            str(job["id"]),
            str(job["project_id"]),
            "JOB_CANCELLED",
            {"reason": "governing_task_loop_cancelled"},
        )
    cancelled_steps = session.execute(
        text(
            """
            SELECT step_id FROM task_loop_iterations
            WHERE loop_id = CAST(:id AS uuid)
              AND status IN ('CREATED', 'ROUTING', 'RUNNING', 'VALIDATING')
            """
        ),
        {"id": loop_id},
    ).scalars().all()
    session.execute(
        text(
            """
            UPDATE task_loop_iterations
            SET status = 'DECIDED', decision = 'CANCEL',
                decision_reason = 'governing task loop cancelled', completed_at = now()
            WHERE loop_id = CAST(:id AS uuid)
              AND status IN ('CREATED', 'ROUTING', 'RUNNING', 'VALIDATING')
            """
        ),
        {"id": loop_id},
    )
    for step_id in cancelled_steps:
        session.execute(
            text("UPDATE task_plan_steps SET status = 'CANCELLED' WHERE id = CAST(:id AS uuid)"),
            {"id": str(step_id)},
        )
    _event(session, loop_id, str(row["project_id"]), "LOOP_CANCELLED", {})
    return dict(row)

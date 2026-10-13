# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import hashlib
import json
from typing import Any, cast

from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from pados.project_goal.engine import human_gate_scope_hash


def stable_hash(value: object) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def _one(session: Session, sql: str, values: dict[str, Any]) -> dict[str, Any]:
    return dict(session.execute(text(sql), values).mappings().one())


def get_goal(session: Session, goal_id: str, *, project_id: str | None = None, lock: bool = False) -> dict[str, Any]:
    clause = " AND project_id = CAST(:project_id AS uuid)" if project_id else ""
    suffix = " FOR UPDATE" if lock else ""
    values: dict[str, Any] = {"id": goal_id, "project_id": project_id}
    return _one(session, f"SELECT * FROM project_goals WHERE id = CAST(:id AS uuid){clause}{suffix}", values)


def list_goals(session: Session, project_id: str) -> list[dict[str, Any]]:
    rows = session.execute(text("SELECT * FROM project_goals WHERE project_id = CAST(:id AS uuid) ORDER BY version"), {"id": project_id}).mappings().all()
    return [dict(row) for row in rows]


def insert_goal(session: Session, values: dict[str, Any]) -> dict[str, Any]:
    return _one(session, """
        INSERT INTO project_goals(
          project_id, version, objective, desired_outcomes, success_criteria, constraints_json,
          scope_paths, prohibited_actions, assumptions, risk_budget, execution_budget,
          time_horizon_seconds, planning_horizon, status, content_hash, supersedes_goal_id,
          idempotency_key)
        VALUES (CAST(:project_id AS uuid), :version, :objective, CAST(:desired_outcomes AS jsonb),
          CAST(:success_criteria AS jsonb), CAST(:constraints AS jsonb), CAST(:scope_paths AS jsonb),
          CAST(:prohibited_actions AS jsonb), CAST(:assumptions AS jsonb), CAST(:risk_budget AS jsonb),
          CAST(:execution_budget AS jsonb), :time_horizon_seconds, :planning_horizon, 'DRAFT',
          :content_hash, CAST(:supersedes_goal_id AS uuid), :idempotency_key)
        ON CONFLICT (project_id, idempotency_key) DO UPDATE SET objective = project_goals.objective
        RETURNING *
    """, values)


def get_plan(session: Session, plan_id: str, *, lock: bool = False) -> dict[str, Any]:
    return _one(session, "SELECT * FROM project_plans WHERE id = CAST(:id AS uuid)" + (" FOR UPDATE" if lock else ""), {"id": plan_id})


def active_plan(session: Session, goal_id: str) -> dict[str, Any]:
    return _one(
        session,
        """
        SELECT * FROM project_plans
        WHERE project_goal_id = CAST(:goal_id AS uuid) AND status IN ('ACTIVE', 'APPROVED')
        ORDER BY CASE status WHEN 'ACTIVE' THEN 0 ELSE 1 END, version DESC
        LIMIT 1
        """,
        {"goal_id": goal_id},
    )


def insert_plan(session: Session, values: dict[str, Any]) -> dict[str, Any]:
    return _one(session, """
        INSERT INTO project_plans(project_goal_id, version, status, planning_horizon,
          strategy_summary, assumptions, priorities, task_graph, dependency_graph, critical_path,
          review_policy, replan_policy, stop_policy, risk_controls, resource_limits, plan_diff,
          supporting_evidence_ids, content_hash, supersedes_plan_id, idempotency_key)
        VALUES (CAST(:project_goal_id AS uuid), :version, 'DRAFT', :planning_horizon,
          :strategy_summary, CAST(:assumptions AS jsonb), CAST(:priorities AS jsonb),
          CAST(:task_graph AS jsonb), CAST(:dependency_graph AS jsonb), CAST(:critical_path AS jsonb),
          CAST(:review_policy AS jsonb), CAST(:replan_policy AS jsonb), CAST(:stop_policy AS jsonb),
          CAST(:risk_controls AS jsonb), CAST(:resource_limits AS jsonb), CAST(:plan_diff AS jsonb),
          CAST(:supporting_evidence_ids AS jsonb), :content_hash, CAST(:supersedes_plan_id AS uuid),
          :idempotency_key)
        ON CONFLICT (project_goal_id, idempotency_key) DO UPDATE SET status = project_plans.status
        RETURNING *
    """, values)


def get_task(session: Session, task_id: str, *, project_id: str | None = None, lock: bool = False) -> dict[str, Any]:
    clause = " AND project_id = CAST(:project_id AS uuid)" if project_id else ""
    suffix = " FOR UPDATE" if lock else ""
    return _one(session, f"SELECT * FROM project_tasks WHERE id = CAST(:id AS uuid){clause}{suffix}", {"id": task_id, "project_id": project_id})


def list_tasks(session: Session, *, goal_id: str, plan_id: str) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            "SELECT * FROM project_tasks WHERE project_goal_id = CAST(:goal_id AS uuid) "
            "AND project_plan_id = CAST(:plan_id AS uuid) ORDER BY priority, created_at"
        ),
        {"goal_id": goal_id, "plan_id": plan_id},
    ).mappings().all()
    return [dict(row) for row in rows]


def dependencies(session: Session, task_id: str) -> list[dict[str, Any]]:
    rows = session.execute(
        text(
            """
            SELECT d.*, t.status AS dependency_status, t.project_id AS dependency_project_id
            FROM project_task_dependencies d
            JOIN project_tasks t ON t.id = d.dependency_task_id
            WHERE d.project_task_id = CAST(:task_id AS uuid)
            ORDER BY d.created_at
            """
        ),
        {"task_id": task_id},
    ).mappings().all()
    return [dict(row) for row in rows]


def insert_task(session: Session, values: dict[str, Any]) -> dict[str, Any]:
    return _one(session, """
        INSERT INTO project_tasks(project_plan_id, project_goal_id, project_id, title, objective,
          success_criteria, constraints_json, scope_paths, priority, risk_level, estimated_cost,
          estimated_duration_seconds, required_capabilities, task_goal_template, task_plan_template,
          status, goal_hash, scope_hash, success_criteria_hash, dependency_hash, deduplication_key)
        VALUES (CAST(:project_plan_id AS uuid), CAST(:project_goal_id AS uuid), CAST(:project_id AS uuid),
          :title, :objective, CAST(:success_criteria AS jsonb), CAST(:constraints AS jsonb),
          CAST(:scope_paths AS jsonb), :priority, :risk_level, :estimated_cost,
          :estimated_duration_seconds, CAST(:required_capabilities AS jsonb),
          CAST(:task_goal_template AS jsonb), CAST(:task_plan_template AS jsonb), 'PROPOSED',
          :goal_hash, :scope_hash, :success_criteria_hash, :dependency_hash, :deduplication_key)
        ON CONFLICT (project_plan_id, deduplication_key) DO UPDATE SET title = project_tasks.title
        RETURNING *
    """, values)


def transition(session: Session, table: str, record_id: str, current: str, desired: str, *, approval_id: str | None = None) -> dict[str, Any]:
    allowed = {"project_goals", "project_plans", "project_tasks", "project_goal_loops", "project_replan_proposals"}
    if table not in allowed:
        raise ValueError("unsupported transition table")
    approval = ", approval_id = CAST(:approval_id AS uuid)" if approval_id else ""
    timestamp = ", approved_at = now()" if desired == "APPROVED" else ", activated_at = now()" if desired == "ACTIVE" and table != "project_tasks" else ""
    return _one(session, f"UPDATE {table} SET status = :desired{approval}{timestamp} WHERE id = CAST(:id AS uuid) AND status = :current RETURNING *", {"id": record_id, "current": current, "desired": desired, "approval_id": approval_id})


def append_event(session: Session, *, project_id: str, goal_id: str, event_type: str, payload: dict[str, Any], idempotency_key: str, loop_id: str | None = None, cycle_id: str | None = None) -> dict[str, Any]:
    return _one(session, """
        WITH inserted AS (
          INSERT INTO project_goal_events(project_id, project_goal_id, project_goal_loop_id,
            project_goal_cycle_id, event_type, payload_json, idempotency_key)
          VALUES (CAST(:project_id AS uuid), CAST(:goal_id AS uuid), CAST(:loop_id AS uuid),
            CAST(:cycle_id AS uuid), :event_type, CAST(:payload AS jsonb), :idempotency_key)
          ON CONFLICT (project_goal_id, idempotency_key) DO NOTHING RETURNING *
        )
        SELECT * FROM inserted
        UNION ALL
        SELECT * FROM project_goal_events
        WHERE project_goal_id = CAST(:goal_id AS uuid) AND idempotency_key = :idempotency_key
        LIMIT 1
    """, {"project_id": project_id, "goal_id": goal_id, "loop_id": loop_id, "cycle_id": cycle_id, "event_type": event_type, "payload": _json(payload), "idempotency_key": idempotency_key})


def events(session: Session, goal_id: str) -> list[dict[str, Any]]:
    rows = session.execute(text("SELECT * FROM project_goal_events WHERE project_goal_id = CAST(:id AS uuid) ORDER BY sequence_number"), {"id": goal_id}).mappings().all()
    return [dict(row) for row in rows]


def acquire_lease(session: Session, *, goal_id: str, owner: str, lease_seconds: int) -> dict[str, Any]:
    if lease_seconds <= 0 or lease_seconds > 3600:
        raise ValueError("lease must be positive and bounded")
    return _one(session, """
        INSERT INTO project_controller_leases(project_goal_id, owner_instance, lease_expires_at)
        VALUES (CAST(:goal_id AS uuid), :owner, now() + make_interval(secs => :lease_seconds))
        ON CONFLICT (project_goal_id) DO UPDATE SET owner_instance = EXCLUDED.owner_instance,
          lease_acquired_at = now(), lease_expires_at = EXCLUDED.lease_expires_at, heartbeat_at = now()
        WHERE project_controller_leases.lease_expires_at <= now()
           OR project_controller_leases.owner_instance = EXCLUDED.owner_instance
        RETURNING *
    """, {"goal_id": goal_id, "owner": owner, "lease_seconds": lease_seconds})


def release_lease(session: Session, *, goal_id: str, owner: str) -> None:
    result = cast(
        CursorResult[Any],
        session.execute(
            text(
                "DELETE FROM project_controller_leases "
                "WHERE project_goal_id = CAST(:goal_id AS uuid) AND owner_instance = :owner"
            ),
            {"goal_id": goal_id, "owner": owner},
        ),
    )
    if result.rowcount != 1:
        raise PermissionError("controller lease owner mismatch")


def link_p6_10(
    session: Session,
    *,
    task_id: str,
    launch_key: str,
    task_goal_id: str,
    task_plan_id: str,
    task_loop_id: str,
) -> dict[str, Any]:
    return _one(
        session,
        """
        UPDATE project_tasks
        SET task_goal_id = CAST(:task_goal_id AS uuid),
            task_plan_id = CAST(:task_plan_id AS uuid),
            task_loop_id = CAST(:task_loop_id AS uuid),
            launch_idempotency_key = :launch_key,
            status = 'ACTIVE',
            activated_at = COALESCE(activated_at, now())
        WHERE id = CAST(:task_id AS uuid)
          AND status IN ('APPROVED','READY','ACTIVE')
          AND (launch_idempotency_key IS NULL OR launch_idempotency_key = :launch_key)
        RETURNING *
        """,
        {
            "task_id": task_id,
            "launch_key": launch_key,
            "task_goal_id": task_goal_id,
            "task_plan_id": task_plan_id,
            "task_loop_id": task_loop_id,
        },
    )


def get_or_create_loop(session: Session, *, goal_id: str, plan_id: str, project_id: str, stop_policy: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
    return _one(session, """
        INSERT INTO project_goal_loops(project_goal_id, active_plan_id, project_id, status, stop_policy, idempotency_key)
        VALUES (CAST(:goal_id AS uuid), CAST(:plan_id AS uuid), CAST(:project_id AS uuid), 'ACTIVE', CAST(:stop_policy AS jsonb), :idempotency_key)
        ON CONFLICT (project_goal_id, idempotency_key) DO UPDATE SET status = project_goal_loops.status
        RETURNING *
    """, {"goal_id": goal_id, "plan_id": plan_id, "project_id": project_id, "stop_policy": _json(stop_policy), "idempotency_key": idempotency_key})


def create_cycle(session: Session, *, loop_id: str, goal_id: str, cycle_number: int, input_hash: str, idempotency_key: str) -> dict[str, Any]:
    row = _one(session, """
        INSERT INTO project_goal_cycles(project_goal_loop_id, project_goal_id, cycle_number, status, checkpoint, input_hash, idempotency_key)
        VALUES (CAST(:loop_id AS uuid), CAST(:goal_id AS uuid), :cycle_number, 'RUNNING', 'LEASE_ACQUIRED', :input_hash, :idempotency_key)
        ON CONFLICT (project_goal_id, idempotency_key) DO UPDATE SET status = project_goal_cycles.status
        RETURNING *
    """, {"loop_id": loop_id, "goal_id": goal_id, "cycle_number": cycle_number, "input_hash": input_hash, "idempotency_key": idempotency_key})
    session.execute(text("""
        UPDATE project_goal_loops
        SET current_cycle = GREATEST(current_cycle, :cycle_number), updated_at = now()
        WHERE id = CAST(:loop_id AS uuid)
    """), {"loop_id": loop_id, "cycle_number": cycle_number})
    return row


def complete_cycle(session: Session, *, cycle_id: str, decision: str, reason: str) -> dict[str, Any]:
    return _one(session, """
        UPDATE project_goal_cycles SET status = 'COMPLETED', checkpoint = 'COMPLETED', decision = :decision,
          decision_reason = :reason, completed_at = now()
        WHERE id = CAST(:id AS uuid) AND status IN ('RUNNING', 'DECIDED') RETURNING *
    """, {"id": cycle_id, "decision": decision, "reason": reason})


def latest_snapshot(session: Session, goal_id: str) -> dict[str, Any] | None:
    row = session.execute(text("SELECT * FROM project_progress_snapshots WHERE project_goal_id = CAST(:id AS uuid) ORDER BY created_at DESC LIMIT 1"), {"id": goal_id}).mappings().first()
    return dict(row) if row else None


def insert_snapshot(session: Session, *, goal_id: str, plan_id: str, cycle_id: str, metrics: dict[str, Any], scope_hash: str, state_hash: str, evidence_hash: str, idempotency_key: str) -> dict[str, Any]:
    return _one(session, """
        INSERT INTO project_progress_snapshots(project_goal_id, project_plan_id, project_goal_cycle_id, metrics, scope_hash, state_hash, evidence_hash, idempotency_key)
        VALUES (CAST(:goal_id AS uuid), CAST(:plan_id AS uuid), CAST(:cycle_id AS uuid), CAST(:metrics AS jsonb), :scope_hash, :state_hash, :evidence_hash, :idempotency_key)
        ON CONFLICT (project_goal_id, idempotency_key) DO UPDATE SET metrics = project_progress_snapshots.metrics
        RETURNING *
    """, {"goal_id": goal_id, "plan_id": plan_id, "cycle_id": cycle_id, "metrics": _json(metrics), "scope_hash": scope_hash, "state_hash": state_hash, "evidence_hash": evidence_hash, "idempotency_key": idempotency_key})


def find_active_gap(session: Session, *, project_id: str, identity_hash: str) -> dict[str, Any] | None:
    row = session.execute(text("SELECT * FROM project_gaps WHERE project_id = CAST(:project_id AS uuid) AND identity_hash = :identity_hash AND status IN ('OPEN','ACKNOWLEDGED','RESOLVING') ORDER BY created_at DESC LIMIT 1"), {"project_id": project_id, "identity_hash": identity_hash}).mappings().first()
    return dict(row) if row else None


def find_related_active_gap(session: Session, *, project_id: str, goal_id: str, plan_id: str, task_id: str | None, criterion_id: str, gap_type: str, required_state_hash: str) -> dict[str, Any] | None:
    row = session.execute(text("""SELECT * FROM project_gaps WHERE project_id = CAST(:project_id AS uuid)
      AND project_goal_id = CAST(:goal_id AS uuid) AND project_plan_id = CAST(:plan_id AS uuid)
      AND COALESCE(project_task_id::text, '') = COALESCE(CAST(:task_id AS uuid)::text, '')
      AND success_criterion_id = :criterion_id AND gap_type = :gap_type
      AND required_state_hash = :required_state_hash AND status IN ('OPEN','ACKNOWLEDGED','RESOLVING')
      ORDER BY created_at DESC LIMIT 1"""), {"project_id": project_id, "goal_id": goal_id, "plan_id": plan_id, "task_id": task_id, "criterion_id": criterion_id, "gap_type": gap_type, "required_state_hash": required_state_hash}).mappings().first()
    return dict(row) if row else None


def find_historical_gap(session: Session, *, project_id: str, identity_hash: str) -> dict[str, Any] | None:
    row = session.execute(text("SELECT * FROM project_gaps WHERE project_id = CAST(:project_id AS uuid) AND identity_hash = :identity_hash AND status IN ('RESOLVED','WAIVED','SUPERSEDED') ORDER BY resolved_at DESC, created_at DESC LIMIT 1"), {"project_id": project_id, "identity_hash": identity_hash}).mappings().first()
    return dict(row) if row else None


def get_gap(session: Session, gap_id: str, *, project_id: str, lock: bool = False) -> dict[str, Any]:
    return _one(session, "SELECT * FROM project_gaps WHERE id = CAST(:id AS uuid) AND project_id = CAST(:project_id AS uuid)" + (" FOR UPDATE" if lock else ""), {"id": gap_id, "project_id": project_id})


def insert_gap(session: Session, *, project_id: str, goal_id: str, plan_id: str, task_id: str | None, criterion_id: str, gap_type: str, description: str, required_state: dict[str, Any], observed_state: dict[str, Any], severity: str, confidence: float, identity_hash: str, idempotency_key: str, occurrence_number: int = 1, reappeared_from_gap_id: str | None = None) -> dict[str, Any]:
    return _one(session, """
        INSERT INTO project_gaps(project_id, project_goal_id, project_goal_version, project_plan_id, project_plan_version, project_task_id, success_criterion_id, gap_type, description, required_state, observed_state, required_state_hash, observed_state_hash, condition_hash, severity, confidence, identity_hash, occurrence_number, reappeared_from_gap_id, idempotency_key, status)
        VALUES (CAST(:project_id AS uuid), CAST(:goal_id AS uuid), :goal_version, CAST(:plan_id AS uuid), :plan_version, CAST(:task_id AS uuid), :criterion_id, :gap_type, :description, CAST(:required_state AS jsonb), CAST(:observed_state AS jsonb), :required_hash, :observed_hash, :condition_hash, :severity, :confidence, :identity_hash, :occurrence_number, CAST(:reappeared_from_gap_id AS uuid), :idempotency_key, 'OPEN')
        ON CONFLICT (project_id, identity_hash) WHERE status IN ('OPEN','ACKNOWLEDGED','RESOLVING') DO UPDATE SET description = project_gaps.description
        RETURNING *
    """, {"project_id": project_id, "goal_id": goal_id, "goal_version": 1, "plan_id": plan_id, "plan_version": 1, "task_id": task_id, "criterion_id": criterion_id, "gap_type": gap_type, "description": description, "required_state": _json(required_state), "observed_state": _json(observed_state), "required_hash": stable_hash(required_state), "observed_hash": stable_hash(observed_state), "condition_hash": identity_hash, "severity": severity, "confidence": confidence, "identity_hash": identity_hash, "occurrence_number": occurrence_number, "reappeared_from_gap_id": reappeared_from_gap_id, "idempotency_key": idempotency_key})


def transition_gap(session: Session, *, gap_id: str, project_id: str, current: str, desired: str, actor: str, reason_code: str, idempotency_key: str, evidence_ids: list[str] | None = None, resolution_state_hash: str | None = None, human_gate_id: str | None = None, superseded_by_gap_id: str | None = None) -> dict[str, Any]:
    return _one(session, """UPDATE project_gaps SET status = :desired,
      resolved_at = CASE WHEN :desired IN ('RESOLVED','WAIVED','SUPERSEDED') THEN now() ELSE resolved_at END,
      resolved_by = CASE WHEN :desired = 'RESOLVED' THEN :actor ELSE resolved_by END,
      resolution_reason_code = :reason_code,
      resolution_evidence_ids = COALESCE(CAST(:evidence_ids AS jsonb), resolution_evidence_ids),
      resolution_state_hash = COALESCE(:resolution_state_hash, resolution_state_hash),
      project_human_gate_id = COALESCE(CAST(:human_gate_id AS uuid), project_human_gate_id),
      superseded_by_gap_id = COALESCE(CAST(:superseded_by_gap_id AS uuid), superseded_by_gap_id),
      idempotency_key = :idempotency_key
      WHERE id = CAST(:gap_id AS uuid) AND project_id = CAST(:project_id AS uuid) AND status = :current RETURNING *""", {"gap_id": gap_id, "project_id": project_id, "current": current, "desired": desired, "actor": actor, "reason_code": reason_code, "idempotency_key": idempotency_key, "evidence_ids": _json(evidence_ids) if evidence_ids is not None else None, "resolution_state_hash": resolution_state_hash, "human_gate_id": human_gate_id, "superseded_by_gap_id": superseded_by_gap_id})


def insert_human_gate(session: Session, *, project_id: str, goal_id: str, plan_id: str | None, task_id: str | None, gate_type: str, reason_codes: list[str], risk_delta: int, scope_delta: list[str], budget_delta: int, time_horizon_delta: int, required_action: str, evidence_hash: str, condition_hash: str, valid_from: str | None, valid_until: str | None, requested_by: str, idempotency_key: str) -> dict[str, Any]:
    goal = get_goal(session, goal_id, project_id=project_id)
    plan = get_plan(session, plan_id) if plan_id else None
    scope = {"project_id": project_id, "goal_id": goal_id, "goal_version": goal["version"], "plan_id": plan_id, "plan_version": plan["version"] if plan else None, "task_id": task_id, "gate_type": gate_type, "risk_delta": risk_delta, "scope_delta": scope_delta, "budget_delta": budget_delta, "time_horizon_delta": time_horizon_delta, "evidence_hash": evidence_hash, "valid_from": valid_from, "valid_until": valid_until, "required_action": required_action}
    return _one(session, """
        INSERT INTO project_human_gates(project_id, project_goal_id, project_plan_id, project_task_id,
          project_goal_version, project_plan_version, gate_type, reason_codes, risk_delta, scope_delta,
          budget_delta, time_horizon_delta, required_action, supporting_evidence_ids, evidence_hash,
          condition_hash, valid_from, valid_until, requested_by, approval_scope_hash, idempotency_key)
        VALUES (CAST(:project_id AS uuid), CAST(:goal_id AS uuid), CAST(:plan_id AS uuid), CAST(:task_id AS uuid),
          :goal_version, :plan_version, :gate_type, CAST(:reason_codes AS jsonb), :risk_delta,
          CAST(:scope_delta AS jsonb), :budget_delta, :time_horizon_delta, :required_action, '[]'::jsonb,
          :evidence_hash, :condition_hash, COALESCE(CAST(:valid_from AS timestamptz), now()),
          CAST(:valid_until AS timestamptz), :requested_by, :scope_hash, :idempotency_key)
        ON CONFLICT (project_goal_id, idempotency_key) DO UPDATE SET gate_type = project_human_gates.gate_type
        RETURNING *
    """, {"project_id": project_id, "goal_id": goal_id, "plan_id": plan_id, "task_id": task_id,
            "gate_type": gate_type, "reason_codes": _json(reason_codes), "risk_delta": risk_delta,
            "scope_delta": _json(scope_delta), "required_action": required_action,
            "budget_delta": budget_delta, "time_horizon_delta": time_horizon_delta, "evidence_hash": evidence_hash,
            "condition_hash": condition_hash, "valid_from": valid_from, "valid_until": valid_until,
            "requested_by": requested_by, "goal_version": goal["version"], "plan_version": plan["version"] if plan else None,
            "scope_hash": human_gate_scope_hash(scope), "idempotency_key": idempotency_key})


def get_human_gate(session: Session, gate_id: str, *, project_id: str | None = None) -> dict[str, Any]:
    clause = " AND project_id = CAST(:project_id AS uuid)" if project_id else ""
    return _one(session, f"SELECT * FROM project_human_gates WHERE id = CAST(:id AS uuid){clause}", {"id": gate_id, "project_id": project_id})


def find_human_gate(session: Session, *, project_id: str, goal_id: str, task_id: str, gate_type: str, condition_hash: str) -> dict[str, Any] | None:
    row = session.execute(text("""SELECT * FROM project_human_gates WHERE project_id = CAST(:project_id AS uuid)
        AND project_goal_id = CAST(:goal_id AS uuid) AND project_task_id = CAST(:task_id AS uuid)
        AND gate_type = :gate_type AND condition_hash = :condition_hash
        ORDER BY created_at DESC LIMIT 1"""), {"project_id": project_id, "goal_id": goal_id, "task_id": task_id, "gate_type": gate_type, "condition_hash": condition_hash}).mappings().first()
    return dict(row) if row else None


def find_latest_human_gate(session: Session, *, project_id: str, goal_id: str, task_id: str, gate_type: str) -> dict[str, Any] | None:
    row = session.execute(text("""SELECT * FROM project_human_gates WHERE project_id = CAST(:project_id AS uuid)
        AND project_goal_id = CAST(:goal_id AS uuid) AND project_task_id = CAST(:task_id AS uuid)
        AND gate_type = :gate_type ORDER BY created_at DESC, id DESC LIMIT 1"""), {"project_id": project_id, "goal_id": goal_id, "task_id": task_id, "gate_type": gate_type}).mappings().first()
    return dict(row) if row else None


def resolve_human_gate(session: Session, *, gate_id: str, current: str, desired: str, actor: str, decision_reason: str, approval_id: str | None, idempotency_key: str) -> dict[str, Any]:
    return _one(session, """UPDATE project_human_gates SET status = :desired, decision = :desired,
        decision_reason = :reason, decision_identity = :key, resolved_by = :actor,
        approval_id = COALESCE(CAST(:approval_id AS uuid), approval_id), resolved_at = now(), updated_at = now()
        WHERE id = CAST(:gate_id AS uuid) AND status = :current RETURNING *""", {"gate_id": gate_id, "current": current, "desired": desired, "actor": actor, "reason": decision_reason, "approval_id": approval_id, "key": idempotency_key})


def wait_for_human(session: Session, *, goal_id: str) -> dict[str, Any]:
    return _one(session, """
        UPDATE project_goal_loops SET status = 'WAITING_HUMAN', updated_at = now()
        WHERE project_goal_id = CAST(:goal_id AS uuid) AND status IN ('ACTIVE', 'EVALUATING', 'PLANNING', 'AT_RISK', 'BLOCKED')
        RETURNING *
    """, {"goal_id": goal_id})


def resolve_gap(session: Session, *, gap_id: str, goal_id: str, resolution_evidence: dict[str, Any]) -> dict[str, Any]:
    return _one(session, """
        UPDATE project_gaps SET status = 'RESOLVED', resolved_at = now(),
          observed_state = CAST(:resolution_evidence AS jsonb)
        WHERE id = CAST(:gap_id AS uuid) AND project_goal_id = CAST(:goal_id AS uuid)
          AND status IN ('OPEN', 'ACKNOWLEDGED', 'RESOLVING')
        RETURNING *
    """, {"gap_id": gap_id, "goal_id": goal_id, "resolution_evidence": _json(resolution_evidence)})


def task_outcome(session: Session, *, task_id: str, desired: str) -> dict[str, Any]:
    return _one(session, """
        UPDATE project_tasks SET status = :desired, completed_at = CASE WHEN :desired IN ('COMPLETED','PARTIALLY_COMPLETED','FAILED','BLOCKED') THEN now() ELSE completed_at END
        WHERE id = CAST(:task_id AS uuid) AND status IN ('ACTIVE','BLOCKED') RETURNING *
    """, {"task_id": task_id, "desired": desired})

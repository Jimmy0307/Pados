# SPDX-License-Identifier: AGPL-3.0-only
"""Read-only queries over the existing PADOS PostgreSQL authorities."""
from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from pados.config import PROOF_REVISION_HEAD, Settings, lane_revisions_accepted, lane_settings
from pados.db import get_engine
from pados.execution.context import redact
from pados.operations.live_evidence import live_provider_evidence
from pados.operations.projections import decision_inbox_projection, mission_control_projection
from pados.product_loop.provider_selection import select_product_loop_provider
from pados.worker.registry import discover_workers

PROOF_USABLE_REVISIONS = frozenset({"p6_19b_0013", "p6_21h_0014", "p6_21p_0015"})


def mission_control(session: Session, settings: Settings) -> dict[str, Any]:
    projects = _rows(
        session,
        """SELECT id::text AS id, name, status, updated_at
           FROM projects ORDER BY name ASC, id ASC""",
    )
    goals = _rows(
        session,
        """SELECT project_id::text AS project_id, id::text AS id, version,
                  objective, status, content_hash, created_at
           FROM project_goals
           WHERE status IN ('ACTIVE', 'AT_RISK', 'BLOCKED', 'WAITING_HUMAN')
           ORDER BY project_id, version DESC, created_at DESC, id DESC""",
    )
    blockers = _rows(
        session,
        """SELECT goal.project_id::text AS project_id, g.id::text AS id,
                  g.project_goal_id::text AS goal_id, g.gap_type, g.description,
                  g.severity, g.status, g.evidence_ids, g.created_at
           FROM project_gaps g JOIN project_goals goal ON goal.id = g.project_goal_id
           WHERE g.status IN ('OPEN', 'ACKNOWLEDGED', 'RESOLVING')
           ORDER BY goal.project_id, g.created_at DESC, g.id DESC""",
    )
    decisions = _pending_decisions(session)
    runs = _runs(session)
    known = {str(row["id"]) for row in projects}
    accepted = [row for row in proof_rows(
        settings,
        """SELECT r.scope_id AS project_id, r.id::text AS id,
                  r.run_id::text AS run_id, r.operation_type,
                  r.parsed_payload_json ->> 'title' AS title,
                  r.input_scope_version,
                  r.input_manifest_hash, c.new_version AS accepted_scope_version,
                  c.created_at
           FROM state_proof_commits c
           JOIN state_proof_results r ON r.id = c.result_id
           WHERE c.status = 'COMMITTED' AND r.status = 'SUCCEEDED'
           ORDER BY c.created_at DESC, r.id DESC LIMIT 100""",
    ) if str(row["project_id"]) in known]
    health = system_health(settings)
    projection = mission_control_projection(projects, goals, blockers, decisions, runs, accepted, health)
    from pados.enterprise.visibility import enterprise_visibility

    # Optional, read-only extension block; it never changes Core health or state.
    projection["enterprise"] = enterprise_visibility(
        config_path=settings.enterprise_config, core_ready=health.get("ready") is True,
        object_root=settings.product_object_root, known_projects=known,
        rows=lambda statement: proof_rows(settings, statement),
    )
    from pados.compute_node.visibility import compute_node_visibility

    # Optional, read-only compute-node block (persisted probe evidence and receipts only).
    projection["compute_nodes"] = compute_node_visibility(settings.compute_nodes_config)
    from pados.connectivity.config import connectivity_visibility

    # Optional, read-only transport block; network events are never semantic events.
    projection["connectivity"] = connectivity_visibility(settings.connectivity_config)
    from pados.herdr.visibility import herdr_visibility

    # Optional, read-only agent-runtime telemetry block; Herdr states are never PADOS states.
    projection["herdr"] = herdr_visibility(settings.herdr_config)
    from pados.machines.visibility import machine_visibility

    # Optional, read-only machine block: references and states only, never credentials or hosts.
    projection["machines"] = machine_visibility(settings.machines_config)
    return projection


def decision_inbox(session: Session) -> dict[str, Any]:
    projects = _rows(session, "SELECT id::text AS id, name, status FROM projects ORDER BY name, id")
    decisions = _pending_decisions(session)
    blockers = _rows(
        session,
        """SELECT goal.project_id::text AS project_id, g.id::text AS id,
                  g.description, g.gap_type, g.severity, g.status, g.created_at
           FROM project_gaps g JOIN project_goals goal ON goal.id = g.project_goal_id
           WHERE g.status IN ('OPEN', 'ACKNOWLEDGED', 'RESOLVING')
           ORDER BY g.created_at, g.id""",
    )
    runs = _runs(session, include_closed=True)
    approvals = _rows(
        session,
        """SELECT id::text AS id, project_id::text AS project_id,
                  proposal_summary AS item, decision_reason AS reason, status, created_at
           FROM control_approvals
           WHERE status = 'PENDING' ORDER BY created_at ASC, id ASC""",
    )
    items = decision_inbox_projection(projects, decisions, blockers, runs, approvals)
    return {"items": items, "count": len(items), "projection_only": True,
            "authority": "PADOS_PROJECT_RUN_AND_DECISION_STATE"}


def _probe_lane(settings: Settings, database: str, lane: str) -> dict[str, Any]:
    """Probe one database authority lane independently of every other lane."""
    result: dict[str, Any] = {
        "database": database or None, "configured": bool(database), "reachable": False,
        "authenticated": False, "migrations_current": False, "usable": False,
        "main_revisions": [], "proof_revisions": [],
    }
    if not database:
        return result
    engine = get_engine(lane_settings(settings, database))
    try:
        with engine.connect() as connection:
            result["reachable"] = result["authenticated"] = True
            result["main_revisions"] = _versions(connection, "alembic_version")
            result["proof_revisions"] = _versions(connection, "p6_19b_alembic_version")
    except SQLAlchemyError as exc:
        detail = str(getattr(exc, "orig", exc))
        # Authentication failure means the server answered: reachable, not authenticated.
        result["reachable"] = "authentication failed" in detail or "password" in detail.casefold()
        result["error"] = type(exc).__name__
        return result
    finally:
        engine.dispose()
    main_ok = lane_revisions_accepted(settings, lane, result["main_revisions"])
    if lane == "proof":
        result["migrations_current"] = main_ok and result["proof_revisions"] == [PROOF_REVISION_HEAD]
        result["usable"] = main_ok and bool(PROOF_USABLE_REVISIONS.intersection(result["proof_revisions"]))
    else:
        # P6.15 separation: proof migrations never belong in the operational lane.
        result["migrations_current"] = main_ok and not result["proof_revisions"]
        result["usable"] = result["migrations_current"]
    return result


def _versions(connection: Any, table_name: str) -> list[str]:
    if table_name not in {"alembic_version", "p6_19b_alembic_version"}:
        raise ValueError("UNREGISTERED_MIGRATION_TABLE")
    if connection.execute(text(f"SELECT to_regclass('public.{table_name}')")).scalar() is None:
        return []
    return sorted(str(v) for v in connection.execute(text(f"SELECT version_num FROM {table_name}")).scalars())


def system_health(settings: Settings) -> dict[str, Any]:
    """Assess each authority lane and runtime dimension; never read or expose credentials."""
    operational = _probe_lane(settings, settings.operational_database_name, "operational")
    proof_name = settings.product_database_name
    separate = bool(proof_name) and proof_name != settings.operational_database_name
    proof = _probe_lane(settings, proof_name if separate else "", "proof")
    configured = bool(
        settings.api_host in {"127.0.0.1", "localhost"}
        and separate
        and settings.product_workspace_root
        and settings.product_object_root
    )
    object_root_usable = _directory_usable(settings.product_object_root)
    provider_status, provider_detail, provider_ready = "BLOCKED", "no eligible PADOS Worker Router provider", False
    provider_live_uat = "NOT_APPLICABLE"
    provider_selection: dict[str, str | None] = {}
    live_evidence: dict[str, Any] = {"status": provider_live_uat}
    try:
        selection = select_product_loop_provider(settings.product_loop_provider, discover_workers(settings))
        provider_selection = selection.diagnostics()
        if selection.selected and selection.worker is not None:
            provider_status = "CONFIGURED"
            provider_detail = (f"{selection.worker.worker_id} selected by Worker Router "
                               f"(preference {selection.preference}); live execution is separate evidence")
            provider_ready = True
            live_evidence = _live_evidence(settings, proof_name, str(selection.provider_id)) if proof["usable"] \
                else {"status": "LIVE_UAT_PENDING", "reason": "PROOF_AUTHORITY_UNAVAILABLE"}
            provider_live_uat = str(live_evidence["status"])
        else:
            provider_detail = f"{selection.error_code}: {selection.explanation}"
    except Exception as exc:  # noqa: BLE001 - provider discovery failure must degrade readiness
        provider_detail = type(exc).__name__

    reasons: list[str] = []
    if not operational["authenticated"]:
        reasons.append("OPERATIONAL_DB_UNAVAILABLE")
    elif not operational["migrations_current"]:
        reasons.append("OPERATIONAL_MIGRATION_STALE")
    if not proof_name:
        reasons.append("PROOF_DB_UNCONFIGURED")
    elif not separate:
        reasons.append("PROOF_DB_NOT_SEPARATE")
    elif not proof["authenticated"]:
        reasons.append("PROOF_DB_UNAVAILABLE")
    elif not proof["migrations_current"]:
        reasons.append("PROOF_MIGRATION_STALE")
    if not object_root_usable:
        reasons.append("OBJECT_ROOT_UNUSABLE")
    if not configured:
        reasons.append("PRODUCT_LOOP_UNCONFIGURED")
    if not provider_ready:
        reasons.append("PROVIDER_UNAVAILABLE")
    blocking = list(reasons)
    if provider_ready and provider_live_uat != "LIVE_UAT_VERIFIED":
        reasons.append("LIVE_PROVIDER_UAT_PENDING")  # informational: never blocks readiness

    usable = bool(operational["usable"] and proof["usable"])
    ready = usable and not blocking
    checks = [
        {"name": "operational_database", "status": "OK" if operational["usable"] else "ERROR",
         "detail": ",".join(operational["main_revisions"]) or operational.get("error", "unavailable")},
        {"name": "proof_database", "status": "OK" if proof["migrations_current"] else "WARNING" if proof["usable"] else "ERROR",
         "detail": ",".join(proof["proof_revisions"]) or proof.get("error", "unavailable")},
        {"name": "product_workspace", "status": "OK" if configured else "WARNING",
         "detail": "configured" if configured else "required settings unavailable"},
        {"name": "product_object_root", "status": "OK" if object_root_usable else "ERROR",
         "detail": "writable directory" if object_root_usable else "missing or not writable"},
        {"name": "codex_provider", "status": provider_status, "detail": provider_detail},
    ]
    return {
        "dimensions": {
            "process_alive": True,
            **{f"operational_{key}": operational[key] for key in ("configured", "reachable", "authenticated", "migrations_current", "usable")},
            **{f"proof_{key}": proof[key] for key in ("configured", "reachable", "authenticated", "migrations_current", "usable")},
            "object_root_usable": object_root_usable,
            "product_loop_configured": configured,
            "provider_configured": provider_ready,
        },
        "lanes": {"operational": operational, "proof": proof},
        "reasons": reasons,
        "provider_live_uat": provider_live_uat,
        "provider_selection": provider_selection,
        "provider_live_evidence": live_evidence,
        "alive": True,
        "configured": configured,
        "usable": usable,
        "ready": ready,
        "degraded": usable and not ready,
        "blocked": not usable,
        "codex_adapter": provider_status,
        "checks": checks,
    }


def _live_evidence(settings: Settings, proof_database: str, provider_id: str) -> dict[str, Any]:
    engine = get_engine(lane_settings(settings, proof_database))
    try:
        with Session(engine) as session:
            return live_provider_evidence(session, provider_id)
    finally:
        engine.dispose()


def proof_rows(settings: Settings, statement: str) -> list[dict[str, Any]]:
    """Read rows from the separate proof authority lane; unconfigured proof authority fails closed."""
    proof_name = settings.product_database_name
    if not proof_name or proof_name == settings.operational_database_name:
        raise RuntimeError("PROOF_AUTHORITY_UNCONFIGURED")
    engine = get_engine(lane_settings(settings, proof_name))
    try:
        with Session(engine) as session:
            return _rows(session, statement)
    finally:
        engine.dispose()


def _directory_usable(value: str) -> bool:
    if not value:
        return False
    path = Path(value).expanduser()
    return path.is_absolute() and path.is_dir() and os.access(path, os.W_OK | os.X_OK)


def _pending_decisions(session: Session) -> list[dict[str, Any]]:
    gates = _rows(
        session,
        """SELECT id::text AS id, project_id::text AS project_id, gate_type,
                  required_action AS question, reason_codes, status,
                  requested_at AS created_at, 'project_human_gates' AS source
           FROM project_human_gates
           WHERE status IN ('OPEN', 'PENDING_APPROVAL')
           ORDER BY requested_at ASC, id ASC""",
    )
    decisions = _rows(
        session,
        """SELECT id::text AS id, project_id::text AS project_id,
                  decision_type, question, rationale, status, created_at,
                  'memory_decisions' AS source
           FROM memory_decisions WHERE status = 'PROPOSED'
           ORDER BY created_at ASC, id ASC""",
    )
    return [*gates, *decisions]


def _runs(session: Session, *, include_closed: bool = False) -> list[dict[str, Any]]:
    statuses = "('READY_FOR_ROUTING', 'ROUTING', 'ROUTED', 'QUEUED', 'RUNNING', 'VALIDATING', 'BLOCKED', 'FAILED', 'TIMED_OUT')"
    workers = _rows(
        session,
        f"""SELECT w.project_id::text AS project_id, w.id::text AS id,
                   w.result_summary AS name, w.status, w.error_code,
                   w.validation_status, w.queued_at AS created_at,
                   (w.status IN ('BLOCKED', 'FAILED', 'TIMED_OUT') OR w.validation_status = 'FAILED') AS human_action_required,
                   'worker_jobs' AS source
            FROM worker_jobs w WHERE w.status IN {statuses}
            ORDER BY w.queued_at DESC LIMIT 100""",
    )
    loops = _rows(
        session,
        """SELECT l.project_id::text AS project_id, l.id::text AS id,
                  g.objective AS name, l.status, l.updated_at AS created_at,
                  (l.status IN ('BLOCKED', 'WAITING_HUMAN')) AS human_action_required,
                  'project_goal_loops' AS source
           FROM project_goal_loops l
           JOIN project_goals g ON g.id = l.project_goal_id
           WHERE l.status IN ('ACTIVE', 'EVALUATING', 'PLANNING', 'AT_RISK', 'BLOCKED', 'WAITING_HUMAN')
           ORDER BY l.updated_at DESC LIMIT 100""",
    )
    for row in [*workers, *loops]:
        for field in ("name", "error_code"):
            if isinstance(row.get(field), str):
                row[field] = redact(row[field])[:500]
    return [*workers, *loops]


def _rows(session: Session, statement: str) -> list[dict[str, Any]]:
    rows = session.execute(text(statement)).mappings().all()
    return [_redact_tree(dict(row)) for row in rows]


def _redact_tree(value: Any) -> Any:
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, Mapping):
        return {key: _redact_tree(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_redact_tree(item) for item in value]
    return value

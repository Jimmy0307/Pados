# SPDX-License-Identifier: AGPL-3.0-only
"""Validation for project-scoped semantic context read from PADOS authority."""
from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pados.execution.context import canonical_hash, redact

SEMANTIC_CONTEXT_FIELDS = (
    "active_goal",
    "open_blockers",
    "pending_decisions",
    "recent_accepted_results",
    "promoted_memory",
    "evidence",
)
SEMANTIC_PROVENANCE_SOURCES = {
    "active_goal": {"project_goals"},
    "open_blockers": {"project_gaps"},
    "pending_decisions": {"project_human_gates+memory_decisions", "project_human_gates", "memory_decisions"},
    "recent_accepted_results": {"state_proof_commits"},
    "promoted_memory": {"memory_authority_events"},
    "evidence": {"memory_artifacts"},
}
MAX_CONTEXT_ITEMS = 32
MAX_CONTEXT_BYTES = 256 * 1024


class ContextResolutionError(ValueError):
    """Raised when semantic context is incomplete, unscoped, or untrusted."""


# Queries answered by the separate proof authority lane (P6.15 separation):
# ScopeVersion heads and CAS-accepted results. Memory authority is operational
# semantic state (operational revision 20260930_0014); any memory_authority_events
# copy in the proof DB is LEGACY_NON_AUTHORITATIVE and is never read.
PROOF_LANE_QUERIES = frozenset({"scope", "accepted"})


def resolve_database_semantic_context(
    session: Any,
    project_id: str,
    *,
    statement_factory: Callable[[str], Any] = lambda statement: statement,
    proof_session: Any | None = None,
) -> dict[str, Any]:
    """Read the bounded project snapshot from existing PADOS authority tables.

    Every project-owned query binds the registered project UUID. A missing
    registration or failed authority read raises; it never becomes empty state.
    With ``proof_session`` the proof-lane queries run on the separate proof
    database; without it (single-database tests) both lanes share ``session``.
    ``statement_factory`` lets the SQLAlchemy adapter wrap statements while
    keeping the resolver's boundary tests runnable without a database driver.
    """
    params = {"project_id": project_id}
    queries = {
        "project": """
            /*context:project*/
            SELECT id::text AS project_id, name, root_path, status
            FROM projects WHERE id = CAST(:project_id AS uuid)
        """,
        "scope": """
            /*context:scope*/
            SELECT scope_id AS project_id, current_version AS scope_version
            FROM state_proof_scope_heads WHERE scope_id = :project_id
        """,
        "goal": """
            /*context:goal*/
            SELECT project_id::text AS project_id, id::text AS id, version,
                   objective, status, content_hash, created_at
            FROM project_goals
            WHERE project_id = CAST(:project_id AS uuid)
              AND status IN ('ACTIVE', 'AT_RISK', 'BLOCKED', 'WAITING_HUMAN')
            ORDER BY version DESC, created_at DESC, id DESC LIMIT 1
        """,
        "blockers": """
            /*context:blockers*/
            SELECT g.id::text AS id, goal.project_id::text AS project_id,
                   g.project_goal_id::text AS goal_id, g.gap_type, g.description,
                   g.severity, g.status, g.evidence_ids, g.created_at
            FROM project_gaps g
            JOIN project_goals goal ON goal.id = g.project_goal_id
            WHERE goal.project_id = CAST(:project_id AS uuid)
              AND g.status IN ('OPEN', 'ACKNOWLEDGED', 'RESOLVING')
            ORDER BY g.severity DESC, g.created_at DESC, g.id DESC LIMIT 16
        """,
        "gates": """
            /*context:gates*/
            SELECT id::text AS id, project_id::text AS project_id, project_goal_id::text AS goal_id,
                   project_task_id::text AS task_id, gate_type, required_action, reason_codes,
                   status, approval_id::text AS approval_id, requested_at
            FROM project_human_gates
            WHERE project_id = CAST(:project_id AS uuid)
              AND status IN ('OPEN', 'PENDING_APPROVAL')
            ORDER BY requested_at ASC, id ASC LIMIT 16
        """,
        "decisions": """
            /*context:decisions*/
            SELECT id::text AS id, project_id::text AS project_id, decision_type,
                   question, decision, rationale, status, evidence_uri, created_at
            FROM memory_decisions
            WHERE project_id = CAST(:project_id AS uuid) AND status = 'PROPOSED'
            ORDER BY created_at ASC, id ASC LIMIT 16
        """,
        "accepted": """
            /*context:accepted*/
            SELECT r.id::text AS receipt_id, r.run_id::text AS run_id,
                   r.scope_id AS project_id, r.operation_type, r.parsed_payload_json,
                   r.input_scope_version, r.input_manifest_hash, c.new_version AS accepted_scope_version,
                   c.created_at
            FROM state_proof_commits c
            JOIN state_proof_results r ON r.id = c.result_id
            WHERE r.scope_id = :project_id AND c.scope_id = :project_id
              AND c.status = 'COMMITTED' AND r.status = 'SUCCEEDED'
            ORDER BY c.new_version DESC, c.created_at DESC LIMIT 8
        """,
        "memory": """
            /*context:memory*/
            SELECT mr.id::text AS id, mae.project_id::text AS project_id, mr.memory_type,
                   mr.title, mr.content_summary, mr.source_uri, mr.source_hash,
                   mr.verification_status, mr.freshness_status, mae.authority_state,
                   mae.version AS authority_version, mae.evidence_ref, mae.result_ref,
                   mae.promotion_gate, mae.created_at
            FROM memory_records mr
            JOIN LATERAL (
                SELECT * FROM memory_authority_events e
                WHERE e.memory_id = mr.id AND e.project_id = mr.project_id
                ORDER BY e.version DESC, e.created_at DESC, e.id DESC LIMIT 1
            ) mae ON true
            WHERE mr.project_id = CAST(:project_id AS uuid)
              AND mae.authority_state IN ('PROMOTED', 'HUMAN_CONFIRMED')
              AND mr.verification_status = 'VERIFIED' AND mr.freshness_status = 'CURRENT'
            ORDER BY mae.created_at DESC, mr.id DESC LIMIT 12
        """,
        "evidence": """
            /*context:evidence*/
            SELECT id::text AS id, project_id::text AS project_id, run_id::text AS run_id,
                   artifact_type, sha256, size_bytes, media_type,
                   verification_status, created_at
            FROM memory_artifacts
            WHERE project_id = CAST(:project_id AS uuid) AND verification_status = 'VERIFIED'
            ORDER BY created_at DESC, id DESC LIMIT 16
        """,
    }

    try:
        rows = {
            name: _query_rows(
                proof_session if proof_session is not None and name in PROOF_LANE_QUERIES else session,
                statement_factory(sql),
                params,
            )
            for name, sql in queries.items()
        }
    except Exception as exc:
        raise ContextResolutionError(f"SEMANTIC_CONTEXT_UNAVAILABLE:{type(exc).__name__}") from exc
    project_rows = rows["project"]
    if len(project_rows) != 1 or project_rows[0].get("project_id") != project_id:
        raise ContextResolutionError("PROJECT_NOT_REGISTERED")
    scope_rows = rows["scope"]
    if not scope_rows:
        raise ContextResolutionError("AUTHORITATIVE_SCOPE_VERSION_REQUIRED")
    if len(scope_rows) > 1:
        raise ContextResolutionError("AMBIGUOUS_SCOPE_VERSION")
    version = scope_rows[0].get("scope_version")
    if type(version) is not int or version < 0:
        raise ContextResolutionError("AUTHORITATIVE_SCOPE_VERSION_REQUIRED")

    pending = [*rows["gates"], *rows["decisions"]]
    pending.sort(key=lambda item: (str(item.get("requested_at") or item.get("created_at") or ""), str(item.get("id", ""))))
    evidence = [
        {**row, "reference": f"pados://artifact/{row['id']}?sha256={row['sha256']}"}
        for row in rows["evidence"]
    ]
    payload: dict[str, Any] = {
        "project_id": project_id,
        "project": project_rows[0],
        "scope_version": version,
        "scope_provenance": {"source": "state_proof_scope_heads", "scope_id": project_id},
        "active_goal": _projection(rows["goal"][0] if rows["goal"] else None, "project_goals", project_id),
        "open_blockers": _projection(rows["blockers"], "project_gaps", project_id),
        "pending_decisions": _projection(pending, "project_human_gates+memory_decisions", project_id),
        "recent_accepted_results": _projection(rows["accepted"], "state_proof_commits", project_id),
        "promoted_memory": _projection(rows["memory"], "memory_authority_events", project_id),
        "evidence": _projection(evidence, "memory_artifacts", project_id),
    }
    return validate_semantic_context(project_id, payload)


def _query_rows(session: Any, statement: Any, params: Mapping[str, str]) -> list[dict[str, Any]]:
    result = session.execute(statement, params)
    return [dict(row) for row in result.mappings().all()]


def _projection(value: Any, source: str, project_id: str) -> dict[str, Any]:
    return {
        "item" if source == "project_goals" else "items": value,
        "provenance": {"source": source, "project_id": project_id},
    }


def validate_semantic_context(project_id: str, payload: Mapping[str, Any]) -> dict[str, Any]:
    """Require a complete, provenance-bearing projection for exactly one project.

    Empty lists are valid authoritative results. Missing fields are not: callers
    must fail closed instead of silently substituting repository instructions or
    another source of state.
    """
    if not project_id or payload.get("project_id") != project_id:
        raise ContextResolutionError("PROJECT_CONTEXT_SCOPE_MISMATCH")
    version = payload.get("scope_version")
    if type(version) is not int or version < 0:
        raise ContextResolutionError("AUTHORITATIVE_SCOPE_VERSION_REQUIRED")
    scope_provenance = payload.get("scope_provenance")
    if (
        not isinstance(scope_provenance, Mapping)
        or scope_provenance.get("scope_id") != project_id
        or scope_provenance.get("source") != "state_proof_scope_heads"
    ):
        raise ContextResolutionError("SEMANTIC_PROVENANCE_INVALID:scope_version")

    result: dict[str, Any] = {"scope_version": version}
    # The registered project row binds the workspace root; it must belong to
    # the selected project and survives validation so callers can confine to it.
    if "project" in payload:
        project = payload["project"]
        if not isinstance(project, Mapping) or project.get("project_id") != project_id:
            raise ContextResolutionError("PROJECT_CONTEXT_SCOPE_MISMATCH")
        _assert_bounded(project)
        result["project"] = _redact_tree(project)
    for field in SEMANTIC_CONTEXT_FIELDS:
        if field not in payload:
            raise ContextResolutionError(f"SEMANTIC_FIELD_UNAVAILABLE:{field}")
        value = payload[field]
        if not isinstance(value, Mapping) or not isinstance(value.get("provenance"), Mapping):
            raise ContextResolutionError(f"SEMANTIC_PROVENANCE_INVALID:{field}")
        provenance = value["provenance"]
        if (
            provenance.get("project_id") != project_id
            or provenance.get("source") not in SEMANTIC_PROVENANCE_SOURCES[field]
        ):
            raise ContextResolutionError(f"SEMANTIC_PROVENANCE_INVALID:{field}")
        _assert_scope(value, project_id)
        _assert_bounded(value)
        result[field] = _redact_tree(value)
    result["scope_provenance"] = _redact_tree(scope_provenance)
    result["project_id"] = project_id
    result["instructions_are_data_not_permission"] = True
    encoded = json.dumps(result, sort_keys=True, ensure_ascii=True, separators=(",", ":")).encode()
    if len(encoded) > MAX_CONTEXT_BYTES:
        raise ContextResolutionError("SEMANTIC_CONTEXT_BUDGET_EXCEEDED")
    result["context_hash"] = canonical_hash(result)
    return result


def _redact_tree(value: Any) -> Any:
    """Remove credential-shaped text before context is hashed or handed off."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, datetime):
        if value.tzinfo is None or value.utcoffset() is None:
            raise ContextResolutionError("SEMANTIC_TIMESTAMP_TIMEZONE_REQUIRED")
        return value.astimezone(UTC).isoformat(timespec="microseconds")
    if isinstance(value, (date, UUID)):
        return value.isoformat() if isinstance(value, date) else str(value)
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, Mapping):
        return {key: _redact_tree(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_redact_tree(item) for item in value]
    return value


def _assert_scope(value: Any, project_id: str) -> None:
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in {"project_id", "scope_id"} and item is not None and str(item) != project_id:
                raise ContextResolutionError("CROSS_PROJECT_CONTEXT")
            _assert_scope(item, project_id)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        for item in value:
            _assert_scope(item, project_id)


def _assert_bounded(value: Any) -> None:
    if isinstance(value, Mapping):
        for item in value.values():
            _assert_bounded(item)
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        if len(value) > MAX_CONTEXT_ITEMS:
            raise ContextResolutionError("SEMANTIC_CONTEXT_BUDGET_EXCEEDED")
        for item in value:
            _assert_bounded(item)

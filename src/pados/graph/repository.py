# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any, cast
from uuid import uuid4

from sqlalchemy import text
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from pados.graph.schemas import (
    GroupCreate,
    SystemEdgeCreate,
    SystemNodeCreate,
    SystemNodeUpdate,
    WorkspaceCreate,
)


def _one(session: Session, sql: str, values: Mapping[str, Any]) -> dict[str, Any]:
    row = session.execute(text(sql), dict(values)).mappings().one_or_none()
    if row is None:
        raise LookupError("graph record not found")
    return dict(row)


def _many(session: Session, sql: str, values: Mapping[str, Any]) -> list[dict[str, Any]]:
    return [dict(row) for row in session.execute(text(sql), dict(values)).mappings().all()]


def create_workspace(session: Session, payload: WorkspaceCreate) -> dict[str, Any]:
    workspace_id = str(uuid4())
    return _one(
        session,
        """
        INSERT INTO graph_workspaces(id, project_id, name, description)
        VALUES (CAST(:id AS uuid), CAST(:project_id AS uuid), :name, :description)
        RETURNING *
        """,
        {
            "id": workspace_id,
            "project_id": payload.project_id,
            "name": payload.name,
            "description": payload.description,
        },
    )


def list_workspaces(session: Session) -> list[dict[str, Any]]:
    return _many(
        session,
        """
        SELECT w.*,
               (SELECT count(*) FROM graph_nodes n WHERE n.workspace_id = w.id) AS node_count,
               (SELECT count(*) FROM graph_edges e WHERE e.workspace_id = w.id) AS edge_count
        FROM graph_workspaces w
        WHERE w.status = 'ACTIVE'
        ORDER BY w.updated_at DESC, w.created_at
        """,
        {},
    )


def get_workspace(session: Session, workspace_id: str) -> dict[str, Any]:
    return _one(
        session,
        "SELECT * FROM graph_workspaces WHERE id = CAST(:id AS uuid)",
        {"id": workspace_id},
    )


def touch_workspace(session: Session, workspace_id: str) -> None:
    session.execute(
        text(
            "UPDATE graph_workspaces SET updated_at = now(), version = version + 1 "
            "WHERE id = CAST(:id AS uuid)"
        ),
        {"id": workspace_id},
    )


def list_nodes(session: Session, workspace_id: str) -> list[dict[str, Any]]:
    return _many(
        session,
        "SELECT * FROM graph_nodes WHERE workspace_id = CAST(:workspace_id AS uuid) ORDER BY created_at, id",
        {"workspace_id": workspace_id},
    )


def get_nodes(session: Session, workspace_id: str, node_ids: Sequence[str]) -> list[dict[str, Any]]:
    if not node_ids:
        return []
    return _many(
        session,
        """
        SELECT * FROM graph_nodes
        WHERE workspace_id = CAST(:workspace_id AS uuid)
          AND id = ANY(CAST(:node_ids AS uuid[]))
        ORDER BY created_at, id
        """,
        {"workspace_id": workspace_id, "node_ids": list(node_ids)},
    )


def get_node(session: Session, workspace_id: str, node_id: str) -> dict[str, Any]:
    return _one(
        session,
        """
        SELECT * FROM graph_nodes
        WHERE workspace_id = CAST(:workspace_id AS uuid) AND id = CAST(:node_id AS uuid)
        """,
        {"workspace_id": workspace_id, "node_id": node_id},
    )


def _assert_group_in_workspace(session: Session, workspace_id: str, group_id: str) -> None:
    _one(
        session,
        "SELECT id FROM graph_groups WHERE id = CAST(:group_id AS uuid) AND workspace_id = CAST(:workspace_id AS uuid)",
        {"group_id": group_id, "workspace_id": workspace_id},
    )


def create_node(session: Session, workspace_id: str, payload: SystemNodeCreate) -> dict[str, Any]:
    if payload.group_id is not None:
        _assert_group_in_workspace(session, workspace_id, payload.group_id)
    node_id = str(uuid4())
    row = _one(
        session,
        """
        INSERT INTO graph_nodes(
            id, workspace_id, group_id, semantic_type, title, content, x, y,
            provenance, assurance_status, idea_state, source_capability_id,
            reuse_mode, source_version, parent_capability_id, internal_ref_type,
            internal_ref_id, lineage_json, metadata_json
        )
        VALUES (
            CAST(:id AS uuid), CAST(:workspace_id AS uuid), CAST(:group_id AS uuid),
            :semantic_type, :title, :content, :x, :y, :provenance,
            :assurance_status, :idea_state, :source_capability_id, :reuse_mode,
            :source_version, :parent_capability_id, :internal_ref_type,
            :internal_ref_id, CAST(:lineage AS jsonb), CAST(:metadata AS jsonb)
        )
        RETURNING *
        """,
        {
            "id": node_id,
            "workspace_id": workspace_id,
            "group_id": payload.group_id,
            "semantic_type": payload.semantic_type.value,
            "title": payload.title,
            "content": payload.content,
            "x": payload.x,
            "y": payload.y,
            "provenance": payload.provenance.value,
            "assurance_status": payload.assurance_status.value,
            "idea_state": payload.idea_state.value if payload.idea_state else None,
            "source_capability_id": payload.source_capability_id,
            "reuse_mode": payload.reuse_mode.value if payload.reuse_mode else None,
            "source_version": payload.source_version,
            "parent_capability_id": payload.parent_capability_id,
            "internal_ref_type": payload.internal_ref_type,
            "internal_ref_id": payload.internal_ref_id,
            "lineage": json.dumps(payload.lineage),
            "metadata": json.dumps(payload.metadata),
        },
    )
    touch_workspace(session, workspace_id)
    return row


def update_node(
    session: Session,
    workspace_id: str,
    node_id: str,
    payload: SystemNodeUpdate,
) -> dict[str, Any]:
    changes = payload.model_dump(exclude_unset=True)
    expected_version = int(changes.pop("version"))
    clear_group = bool(changes.pop("clear_group", False))
    if changes.get("group_id") is not None:
        _assert_group_in_workspace(session, workspace_id, str(changes["group_id"]))
    assignments: list[str] = []
    values: dict[str, Any] = {
        "workspace_id": workspace_id,
        "node_id": node_id,
        "expected_version": expected_version,
    }
    for field in ("title", "content", "x", "y", "assurance_status", "idea_state"):
        if field in changes:
            value = changes[field]
            values[field] = value.value if hasattr(value, "value") else value
            assignments.append(f"{field} = :{field}")
    if "metadata" in changes:
        values["metadata"] = json.dumps(changes["metadata"] or {})
        assignments.append("metadata_json = CAST(:metadata AS jsonb)")
    if clear_group:
        assignments.append("group_id = NULL")
    elif "group_id" in changes:
        assignments.append("group_id = CAST(:group_id AS uuid)")
        values["group_id"] = changes["group_id"]
    if not assignments:
        return get_node(session, workspace_id, node_id)
    assignments.extend(("updated_at = now()", "version = version + 1"))
    row = _one(
        session,
        f"""
        UPDATE graph_nodes SET {", ".join(assignments)}
        WHERE workspace_id = CAST(:workspace_id AS uuid)
          AND id = CAST(:node_id AS uuid)
          AND version = :expected_version
        RETURNING *
        """,
        values,
    )
    touch_workspace(session, workspace_id)
    return row


def create_artifact(
    session: Session,
    *,
    artifact_id: str,
    artifact_type: str,
    relative_path: str,
    sha256: str,
    size_bytes: int,
) -> dict[str, Any]:
    """Register a durable file using the existing generic artifact contract."""

    return _one(
        session,
        """
        INSERT INTO artifacts(id, task_id, artifact_type, relative_path, sha256, size_bytes)
        VALUES (CAST(:id AS uuid), NULL, :artifact_type, :relative_path, :sha256, :size_bytes)
        RETURNING *
        """,
        {
            "id": artifact_id,
            "artifact_type": artifact_type,
            "relative_path": relative_path,
            "sha256": sha256,
            "size_bytes": size_bytes,
        },
    )


def delete_node(session: Session, workspace_id: str, node_id: str) -> bool:
    result = cast(CursorResult[Any], session.execute(
        text(
            "DELETE FROM graph_nodes WHERE workspace_id = CAST(:workspace_id AS uuid) "
            "AND id = CAST(:node_id AS uuid)"
        ),
        {"workspace_id": workspace_id, "node_id": node_id},
    ))
    deleted = bool(result.rowcount)
    if deleted:
        touch_workspace(session, workspace_id)
    return deleted


def list_edges(session: Session, workspace_id: str) -> list[dict[str, Any]]:
    return _many(
        session,
        "SELECT * FROM graph_edges WHERE workspace_id = CAST(:workspace_id AS uuid) ORDER BY created_at, id",
        {"workspace_id": workspace_id},
    )


def create_edge(session: Session, workspace_id: str, payload: SystemEdgeCreate) -> dict[str, Any]:
    edge_id = str(uuid4())
    row = _one(
        session,
        """
        INSERT INTO graph_edges(
            id, workspace_id, source_node_id, target_node_id, relationship_type,
            label, provenance, metadata_json
        )
        SELECT CAST(:id AS uuid), CAST(:workspace_id AS uuid), CAST(:source AS uuid),
               CAST(:target AS uuid), :relationship_type, :label, :provenance,
               CAST(:metadata AS jsonb)
        WHERE EXISTS (
            SELECT 1 FROM graph_nodes
            WHERE id = CAST(:source AS uuid) AND workspace_id = CAST(:workspace_id AS uuid)
        ) AND EXISTS (
            SELECT 1 FROM graph_nodes
            WHERE id = CAST(:target AS uuid) AND workspace_id = CAST(:workspace_id AS uuid)
        )
        RETURNING *
        """,
        {
            "id": edge_id,
            "workspace_id": workspace_id,
            "source": payload.source_node_id,
            "target": payload.target_node_id,
            "relationship_type": payload.relationship_type,
            "label": payload.label,
            "provenance": payload.provenance.value,
            "metadata": json.dumps(payload.metadata),
        },
    )
    touch_workspace(session, workspace_id)
    return row


def delete_edge(session: Session, workspace_id: str, edge_id: str) -> bool:
    result = cast(CursorResult[Any], session.execute(
        text(
            "DELETE FROM graph_edges WHERE workspace_id = CAST(:workspace_id AS uuid) "
            "AND id = CAST(:edge_id AS uuid)"
        ),
        {"workspace_id": workspace_id, "edge_id": edge_id},
    ))
    deleted = bool(result.rowcount)
    if deleted:
        touch_workspace(session, workspace_id)
    return deleted


def list_groups(session: Session, workspace_id: str) -> list[dict[str, Any]]:
    return _many(
        session,
        "SELECT * FROM graph_groups WHERE workspace_id = CAST(:workspace_id AS uuid) ORDER BY created_at, id",
        {"workspace_id": workspace_id},
    )


def create_group(session: Session, workspace_id: str, payload: GroupCreate) -> dict[str, Any]:
    group_id = str(uuid4())
    row = _one(
        session,
        """
        INSERT INTO graph_groups(id, workspace_id, title, x, y, width, height)
        VALUES (CAST(:id AS uuid), CAST(:workspace_id AS uuid), :title, :x, :y, :width, :height)
        RETURNING *
        """,
        {
            "id": group_id,
            "workspace_id": workspace_id,
            "title": payload.title,
            "x": payload.x,
            "y": payload.y,
            "width": payload.width,
            "height": payload.height,
        },
    )
    if payload.node_ids:
        result = cast(CursorResult[Any], session.execute(
            text(
                """
                UPDATE graph_nodes SET group_id = CAST(:group_id AS uuid), updated_at = now(),
                       version = version + 1
                WHERE workspace_id = CAST(:workspace_id AS uuid)
                  AND id = ANY(CAST(:node_ids AS uuid[]))
                """
            ),
            {"group_id": group_id, "workspace_id": workspace_id, "node_ids": payload.node_ids},
        ))
        if int(result.rowcount or 0) != len(payload.node_ids):
            raise LookupError("one or more grouped nodes are outside the workspace")
    touch_workspace(session, workspace_id)
    return row


def search_nodes(session: Session, workspace_id: str, query: str, limit: int) -> list[dict[str, Any]]:
    return _many(
        session,
        """
        SELECT * FROM graph_nodes
        WHERE workspace_id = CAST(:workspace_id AS uuid)
          AND (title ILIKE :query OR content ILIKE :query)
        ORDER BY updated_at DESC
        LIMIT :limit
        """,
        {"workspace_id": workspace_id, "query": f"%{query}%", "limit": limit},
    )


def insert_intent(
    session: Session,
    *,
    workspace_id: str,
    action: str,
    selected_node_ids: Sequence[str],
    selection_context: Mapping[str, Any],
    interpreted_goal: str,
    plan: Mapping[str, Any],
    governance: Mapping[str, Any],
    status: str,
    harness_loop_id: str | None = None,
) -> dict[str, Any]:
    return _one(
        session,
        """
        INSERT INTO graph_intents(
            workspace_id, action, selected_node_ids, selection_context, interpreted_goal,
            plan_json, governance_json, status, harness_loop_id
        ) VALUES (
            CAST(:workspace_id AS uuid), :action, CAST(:selected_node_ids AS jsonb),
            CAST(:selection_context AS jsonb), :interpreted_goal, CAST(:plan AS jsonb),
            CAST(:governance AS jsonb), :status, CAST(:harness_loop_id AS uuid)
        ) RETURNING *
        """,
        {
            "workspace_id": workspace_id,
            "action": action,
            "selected_node_ids": json.dumps(list(selected_node_ids)),
            "selection_context": json.dumps(dict(selection_context)),
            "interpreted_goal": interpreted_goal,
            "plan": json.dumps(dict(plan)),
            "governance": json.dumps(dict(governance)),
            "status": status,
            "harness_loop_id": harness_loop_id,
        },
    )


def create_cognitive_item(
    session: Session,
    *,
    workspace_id: str,
    operation: str,
    selected_node_ids: Sequence[str],
    selection_context: Mapping[str, Any],
    idea_node_id: str | None = None,
) -> dict[str, Any]:
    return _one(
        session,
        """
        INSERT INTO cognitive_work_items(
            workspace_id, idea_node_id, operation, selected_node_ids, selection_context
        ) VALUES (
            CAST(:workspace_id AS uuid), CAST(:idea_node_id AS uuid), :operation,
            CAST(:selected_node_ids AS jsonb), CAST(:selection_context AS jsonb)
        ) RETURNING *
        """,
        {
            "workspace_id": workspace_id,
            "idea_node_id": idea_node_id,
            "operation": operation,
            "selected_node_ids": json.dumps(list(selected_node_ids)),
            "selection_context": json.dumps(dict(selection_context)),
        },
    )


def get_cognitive_item(session: Session, item_id: str) -> dict[str, Any]:
    return _one(
        session,
        "SELECT * FROM cognitive_work_items WHERE id = CAST(:id AS uuid)",
        {"id": item_id},
    )


def list_cognitive_items(session: Session, workspace_id: str) -> list[dict[str, Any]]:
    return _many(
        session,
        """
        SELECT * FROM cognitive_work_items
        WHERE workspace_id = CAST(:workspace_id AS uuid)
        ORDER BY created_at DESC
        """,
        {"workspace_id": workspace_id},
    )


def mark_cognitive_processing(session: Session, item_id: str) -> dict[str, Any]:
    return _one(
        session,
        """
        UPDATE cognitive_work_items
        SET status = 'PROCESSING', started_at = now(), error_code = NULL
        WHERE id = CAST(:id AS uuid) AND status = 'QUEUED'
        RETURNING *
        """,
        {"id": item_id},
    )


def complete_cognitive_item(
    session: Session,
    item_id: str,
    *,
    provider: str,
    candidates: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    return _one(
        session,
        """
        UPDATE cognitive_work_items
        SET status = 'COMPLETED', provider = :provider,
            candidate_outputs = CAST(:candidates AS jsonb), completed_at = now()
        WHERE id = CAST(:id AS uuid) AND status = 'PROCESSING'
        RETURNING *
        """,
        {"id": item_id, "provider": provider, "candidates": json.dumps(list(candidates))},
    )


def fail_cognitive_item(
    session: Session,
    item_id: str,
    *,
    error_code: str,
    status: str = "BLOCKED",
) -> dict[str, Any]:
    if status not in {"BLOCKED", "FAILED"}:
        raise ValueError("invalid cognitive failure status")
    return _one(
        session,
        """
        UPDATE cognitive_work_items
        SET status = :status, error_code = :error_code, completed_at = now()
        WHERE id = CAST(:id AS uuid) AND status IN ('QUEUED', 'PROCESSING')
        RETURNING *
        """,
        {"id": item_id, "error_code": error_code, "status": status},
    )


def harness_loop(session: Session, loop_id: str) -> dict[str, Any]:
    return _one(
        session,
        """
        SELECT id, project_id, approval_id, status, active_plan_id, task_goal_id
        FROM task_loops WHERE id = CAST(:id AS uuid)
        """,
        {"id": loop_id},
    )

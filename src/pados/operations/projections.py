# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic, non-authoritative Mission Control and Inbox projections."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

_ACTIVE_RUN_STATES = {
    "ACTIVE", "CREATED", "EVALUATING", "PLANNING", "QUEUED", "ROUTED",
    "RUNNING", "VALIDATING", "WORKING",
}
_BLOCKED_RUN_STATES = {"BLOCKED", "FAILED", "TIMED_OUT", "WAITING_HUMAN"}
_PENDING_APPROVAL_STATES = {"OPEN", "PENDING", "PENDING_APPROVAL"}


def mission_control_projection(
    projects: Sequence[Mapping[str, Any]],
    goals: Sequence[Mapping[str, Any]],
    blockers: Sequence[Mapping[str, Any]],
    decisions: Sequence[Mapping[str, Any]],
    runs: Sequence[Mapping[str, Any]],
    accepted_results: Sequence[Mapping[str, Any]],
    system_health: Mapping[str, Any],
) -> dict[str, Any]:
    """Group authoritative rows by registered project without cross-project joins."""
    project_rows = [dict(project) for project in projects if project.get("id")]
    project_rows.sort(key=lambda row: (str(row.get("name") or "").casefold(), str(row["id"])))
    result = []
    for project in project_rows:
        project_id = str(project["id"])
        matching_goals = _for_project(goals, project_id)
        matching_goals.sort(key=lambda row: (int(row.get("version") or 0), str(row.get("created_at") or "")), reverse=True)
        matching_blockers = _for_project(blockers, project_id)
        matching_blockers.sort(key=lambda row: (-_priority(row.get("severity")), str(row.get("id") or "")))
        matching_decisions = _for_project(decisions, project_id)
        matching_runs = [
            row for row in _for_project(runs, project_id)
            if str(row.get("status") or "").upper() in _ACTIVE_RUN_STATES
        ]
        matching_accepted = _for_project(accepted_results, project_id)
        matching_accepted.sort(
            key=lambda row: (int(row.get("accepted_scope_version") or 0), str(row.get("created_at") or "")),
            reverse=True,
        )
        result.append(
            {
                "project_id": project_id,
                "project": project,
                "project_status": project.get("status", "UNKNOWN"),
                "current_goal": matching_goals[0] if matching_goals else None,
                "blockers": matching_blockers[:16],
                "next_decisions": matching_decisions[:16],
                "active_runs": matching_runs[:16],
                "recent_accepted_results": matching_accepted[:8],
            }
        )
    return {
        "projects": result,
        "system_health": _health_projection(system_health),
        "projection_only": True,
        "authority": "PADOS_POSTGRESQL_AND_PROJECT_REGISTRY",
    }


def decision_inbox_projection(
    projects: Sequence[Mapping[str, Any]],
    decisions: Sequence[Mapping[str, Any]],
    blockers: Sequence[Mapping[str, Any]],
    runs: Sequence[Mapping[str, Any]],
    approvals: Sequence[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Derive human actions from current PADOS project, run, and decision rows."""
    project_by_id = {str(row["id"]): dict(row) for row in projects if row.get("id")}
    items: list[dict[str, Any]] = []
    for row in decisions:
        project_id = _known_project_id(row, project_by_id)
        if project_id is None:
            continue
        items.append(_inbox_item(
            project_by_id[project_id], row, item_type="DECISION",
            item=str(row.get("question") or row.get("item") or row.get("id") or "Decision"),
            reason=str(row.get("source") or row.get("decision_type") or "PADOS memory_decisions"),
            action="Review and resolve this project decision",
            target=f"/control/projects/{project_id}",
            priority=row.get("priority", 80),
        ))
    for row in blockers:
        project_id = _known_project_id(row, project_by_id)
        if project_id is None:
            continue
        items.append(_inbox_item(
            project_by_id[project_id], row, item_type="BLOCKER",
            item=str(row.get("description") or row.get("item") or row.get("id") or "Project blocker"),
            reason=str(row.get("source") or row.get("gap_type") or "PADOS project gap"),
            action="Resolve or explicitly disposition this project blocker",
            target=f"/control/projects/{project_id}",
            priority=row.get("priority", row.get("severity", 70)),
        ))
    for row in approvals:
        project_id = _known_project_id(row, project_by_id)
        if project_id is None or str(row.get("status") or "").upper() not in _PENDING_APPROVAL_STATES:
            continue
        approval_id = str(row.get("id") or row.get("approval_id") or "")
        if not approval_id:
            continue
        items.append(_inbox_item(
            project_by_id[project_id], row, item_type="APPROVAL",
            item=str(row.get("item") or row.get("title") or row.get("scope_type") or approval_id),
            reason=str(row.get("reason") or row.get("source") or "PADOS approval gate"),
            action="Approve, reject, or revise the governed work package",
            target=f"/control/approvals/{approval_id}", priority=row.get("priority", 90),
        ))
    for row in runs:
        project_id = _known_project_id(row, project_by_id)
        if project_id is None:
            continue
        status = str(row.get("status") or "").upper()
        validation = str(row.get("validation_status") or row.get("validation_result") or "").upper()
        run_id = str(row.get("id") or row.get("run_id") or "")
        if not run_id:
            continue
        if status in _BLOCKED_RUN_STATES:
            items.append(_inbox_item(
                project_by_id[project_id], row, item_type="EXECUTION",
                item=str(row.get("name") or row.get("goal") or run_id),
                reason=str(row.get("reason") or row.get("error_code") or status),
                action="Inspect the blocked execution and choose the next step",
                target=f"/product-loop/runs/{run_id}", priority=row.get("priority", 85),
            ))
        if validation in {"FAILED", "BLOCKED"} and row.get("human_action_required", True):
            items.append(_inbox_item(
                project_by_id[project_id], row, item_type="VALIDATION",
                item=str(row.get("name") or row.get("goal") or run_id),
                reason=str(row.get("validation_reason") or "Deterministic validation requires direction"),
                action="Review validation evidence and decide whether to revise or stop",
                target=f"/product-loop/runs/{run_id}", priority=row.get("priority", 88),
            ))
    items.sort(key=lambda item: (-item["priority"], item["project"].casefold(), item["type"], item["id"]))
    return items


def _for_project(rows: Sequence[Mapping[str, Any]], project_id: str) -> list[dict[str, Any]]:
    return [dict(row) for row in rows if str(row.get("project_id") or "") == project_id]


def _known_project_id(row: Mapping[str, Any], project_by_id: Mapping[str, Any]) -> str | None:
    project_id = str(row.get("project_id") or "")
    return project_id if project_id in project_by_id else None


_INBOX_CATEGORIES = {
    "DECISION": "HUMAN_DECISION_REQUIRED",
    "APPROVAL": "APPROVAL_REQUIRED",
    "VALIDATION": "VALIDATION_FAILED",
    "EXECUTION": "EXECUTION_BLOCKED",
    "BLOCKER": "PROJECT_BLOCKER",
}
_INBOX_RECORDS = {
    "DECISION": "memory_decisions",
    "APPROVAL": "control_approvals",
    "VALIDATION": "worker_jobs",
    "EXECUTION": "worker_jobs",
    "BLOCKER": "project_gaps",
}


def _inbox_item(
    project: Mapping[str, Any],
    row: Mapping[str, Any],
    *,
    item_type: str,
    item: str,
    reason: str,
    action: str,
    target: str,
    priority: Any,
) -> dict[str, Any]:
    project_id = str(project["id"])
    identity = str(row.get("id") or row.get("run_id") or row.get("approval_id") or item)
    return {
        "id": f"{item_type.lower()}:{identity}",
        "project_id": project_id,
        "project": str(project.get("name") or project.get("slug") or project_id),
        "item": item[:500],
        "type": item_type,
        "category": _INBOX_CATEGORIES[item_type],
        "priority": _priority(priority),
        "status": str(row.get("status") or "OPEN"),
        "reason_source": reason[:500],
        "required_human_action": action,
        "target": target,
        "provenance": {
            "project_id": project_id,
            "record": str(row.get("source") or _INBOX_RECORDS[item_type]),
            "record_id": identity,
        },
        "projection_only": True,
    }


def _priority(value: Any) -> int:
    if isinstance(value, (int, float)):
        return max(0, min(100, int(value)))
    return {"CRITICAL": 100, "HIGH": 85, "MEDIUM": 60, "LOW": 30}.get(str(value).upper(), 50)


def _health_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    dimensions = {
        name: value.get(name) is True
        for name in ("alive", "configured", "usable", "ready", "degraded", "blocked")
    }
    state = next(
        (name for name in ("blocked", "degraded", "ready", "usable", "configured", "alive") if dimensions[name]),
        "unavailable",
    )
    return {**dimensions, "state": state, "checks": value.get("checks", [])}

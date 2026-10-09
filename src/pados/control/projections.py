# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import json
from typing import Any

START = "<!-- PADOS:GENERATED:START -->"
END = "<!-- PADOS:GENERATED:END -->"


def render_project_markdown(project: dict[str, object], existing: str | None = None) -> str:
    generated = "\n".join(
        [
            START,
            f"# {project.get('name', project.get('project_id', 'Project'))}",
            "",
            f"- Current objective: {project.get('objective', 'Not set')}",
            f"- Status: {project.get('status', 'active')}",
            f"- Phase: {project.get('phase', 'P6.8')}",
            f"- Health: {project.get('health', 'unknown')}",
            f"- Last verified: {project.get('last_verified', 'unknown')}",
            f"- Plan summary: {project.get('plan_summary', 'Not set')}",
            f"- Recent changes: {project.get('recent_changes', 'None recorded')}",
            f"- Blockers: {project.get('blockers', 'None recorded')}",
            f"- Next decision: {project.get('next_decision', 'None pending')}",
            f"- Approvals: {project.get('approvals', 'None pending')}",
            f"- Current Worker Job: {project.get('current_worker_job', 'None')}",
            f"- Selected Worker: {project.get('selected_worker', 'None')}",
            f"- Routing Explanation: {project.get('routing_explanation', 'None')}",
            f"- Job Status: {project.get('job_status', 'None')}",
            f"- Validation Status: {project.get('validation_status', 'None')}",
            f"- Latest Execution Result: {project.get('latest_execution_result', 'None')}",
            f"- Task Goal: {project.get('task_goal', 'None')}",
            f"- Goal success criteria: {project.get('goal_success_criteria', 'None')}",
            f"- Active Plan Version: {project.get('active_plan_version', 'None')}",
            f"- Current Step: {project.get('current_step', 'None')}",
            f"- Loop Iteration: {project.get('loop_iteration', 'None')}",
            f"- Loop Progress: {project.get('loop_progress', 'None')}",
            f"- Progress Delta: {project.get('progress_delta', 'None')}",
            f"- Loop Decision: {project.get('loop_decision', 'None')}",
            f"- Human Action Required: {project.get('human_action_required', 'None')}",
            f"- Stop Limits: {project.get('stop_limits', 'None')}",
            f"- Changed Files: {project.get('changed_files', 'None')}",
            f"- Remaining Risks: {project.get('remaining_risks', 'None')}",
            f"- Artifacts: {project.get('artifacts', 'None linked')}",
            f"- Context provenance: {project.get('context_provenance', 'PADOS PostgreSQL')}",
            f"- Message/work request input: {project.get('message_input', 'Use Control API')}",
            END,
        ]
    )
    if existing is None or existing == "":
        return generated + "\n\n## My Notes\n"
    if existing.count(START) != 1 or existing.count(END) != 1 or existing.index(START) > existing.index(END):
        raise ValueError("projection conflict: corrupt markdown markers")
    before = existing[: existing.index(START)]
    after = existing[existing.index(END) + len(END) :]
    return before + generated + after


def render_command_center_canvas(
    projects: list[dict[str, object]],
    existing: dict[str, Any] | None = None,
) -> dict[str, Any]:
    canvas = existing or {"nodes": [], "edges": []}
    if not isinstance(canvas.get("nodes", []), list) or not isinstance(canvas.get("edges", []), list):
        raise ValueError("projection conflict: corrupt canvas")  # noqa: TRY004 - API maps ValueError to 409 conflict
    user_nodes = [node for node in canvas["nodes"] if not str(node.get("id", "")).startswith("pados-managed-")]
    managed_positions = {
        node["id"]: {"x": node.get("x", 0), "y": node.get("y", 0)}
        for node in canvas["nodes"]
        if str(node.get("id", "")).startswith("pados-managed-")
    }
    sections = [
        "Today",
        "Needs Decision",
        "Active Projects",
        "Blocked Projects",
        "Recently Changed",
        "Pending Work Requests",
        "Pending Approvals",
        "Running Jobs",
        "Queued Jobs",
        "Failed Jobs",
        "Worker Availability",
        "Active Task Loops",
        "Waiting for Human",
        "No Progress",
        "Failed Loops",
        "Recently Completed Loops",
    ]
    managed_nodes: list[dict[str, object]] = []
    for index, section in enumerate(sections):
        node_id = "pados-managed-" + section.lower().replace(" ", "-")
        pos = managed_positions.get(node_id, {"x": 0, "y": index * 160})
        managed_nodes.append(
            {
                "id": node_id,
                "type": "text",
                "text": section,
                "x": pos["x"],
                "y": pos["y"],
                "width": 260,
                "height": 120,
            }
        )
    for index, project in enumerate(projects):
        project_id = str(project.get("project_id", project.get("id", index)))
        node_id = f"pados-managed-project-{project_id}"
        pos = managed_positions.get(node_id, {"x": 360, "y": index * 160})
        managed_nodes.append(
            {
                "id": node_id,
                "type": "file",
                "file": f"PADOS/Projects/{project_id}/CONTROL.md",
                "x": pos["x"],
                "y": pos["y"],
                "width": 320,
                "height": 140,
            }
        )
    user_edges = [edge for edge in canvas["edges"] if not str(edge.get("id", "")).startswith("pados-managed-")]
    return {"nodes": managed_nodes + user_nodes, "edges": user_edges}


def canvas_json(canvas: dict[str, Any]) -> str:
    return json.dumps(canvas, indent=2, sort_keys=True)

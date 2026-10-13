# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from pados.memory.security import assert_no_secret
from pados.worker.schemas import WorkerHandoff


def stable_json(payload: Any) -> str:
    return json.dumps(payload, indent=2, sort_keys=True)


def packet_hash(payload: dict[str, Any]) -> str:
    return hashlib.sha256(stable_json(payload).encode("utf-8")).hexdigest().upper()


def build_handoff(
    *,
    job_id: str,
    project_id: str,
    request: dict[str, Any],
    context_packet: dict[str, Any],
    allowed_actions: list[str],
    prohibited_actions: list[str],
    allowed_paths: list[str],
    validation_commands: list[str],
) -> WorkerHandoff:
    packet = context_packet["context_packet"]
    selected_json = stable_json(packet)
    assert_no_secret(selected_json, field_name="worker_handoff")
    draft = {
        "job_id": job_id,
        "project_id": project_id,
        "request": request,
        "success_criteria": "Complete exactly the approved request and satisfy validation policy.",
        "constraints": ["no merge", "no push", "no release", "stay inside allowed paths"],
        "allowed_actions": allowed_actions,
        "prohibited_actions": prohibited_actions,
        "relevant_facts": packet.get("verified_facts", []),
        "locked_decisions": packet.get("locked_decisions", []),
        "recent_failures": packet.get("known_failures", []),
        "artifacts": packet.get("relevant_artifacts", []),
        "allowed_paths": allowed_paths,
        "validation_commands": validation_commands,
        "output_contract": {
            "required": ["result_summary", "changed_files", "validation_results", "remaining_risks"],
            "forbidden": ["merge", "push", "release", "deploy"],
        },
        "packet_hash": "",
        "token_estimate": int(packet.get("estimated_tokens", 0)),
        "provenance": packet.get("provenance", {}),
    }
    draft["packet_hash"] = packet_hash(draft)
    return WorkerHandoff.model_validate(draft)


def write_handoff_artifacts(handoff: WorkerHandoff, root: Path) -> tuple[Path, Path]:
    root.mkdir(parents=True, exist_ok=True)
    json_path = root / "handoff.json"
    md_path = root / "handoff.md"
    json_path.write_text(stable_json(handoff.model_dump()), encoding="utf-8")
    lines = [
        f"# Worker Handoff {handoff.job_id}",
        "",
        f"- Project: {handoff.project_id}",
        f"- Packet hash: {handoff.packet_hash}",
        f"- Token estimate: {handoff.token_estimate}",
        "",
        "## Request",
        str(handoff.request.get("title", handoff.request.get("body", ""))),
        "",
        "## Constraints",
    ]
    lines.extend(f"- {item}" for item in handoff.constraints)
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return json_path, md_path

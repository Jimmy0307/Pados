# SPDX-License-Identifier: AGPL-3.0-only
import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

from sqlalchemy import text
from sqlalchemy.orm import Session

from pados.core.context_compiler import context_hash
from pados.core.task_service import create_checkpoint, create_task, get_task, transition_task
from pados.providers.base import LLMProvider
from pados.retrieval.indexer import index_repository, search_chunks


def _sha256_text(content: str) -> str:
    return hashlib.sha256(content.encode("utf-8")).hexdigest()


def validate_scope(*, target_path: Path, allowed_root: Path, forbidden_paths: list[Path]) -> None:
    resolved_target = target_path.resolve()
    resolved_root = allowed_root.resolve()
    if not resolved_target.is_relative_to(resolved_root):
        raise PermissionError(f"target outside allowed root: {target_path}")
    for forbidden in forbidden_paths:
        if resolved_target == forbidden.resolve() or forbidden.resolve() in resolved_target.parents:
            raise PermissionError(f"target inside forbidden path: {target_path}")


def run_single_agent_loop(
    session: Session,
    *,
    project_id: str,
    repository_root: Path,
    artifact_root: Path,
    forbidden_paths: list[Path],
    git_commit: str,
    provider: LLMProvider | None = None,
    provider_name: str = "configured",
) -> dict[str, Any]:
    if provider is None:
        raise RuntimeError("PROVIDER_NOT_CONFIGURED: single-agent loop requires provider injection")
    task_id = create_task(
        session,
        project_id=project_id,
        title="P3 single-agent loop",
        objective="compile context, call configured provider, write artifact, and checkpoint",
    )
    task = get_task(session, task_id)
    planned = transition_task(
        session,
        task_id=task.id,
        from_version=task.version,
        new_status="PLANNED",
        new_gate="G3",
        actor="planner",
    )
    indexed = index_repository(session, project_id=project_id, root=repository_root)
    retrieved = search_chunks(session, project_id=project_id, query="greet")
    packet = {
        "task": {"id": task_id, "objective": "single-agent closed loop", "current_gate": "G3"},
        "scope": {
            "allowed_paths": [artifact_root.as_posix()],
            "forbidden_paths": [path.as_posix() for path in forbidden_paths],
        },
        "retrieved_context": retrieved,
        "indexed_files": [item.relative_path for item in indexed],
    }
    packet_hash = context_hash(packet)
    provider_output = asyncio.run(provider.chat(json.dumps(packet, sort_keys=True)))
    artifact_root.mkdir(parents=True, exist_ok=True)
    artifact_path = artifact_root / f"{task_id}_{provider_name}_output.json"
    validate_scope(target_path=artifact_path, allowed_root=artifact_root, forbidden_paths=forbidden_paths)
    artifact_payload = json.dumps(
        {"task_id": task_id, "packet_hash": packet_hash, "provider_output": provider_output},
        indent=2,
        sort_keys=True,
    )
    artifact_path.write_text(artifact_payload, encoding="utf-8")
    output_hash = _sha256_text(artifact_payload)
    run_id = session.execute(
        text(
            """
            INSERT INTO agent_runs
              (task_id, agent_role, provider, model_name, prompt_version, status,
               input_context_hash, output_hash, result_summary, structured_output)
            VALUES
              (:task_id, 'builder', :provider_name, :provider_name, 'p3', 'completed',
               :input_context_hash, :output_hash, 'provider loop completed',
               CAST(:structured_output AS jsonb))
            RETURNING id
            """
        ),
        {
            "task_id": task_id,
            "provider_name": provider_name,
            "input_context_hash": packet_hash,
            "output_hash": output_hash,
            "structured_output": json.dumps({"artifact": artifact_path.as_posix()}, sort_keys=True),
        },
    ).scalar_one()
    session.execute(
        text(
            """
            INSERT INTO test_runs
              (task_id, run_type, command, status, exit_code, summary, completed_at)
            VALUES
              (:task_id, 'deterministic', 'p3 scope validation', 'passed', 0,
               'artifact path validated against allowlist and forbidden paths', now())
            """
        ),
        {"task_id": task_id},
    )
    session.execute(
        text(
            """
            INSERT INTO artifacts
              (task_id, artifact_type, relative_path, sha256, size_bytes)
            VALUES
              (:task_id, 'agent_output', :relative_path, :sha256, :size_bytes)
            """
        ),
        {
            "task_id": task_id,
            "relative_path": artifact_path.as_posix(),
            "sha256": output_hash,
            "size_bytes": len(artifact_payload.encode("utf-8")),
        },
    )
    checkpoint_id = create_checkpoint(
        session,
        task_id=task_id,
        git_commit=git_commit,
        database_revision="20260710_0002",
        snapshot={"task_status": "QA_RUNNING", "packet_hash": packet_hash},
    )
    transition_task(
        session,
        task_id=task_id,
        from_version=planned.version,
        new_status="QA_RUNNING",
        new_gate="G5",
        actor="qa",
    )
    return {
        "task_id": task_id,
        "agent_run_id": str(run_id),
        "checkpoint_id": checkpoint_id,
        "artifact_path": artifact_path.as_posix(),
        "packet_hash": packet_hash,
    }


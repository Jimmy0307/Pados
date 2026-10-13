import json
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import text

from pados.core.task_service import create_project
from pados.db import session_scope
from pados.memory.packet import assemble_context_packet
from pados.memory.retrieval import RetrievalRequest, search_memory
from pados.memory.service import (
    add_memory_record,
    append_run_event,
    create_run,
    deprecate_memory,
    mark_conflict,
    mark_memory_stale,
    promote_memory,
    record_decision,
    register_artifact,
    register_markdown_source,
    register_project,
    validate_artifact_integrity,
)


def _promoted_memory(session: Any, **overrides: Any) -> str:
    values: dict[str, Any] = {
        "memory_type": "semantic",
        "title": "promoted test memory",
        "content_summary": "promoted test content",
        "source_type": "observation",
        "source_uri": "obs:test-promoted",
        "source_trust_level": "OBSERVED",
        "verification_status": "UNVERIFIED",
        "freshness_status": "CURRENT",
        "origin_type": "SYSTEM",
        "actor_type": "SYSTEM",
        "actor_id": "integration-test",
    }
    values.update(overrides)
    memory_id = add_memory_record(session, **values)
    promote_memory(
        session,
        memory_id=memory_id,
        project_id=str(values["project_id"]),
        expected_authority_version=1,
        authority_state="PROMOTED",
        actor_type="SYSTEM",
        actor_id="integration-test",
        promotion_gate="integration-test",
        evidence_status="EVIDENCE_UNAVAILABLE",
    )
    return memory_id


def _project(tmp_path: Path, name: str) -> tuple[str, Path]:
    root = tmp_path / name
    root.mkdir()
    with session_scope() as session:
        project_id = create_project(session, name=name, root_path=str(root))
        register_project(session, project_id=project_id, canonical_root=root)
    return project_id, root


def test_project_registry_rejects_duplicate_canonical_root(tmp_path: Path) -> None:
    project_a, root = _project(tmp_path, "project-a")
    with session_scope() as session:
        project_b = create_project(session, name="project-b", root_path=str(root))
        with pytest.raises(ValueError, match="duplicate canonical root"):
            register_project(session, project_id=project_b, canonical_root=root)
    assert project_a


def test_registered_source_rejects_external_and_symlink_escape(tmp_path: Path) -> None:
    project_id, root = _project(tmp_path, "project-source")
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    with session_scope() as session, pytest.raises(PermissionError):
        register_markdown_source(session, project_id=project_id, path=outside)
    target = tmp_path / "target.md"
    target.write_text("escape", encoding="utf-8")
    link = root / "escape.md"
    try:
        link.symlink_to(target)
    except OSError:
        pytest.skip("symlink not available")
    with session_scope() as session, pytest.raises(PermissionError):
        register_markdown_source(session, project_id=project_id, path=link)


def test_memory_lifecycle_conflict_supersede_stale_deprecate(tmp_path: Path) -> None:
    project_id, _root = _project(tmp_path, "project-memory")
    with session_scope() as session:
        run_id = create_run(session, project_id=project_id, run_type="decision", worker_type="planner")
        decision_id = record_decision(
            session,
            project_id=project_id,
            run_id=run_id,
            decision_type="architecture",
            question="Where should context memory live?",
            decision="PostgreSQL stores operational context memory.",
            rationale="The loop requires durable events, decisions and evidence pointers.",
            status="APPROVED",
            decided_by="test",
        )
        old_id = _promoted_memory(
            session,
            project_id=project_id,
            title="runtime truth",
            content_summary="Native PostgreSQL is canonical.",
            source_uri="obs:old",
        )
        conflict_id = add_memory_record(
            session,
            project_id=project_id,
            memory_type="semantic",
            title="runtime truth conflict",
            content_summary="Docker is required.",
            source_type="observation",
            source_uri="obs:conflict",
            source_trust_level="MODEL_INFERRED",
            verification_status="UNVERIFIED",
            freshness_status="CURRENT",
            origin_type="MODEL",
            provider="fixture",
            model="fixture",
            result_ref="result:conflict",
        )
        mark_conflict(session, project_id=project_id, first_memory_id=old_id, second_memory_id=conflict_id)
        new_id = _promoted_memory(
            session,
            project_id=project_id,
            title="runtime truth corrected",
            content_summary="Native PostgreSQL is canonical; Docker is optional.",
            source_uri="obs:new",
            supersedes_memory_id=old_id,
        )
        mark_memory_stale(session, memory_id=conflict_id)
        deprecate_memory(session, memory_id=conflict_id)
        rows = session.execute(
            text("SELECT id, verification_status, freshness_status FROM memory_records")
        ).mappings().all()
    statuses = {str(row["id"]): (row["verification_status"], row["freshness_status"]) for row in rows}
    assert decision_id
    assert statuses[old_id][0] == "DEPRECATED"
    assert statuses[conflict_id] == ("DEPRECATED", "STALE")
    assert statuses[new_id] == ("VERIFIED", "CURRENT")


def test_run_events_are_ordered_and_append_only(tmp_path: Path) -> None:
    project_id, _root = _project(tmp_path, "project-events")
    with session_scope() as session:
        run_id = create_run(session, project_id=project_id, run_type="validation", worker_type="qa")
        append_run_event(
            session,
            run_id=run_id,
            project_id=project_id,
            event_type="TEST_EXECUTED",
            payload_json={"command": "pytest", "log_path": "work/logs/test.log"},
        )
        append_run_event(
            session,
            run_id=run_id,
            project_id=project_id,
            event_type="TEST_PASSED",
            payload_json={"command": "pytest", "exit_code": 0, "log_path": "work/logs/test.log"},
        )
        sequences = session.execute(
            text("SELECT sequence_number FROM run_events WHERE run_id = :run_id ORDER BY sequence_number"),
            {"run_id": run_id},
        ).scalars().all()
        with pytest.raises(Exception, match="append-only"):
            session.execute(
                text("UPDATE run_events SET payload_json = '{}'::jsonb WHERE run_id = :run_id"),
                {"run_id": run_id},
            )
    assert sequences == [1, 2, 3]


def test_invalid_event_payload_and_secret_are_rejected(tmp_path: Path) -> None:
    project_id, _root = _project(tmp_path, "project-invalid-events")
    with session_scope() as session:
        run_id = create_run(session, project_id=project_id, run_type="validation", worker_type="qa")
        with pytest.raises(ValueError, match="invalid event payload schema"):
            append_run_event(
                session,
                run_id=run_id,
                project_id=project_id,
                event_type="TEST_EXECUTED",
                payload_json={"unexpected": "field"},
            )
        with pytest.raises(ValueError, match="secret-like content"):
            append_run_event(
                session,
                run_id=run_id,
                project_id=project_id,
                event_type="TEST_EXECUTED",
                payload_json={"command": "pass" + "word=abc", "log_path": "work/logs/test.log"},
            )


def test_artifact_integrity_detects_missing_and_hash_mismatch(tmp_path: Path) -> None:
    project_id, root = _project(tmp_path, "project-artifact")
    artifact = root / "report.md"
    artifact.write_text("original", encoding="utf-8")
    with session_scope() as session:
        artifact_id = register_artifact(session, project_id=project_id, path=artifact, artifact_type="report")
        assert validate_artifact_integrity(session, artifact_id=artifact_id) == "VERIFIED"
    artifact.write_text("changed", encoding="utf-8")
    with session_scope() as session:
        assert validate_artifact_integrity(session, artifact_id=artifact_id) == "HASH_MISMATCH"
    artifact.unlink()
    with session_scope() as session:
        assert validate_artifact_integrity(session, artifact_id=artifact_id) == "MISSING_ARTIFACT"


def test_retrieval_prefers_verified_current_canonical_and_blocks_cross_project(tmp_path: Path) -> None:
    project_a, _root_a = _project(tmp_path, "project-retrieval-a")
    project_b, _root_b = _project(tmp_path, "project-retrieval-b")
    with session_scope() as session:
        _promoted_memory(
            session,
            project_id=project_a,
            title="postgres canonical",
            content_summary="PostgreSQL memory is canonical for context retrieval.",
            source_uri="obs:canonical",
        )
        add_memory_record(
            session,
            project_id=project_a,
            memory_type="semantic",
            title="postgres inferred",
            content_summary="PostgreSQL memory is maybe useful.",
            source_type="observation",
            source_uri="obs:inferred",
            source_trust_level="MODEL_INFERRED",
            verification_status="UNVERIFIED",
            freshness_status="CURRENT",
            importance=100,
            retrieval_priority=100,
        )
        _promoted_memory(
            session,
            project_id=project_b,
            title="postgres other project",
            content_summary="Other project context must not leak.",
            source_uri="obs:other",
        )
        rows = search_memory(
            session,
            RetrievalRequest(
                project_id=project_a,
                query="PostgreSQL memory",
                purpose="test",
                max_tokens=2000,
            ),
        )
    assert rows[0].title == "postgres canonical"
    assert all(row.project_id == project_a for row in rows)


def test_context_packet_schema_provenance_and_secret_safety(tmp_path: Path) -> None:
    project_id, _root = _project(tmp_path, "project-packet")
    with session_scope() as session:
        _promoted_memory(
            session,
            project_id=project_id,
            title="context packet fact",
            content_summary="Context packets use stable JSON with provenance.",
            source_uri="obs:packet",
        )
        with pytest.raises(ValueError, match="secret-like content"):
            add_memory_record(
                session,
                project_id=project_id,
                memory_type="semantic",
                title="bad",
                content_summary="pass" + "word=do-not-store",
                source_type="observation",
                source_uri="obs:bad",
            )
        packet = assemble_context_packet(
            session,
            request=RetrievalRequest(
                project_id=project_id,
                query="stable JSON provenance",
                purpose="test-packet",
                max_tokens=1600,
            ),
            request_id="test-request",
        )
    encoded = json.dumps(packet, sort_keys=True)
    assert packet["context_packet"]["purpose"] == "test-packet"
    assert packet["context_packet"]["provenance"]["policy_version"] == "p6.7-context-memory-v1"
    assert "password" not in encoded.lower()

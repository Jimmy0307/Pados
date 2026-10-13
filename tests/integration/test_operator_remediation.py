from pathlib import Path

import pytest
from sqlalchemy import text

from pados.core.task_service import create_project
from pados.db import session_scope
from pados.memory.packet import assemble_context_packet
from pados.memory.retrieval import RetrievalRequest, search_memory
from pados.memory.service import add_memory_record, promote_memory, register_project


def _promoted_memory(session: object, **values: object) -> str:
    memory_id = add_memory_record(session, **values)  # type: ignore[arg-type]
    promote_memory(
        session,  # type: ignore[arg-type]
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


def test_project_bootstrap_is_atomic_and_rejects_duplicate_root(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    with session_scope() as session:
        savepoint = session.begin_nested()
        from pados.memory.service import bootstrap_project

        project_id = bootstrap_project(session, name="operator-bootstrap", root_path=root)
        assert session.execute(
            text("SELECT project_id FROM context_project_registry WHERE project_id = CAST(:id AS uuid)"),
            {"id": project_id},
        ).scalar_one()
        with pytest.raises(ValueError, match="duplicate project identity"):
            bootstrap_project(session, name="operator-bootstrap", root_path=root)
        savepoint.rollback()


def test_relaxed_retrieval_and_structured_failure_classification(tmp_path: Path) -> None:
    root = tmp_path / "project"
    root.mkdir()
    with session_scope() as session:
        savepoint = session.begin_nested()
        project_id = create_project(session, name="operator-retrieval", root_path=str(root))
        register_project(session, project_id=project_id, canonical_root=root)
        _promoted_memory(
            session,
            project_id=project_id,
            memory_type="semantic",
            title="operator remediation fact",
            content_summary="alpha keyword is stored for controlled lexical fallback",
            source_type="observation",
            source_uri="obs:operator-fallback",
            source_trust_level="OBSERVED",
            verification_status="UNVERIFIED",
            freshness_status="CURRENT",
            origin_type="SYSTEM",
            actor_type="SYSTEM",
            actor_id="integration-test",
        )
        _promoted_memory(
            session,
            project_id=project_id,
            memory_type="semantic",
            title="operator usability defects",
            content_summary="structured failure evidence is reviewable",
            source_type="failure",
            source_uri="failure:operator",
            source_trust_level="OBSERVED",
            verification_status="UNVERIFIED",
            freshness_status="CURRENT",
            origin_type="SYSTEM",
            actor_type="SYSTEM",
            actor_id="integration-test",
        )
        rows = search_memory(
            session,
            RetrievalRequest(
                project_id=project_id,
                query="alpha beta gamma",
                purpose="operator-remediation",
                max_tokens=1200,
            ),
        )
        assert rows
        assert rows[0].retrieval_reason == "relaxed_fts"
        packet = assemble_context_packet(
            session,
            request=RetrievalRequest(
                project_id=project_id,
                query="operator usability defects",
                purpose="operator-remediation",
                max_tokens=1200,
            ),
            request_id="operator-remediation-test",
        )
        assert packet["context_packet"]["known_failures"]
        assert packet["context_packet"]["provenance"]["vector_status"] == (
            "disabled_mock_embedding_not_semantic"
        )
        savepoint.rollback()

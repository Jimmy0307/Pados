"""Memory authority is operational semantic state (V1 decision 2).

Runs against a disposable operational database at the operational head declared in
PADOS_TEST_OPERATIONAL_DATABASE; the default PADOS_DB_NAME is a proof-lane
database whose memory_authority_events copy is LEGACY_NON_AUTHORITATIVE.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.exc import OperationalError

from pados.config import OPERATIONAL_REVISION_DEFAULT, get_settings, lane_settings
from pados.core.task_service import create_project
from pados.db import get_engine, session_scope
from pados.memory.retrieval import RetrievalRequest, search_memory
from pados.memory.service import (
    add_memory_record,
    promote_memory,
    record_decision,
    register_project,
)
from pados.product_loop.state import P6_19BStateGateway
from pados.state_proof import ObjectStore, StateProofService

OPERATIONAL = os.environ.get("PADOS_TEST_OPERATIONAL_DATABASE", "")
pytestmark = pytest.mark.skipif(not OPERATIONAL, reason="DATABASE_INTEGRATION_SUITE: set PADOS_TEST_OPERATIONAL_DATABASE")


def _op():
    return lane_settings(get_settings(), OPERATIONAL)


def _project(tmp_path: Path) -> str:
    root = tmp_path / f"memory-{uuid4().hex}"
    root.mkdir()
    with session_scope(_op()) as session:
        project_id = create_project(session, name=root.name, root_path=str(root))
        register_project(session, project_id=project_id, canonical_root=root)
    return project_id


def _memory(session: Any, project_id: str, title: str) -> str:
    return add_memory_record(
        session, project_id=project_id, memory_type="semantic", title=title,
        content_summary=f"{title} operational authority content", source_type="observation",
        source_uri=f"obs:{uuid4().hex}", source_trust_level="OBSERVED", verification_status="UNVERIFIED",
        freshness_status="CURRENT", origin_type="HUMAN", actor_type="HUMAN", actor_id="reviewer-1",
        evidence_status="EVIDENCE_UNAVAILABLE",
    )


def _promote(session: Any, project_id: str, memory_id: str) -> dict[str, Any]:
    return promote_memory(session, memory_id=memory_id, project_id=project_id, expected_authority_version=1,
                          authority_state="PROMOTED", actor_type="SYSTEM", actor_id="policy-1",
                          promotion_gate="operational-test", evidence_status="EVIDENCE_UNAVAILABLE",
                          evidence_ref="evidence:operational-test")


def _events(database: str, memory_id: str) -> int:
    engine = get_engine(lane_settings(get_settings(), database))
    try:
        with engine.connect() as conn:
            if conn.execute(text("SELECT to_regclass('public.memory_authority_events')")).scalar() is None:
                return -1
            return int(conn.execute(text("SELECT count(*) FROM memory_authority_events WHERE memory_id = CAST(:m AS uuid)"),
                                    {"m": memory_id}).scalar_one())
    finally:
        engine.dispose()


def test_operational_lane_carries_memory_authority_schema() -> None:
    engine = get_engine(_op())
    try:
        with engine.connect() as conn:
            assert sorted(conn.execute(text("SELECT version_num FROM alembic_version")).scalars()) == [
                OPERATIONAL_REVISION_DEFAULT
            ]
            assert conn.execute(text("SELECT to_regclass('public.memory_authority_events')")).scalar() is not None
            assert conn.execute(text("SELECT to_regclass('public.state_proof_results')")).scalar() is None
            assert conn.execute(text("SELECT to_regclass('public.p6_19b_alembic_version')")).scalar() is None
    finally:
        engine.dispose()


def test_write_read_promote_search_and_decision_use_operational_authority(tmp_path: Path) -> None:
    project_id = _project(tmp_path)
    title = f"operational-fact-{uuid4().hex[:8]}"
    with session_scope(_op()) as session:
        memory_id = _memory(session, project_id, title)
        promoted = _promote(session, project_id, memory_id)
        decision_id = record_decision(session, project_id=project_id, run_id=None, decision_type="architecture",
                                      question="Is memory authority operational?", decision="Yes",
                                      rationale="V1 decision 2", status="PROPOSED", decided_by="test")
        row = session.execute(text("SELECT title FROM memory_records WHERE id = CAST(:m AS uuid)"), {"m": memory_id}).one()
        assert row.title == title
        assert promoted["authority_state"] == "PROMOTED"
        event = session.execute(text(
            "SELECT authority_state, actor_id, promotion_gate, evidence_ref, version FROM memory_authority_events "
            "WHERE memory_id = CAST(:m AS uuid) ORDER BY version DESC LIMIT 1"), {"m": memory_id}).one()
        assert (event.authority_state, event.actor_id, event.promotion_gate, event.evidence_ref, event.version) == (
            "PROMOTED", "policy-1", "operational-test", "evidence:operational-test", 2)
        found = search_memory(session, RetrievalRequest(project_id=project_id, query=title, purpose="test",
                                                        max_tokens=2000, minimum_verification_status="UNVERIFIED"))
        assert any(str(candidate.memory_id) == memory_id for candidate in found)
        assert session.execute(text("SELECT count(*) FROM memory_decisions WHERE id = CAST(:d AS uuid)"),
                               {"d": decision_id}).scalar_one() == 1
    assert _events(OPERATIONAL, memory_id) == 2  # CANDIDATE + PROMOTED, append-only
    assert _events(get_settings().db_name, memory_id) == 0  # proof legacy copy untouched


def test_cross_project_memory_is_not_disclosed(tmp_path: Path) -> None:
    project_a, project_b = _project(tmp_path), _project(tmp_path)
    marker = f"isolated-{uuid4().hex[:8]}"
    with session_scope(_op()) as session:
        _memory(session, project_b, marker)
        found = search_memory(session, RetrievalRequest(project_id=project_a, query=marker, purpose="test",
                                                        max_tokens=2000, minimum_verification_status="UNVERIFIED"))
    assert found == []


def test_resolver_reads_promoted_memory_from_operational_not_proof_legacy(tmp_path: Path) -> None:
    project_id = _project(tmp_path)
    with session_scope(_op()) as session:
        operational_memory = _memory(session, project_id, "operational promoted")
        _promote(session, project_id, operational_memory)
        session.execute(text("UPDATE memory_records SET verification_status = 'VERIFIED' WHERE id = CAST(:m AS uuid)"),
                        {"m": operational_memory})
    proof_engine = get_engine(get_settings())
    operational_engine = get_engine(_op())
    try:
        proof = StateProofService(proof_engine, ObjectStore(tmp_path / "objects"))
        proof.ensure_scope(project_id)
        context = P6_19BStateGateway(proof, operational_engine).resolve_project_context(project_id)
    finally:
        proof_engine.dispose()
        operational_engine.dispose()
    ids = {item["id"] for item in context["promoted_memory"]["items"]}
    assert ids == {operational_memory}
    assert context["promoted_memory"]["provenance"]["source"] == "memory_authority_events"


def test_proof_unavailable_does_not_redirect_memory_ownership(tmp_path: Path) -> None:
    project_id = _project(tmp_path)
    absent_proof = create_engine(lane_settings(get_settings(), "pados_absent_proof_lane").database_url)
    operational_engine = get_engine(_op())
    try:
        with session_scope(_op()) as session:
            memory_id = _memory(session, project_id, "written while proof is down")
        assert _events(OPERATIONAL, memory_id) == 1
        gateway = P6_19BStateGateway(StateProofService(absent_proof, ObjectStore(tmp_path / "o")), operational_engine)
        with pytest.raises(ValueError, match="SEMANTIC_CONTEXT_UNAVAILABLE"):
            gateway.resolve_project_context(project_id)
    finally:
        absent_proof.dispose()
        operational_engine.dispose()


def test_operational_unavailable_fails_closed(tmp_path: Path) -> None:
    project_id = str(uuid4())
    with pytest.raises(OperationalError), session_scope(lane_settings(get_settings(), "pados_absent_operational_lane")) as session:
        _memory(session, project_id, "must not be written anywhere")

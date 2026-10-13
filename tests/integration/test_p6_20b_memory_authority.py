from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text

from pados.core.task_service import create_project
from pados.db import session_scope
from pados.memory import repository
from pados.memory.packet import assemble_context_packet
from pados.memory.retrieval import RetrievalRequest, search_memory
from pados.memory.service import (
    add_memory_record,
    create_run,
    promote_memory,
    register_project,
    reject_memory,
)


def _project(tmp_path: Path) -> str:
    root = tmp_path / f"project-{uuid4().hex}"
    root.mkdir()
    with session_scope() as session:
        project_id = create_project(session, name=root.name, root_path=str(root))
        register_project(session, project_id=project_id, canonical_root=root)
    return project_id


def _candidate(session: Any, project_id: str, **overrides: Any) -> str:
    values: dict[str, Any] = {
        "project_id": project_id,
        "memory_type": "semantic",
        "title": f"candidate-{uuid4().hex}",
        "content_summary": "candidate content remains non-authoritative",
        "source_type": "observation",
        "source_uri": f"source:{uuid4().hex}",
        "source_trust_level": "OBSERVED",
        "verification_status": "UNVERIFIED",
        "freshness_status": "CURRENT",
        "origin_type": "HUMAN",
        "actor_type": "HUMAN",
        "actor_id": "reviewer-1",
        "evidence_status": "EVIDENCE_UNAVAILABLE",
    }
    values.update(overrides)
    return add_memory_record(session, **values)


def _promote(
    session: Any,
    project_id: str,
    memory_id: str,
    *,
    state: str = "PROMOTED",
    expected_version: int = 1,
    **overrides: Any,
) -> dict[str, Any]:
    values: dict[str, Any] = {
        "memory_id": memory_id,
        "project_id": project_id,
        "expected_authority_version": expected_version,
        "authority_state": state,
        "actor_type": "SYSTEM" if state == "PROMOTED" else "HUMAN",
        "actor_id": "policy-1" if state == "PROMOTED" else "reviewer-1",
        "promotion_gate": "test-gate",
        "evidence_status": "EVIDENCE_UNAVAILABLE",
    }
    values.update(overrides)
    return promote_memory(session, **values)


def test_candidate_promotion_and_context_keep_authority_explicit(tmp_path: Path) -> None:
    project_id = _project(tmp_path)
    with session_scope() as session:
        run_id = create_run(session, project_id=project_id, run_type="fixture", worker_type="fixture")
        memory_id = _candidate(
            session,
            project_id,
            origin_type="MODEL",
            actor_type="MODEL",
            actor_id="model-runtime",
            provider="fixture-provider",
            model="fixture-model",
            runtime="fixture-runtime",
            contract_id="fixture-contract-v1",
            run_id=run_id,
            result_ref="result:fixture-1",
        )
        row = search_memory(
            session,
            RetrievalRequest(project_id=project_id, query="candidate content", purpose="test", max_tokens=1200),
        )[0]
        assert row.authority_state == "CANDIDATE"
        assert row.origin_type == "MODEL"
        assert row.model == "fixture-model"
        assert row.context_eligible is False
        packet = assemble_context_packet(
            session,
            request=RetrievalRequest(project_id=project_id, query="candidate content", purpose="test", max_tokens=1200),
            request_id="candidate-only",
        )
        assert packet["context_packet"]["verified_facts"] == []
        assert packet["context_packet"]["excluded_sources"][0]["reason"] == "AUTHORITY_NOT_PROMOTED"

        promoted = _promote(session, project_id, memory_id, result_ref="result:fixture-1")
        assert promoted["authority_state"] == "PROMOTED"
        current = repository.get_current_authority(session, memory_id=memory_id)
        assert current is not None
        assert current["authority_state"] == "PROMOTED"
        assert current["origin_type"] == "MODEL"
        assert current["model"] == "fixture-model"
        assert current["promotion_gate"] == "test-gate"
        promoted_row = search_memory(
            session,
            RetrievalRequest(project_id=project_id, query="candidate content", purpose="test", max_tokens=1200),
        )[0]
        assert promoted_row.context_eligible is True
        promoted_packet = assemble_context_packet(
            session,
            request=RetrievalRequest(project_id=project_id, query="candidate content", purpose="test", max_tokens=1200),
            request_id="promoted",
        )
    selected = promoted_packet["context_packet"]["verified_facts"]
    assert selected[0]["authority_state"] == "PROMOTED"
    assert selected[0]["origin_type"] == "MODEL"
    assert selected[0]["model"] == "fixture-model"
    assert selected[0]["promotion_gate"] == "test-gate"


def test_human_confirmation_is_distinct_and_machine_provenance_is_retained(tmp_path: Path) -> None:
    project_id = _project(tmp_path)
    with session_scope() as session:
        memory_id = _candidate(
            session,
            project_id,
            origin_type="MODEL",
            actor_type="MODEL",
            actor_id="model-runtime",
            provider="fixture-provider",
            model="fixture-model",
            result_ref="result:human-review",
        )
        result = _promote(
            session,
            project_id,
            memory_id,
            state="HUMAN_CONFIRMED",
            actor_type="HUMAN",
            actor_id="reviewer-42",
            evidence_ref="evidence:review-42",
            evidence_status="PROVIDED",
        )
        current = repository.get_current_authority(session, memory_id=memory_id)
    assert result["authority_state"] == "HUMAN_CONFIRMED"
    assert current is not None
    assert current["authority_state"] == "HUMAN_CONFIRMED"
    assert current["actor_type"] == "HUMAN"
    assert current["actor_id"] == "reviewer-42"
    assert current["origin_type"] == "MODEL"
    assert current["origin_actor_type"] == "MODEL"
    assert current["model"] == "fixture-model"


def test_trusted_labels_cannot_bypass_candidate_boundary(tmp_path: Path) -> None:
    project_id = _project(tmp_path)
    with session_scope() as session:
        common = {
            "project_id": project_id,
            "memory_type": "semantic",
            "title": "forbidden authority",
            "content_summary": "must not be directly promoted",
            "source_type": "observation",
            "source_uri": "source:forbidden",
            "freshness_status": "CURRENT",
            "origin_type": "UNKNOWN",
        }
        with pytest.raises(ValueError, match="explicit promotion"):
            add_memory_record(
                session,
                **common,
                source_trust_level="CANONICAL",
                verification_status="UNVERIFIED",
            )
        with pytest.raises(ValueError, match="explicit promotion"):
            add_memory_record(
                session,
                **common,
                source_trust_level="OBSERVED",
                verification_status="VERIFIED",
            )


def test_repeated_model_output_does_not_increase_authority(tmp_path: Path) -> None:
    project_id = _project(tmp_path)
    with session_scope() as session:
        first = _candidate(
            session,
            project_id,
            origin_type="MODEL",
            actor_type="MODEL",
            actor_id="model-runtime",
            provider="fixture-provider",
            model="fixture-model",
            result_ref="result:repeat-1",
            content_summary="same model statement",
        )
        second = _candidate(
            session,
            project_id,
            origin_type="MODEL",
            actor_type="MODEL",
            actor_id="model-runtime",
            provider="fixture-provider",
            model="fixture-model",
            result_ref="result:repeat-2",
            content_summary="same model statement",
        )
        rows = search_memory(
            session,
            RetrievalRequest(project_id=project_id, query="same model statement", purpose="test", max_tokens=1200),
        )
        assert {row.memory_id for row in rows} == {first, second}
        assert all(row.authority_state == "CANDIDATE" for row in rows)
        assert all(row.context_eligible is False for row in rows)


def test_supersession_history_and_stale_promotion_fail_closed(tmp_path: Path) -> None:
    project_id = _project(tmp_path)
    with session_scope() as session:
        old_id = _candidate(session, project_id, title="old version", content_summary="old semantic version")
        _promote(session, project_id, old_id)
        new_id = _candidate(
            session,
            project_id,
            title="new version",
            content_summary="new semantic version",
            supersedes_memory_id=old_id,
        )
        _promote(session, project_id, new_id, expected_superseded_version=2)
        old_current = repository.get_current_authority(session, memory_id=old_id)
        new_current = repository.get_current_authority(session, memory_id=new_id)
        history = session.execute(
            text(
                "SELECT authority_state, version FROM memory_authority_events "
                "WHERE memory_id = :memory_id ORDER BY version"
            ),
            {"memory_id": old_id},
        ).mappings().all()
        old_event_id = str(
            session.execute(
                text(
                    "SELECT id FROM memory_authority_events "
                    "WHERE memory_id = :memory_id ORDER BY version LIMIT 1"
                ),
                {"memory_id": old_id},
            ).scalar_one()
        )
        with pytest.raises(Exception, match="append-only"), session.begin_nested():
                session.execute(
                    text("UPDATE memory_authority_events SET promotion_reason = 'tampered' WHERE id = :event_id"),
                    {"event_id": old_event_id},
                )
        with pytest.raises(ValueError, match="STALE_CANDIDATE"):
            promote_memory(
                session,
                memory_id=new_id,
                project_id=project_id,
                expected_authority_version=1,
                authority_state="PROMOTED",
                actor_type="SYSTEM",
                actor_id="policy-2",
                promotion_gate="stale-attempt",
            )
    assert old_current is not None and old_current["authority_state"] == "SUPERSEDED"
    assert new_current is not None and new_current["authority_state"] == "PROMOTED"
    assert [(row["authority_state"], int(row["version"])) for row in history] == [
        ("CANDIDATE", 1),
        ("PROMOTED", 2),
        ("SUPERSEDED", 3),
    ]


def test_rejected_and_legacy_rows_are_inspectable_but_not_current_context(tmp_path: Path) -> None:
    project_id = _project(tmp_path)
    with session_scope() as session:
        rejected_id = _candidate(
            session,
            project_id,
            title="rejected candidate",
            content_summary="authority row rejected candidate",
        )
        reject_memory(
            session,
            memory_id=rejected_id,
            project_id=project_id,
            expected_authority_version=1,
            actor_type="HUMAN",
            actor_id="reviewer-1",
            promotion_gate="reject-gate",
            reason="insufficient evidence",
        )
        legacy_id = str(uuid4())
        session.execute(
            text(
                """
                INSERT INTO memory_records
                  (id, project_id, memory_type, title, content_summary, source_type,
                   source_uri, source_trust_level, verification_status, freshness_status,
                   importance, retrieval_priority)
                VALUES
                  (CAST(:id AS uuid), CAST(:project_id AS uuid), 'semantic',
                   'legacy authority row', 'legacy row has no authority event', 'observation',
                   'legacy:source', 'CANONICAL', 'VERIFIED', 'CURRENT', 80, 80)
                """
            ),
            {"id": legacy_id, "project_id": project_id},
        )
        rows = search_memory(
            session,
            RetrievalRequest(project_id=project_id, query="authority row", purpose="test", max_tokens=1200),
        )
        by_id = {row.memory_id: row for row in rows}
        assert by_id[rejected_id].authority_state == "REJECTED"
        assert by_id[rejected_id].context_eligible is False
        assert by_id[legacy_id].authority_state == "LEGACY_UNKNOWN"
        assert by_id[legacy_id].context_eligible is False
        packet = assemble_context_packet(
            session,
            request=RetrievalRequest(project_id=project_id, query="authority row", purpose="test", max_tokens=1200),
            request_id="legacy-and-rejected",
        )
    assert packet["context_packet"]["verified_facts"] == []
    reasons = {item["reason"] for item in packet["context_packet"]["excluded_sources"]}
    assert "LEGACY_UNKNOWN" in reasons or "AUTHORITY_NOT_PROMOTED" in reasons

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError, IntegrityError

from pados.config import get_settings
from pados.db import get_engine
from pados.graph.cognition import ChatCognitiveProvider
from pados.graph.schemas import CognitiveOperation
from pados.providers.mock import MockProvider
from pados.state_proof import ObjectStore, StateProofService


class CountingProvider:
    def __init__(self, response: str | None = None, error: Exception | None = None) -> None:
        self.calls = 0
        self.response = response or json.dumps(
            {
                "title": "Proof output",
                "answer": "A committed result is a projection input, not truth.",
                "subquestions": [],
                "gaps": [],
                "claims": [],
                "evidence_refs": ["E1"],
                "proposed_graph_changes": [{"kind": "EDGE_PROPOSAL", "target": "N2"}],
                "limitations": ["PROVISIONAL"],
            }
        )
        self.error = error

    async def synthesize(self, specification: Mapping[str, Any]) -> str:
        del specification
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.response


@pytest.fixture
def proof_service(tmp_path: Path) -> StateProofService:
    settings = get_settings()
    engine = get_engine(settings)
    service = StateProofService(engine, ObjectStore(tmp_path / "approved-proof-objects"))
    yield service
    engine.dispose()


def _specification() -> dict[str, Any]:
    return {"goal": {"current_objective": "bounded proof"}, "evidence": [{"evidence_id": "E1"}]}


def _context() -> dict[str, Any]:
    return {"fingerprint": "context-fingerprint-1", "selected_evidence": ["E1"]}


def _run(
    service: StateProofService,
    provider: CountingProvider,
    *,
    scope_id: str,
    operation_id: str | None = None,
    expected_version: int | None = None,
    run_id: str | None = None,
    auto_commit: bool = True,
) -> dict[str, Any]:
    return asyncio.run(
        service.execute(
            scope_id=scope_id,
            operation_id=operation_id or str(uuid4()),
            specification=_specification(),
            context_receipt=_context(),
            provider=provider,
            expected_version=expected_version,
            run_id=run_id,
            auto_commit=auto_commit,
        )
    )


def test_receipt_preserves_input_scope_context_and_payload_ref(proof_service: StateProofService) -> None:
    scope_id = f"scope-{uuid4()}"
    provider = CountingProvider()
    run_id = str(uuid4())
    outcome = _run(proof_service, provider, scope_id=scope_id, run_id=run_id)
    result = outcome["result"]

    assert result["status"] == "SUCCEEDED"
    assert result["input_scope_version"] == 0
    assert result["input_manifest_hash"]
    assert result["context_fingerprint"] == "context-fingerprint-1"
    assert result["operation_type"] == "COGNITIVE_SYNTHESIS"
    assert str(result["run_id"]) == run_id
    assert str(result["run_id"]) != str(result["id"])
    assert result["operation_id"] != str(result["id"])
    assert result["output_content_hash"]
    assert result["output_storage_uri"]
    assert outcome["commit"]["new_version"] == 1


def test_stale_cas_preserves_result_and_blocks_overwrite(proof_service: StateProofService) -> None:
    scope_id = f"scope-{uuid4()}"
    first = _run(proof_service, CountingProvider(), scope_id=scope_id, expected_version=0)
    stale = _run(proof_service, CountingProvider(), scope_id=scope_id, expected_version=0)

    assert first["commit"]["status"] == "COMMITTED"
    assert stale["commit"]["status"] == "STALE_CANDIDATE"
    assert stale["result"]["input_scope_version"] == 0
    assert proof_service.get_result(str(stale["result"]["id"])) is not None


def test_invalid_schema_preserves_payload_and_receipt_without_commit(
    proof_service: StateProofService,
) -> None:
    invalid = CountingProvider(response=json.dumps({"answer": "invalid", "unexpected": True}))
    outcome = _run(proof_service, invalid, scope_id=f"scope-{uuid4()}")

    assert outcome["result"]["status"] == "INVALID_SCHEMA"
    assert outcome["result"]["failure_classification"] == "INVALID_SCHEMA"
    assert outcome["result"]["output_content_hash"]
    assert outcome["commit"] is None


def test_two_candidates_one_parent_only_one_commit_and_retry_is_idempotent(
    proof_service: StateProofService,
) -> None:
    scope_id = f"scope-{uuid4()}"
    first = _run(proof_service, CountingProvider(), scope_id=scope_id, auto_commit=False)
    second = _run(proof_service, CountingProvider(), scope_id=scope_id, auto_commit=False)
    first_commit = proof_service.commit_result(
        result_id=str(first["result"]["id"]),
        scope_id=scope_id,
        expected_version=0,
        candidate_ref=str(first["result"]["id"]),
        manifest_hash=str(first["result"]["input_manifest_hash"]),
    )
    retry = proof_service.commit_result(
        result_id=str(first["result"]["id"]),
        scope_id=scope_id,
        expected_version=0,
        candidate_ref=str(first["result"]["id"]),
        manifest_hash=str(first["result"]["input_manifest_hash"]),
    )
    second_commit = proof_service.commit_result(
        result_id=str(second["result"]["id"]),
        scope_id=scope_id,
        expected_version=0,
        candidate_ref=str(second["result"]["id"]),
        manifest_hash=str(second["result"]["input_manifest_hash"]),
    )

    assert first_commit["status"] == "COMMITTED"
    assert retry["id"] == first_commit["id"]
    assert second_commit["status"] == "STALE_CANDIDATE"


def test_semantic_commit_appends_idempotent_immutable_audit_with_metadata(
    proof_service: StateProofService,
) -> None:
    scope_id = f"scope-{uuid4()}"
    outcome = _run(proof_service, CountingProvider(), scope_id=scope_id, auto_commit=False)
    result = outcome["result"]
    values = {
        "result_id": str(result["id"]),
        "scope_id": scope_id,
        "expected_version": int(result["input_scope_version"]),
        "candidate_ref": str(result["id"]),
        "manifest_hash": str(result["input_manifest_hash"]),
        "request_id": "request-audit-1",
        "idempotency_key": "audit-idem-1",
        "actor": "HUMAN_APPROVED_OPERATOR",
        "reason": "approved bounded semantic write",
        "approval_ref": "approval-record-1",
        "rollback_ref": "rollback-record-1",
    }
    committed = proof_service.commit_result(**values)
    retry = proof_service.commit_result(**values)
    assert committed["status"] == retry["status"] == "COMMITTED"
    assert retry["id"] == committed["id"]
    assert proof_service.verify_semantic_audit_chain(scope_id)

    with proof_service._session() as session:
        row = session.execute(
            text("SELECT * FROM state_proof_semantic_audit WHERE scope_id = :scope_id"),
            {"scope_id": scope_id},
        ).mappings().one()
        assert row["actor"] == "HUMAN_APPROVED_OPERATOR"
        assert row["request_id"] == "request-audit-1"
        assert row["idempotency_key"] == "audit-idem-1"
        assert row["approval_ref"] == "approval-record-1"
        assert row["rollback_ref"] == "rollback-record-1"
        assert row["before_state"]["expected_scope_version"] == 0
        assert row["after_state"]["scope_version"] == 1
        with pytest.raises(DBAPIError, match="IMMUTABLE_STATE_PROOF_AUDIT_RECORD"):
            session.execute(
                text("UPDATE state_proof_semantic_audit SET reason = 'mutated' WHERE scope_id = :scope_id"),
                {"scope_id": scope_id},
            )
    assert proof_service.verify_semantic_audit_chain(scope_id)


def test_semantic_commit_binds_scope_version_and_manifest(proof_service: StateProofService) -> None:
    scope_id = f"scope-{uuid4()}"
    result = _run(proof_service, CountingProvider(), scope_id=scope_id, auto_commit=False)["result"]
    common = {
        "result_id": str(result["id"]),
        "scope_id": scope_id,
        "expected_version": 0,
        "candidate_ref": str(result["id"]),
        "manifest_hash": str(result["input_manifest_hash"]),
    }
    with pytest.raises(ValueError, match="RESULT_SCOPE_OR_CANDIDATE_MISMATCH"):
        proof_service.commit_result(**{**common, "scope_id": f"other-{uuid4()}"})
    with pytest.raises(ValueError, match="RESULT_SCOPE_VERSION_MISMATCH"):
        proof_service.commit_result(**{**common, "expected_version": 1})
    with pytest.raises(ValueError, match="RESULT_MANIFEST_MISMATCH"):
        proof_service.commit_result(**{**common, "manifest_hash": "0" * 64})


def test_same_operation_retry_reuses_receipt_without_provider_rerun(
    proof_service: StateProofService,
) -> None:
    scope_id = f"scope-{uuid4()}"
    operation_id = str(uuid4())
    first_provider = CountingProvider()
    first = _run(proof_service, first_provider, scope_id=scope_id, operation_id=operation_id)
    retry_provider = CountingProvider()
    retry = _run(proof_service, retry_provider, scope_id=scope_id, operation_id=operation_id)

    assert first["result"]["id"] == retry["result"]["id"]
    assert first["commit"]["id"] == retry["commit"]["id"]
    assert first_provider.calls == 1
    assert retry_provider.calls == 0


def test_result_receipt_semantic_fields_are_database_immutable(proof_service: StateProofService) -> None:
    scope_id = f"scope-{uuid4()}"
    outcome = _run(proof_service, CountingProvider(), scope_id=scope_id)
    result_id = str(outcome["result"]["id"])
    with proof_service._session() as session, pytest.raises(
        DBAPIError, match="IMMUTABLE_STATE_PROOF_RECORD"
    ):
        session.execute(
            text(
                "UPDATE state_proof_results SET operation_id = 'mutated' WHERE id = CAST(:id AS uuid)"
            ),
            {"id": result_id},
        )
    assert proof_service.get_result(result_id)["operation_id"] != "mutated"


def test_projection_rebuild_does_not_call_provider_or_change_semantic_state(
    proof_service: StateProofService,
) -> None:
    scope_id = f"scope-{uuid4()}"
    provider = CountingProvider()
    outcome = _run(proof_service, provider, scope_id=scope_id)
    result_id = str(outcome["result"]["id"])
    projection_id = str(outcome["projection"]["id"])
    before_result = proof_service.get_result(result_id)
    assert before_result is not None
    proof_service.delete_projection(projection_id)
    rebuilt = proof_service.rebuild_projection(result_id=result_id, projection_kind="LIVING_SYNTHESIS")

    assert provider.calls == 1
    assert rebuilt["projection_kind"] == "LIVING_SYNTHESIS"
    assert rebuilt["content"] == outcome["projection"]["content"]
    assert proof_service.get_result(result_id)["id"] == before_result["id"]


def test_failure_and_projection_error_remain_recoverable(proof_service: StateProofService) -> None:
    scope_id = f"scope-{uuid4()}"
    failed = _run(
        proof_service,
        CountingProvider(error=RuntimeError("provider unavailable")),
        scope_id=scope_id,
    )
    assert failed["result"]["status"] == "FAILED"
    assert failed["commit"] is None
    assert proof_service.get_result(str(failed["result"]["id"]))["failure_classification"] == "PROVIDER_FAILURE"

    committed = _run(proof_service, CountingProvider(), scope_id=scope_id)
    with pytest.raises(IntegrityError):
        proof_service.rebuild_projection(result_id=str(committed["result"]["id"]), projection_kind="BAD")
    recovered = proof_service.rebuild_projection(result_id=str(committed["result"]["id"]), projection_kind="OUTPUT")
    assert recovered["status"] == "MATERIALIZED"


def test_proposal_and_memory_candidate_are_not_authority(proof_service: StateProofService) -> None:
    scope_id = f"scope-{uuid4()}"
    with proof_service._session() as session:
        before_memory = int(session.execute(text("SELECT count(*) FROM memory_records")).scalar_one())
        before_relationships = int(
            session.execute(text("SELECT count(*) FROM memory_relationships")).scalar_one()
        )
    outcome = _run(proof_service, CountingProvider(), scope_id=scope_id)
    metadata = outcome["projection"]["projection_metadata_json"]
    assert metadata["graph_authority"] == "PROPOSAL_ONLY"
    assert metadata["candidate_memory"]["authority_state"] == "CANDIDATE"

    with proof_service._session() as session:
        memory_rows = int(session.execute(text("SELECT count(*) FROM memory_records")).scalar_one())
        relationship_rows = int(session.execute(text("SELECT count(*) FROM memory_relationships")).scalar_one())
    assert memory_rows == before_memory
    assert relationship_rows == before_relationships


def test_p617_provider_adapter_uses_frozen_parser_contract(proof_service: StateProofService) -> None:
    outcome = _run(
        proof_service,
        ChatCognitiveProvider(MockProvider(), CognitiveOperation.THINK),
        scope_id=f"scope-{uuid4()}",
    )
    assert outcome["result"]["status"] == "SUCCEEDED"
    assert outcome["result"]["contract_id"] == "P6.17_COGNITIVE_RESPONSE_V1"


def test_one_result_payload_is_referenced_by_multiple_receipts(proof_service: StateProofService) -> None:
    first = _run(proof_service, CountingProvider(), scope_id=f"scope-{uuid4()}")
    second = _run(proof_service, CountingProvider(), scope_id=f"scope-{uuid4()}")
    content_hash = str(first["result"]["output_content_hash"])
    with proof_service._session() as session:
        object_row = session.execute(
            text(
                "SELECT id, size_bytes FROM state_proof_objects WHERE content_hash = :content_hash"
            ),
            {"content_hash": content_hash},
        ).mappings().one()
        receipt_count = int(
            session.execute(
                text("SELECT count(*) FROM state_proof_results WHERE output_object_id = :object_id"),
                {"object_id": object_row["id"]},
            ).scalar_one()
        )
    assert second["result"]["output_content_hash"] == content_hash
    assert receipt_count >= 2
    assert int(object_row["size_bytes"]) == len(CountingProvider().response.encode("utf-8"))

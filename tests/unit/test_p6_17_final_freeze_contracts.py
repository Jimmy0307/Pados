from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

from pados.graph import cognition, service
from pados.graph.context import CognitiveContextAssembler
from pados.graph.evidence_contract import evidence_state
from pados.graph.schemas import CognitiveOperation, EdgeCreate, IntentRequest
from pados.graph.source_discovery import SourceDiscoveryError, UnconfiguredSourceDiscovery
from pados.providers.mock import MockProvider


def _resource(*, content: str, eligibility: str = "EVIDENCE_ELIGIBLE") -> dict[str, Any]:
    return {
        "id": "source-1",
        "semantic_type": "RESOURCE",
        "title": "DNS source",
        "content": content,
        "metadata_json": {
            "source_type": "URL",
            "final_url": "https://example.test/dns",
            "fetched_at": "2026-08-21T00:00:00Z",
            "retrieval_status": "FETCHED",
            "normalization_status": "NORMALIZED",
            "evidence_eligibility": eligibility,
            "extracted_text_hash": "source-hash-1",
        },
    }


def test_source_and_evidence_contract_is_provenance_complete() -> None:
    assembled = CognitiveContextAssembler().assemble(
        workspace_id="workspace",
        root_node_ids=["idea-1"],
        intent="THINK",
        action="THINK",
        nodes=[
            {
                "id": "idea-1",
                "semantic_type": "IDEA",
                "title": "DNS question",
                "content": "DNS",
                "metadata_json": {"current_objective": "DNS"},
            },
            _resource(content="DNS maps names to addresses.")
            | {"id": "source-1"},
        ],
        edges=[],
    )

    assert assembled["resource_references"][0]["source_id"] == "source-1"
    assert assembled["resource_references"][0]["content_version"] == "source-hash-1"
    assert assembled["evidence"][0]["source_id"] == "source-1"
    assert assembled["evidence"][0]["source_version"] == "source-hash-1"
    assert assembled["evidence"][0]["selection_status"] == "SELECTED"
    assert assembled["evidence"][0]["location"]["paragraph_index"] == 0
    assert evidence_state(assembled["resource_references"], assembled["evidence"]) == "EVIDENCE_SELECTED"


def test_reference_only_source_cannot_claim_selected_evidence() -> None:
    assembled = CognitiveContextAssembler().assemble(
        workspace_id="workspace",
        root_node_ids=["idea-1"],
        intent="THINK",
        action="THINK",
        nodes=[
            {"id": "idea-1", "semantic_type": "IDEA", "title": "DNS", "content": "DNS"},
            _resource(content="Navigation and pricing", eligibility="UNKNOWN"),
        ],
        edges=[],
    )

    assert assembled["evidence"] == []
    assert assembled["evidence_state"] == "REFERENCE_ONLY"
    assert any(item["code"] == "EVIDENCE_NOT_ELIGIBLE" for item in assembled["gaps"])


def test_raw_resource_body_is_excluded_from_provider_context() -> None:
    raw_body = "PRIVATE_RAW_BODY_DO_NOT_SEND " * 400
    assembled = CognitiveContextAssembler().assemble(
        workspace_id="workspace",
        root_node_ids=["idea-1"],
        intent="THINK",
        action="THINK",
        nodes=[
            {"id": "idea-1", "semantic_type": "IDEA", "title": "DNS", "content": "DNS"},
            _resource(content=f"DNS evidence paragraph. {raw_body}"),
        ],
        edges=[],
    )
    prompt = cognition.build_prompt(CognitiveOperation.THINK, {"context_assembly": assembled})

    assert all(node["semantic_type"] != "RESOURCE" for node in assembled["nodes"])
    assert raw_body not in prompt
    assert assembled["context_receipt"]["estimated_input_tokens"] <= assembled["context_budget"]["total"]


def test_context_roles_keep_actions_and_experts_distinct_from_concepts() -> None:
    assembled = CognitiveContextAssembler().assemble(
        workspace_id="workspace",
        root_node_ids=["idea-1"],
        intent="THINK",
        action="THINK",
        nodes=[
            {
                "id": "idea-1",
                "semantic_type": "IDEA",
                "title": "DNS question",
                "content": "DNS",
                "metadata_json": {"root_question": "DNS"},
            },
            {"id": "action-1", "semantic_type": "ACTION", "title": "DNS action", "content": "DNS"},
            {"id": "expert-1", "semantic_type": "EXPERT", "title": "DNS expert", "content": "DNS"},
        ],
        edges=[],
    )
    roles = {node["id"]: node["context_role"] for node in assembled["nodes"]}
    assert roles["idea-1"] == "ROOT_QUESTION"
    assert roles["action-1"] == "USER_CONSTRAINT"
    assert roles["expert-1"] == "EXPERT_REFERENCE"


def test_generic_decomposition_is_panel_only_and_root_focus_is_preserved(monkeypatch: pytest.MonkeyPatch) -> None:
    created: list[dict[str, Any]] = []

    monkeypatch.setattr(service.repository, "get_workspace", lambda *_args: {"id": "workspace", "name": "Personal", "description": ""})
    monkeypatch.setattr(service.repository, "list_nodes", lambda *_args: [])
    monkeypatch.setattr(service.repository, "list_edges", lambda *_args: [])
    monkeypatch.setattr(
        service._CONTEXT_ASSEMBLER,
        "assemble",
        lambda **_kwargs: {
            "nodes": [],
            "resource_references": [],
            "evidence": [],
            "gaps": [],
            "source_summary": {},
            "context_fingerprint": "freeze-test",
        },
    )

    def fake_create(_session: object, _workspace_id: str, payload: Any) -> dict[str, Any]:
        row = {
            "id": "idea-1",
            "semantic_type": payload.semantic_type.value,
            "title": payload.title,
            "content": payload.content,
            "metadata_json": payload.metadata,
        }
        created.append(row)
        return row

    monkeypatch.setattr(service, "create_node", fake_create)
    result = service.interpret_intent(
        object(),
        "workspace",
        IntentRequest(text="What is DNS?", x=10, y=20),
    )

    assert result["decomposition"]
    assert result["decomposition_nodes"] == []
    assert [row["semantic_type"] for row in created] == ["IDEA"]
    metadata = result["node"]["metadata_json"]
    assert metadata["root_question"] == "What is DNS?"
    assert metadata["current_focus"] == "What is DNS?"
    assert metadata["decomposition_persistence"] == "PANEL_ONLY"


def test_relationship_vocabulary_is_bounded() -> None:
    assert EdgeCreate(source_node_id="a", target_node_id="b", relationship_type="SUPPORTS").relationship_type == "SUPPORTS"
    with pytest.raises(ValueError, match="bounded graph relation"):
        EdgeCreate(source_node_id="a", target_node_id="b", relationship_type="INVENTED_RELATION")


def test_strict_provider_contract_rejects_legacy_array_and_accepts_mock_object() -> None:
    with pytest.raises(cognition.CognitiveResponseError, match="JSON object"):
        cognition.parse_cognitive_response(json.dumps([{"type": "OUTPUT", "content": "legacy"}]))
    with pytest.raises(cognition.CognitiveResponseError, match="unknown fields"):
        cognition.parse_cognitive_response(json.dumps({"answer": "ok", "unexpected": True}))

    response = json.loads(asyncio.run(MockProvider().chat("Explain DNS.")))
    parsed = cognition.parse_cognitive_response(json.dumps(response))
    assert parsed["answer"]
    assert "MOCK_CONTRACT_TEST_ONLY" in parsed["limitations"]


def test_search_seam_fails_closed_without_pretending_to_search() -> None:
    with pytest.raises(SourceDiscoveryError, match="公開資料搜尋來源"):
        UnconfiguredSourceDiscovery().discover("DNS sandbox")


def test_provider_failure_classification_is_explicit() -> None:
    assert service._provider_failure_code(RuntimeError("timeout")) == "GENERATION_FAILED"
    assert service._provider_failure_message("INVALID_SCHEMA")


def test_provider_context_budget_fails_closed() -> None:
    with pytest.raises(cognition.CognitiveResponseError, match="exceeds budget") as error:
        service._enforce_provider_context_budget({"context_budget": {"total": 10}}, 11)
    assert error.value.code == "CONTEXT_LIMIT"

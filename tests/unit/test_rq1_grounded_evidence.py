from __future__ import annotations

import asyncio
import json
import re
from typing import Any

import pytest

from pados.graph import cognition, service
from pados.graph.cognition import CognitiveResponseError, build_prompt, validate_evidence_refs
from pados.graph.cognitive_pipeline import CognitiveGoalInterpreter, ProblemDecomposer, SynthesisPlanner
from pados.graph.context import CognitiveContextAssembler, node_semantic_projection
from pados.graph.schemas import CognitiveOperation
from pados.providers.mock import MockProvider

ROOT = {"id": "root", "semantic_type": "IDEA", "title": "Storage question",
        "content": "Describe storage pressure levels and eviction.", "metadata_json": {}}
PRESSURE = {"id": "pressure", "semantic_type": "CONCEPT", "title": "Graceful storage pressure",
            "content": "Storage pressure levels are OK, WARN and CRITICAL.",
            "metadata_json": {}}
EVICTION = {"id": "eviction", "semantic_type": "CONCEPT", "title": "Eviction",
            "content": "Eviction is a recommendation only and never deletes data automatically.",
            "metadata_json": {}}
RESOURCE = {
    "id": "source-1",
    "semantic_type": "RESOURCE",
    "title": "Storage reference",
    "content": "Storage pressure guidance from the operator manual.",
    "metadata_json": {
        "final_url": "https://example.test/storage",
        "retrieval_status": "FETCHED",
        "evidence_eligibility": "EVIDENCE_ELIGIBLE",
        "extracted_text_hash": "source-hash-1",
    },
}


def _assemble(nodes: list[dict[str, Any]]) -> dict[str, Any]:
    return CognitiveContextAssembler().assemble(
        workspace_id="w", root_node_ids=["root"], intent="THINK", action="THINK", nodes=nodes, edges=[])


def _spec(assembly: dict[str, Any]) -> dict[str, Any]:
    goal = CognitiveGoalInterpreter().interpret(str(ROOT["content"]))
    return SynthesisPlanner().plan(goal, ProblemDecomposer().decompose(goal), assembly)


def test_evidence_ids_are_server_assigned_in_prompt_order_with_provenance() -> None:
    assembled = _assemble([ROOT, PRESSURE, EVICTION, RESOURCE])
    evidence = assembled["evidence"]

    assert [item["evidence_id"] for item in evidence] == [f"E{index}" for index in range(1, len(evidence) + 1)]
    assert evidence[0]["source_node_id"] == "source-1"
    assert evidence[0]["source_ref"] == "source-1:0:0"
    concept = {item["source_node_id"]: item for item in evidence[1:]}
    assert set(concept) == {"pressure", "eviction"}
    for node in (PRESSURE, EVICTION):
        item = concept[str(node["id"])]
        assert item["source_title"] == node["title"]
        assert item["content_hash"] == node_semantic_projection(node)["content_hash"]
        assert item["excerpt"] == node["content"]
        assert item["source_ref"] == node["id"]
    prompt = build_prompt(CognitiveOperation.THINK, {"synthesis_spec": _spec(assembled)})
    positions = [prompt.index(f"[{item['evidence_id']}] {item['source_title']}") for item in evidence]
    assert positions == sorted(positions)


def test_prompt_renders_the_evidence_block_once_and_instructs_grounded_citation() -> None:
    assembled = _assemble([ROOT, PRESSURE, EVICTION])
    spec = _spec(assembled)
    prompt = build_prompt(CognitiveOperation.THINK, {"synthesis_spec": spec})

    assert "EVIDENCE (server-assigned ids; cite only these):" in prompt
    assert "[E1] Graceful storage pressure" in prompt
    assert prompt.count(str(PRESSURE["content"])) == 1
    assert "END EVIDENCE" in prompt
    assert "Answer only from the EVIDENCE block" in prompt
    assert "If the evidence is insufficient, say so in limitations" in prompt
    assert "evidence_refs must be []" in prompt
    embedded = json.loads(re.search(r"specification JSON:\n(\{.*\})\nEnd cognitive specification", prompt, re.S).group(1))  # type: ignore[union-attr]
    assert [item["evidence_id"] for item in embedded["evidence"]] == ["E1", "E2"]
    assert all("excerpt" not in item for item in embedded["evidence"])
    assert [item["title"] for item in spec["supporting_context"]] == [ROOT["title"]]


def test_no_evidence_prompt_says_so() -> None:
    assembled = _assemble([ROOT])
    prompt = build_prompt(CognitiveOperation.THINK, {"synthesis_spec": _spec(assembled)})

    assert assembled["evidence"] == []
    assert "EVIDENCE: none provided." in prompt


@pytest.mark.parametrize("ref", ["E99", "Graceful storage pressure", "pressure"])
def test_fabricated_reference_is_invalid_schema(ref: str) -> None:
    allowed = [item["evidence_id"] for item in _assemble([ROOT, PRESSURE, EVICTION])["evidence"]]
    response = {"answer": "x", "claims": [{"claim": "c", "evidence_refs": ["E1", ref]}], "evidence_refs": ["E1"]}

    with pytest.raises(CognitiveResponseError) as error:
        validate_evidence_refs(response, allowed)
    assert error.value.code == "INVALID_SCHEMA"


def test_valid_subset_of_provided_ids_passes() -> None:
    allowed = [item["evidence_id"] for item in _assemble([ROOT, PRESSURE, EVICTION])["evidence"]]
    validate_evidence_refs({"answer": "x", "claims": [{"claim": "c", "evidence_refs": ["E2"]}], "evidence_refs": ["E2"]}, allowed)
    validate_evidence_refs({"answer": "x", "claims": [], "evidence_refs": []}, allowed)


def test_concept_only_selection_now_produces_evidence() -> None:
    assembled = _assemble([ROOT, PRESSURE])

    assert [item["source_node_id"] for item in assembled["evidence"]] == ["pressure"]
    assert assembled["evidence_state"] == "EVIDENCE_SELECTED"
    assert assembled["source_summary"]["evidence_units"] == 1
    stub = next(node for node in assembled["nodes"] if node["id"] == "pressure")
    assert stub["evidence_id"] == "E1" and stub["content"] == ""


def test_mock_provider_echoes_e_ids_that_validate() -> None:
    spec = _spec(_assemble([ROOT, PRESSURE, EVICTION]))
    response = cognition.parse_cognitive_response(
        asyncio.run(MockProvider().chat(build_prompt(CognitiveOperation.THINK, {"synthesis_spec": spec}))))

    assert response["evidence_refs"] == ["E1", "E2"]
    validate_evidence_refs(response, [item["evidence_id"] for item in spec["evidence"]])


def test_output_metadata_maps_e_ids_back_to_source_nodes() -> None:
    assembled = _assemble([ROOT, PRESSURE, EVICTION])
    response = {"claims": [{"claim": "c", "evidence_refs": ["E2"]}], "evidence_refs": ["E2"]}
    provenance = service._evidence_provenance(assembled["evidence"], response)

    assert provenance["evidence_ids"] == ["E1", "E2"]
    by_id = {item["evidence_id"]: item for item in provenance["evidence_sources"]}
    assert by_id["E2"]["source_node_id"] == assembled["evidence"][1]["source_node_id"]
    assert by_id["E2"]["content_hash"] == assembled["evidence"][1]["content_hash"]
    assert provenance["cited_source_node_ids"] == [assembled["evidence"][1]["source_node_id"]]
    used, _ = service._cognitive_source_ids(assembled)
    assert used == ["root", *[item["source_node_id"] for item in assembled["evidence"]]]

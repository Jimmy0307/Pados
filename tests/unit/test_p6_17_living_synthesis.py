from __future__ import annotations

import asyncio
import json
from contextlib import contextmanager
from typing import Any

from pados.config import Settings
from pados.graph import cognition, context, service
from pados.graph.schemas import CognitiveOperation
from pados.providers.mock import MockProvider

WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
ROOT_ID = "22222222-2222-4222-8222-222222222222"
CONCEPT_ID = "33333333-3333-4333-8333-333333333333"
RESOURCE_ID = "44444444-4444-4444-8444-444444444444"


def node(node_id: str, semantic_type: str, title: str, content: str = "", **extra: Any) -> dict[str, Any]:
    return {
        "id": node_id,
        "semantic_type": semantic_type,
        "title": title,
        "content": content,
        "x": 10.0,
        "y": 20.0,
        "version": 1,
        "provenance": "HUMAN_CREATED",
        "assurance_status": "PROVISIONAL",
        "metadata_json": {},
        **extra,
    }


def assemble(nodes: list[dict[str, Any]], edges: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return context.CognitiveContextAssembler().assemble(
        workspace_id=WORKSPACE_ID,
        root_node_ids=[ROOT_ID],
        intent="THINK",
        action="THINK",
        nodes=nodes,
        edges=edges or [],
    )


def test_context_fingerprint_ignores_position_and_cosmetic_version_churn() -> None:
    original = node(ROOT_ID, "IDEA", "DNS system architecture")
    moved = {**original, "x": 900, "y": -300, "version": 8}

    first = assemble([original])
    second = assemble([moved])

    assert first["context_fingerprint"] == second["context_fingerprint"]
    assert context.classify_node_change(original, moved) == context.DirtyEvent.COSMETIC_CHANGE


def test_semantic_and_source_changes_mark_synthesis_stale() -> None:
    original = assemble([node(ROOT_ID, "IDEA", "DNS system architecture")])
    metadata = {
        "context_fingerprint": original["context_fingerprint"],
        "source_snapshot": original["source_snapshot"],
    }

    with_concept = assemble(
        [
            node(ROOT_ID, "IDEA", "DNS system architecture"),
            node(CONCEPT_ID, "CONCEPT", "DNSSEC and DoH"),
        ]
    )
    stale, reason = context.compare_synthesis_context(metadata, with_concept)
    assert stale is True
    assert reason is not None
    assert reason["kind"] == "SEMANTIC_CHANGE"
    assert CONCEPT_ID in reason["added_node_ids"]

    with_resource = assemble(
        [
            node(ROOT_ID, "IDEA", "DNS system architecture"),
            node(RESOURCE_ID, "RESOURCE", "DNS operations reference", "DNSSEC, DoH and enterprise DNS"),
        ]
    )
    stale, reason = context.compare_synthesis_context(metadata, with_resource)
    assert stale is True
    assert reason is not None
    assert reason["kind"] == "SOURCE_CHANGE"
    assert RESOURCE_ID in reason["added_resource_ids"]


def test_context_assembly_is_bounded_and_has_provenance_snapshot() -> None:
    nodes = [node(ROOT_ID, "IDEA", "DNS system architecture")]
    nodes.extend(node(str(index).zfill(36), "CONCEPT", f"DNS concept {index}") for index in range(1, 100))

    assembled = assemble(nodes)

    assert len(assembled["nodes"]) <= 40
    assert assembled["source_snapshot"]["root_node_ids"] == [ROOT_ID]
    assert assembled["source_snapshot"]["node_versions"]
    assert assembled["context_fingerprint"]


def test_mock_synthesis_changes_when_context_adds_dns_topics_and_resource() -> None:
    assembled = assemble(
        [
            node(ROOT_ID, "IDEA", "DNS system architecture"),
            node(CONCEPT_ID, "CONCEPT", "DNSSEC / DoH / Enterprise DNS"),
            node(RESOURCE_ID, "RESOURCE", "DNS standards reference", "Authoritative source for DNSSEC and DoH."),
        ]
    )
    prompt = cognition.build_prompt(CognitiveOperation.THINK, {"context_assembly": assembled})
    response = json.loads(asyncio.run(MockProvider().chat(prompt)))

    assert "DNSSEC / DoH / Enterprise DNS" in response["answer"]
    assert "DNS standards reference" in response["answer"]
    assert assembled["context_fingerprint"] in prompt


def test_workspace_projection_marks_stale_without_mutating_immutable_output() -> None:
    root = node(ROOT_ID, "IDEA", "DNS system architecture")
    initial = assemble([root])
    output_metadata = {
        "living_synthesis": True,
        "result_type": "LIVING_SYNTHESIS",
        "synthesis_id": "synthesis-1",
        "synthesis_version": 1,
        "context_fingerprint": initial["context_fingerprint"],
        "source_snapshot": initial["source_snapshot"],
        "root_node_ids": [ROOT_ID],
    }
    output = node("55555555-5555-4555-8555-555555555555", "OUTPUT", "DNS synthesis", "Immutable V1", metadata_json=output_metadata)
    nodes = [root, node(CONCEPT_ID, "CONCEPT", "DNSSEC"), output]

    projected, history = service._annotate_living_syntheses(WORKSPACE_ID, nodes, [])
    projected_output = next(item for item in projected if item["semantic_type"] == "OUTPUT")

    assert projected_output["metadata_json"]["synthesis_status"] == "STALE"
    assert projected_output["content"] == "Immutable V1"
    assert output["metadata_json"].get("synthesis_status") is None
    assert history[0]["version"] == 1


def test_refresh_creates_v2_keeps_v1_and_uses_current_context(monkeypatch: Any) -> None:
    root = node(ROOT_ID, "IDEA", "DNS system architecture")
    concept = node(CONCEPT_ID, "CONCEPT", "DNSSEC and DoH")
    initial = assemble([root])
    output = node(
        "55555555-5555-4555-8555-555555555555",
        "OUTPUT",
        "DNS system architecture",
        "V1 baseline",
        provenance="AI_PROPOSED",
        metadata_json={
            "living_synthesis": True,
            "result_type": "LIVING_SYNTHESIS",
            "synthesis_id": "synthesis-1",
            "synthesis_version": 1,
            "context_fingerprint": initial["context_fingerprint"],
            "source_snapshot": initial["source_snapshot"],
            "root_node_ids": [ROOT_ID],
            "source_node_ids": [ROOT_ID],
            "intent": "THINK",
            "action": "THINK",
            "auto_refresh_policy": "AUTO_REFRESH_PROMPT",
            "provider": "mock",
            "model": "mock",
        },
    )
    rows = [root, concept, output]

    @contextmanager
    def fake_scope(_settings: Settings) -> Any:
        yield object()

    def fake_create(_session: object, _workspace_id: str, payload: Any) -> dict[str, Any]:
        created = {
            "id": "66666666-6666-4666-8666-666666666666",
            "semantic_type": payload.semantic_type.value,
            "title": payload.title,
            "content": payload.content,
            "x": payload.x,
            "y": payload.y,
            "version": 1,
            "provenance": payload.provenance.value,
            "assurance_status": payload.assurance_status.value,
            "metadata_json": payload.metadata,
        }
        rows.append(created)
        return created

    monkeypatch.setattr(service, "session_scope", fake_scope)
    monkeypatch.setattr(service.repository, "get_workspace", lambda *_args: {"id": WORKSPACE_ID})
    monkeypatch.setattr(service.repository, "get_node", lambda *_args: output)
    monkeypatch.setattr(service.repository, "list_nodes", lambda *_args: rows)
    monkeypatch.setattr(service.repository, "list_edges", lambda *_args: [])
    monkeypatch.setattr(service, "create_node", fake_create)

    result = asyncio.run(
        service.refresh_living_synthesis(
            Settings.model_validate({"PADOS_ENV": "validation", "PADOS_CHAT_PROVIDER": "mock"}),
            WORKSPACE_ID,
            str(output["id"]),
        )
    )

    refreshed = result["synthesis"]
    assert result["refreshed"] is True
    assert refreshed["metadata_json"]["synthesis_version"] == 2
    assert refreshed["metadata_json"]["previous_synthesis_node_id"] == output["id"]
    assert "DNSSEC and DoH" in refreshed["content"]
    assert any(row["id"] == output["id"] and row["content"] == "V1 baseline" for row in rows)
    assert len(result["history"]) == 2


def test_living_synthesis_metadata_and_artifact_export_preserve_version_provenance() -> None:
    assembled = assemble([node(ROOT_ID, "IDEA", "DNS system architecture")])
    output = {
        "id": "55555555-5555-4555-8555-555555555555",
        "semantic_type": "OUTPUT",
        "title": "DNS system architecture",
        "content": "Current DNS synthesis",
        "provenance": "AI_PROPOSED",
        "assurance_status": "PROVISIONAL",
        "metadata_json": {
            "living_synthesis": True,
            "result_type": "LIVING_SYNTHESIS",
            "synthesis_id": "synthesis-1",
            "synthesis_version": 2,
            "context_fingerprint": assembled["context_fingerprint"],
            "source_snapshot": assembled["source_snapshot"],
            "source_summary_text": "1 個概念、0 份資料、0 個記憶、0 筆 Corpus 證據",
        },
    }

    markdown = service.build_output_markdown(output, [node(ROOT_ID, "IDEA", "DNS system architecture")])

    assert "Synthesis version: 2" in markdown
    assert assembled["context_fingerprint"] in markdown
    assert "synthesis-1" not in markdown

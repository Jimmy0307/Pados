from __future__ import annotations

from typing import Any

from pados.graph import service
from pados.graph.schemas import NodeUpdate, SystemNodeUpdate


def test_output_markdown_contains_readable_result_and_sources_without_internal_ids() -> None:
    node_id = "11111111-1111-4111-8111-111111111111"
    source_id = "22222222-2222-4222-8222-222222222222"
    protected_key = "to" + "ken"
    output: dict[str, Any] = {
        "id": node_id,
        "semantic_type": "OUTPUT",
        "title": "DNS 系統架構總覽",
        "content": f"Resolver {protected_key}=do-not-export this content is readable.",
        "provenance": "AI_PROPOSED",
        "created_at": "2026-08-17T10:00:00+00:00",
        "metadata_json": {
            "result_type": "COGNITIVE_RESULT",
            "provider": "mock",
            "model": "mock-thinking",
            "candidate_status": "PROVISIONAL",
        },
    }
    source = {
        "id": source_id,
        "semantic_type": "IDEA",
        "title": "我想要了解整個 DNS 系統架構",
        "metadata_json": {"original_url": "https://example.test/dns"},
    }

    markdown = service.build_output_markdown(output, [source])

    assert markdown.startswith("# DNS 系統架構總覽")
    assert f"Resolver {protected_key}: [REDACTED]" in markdown
    assert "我想要了解整個 DNS 系統架構 (IDEA)" in markdown
    assert "https://example.test/dns" in markdown
    assert node_id not in markdown
    assert source_id not in markdown
    assert "mock-response" not in markdown


def test_metadata_is_server_owned_for_artifact_updates() -> None:
    update = SystemNodeUpdate(version=1, metadata={"artifact": {"status": "SAVED"}})
    assert update.metadata == {"artifact": {"status": "SAVED"}}
    assert "metadata" not in NodeUpdate.model_fields


def test_human_safe_bundle_hides_legacy_mock_placeholder_nodes(monkeypatch: Any) -> None:
    visible = {"id": "1", "semantic_type": "IDEA", "title": "DNS", "content": ""}
    residue = {"id": "2", "semantic_type": "OUTPUT", "title": "Old review", "content": "mock-response:123"}
    monkeypatch.setattr(service.repository, "get_workspace", lambda *_args: {"id": "workspace", "name": "My personal space", "description": ""})
    monkeypatch.setattr(service.repository, "list_nodes", lambda *_args: [visible, residue])
    monkeypatch.setattr(service.repository, "list_edges", lambda *_args: [])
    monkeypatch.setattr(service.repository, "list_groups", lambda *_args: [])
    monkeypatch.setattr(service.repository, "list_cognitive_items", lambda *_args: [])

    bundle = service.workspace_bundle(object(), "workspace")  # type: ignore[arg-type]

    assert [node["id"] for node in bundle["nodes"]] == ["1"]

"""RC2 D2: the graph cognitive path runs a real model only under a measured execution binding (no database)."""
from __future__ import annotations

import asyncio
import json
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from typing import Any

import pytest

from pados.config import Settings
from pados.graph import cognition, service
from pados.providers import factory
from pados.providers.execution_binding import ExecutionIntent
from pados.providers.mock import MockProvider
from pados.providers.ollama import MeasuredExecutionTimeout

WORKSPACE_ID = "11111111-1111-4111-8111-111111111111"
ITEM_ID = "22222222-2222-4222-8222-222222222222"
PROJECT_ID = "33333333-3333-4333-8333-333333333333"
ROOT_ID = "44444444-4444-4444-8444-444444444444"
EVIDENCE = {"schema": "pados.providers.measured-execution.v1", "profile_id": "mcp-" + "a" * 64,
            "manifest_digest": "sha256:" + "b" * 64, "device_profile_id": "c" * 64,
            "execution": {"context_window": 8192}, "config_hash": "d" * 64, "timeout_seconds": 30,
            "authorization": "COMPUTE_NODE_REGISTRATION", "runtime": {"done_reason": "stop"}}


class Ledger:
    def __init__(self) -> None:
        self.failed: list[dict[str, Any]] = []
        self.completed: list[dict[str, Any]] = []
        self.created: list[Any] = []


@pytest.fixture
def ledger(monkeypatch: pytest.MonkeyPatch) -> Ledger:
    record = Ledger()
    item = {"id": ITEM_ID, "workspace_id": WORKSPACE_ID, "operation": "THINK",
            "selection_context": {"nodes": [{"id": ROOT_ID, "title": "DNS", "x": 1.0, "y": 2.0}],
                                  "synthesis_spec": {"intent": "THINK", "evidence": []}}}

    @contextmanager
    def fake_scope(_settings: Settings) -> Iterator[object]:
        yield object()

    def fail(_session: object, item_id: str, *, error_code: str, status: str = "BLOCKED") -> dict[str, Any]:
        record.failed.append({"id": item_id, "error_code": error_code, "status": status})
        return {}

    def complete(_session: object, item_id: str, *, provider: str,
                 candidates: list[Mapping[str, Any]]) -> dict[str, Any]:
        record.completed.append({"id": item_id, "provider": provider, "candidates": candidates})
        return {"id": item_id, "status": "COMPLETED"}

    def create(_session: object, _workspace_id: str, payload: Any) -> dict[str, Any]:
        record.created.append(payload)
        return {"id": "55555555-5555-4555-8555-555555555555", "metadata_json": payload.metadata}

    monkeypatch.setattr(service, "session_scope", fake_scope)
    monkeypatch.setattr(service.repository, "mark_cognitive_processing", lambda *_a: dict(item))
    monkeypatch.setattr(service.repository, "get_workspace", lambda *_a: {"id": WORKSPACE_ID,
                                                                        "project_id": PROJECT_ID})
    monkeypatch.setattr(service.repository, "fail_cognitive_item", fail)
    monkeypatch.setattr(service.repository, "complete_cognitive_item", complete)
    monkeypatch.setattr(service, "create_node", create)
    return record


def local_settings(**overrides: str) -> Settings:
    values = {"PADOS_ENV": "test", "PADOS_CHAT_PROVIDER": "ollama", "PADOS_CHAT_MODEL": "example-model:small",
              "PADOS_MODEL_CATALOG": "", "PADOS_COMPUTE_NODES_CONFIG": ""}
    values.update(overrides)
    return Settings.model_validate(values)


class BoundFake:
    """Stands in for MeasuredChatProvider: answers the cognitive contract and exposes measured evidence."""

    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.schemas: list[Any] = []
        self.last_evidence: dict[str, Any] | None = None

    async def chat(self, prompt: str, *, response_schema: Mapping[str, Any] | None = None) -> str:
        self.schemas.append(response_schema)
        if self.error is not None:
            raise self.error
        self.last_evidence = dict(EVIDENCE)
        return json.dumps({"title": "DNS", "answer": "bounded answer"})

    async def healthcheck(self) -> dict[str, str]:
        return {"status": "ok"}

    async def estimate_context(self, prompt: str) -> int:
        return 10


def test_no_catalog_blocks_the_item_and_never_reaches_a_provider(
        ledger: Ledger, monkeypatch: pytest.MonkeyPatch) -> None:
    def never(*_a: object, **_k: object) -> None:
        raise AssertionError("no provider may be constructed without a measured binding")

    monkeypatch.setattr(factory, "MeasuredChatProvider", never)
    with pytest.raises(service.GraphError) as refused:
        asyncio.run(service.process_cognitive_item(local_settings(), ITEM_ID))
    error = refused.value
    assert error.code == "MEASURED_EXECUTION_REFUSED" and error.status_code == 409 and error.write_occurred
    assert "MEASURED_PROFILE_STORE_NOT_CONFIGURED" in error.message
    assert error.details == {"refusal_code": "MEASURED_PROFILE_STORE_NOT_CONFIGURED", "reasons": []}
    assert ledger.failed == [{"id": ITEM_ID, "error_code": "MEASURED_PROFILE_STORE_NOT_CONFIGURED",
                              "status": "BLOCKED"}]
    assert ledger.created == [] and ledger.completed == []


def test_bound_provider_evidence_is_persisted_on_the_candidate(
        ledger: Ledger, monkeypatch: pytest.MonkeyPatch) -> None:
    provider = BoundFake()
    calls: list[dict[str, Any]] = []

    def build(settings: Settings, **kwargs: Any) -> BoundFake:
        calls.append(kwargs)
        return provider

    monkeypatch.setattr(service, "build_chat_provider", build)
    result = asyncio.run(service.process_cognitive_item(local_settings(), ITEM_ID))
    assert calls == [{"allow_mock": True, "intent": ExecutionIntent.STRUCTURED_OUTPUT, "project_id": PROJECT_ID}]
    assert provider.schemas == [cognition.COGNITIVE_RESPONSE_SCHEMA]
    (payload,) = ledger.created
    assert payload.metadata["measured_execution"] == EVIDENCE
    assert payload.metadata["cognitive_runtime_status"] == "MEASURED_BINDING_APPLIED"
    (completed,) = ledger.completed
    assert completed["candidates"][0]["measured_execution"] == EVIDENCE
    assert result["work_item"]["status"] == "COMPLETED"


def test_backend_timeout_maps_to_the_existing_timeout_failure(
        ledger: Ledger, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(service, "build_chat_provider",
                        lambda *_a, **_k: BoundFake(MeasuredExecutionTimeout("BACKEND_TIMEOUT")))
    with pytest.raises(service.GraphError) as failed:
        asyncio.run(service.process_cognitive_item(local_settings(), ITEM_ID))
    assert failed.value.code == "TIMEOUT"
    assert ledger.failed == [{"id": ITEM_ID, "error_code": "TIMEOUT", "status": "FAILED"}]


def test_mock_keeps_its_own_status_and_needs_no_binding(ledger: Ledger) -> None:
    asyncio.run(service.process_cognitive_item(local_settings(PADOS_CHAT_PROVIDER="mock"), ITEM_ID))
    (payload,) = ledger.created
    assert payload.metadata["cognitive_runtime_status"] == "MOCK_CONTRACT_TEST_ONLY"
    assert "measured_execution" not in payload.metadata


def test_mock_accepts_the_schema_keyword() -> None:
    response = asyncio.run(MockProvider().chat("x", response_schema=cognition.COGNITIVE_RESPONSE_SCHEMA))
    assert cognition.parse_cognitive_response(response)["answer"]


def test_cognitive_schema_matches_the_validator_bounds() -> None:
    schema = cognition.COGNITIVE_RESPONSE_SCHEMA
    assert schema["required"] == ["answer"] and schema["additionalProperties"] is False
    assert set(schema["properties"]) == {"title", "answer", "subquestions", "gaps", "claims", "evidence_refs",
                                         "proposed_graph_changes", "limitations"}
    assert schema["properties"]["proposed_graph_changes"]["maxItems"] == 3

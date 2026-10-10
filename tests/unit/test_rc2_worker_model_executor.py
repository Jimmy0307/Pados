from types import SimpleNamespace

import pytest

from pados.providers import execution_binding
from pados.providers.execution_binding import ExecutionBindingRefused, ExecutionIntent, ExecutionRefusal
from pados.worker import registry
from pados.worker.service import _execution_intent_for_capability, _model_stdout_contract
from pados.worker.schemas import WorkerAvailability


def test_model_capabilities_map_only_to_supported_measured_intents() -> None:
    assert _execution_intent_for_capability("SUMMARIZE") is ExecutionIntent.TEXT_SUMMARY
    assert _execution_intent_for_capability("CLASSIFY_REQUEST") is ExecutionIntent.STRUCTURED_OUTPUT
    assert _execution_intent_for_capability("MODIFY_CODE") is None


def test_model_stdout_contract_is_fixed_and_bounded() -> None:
    result = _model_stdout_contract("x" * 9000)
    assert set(result) == {"result_summary", "changed_files", "validation_results", "remaining_risks"}
    assert len(result["result_summary"]) == 8000
    assert result["changed_files"] == []
    assert result["validation_results"] == []
    assert result["remaining_risks"] == []


def test_ollama_discovery_refuses_when_project_binding_is_unavailable(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple[ExecutionIntent, str]] = []

    def refuse(_settings: object, intent: ExecutionIntent, *, project_id: str) -> None:
        calls.append((intent, project_id))
        raise ExecutionBindingRefused(ExecutionRefusal("MEASURED_EXECUTION_INTENT_REQUIRED"))

    monkeypatch.setattr(execution_binding, "resolve_bound_execution", refuse)
    worker = registry._ollama_spec(SimpleNamespace(), project_id=None)  # type: ignore[arg-type]
    assert calls == [(ExecutionIntent.TEXT_SUMMARY, "")]
    assert worker.availability is WorkerAvailability.UNAVAILABLE
    assert worker.reason == "MEASURED_EXECUTION_INTENT_REQUIRED"

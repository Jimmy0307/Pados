"""Product-loop provider pin over the existing Worker Router."""
from __future__ import annotations

from pados.product_loop.provider_selection import select_product_loop_provider
from pados.worker.schemas import CapabilityId, RiskLevel, WorkerAvailability, WorkerSpec, WorkerType


def _worker(worker_id: str, worker_type: WorkerType, *, available: bool = True) -> WorkerSpec:
    return WorkerSpec(
        worker_id=worker_id, worker_type=worker_type, display_name=worker_id,
        availability=WorkerAvailability.AVAILABLE if available else WorkerAvailability.UNAVAILABLE,
        capabilities=[CapabilityId.MODIFY_CODE], risk_ceiling=RiskLevel.MEDIUM,
        supports_structured_output=True, supports_file_changes=True, supports_tool_calls=True,
        supports_streaming=False, max_context_tokens=2000, default_timeout_seconds=30,
    )


def _both(*, codex: bool = True, claude: bool = True) -> dict[str, WorkerSpec]:
    return {
        "claude-code-runtime": _worker("claude-code-runtime", WorkerType.CLAUDE_CODE, available=claude),
        "codex-cli": _worker("codex-cli", WorkerType.CODEX_CLI, available=codex),
    }


def test_auto_ranks_only_executable_adapters_so_claude_does_not_shadow_codex() -> None:
    selection = select_product_loop_provider("AUTO", _both())
    assert selection.selected and selection.worker is not None
    assert selection.worker.worker_type == WorkerType.CODEX_CLI
    assert selection.provider_id == "PADOS_CODEX_CLI"


def test_auto_with_only_non_adapter_worker_fails_closed() -> None:
    selection = select_product_loop_provider("AUTO", _both(codex=False))
    assert not selection.selected and selection.error_code


def test_explicit_codex_selects_codex_and_fails_closed_when_unavailable() -> None:
    assert select_product_loop_provider("codex", _both()).provider_id == "PADOS_CODEX_CLI"
    missing = select_product_loop_provider("CODEX", _both(codex=False))
    assert not missing.selected
    assert missing.error_code == "PRODUCT_LOOP_PROVIDER_UNAVAILABLE"


def test_explicit_provider_without_adapter_and_invalid_values_fail_closed() -> None:
    assert select_product_loop_provider("CLAUDE_CODE", _both()).error_code == "PRODUCT_LOOP_PROVIDER_ADAPTER_UNAVAILABLE"
    assert select_product_loop_provider("ANYTHING", _both()).error_code == "PRODUCT_LOOP_PROVIDER_INVALID"


def test_selection_is_visible_in_diagnostics() -> None:
    diagnostics = select_product_loop_provider("AUTO", _both()).diagnostics()
    assert diagnostics["preference"] == "AUTO"
    assert diagnostics["selected_worker_id"] == "codex-cli"
    assert diagnostics["provider_id"] == "PADOS_CODEX_CLI"


def test_router_ranking_is_unchanged_without_a_filter() -> None:
    from pados.worker.router import select_worker

    decision = select_worker(_both(), capability_id=CapabilityId.MODIFY_CODE, risk_level=RiskLevel.LOW)
    assert decision.selected_worker_id == "claude-code-runtime"

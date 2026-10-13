"""ResultReceipt provider identity comes from the verified ExecutionReceipt."""

from pados.product_loop.schemas import RuntimeResult, RuntimeStatus
from pados.product_loop.state import provider_identity


def _runtime(receipt: dict | None) -> RuntimeResult:
    return RuntimeResult(
        run_id="run-1", attempt=1, runtime_id="codex-cli-run-1-1", status=RuntimeStatus.COMPLETED,
        started_at="2026-09-30T00:00:00+00:00", completed_at="2026-09-30T00:00:01+00:00",
        summary="done", completion_declared=True, execution_receipt=receipt,
    )


def test_live_provider_receipt_names_the_provider() -> None:
    assert provider_identity(_runtime({"provider_id": "PADOS_CODEX_CLI", "evidence_kind": "LIVE_PROVIDER"})) == "PADOS_CODEX_CLI"


def test_attempt_without_receipt_is_never_attributed_to_a_maker() -> None:
    # Observed on the canonical Windows run: a blocked Codex attempt without a
    # receipt had been recorded as DIRECT_MAKER.
    assert provider_identity(_runtime(None)) == "UNVERIFIED_PROVIDER"
    assert provider_identity(_runtime({"provider_id": ""})) == "UNVERIFIED_PROVIDER"

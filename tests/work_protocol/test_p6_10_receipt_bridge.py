from __future__ import annotations

import pytest

from pados.execution.receipts import execution_receipt, verify_execution_receipt
from pados.loop.amendments import _p6_10_resource_kind, normalize_p6_10_capability_outcome
from pados.work_protocol.models import CoverageStatus, ResourceKind


def _binding() -> dict[str, str | None]:
    return {
        "task_id": "task-1",
        "goal_id": "goal-b2",
        "residual_id": "residual-1",
        "plan_id": "plan-1",
        "step_id": "step-1",
        "loop_id": "loop-1",
        "worker_id": "worker-a",
        "worker_type": "DETERMINISTIC_TOOL",
        "capability": "SOURCE_VERIFY",
        "provider_resource": "DETERMINISTIC_TOOL:worker-a",
        "project_ref": "project-1",
        "classification": "INTERNAL",
        "scope_version_ref": "scope:7",
        "authorization_ref": "approval-1",
        "execution_status": "SUCCEEDED",
        "started_at": "2026-10-06T10:00:00+00:00",
        "completed_at": "2026-10-06T10:00:01+00:00",
        "provenance_hash": "a" * 64,
        "protocol_version": "1.0",
        "schema_version": "pados.execution-receipt.v1",
    }


def test_p6_10_receipt_binds_product_and_runtime_identity_without_context_payload() -> None:
    contract = {"work_contract_id": "p6.10-step:step-1"}
    receipt = execution_receipt(
        run_id="p6.10-loop:loop-1",
        attempt=1,
        contract=contract,
        artifacts=(),
        provider_id="worker-a",
        evidence_kind="DETERMINISTIC_HOST",
        ok=True,
        execution_binding=_binding(),
    )

    assert receipt["semantic_authority"] is False
    assert receipt["execution_binding"]["task_id"] == "task-1"
    assert receipt["execution_binding"]["goal_id"] == "goal-b2"
    assert "prompt" not in receipt
    assert verify_execution_receipt(
        receipt,
        run_id="p6.10-loop:loop-1",
        attempt=1,
        contract=contract,
        artifacts=(),
        execution_binding=_binding(),
    )


def test_receipt_binding_tamper_and_unbounded_context_are_rejected() -> None:
    contract = {"work_contract_id": "p6.10-step:step-1"}
    receipt = execution_receipt(
        run_id="p6.10-loop:loop-1",
        attempt=1,
        contract=contract,
        artifacts=(),
        provider_id="worker-a",
        evidence_kind="DETERMINISTIC_HOST",
        ok=True,
        execution_binding=_binding(),
    )

    assert not verify_execution_receipt(
        receipt,
        run_id="p6.10-loop:loop-1",
        attempt=1,
        contract=contract,
        artifacts=(),
        execution_binding={**_binding(), "goal_id": "goal-other"},
    )
    with pytest.raises(ValueError, match="INVALID_EXECUTION_BINDING_METADATA"):
        execution_receipt(
            run_id="p6.10-loop:loop-1",
            attempt=1,
            contract=contract,
            artifacts=(),
            provider_id="worker-a",
            evidence_kind="DETERMINISTIC_HOST",
            ok=True,
            execution_binding={**_binding(), "prompt": "must never be persisted"},
        )


def test_native_job_success_requires_declared_typed_capability_outcome() -> None:
    contract = {"pados_capability_outcome": "pados.capability-outcome.v1"}

    assert normalize_p6_10_capability_outcome(
        {}, b'{"schema_version":"pados.capability-outcome.v1","outcome":"COMPLETE"}',
        execution_succeeded=True,
    ) is CoverageStatus.UNRESOLVED
    assert normalize_p6_10_capability_outcome(
        contract, b"", execution_succeeded=True
    ) is CoverageStatus.UNRESOLVED
    assert normalize_p6_10_capability_outcome(
        contract,
        b'{"schema_version":"pados.capability-outcome.v1","outcome":"COMPLETE"}',
        execution_succeeded=True,
    ) is CoverageStatus.COMPLETE
    assert normalize_p6_10_capability_outcome(
        contract,
        b'{"schema_version":"pados.capability-outcome.v1","outcome":"REJECTED"}',
        execution_succeeded=True,
    ) is CoverageStatus.UNRESOLVED
    assert normalize_p6_10_capability_outcome(
        {}, b"", execution_succeeded=False, execution_status="BLOCKED"
    ) is CoverageStatus.BLOCKED


def test_p6_10_worker_resource_kind_does_not_mislabel_deterministic_execution() -> None:
    assert _p6_10_resource_kind("DETERMINISTIC_TOOL") == (ResourceKind.TOOL, None)
    assert _p6_10_resource_kind("OLLAMA") == (ResourceKind.LOCAL_MODEL, "ollama")
    with pytest.raises(ValueError, match="P6_10_WORKER_TYPE_UNSUPPORTED"):
        _p6_10_resource_kind("UNKNOWN_WORKER")

from __future__ import annotations

import pytest

from pados.work_protocol.compatibility import ProtocolCompatibilityError, normalize_capability_result
from pados.work_protocol.models import (
    AuthorizationMetadata,
    CapabilityRequest,
    ExecutionReceiptRef,
    Provenance,
    ResourceKind,
)


def _inputs() -> tuple[CapabilityRequest, AuthorizationMetadata, Provenance, ExecutionReceiptRef]:
    authorization = AuthorizationMetadata(
        project_ref="project-1",
        classification="INTERNAL",
        scope_version_ref="scope:7",
        grant_ref="grant:1",
        policy_decision_ref="decision:1",
        capability="DOC_DRAFT",
    )
    request = CapabilityRequest(
        protocol_version="1.0",
        schema_version="pados.capability-request.v1",
        request_id="request-1",
        task_id="task-1",
        goal_id="goal-a",
        capability="DOC_DRAFT",
        constraints=(),
        authorization=authorization,
    )
    provenance = Provenance(
        worker_id="worker-a",
        resource_id="algorithm-draft",
        resource_kind=ResourceKind.DETERMINISTIC_ALGORITHM,
        adapter_id="adapter.doc-draft",
        adapter_version="1.0",
        normalized_from="deterministic-adapter",
        provider_response_digest="a" * 64,
        input_digests=("b" * 64,),
    )
    receipt = ExecutionReceiptRef(
        protocol_version="1.0",
        schema_version="pados.execution-receipt-ref.v1",
        receipt_id="receipt-1",
        digest="c" * 64,
    )
    return request, authorization, provenance, receipt


def test_adapter_normalization_uses_pados_authority_and_preserves_receipts() -> None:
    request, authorization, provenance, receipt = _inputs()
    result = normalize_capability_result(
        {
            "completed_goals": ["goal-a"],
            "contributions": [{"contribution_id": "section-a", "goal_id": "goal-a",
                               "content": "Draft prepared.", "conflict_key": None}],
            "artifacts": [],
            "evidence": [],
            "metrics": {"rows": 2},
            "confidence": None,
        },
        request=request,
        result_id="result-1",
        resource_id="algorithm-draft",
        resource_kind=ResourceKind.DETERMINISTIC_ALGORITHM,
        provider=None,
        provenance=provenance,
        authorization=authorization,
        execution_receipt_ref=receipt,
    )

    assert result.task_id == request.task_id
    assert result.authorization == authorization
    assert result.provenance == provenance
    assert result.execution_receipt_ref == receipt
    assert result.provider is None


def test_adapter_does_not_accept_provider_supplied_scope_or_classification() -> None:
    request, authorization, provenance, receipt = _inputs()
    with pytest.raises(ProtocolCompatibilityError, match="PROVIDER_RESPONSE_FIELD_UNSUPPORTED"):
        normalize_capability_result(
            {"completed_goals": ["goal-a"], "scope_version_ref": "scope:forged"},
            request=request,
            result_id="result-1",
            resource_id="algorithm-draft",
            resource_kind=ResourceKind.DETERMINISTIC_ALGORITHM,
            provider=None,
            provenance=provenance,
            authorization=authorization,
            execution_receipt_ref=receipt,
        )


def test_deterministic_algorithm_cannot_claim_an_llm_provider() -> None:
    request, authorization, provenance, receipt = _inputs()
    with pytest.raises(ProtocolCompatibilityError, match="PROVIDER_RESPONSE_INVALID"):
        normalize_capability_result(
            {"completed_goals": ["goal-a"]},
            request=request,
            result_id="result-1",
            resource_id="algorithm-draft",
            resource_kind=ResourceKind.DETERMINISTIC_ALGORITHM,
            provider="provider-a",
            provenance=provenance,
            authorization=authorization,
            execution_receipt_ref=receipt,
        )

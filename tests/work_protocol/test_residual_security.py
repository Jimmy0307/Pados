from __future__ import annotations

from tests.work_protocol.test_completion import _envelope, _result
from pados.work_protocol.coverage import build_coverage
from pados.work_protocol.models import (
    AuthorizationMetadata,
    CapabilityResult,
    PolicyRefusal,
    Provenance,
    ResourceCandidate,
    ResourceKind,
)
from pados.work_protocol.residual import create_residual_tasks, select_residual_candidates


def test_policy_refusal_never_automatically_routes_to_local_model() -> None:
    envelope = _envelope()
    refused = CapabilityResult(
        protocol_version="1.0",
        schema_version="pados.capability-result.v1",
        result_id="result-refused",
        task_id="task-1",
        goal_id="goal-b2",
        capability="OCR_TEXT",
        resource_id="cloud-model-a",
        resource_kind=ResourceKind.CLOUD_MODEL,
        provider="provider-a",
        completed_goals=(),
        partial_goals=(),
        unresolved_goals=("goal-b2",),
        blocked_goals=(),
        rejected_goals=(),
        contributions=(),
        artifacts=(),
        evidence=(),
        metrics={},
        confidence=None,
        failure_reason="POLICY_RESTRICTION",
        policy_refusal=PolicyRefusal(
            policy="PROVIDER_POLICY",
            code="CONTENT_REFUSED",
            automatic_bypass=False,
        ),
        execution_receipt_ref={
            "protocol_version": "1.0",
            "schema_version": "pados.execution-receipt-ref.v1",
            "receipt_id": "receipt-refused",
            "digest": "c" * 64,
        },
        authorization=AuthorizationMetadata(
            project_ref="project-1",
            classification="INTERNAL",
            scope_version_ref="scope:7",
            grant_ref="grant:1",
            policy_decision_ref="decision:refused",
            capability="OCR_TEXT",
        ),
        provenance=Provenance(
            worker_id="worker-cloud",
            resource_id="cloud-model-a",
            resource_kind=ResourceKind.CLOUD_MODEL,
            adapter_id="adapter.ocr",
            adapter_version="1.0",
            normalized_from="provider-adapter",
            provider_response_digest="d" * 64,
            input_digests=(),
        ),
    )
    coverage = build_coverage(envelope, (refused,))
    residuals = create_residual_tasks(envelope, coverage, (refused,))
    residual = next(item for item in residuals if item.goal_ids == ("goal-b2",))
    candidates = select_residual_candidates(
        residual,
        (
            ResourceCandidate(resource_id="local-llm", worker_id="worker-local",
                              capabilities=("OCR_TEXT",), resource_kind=ResourceKind.LOCAL_MODEL),
            ResourceCandidate(resource_id="ocr-pack", worker_id="worker-ocr",
                              capabilities=("OCR_TEXT",), resource_kind=ResourceKind.OCR_ENGINE),
        ),
        authorize=lambda _candidate, _capability: True,
    )

    assert residual.automatic_routing_allowed is False
    assert candidates == ()


def test_normal_residual_candidates_still_require_policy_authorization() -> None:
    envelope = _envelope()
    partial = _result("result-b", "goal-b", "SOURCE_VERIFY", "PARTIAL", "Some sources verified")
    coverage = build_coverage(envelope, (partial,))
    residuals = create_residual_tasks(envelope, coverage, (partial,))
    residual = next(item for item in residuals if item.goal_ids == ("goal-b2",))
    allowed = select_residual_candidates(
        residual,
        (
            ResourceCandidate(resource_id="candidate-a", worker_id="worker-a",
                              capabilities=("SOURCE_VERIFY",), resource_kind=ResourceKind.LOCAL_MODEL),
            ResourceCandidate(resource_id="candidate-b", worker_id="worker-b",
                              capabilities=("OCR_TEXT",), resource_kind=ResourceKind.OCR_ENGINE),
        ),
        authorize=lambda candidate, _capability: candidate.resource_id == "candidate-b",
    )

    assert [(item.goal_id, item.resource_id, item.capability) for item in allowed] == [
        ("goal-b2", "candidate-b", "OCR_TEXT")
    ]

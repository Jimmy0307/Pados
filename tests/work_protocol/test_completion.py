from __future__ import annotations

from pados.work_protocol.composition import compose_results
from pados.work_protocol.coverage import build_coverage
from pados.work_protocol.graph import project_work_graph
from pados.work_protocol.models import (
    ArtifactReference,
    AuthorizationMetadata,
    CapabilityResult,
    Goal,
    GoalRelation,
    GoalRelationType,
    Provenance,
    ResourceKind,
    TaskEnvelope,
)
from pados.work_protocol.residual import create_residual_tasks


def _goal(goal_id: str, title: str, required: str) -> Goal:
    return Goal(
        protocol_version="1.0",
        schema_version="pados.goal.v1",
        goal_id=goal_id,
        title=title,
        objective=title,
        required_capabilities=(required,),
    )


def _envelope() -> TaskEnvelope:
    return TaskEnvelope(
        protocol_version="1.0",
        schema_version="pados.task-envelope.v1",
        task_id="task-1",
        original_intent="Prepare a project brief from the available materials.",
        normalized_goals=(
            _goal("goal-a", "Draft the brief", "DOC_DRAFT"),
            _goal("goal-b", "Verify citations", "SOURCE_VERIFY"),
            _goal("goal-b2", "Resolve two missing citations", "OCR_TEXT"),
        ),
        goal_relations=(GoalRelation(
            protocol_version="1.0",
            schema_version="pados.goal-relation.v1",
            source_goal_id="goal-b",
            target_goal_id="goal-b2",
            relation=GoalRelationType.PARENT_OF,
        ),),
        constraints=("Keep source references.",),
        project_ref="project-1",
        classification="INTERNAL",
        priority="NORMAL",
        deadline=None,
        scope_version_ref="scope:7",
        created_by="human:jimmy",
        created_at="2026-10-05T08:00:00+00:00",
    )


def _result(
    result_id: str,
    goal_id: str,
    capability: str,
    outcome: str,
    text: str,
    *,
    resource_kind: ResourceKind = ResourceKind.DETERMINISTIC_ALGORITHM,
    provider: str | None = None,
) -> CapabilityResult:
    return CapabilityResult(
        protocol_version="1.0",
        schema_version="pados.capability-result.v1",
        result_id=result_id,
        task_id="task-1",
        goal_id=goal_id,
        capability=capability,
        resource_id=f"resource-{result_id}",
        resource_kind=resource_kind,
        provider=provider,
        completed_goals=(goal_id,) if outcome == "COMPLETE" else (),
        partial_goals=(goal_id,) if outcome == "PARTIAL" else (),
        unresolved_goals=(goal_id,) if outcome == "UNRESOLVED" else (),
        blocked_goals=(),
        rejected_goals=(),
        contributions=({"contribution_id": f"section-{result_id}", "goal_id": goal_id,
                       "content": text, "conflict_key": None},),
        artifacts=(),
        evidence=(),
        metrics={},
        confidence=None,
        failure_reason=None,
        policy_refusal=None,
        execution_receipt_ref={
            "protocol_version": "1.0",
            "schema_version": "pados.execution-receipt-ref.v1",
            "receipt_id": f"receipt-{result_id}",
            "digest": "a" * 64,
        },
        authorization=AuthorizationMetadata(
            project_ref="project-1",
            classification="INTERNAL",
            scope_version_ref="scope:7",
            grant_ref="grant:1",
            policy_decision_ref="decision:1",
            capability=capability,
        ),
        provenance=Provenance(
            worker_id=f"worker-{result_id}",
            resource_id=f"resource-{result_id}",
            resource_kind=resource_kind,
            adapter_id=f"adapter.{capability.lower()}",
            adapter_version="1.0",
            normalized_from="deterministic-test-adapter",
            provider_response_digest="b" * 64,
            input_digests=(),
        ),
    )


def test_partial_result_creates_residual_only_for_unresolved_goal_and_composes_lineage() -> None:
    envelope = _envelope()
    result_a = _result("result-a", "goal-a", "DOC_DRAFT", "COMPLETE", "Draft brief").model_copy(
        update={"artifacts": (ArtifactReference(artifact_id="artifact-brief", digest="e" * 64,
                                                 project_ref="project-1", classification="INTERNAL"),)}
    )
    result_b = _result("result-b", "goal-b", "SOURCE_VERIFY", "PARTIAL", "Verified citation 1")
    result_b2 = _result("result-b2", "goal-b2", "OCR_TEXT", "COMPLETE", "Resolved citations 2 and 3")

    initial_coverage = build_coverage(envelope, (result_a, result_b))
    residuals = create_residual_tasks(envelope, initial_coverage, (result_a, result_b))

    assert len(residuals) == 1
    assert residuals[0].goal_ids == ("goal-b2",)
    assert residuals[0].preserved_result_refs == ("result-a", "result-b")
    assert residuals[0].automatic_routing_allowed is True

    final_coverage = build_coverage(envelope, (result_a, result_b, result_b2))
    candidate = compose_results(envelope, (result_a, result_b, result_b2), final_coverage)

    assert [item.goal_id for item in candidate.sections] == ["goal-a", "goal-b", "goal-b2"]
    assert [item.result_ref for item in candidate.sections] == ["result-a", "result-b", "result-b2"]
    assert candidate.verification_status == "NOT_VERIFIED"
    assert candidate.semantic_acceptance == "PENDING_GOVERNED_ACCEPTANCE"
    assert final_coverage.by_goal("goal-a").status.value == "COMPLETE"
    assert final_coverage.by_goal("goal-b").status.value == "PARTIAL"
    assert final_coverage.by_goal("goal-b2").status.value == "COMPLETE"

    graph = project_work_graph(envelope, (result_a, result_b, result_b2), candidate)
    assert graph.authority == "PROJECTION"
    assert graph.node("task-1").kind.value == "TASK"
    assert graph.node("goal-b2").status == "COMPLETE"
    assert any(edge.kind.value == "PRODUCED" for edge in graph.edges)


def test_partial_parent_is_not_rerun_after_its_residual_child_completes() -> None:
    envelope = _envelope()
    results = (
        _result("result-a", "goal-a", "DOC_DRAFT", "COMPLETE", "Draft brief"),
        _result("result-b", "goal-b", "SOURCE_VERIFY", "PARTIAL", "Parent partially complete"),
        _result("result-b2", "goal-b2", "OCR_TEXT", "COMPLETE", "Residual child complete"),
    )
    coverage = build_coverage(envelope, results)

    residuals = create_residual_tasks(envelope, coverage, results)

    assert coverage.by_goal("goal-b").status.value == "PARTIAL"
    assert coverage.by_goal("goal-b2").status.value == "COMPLETE"
    assert residuals == ()


def test_conflicting_contributions_are_reported_without_overwrite() -> None:
    envelope = _envelope()
    first = _result("result-a", "goal-a", "DOC_DRAFT", "COMPLETE", "Version A")
    first = type(first).model_validate({**first.model_dump(mode="python"), "contributions": ({
        "contribution_id": "section-a",
        "goal_id": "goal-a",
        "content": "Version A",
        "conflict_key": "release-summary",
    },)})
    second = type(first).model_validate({**first.model_dump(mode="python"),
        "result_id": "result-a2", "execution_receipt_ref": {
            "protocol_version": "1.0", "schema_version": "pados.execution-receipt-ref.v1",
            "receipt_id": "receipt-a2", "digest": "e" * 64,
        },
        "provenance": {**first.provenance.model_dump(mode="python"), "worker_id": "worker-a2",
                       "resource_id": "resource-result-a2"},
        "resource_id": "resource-result-a2", "contributions": ({
            "contribution_id": "section-a2",
            "goal_id": "goal-a",
            "content": "Version B",
            "conflict_key": "release-summary",
        },)})
    coverage = build_coverage(envelope, (first, second))

    candidate = compose_results(envelope, (first, second), coverage)

    assert len(candidate.sections) == 2
    assert len(candidate.conflicts) == 1
    assert candidate.conflicts[0].section_refs == ("section-a", "section-a2")
    assert candidate.verification_status == "NOT_VERIFIED"

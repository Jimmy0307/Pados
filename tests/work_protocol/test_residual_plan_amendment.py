from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from pados.loop.amendments import (
    AmendmentAuthority,
    ApprovedExecutionEnvelope,
    ResidualExecutionRequest,
    RuntimeGoalBinding,
    _assert_immutable_amendment_content,
    classify_residual_authority,
    residual_amendment_idempotency_key,
    resolve_runtime_dependencies,
)
from pados.loop.scope_authority import read_current_scope_version
from pados.work_protocol.models import (
    AmendmentAuthorityDecision,
    Provenance,
    ResidualPlanAmendment,
    ResourceKind,
    TaskPriority,
)


@pytest.fixture
def envelope() -> ApprovedExecutionEnvelope:
    return ApprovedExecutionEnvelope(
        task_id="task-1",
        project_ref="project-1",
        classification="INTERNAL",
        scope_version_ref="scope:7",
        goal_ids=frozenset({"goal-b", "goal-b2"}),
        allowed_capabilities=frozenset({"SOURCE_VERIFY"}),
        allowed_paths=frozenset({"docs/"}),
        allowed_actions=frozenset({"READ_REPOSITORY"}),
        worker_types=frozenset({"DETERMINISTIC_TOOL"}),
        network_allowed=False,
        approval_valid=True,
        current_scope_version_ref="scope:7",
    )


@pytest.fixture
def residual_request() -> ResidualExecutionRequest:
    return ResidualExecutionRequest(
        task_id="task-1",
        residual_id="residual:task-1:goal-b2:cov1",
        goal_id="goal-b2",
        parent_goal_id="goal-b",
        project_ref="project-1",
        classification="INTERNAL",
        scope_version_ref="scope:7",
        required_capability="SOURCE_VERIFY",
        dependencies=("goal-b",),
        allowed_paths=("docs/",),
        allowed_actions=("READ_REPOSITORY",),
        worker_type="DETERMINISTIC_TOOL",
        network_required=False,
        fresh_approval_required=False,
        authorization_valid=True,
    )


def test_residual_inside_approved_envelope_is_auto_amendable(
    envelope: ApprovedExecutionEnvelope,
    residual_request: ResidualExecutionRequest,
) -> None:
    assert classify_residual_authority(envelope, residual_request) is AmendmentAuthority.AUTO_AMENDABLE


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("project_ref", "project-2"),
        ("classification", "RESTRICTED"),
        ("scope_version_ref", "scope:8"),
        ("required_capability", "PRIVILEGED_EXPORT"),
        ("allowed_paths", ("secrets/",)),
        ("allowed_actions", ("WRITE_REPOSITORY",)),
        ("worker_type", "CLOUD_MODEL"),
        ("network_required", True),
        ("fresh_approval_required", True),
    ],
)
def test_authority_expansion_requires_reapproval(
    envelope: ApprovedExecutionEnvelope,
    residual_request: ResidualExecutionRequest,
    field: str,
    value: object,
) -> None:
    expanded = replace(residual_request, **{field: value})
    assert classify_residual_authority(envelope, expanded) is AmendmentAuthority.REAPPROVAL_REQUIRED


def test_stale_current_scope_and_invalid_authorization_fail_closed(
    envelope: ApprovedExecutionEnvelope,
    residual_request: ResidualExecutionRequest,
) -> None:
    stale = replace(envelope, current_scope_version_ref="scope:8")
    denied = replace(residual_request, authorization_valid=False)

    assert classify_residual_authority(stale, residual_request) is AmendmentAuthority.REAPPROVAL_REQUIRED
    assert classify_residual_authority(envelope, denied) is AmendmentAuthority.REAPPROVAL_REQUIRED


def test_goal_outside_original_envelope_requires_reapproval(
    envelope: ApprovedExecutionEnvelope,
    residual_request: ResidualExecutionRequest,
) -> None:
    out_of_scope = replace(residual_request, goal_id="goal-new")
    assert classify_residual_authority(envelope, out_of_scope) is AmendmentAuthority.REAPPROVAL_REQUIRED


def test_dependency_translation_uses_same_plan_runtime_ids_and_keeps_passed_dependency() -> None:
    binding = RuntimeGoalBinding(
        task_id="task-1",
        goal_id="goal-b",
        plan_id="plan-1",
        loop_id="loop-1",
        step_id="step-1",
        sequence=2,
        status="PASSED",
    )

    assert resolve_runtime_dependencies(
        ("goal-b",), {"goal-b": binding}, plan_id="plan-1", loop_id="loop-1", new_sequence=3
    ) == (2,)


def test_dependency_translation_fails_closed_for_missing_cross_plan_and_cycle() -> None:
    binding = RuntimeGoalBinding(
        task_id="task-1",
        goal_id="goal-b",
        plan_id="plan-other",
        loop_id="loop-1",
        step_id="step-1",
        sequence=3,
        status="PENDING",
    )

    with pytest.raises(ValueError, match="RUNTIME_BINDING_MISSING"):
        resolve_runtime_dependencies(("goal-missing",), {}, plan_id="plan-1", loop_id="loop-1", new_sequence=4)
    with pytest.raises(ValueError, match="RUNTIME_BINDING_PLAN_MISMATCH"):
        resolve_runtime_dependencies(("goal-b",), {"goal-b": binding}, plan_id="plan-1", loop_id="loop-1", new_sequence=4)
    with pytest.raises(ValueError, match="RUNTIME_DEPENDENCY_CYCLE"):
        resolve_runtime_dependencies(("goal-b",), {"goal-b": binding}, plan_id="plan-other", loop_id="loop-1", new_sequence=3)


def test_amendment_retry_has_stable_identity() -> None:
    first = residual_amendment_idempotency_key("task-1", "residual-1", "plan-1")
    retry = residual_amendment_idempotency_key("task-1", "residual-1", "plan-1")
    other = residual_amendment_idempotency_key("task-1", "residual-2", "plan-1")

    assert first == retry
    assert first != other


def test_amendment_retry_rejects_changed_content_under_same_identity() -> None:
    _assert_immutable_amendment_content("a" * 64, "a" * 64)
    with pytest.raises(ValueError, match="RESIDUAL_AMENDMENT_IDEMPOTENCY_CONTENT_CONFLICT"):
        _assert_immutable_amendment_content("a" * 64, "b" * 64)


def test_scope_version_reader_fails_closed_without_a_separate_proof_database() -> None:
    settings = SimpleNamespace(
        product_database_name="pados-runtime",
        operational_database_name="pados-runtime",
        db_host="127.0.0.1",
    )

    assert read_current_scope_version(settings, "project-1") is None


def test_residual_plan_amendment_is_hash_bound_and_carries_only_authority_references() -> None:
    amendment = ResidualPlanAmendment(
        protocol_version="1.0",
        schema_version=ResidualPlanAmendment.SCHEMA,
        amendment_id="amendment:one",
        task_id="task-1",
        goal_id="goal-b2",
        residual_id="residual-1",
        parent_goal_id="goal-b",
        parent_plan_ref="plan-1",
        parent_step_ref="step-1",
        project_ref="project-1",
        classification="INTERNAL",
        priority=TaskPriority.NORMAL,
        deadline=None,
        dependencies=("goal-b",),
        required_capability="SOURCE_VERIFY",
        network_requirement="NONE",
        resource_constraints={"worker_type": "DETERMINISTIC_TOOL"},
        authorization_refs=("approval-1",),
        expected_scope_version_ref="scope:7",
        provenance=Provenance(
            worker_id="worker-a",
            resource_id="worker-a",
            resource_kind=ResourceKind.TOOL,
            adapter_id="p6.10.taskloop",
            adapter_version="1.0",
            normalized_from="capability-result",
            provider_response_digest="a" * 64,
        ),
        source_result_hash="b" * 64,
        coverage_map_hash="c" * 64,
        idempotency_key="amendment-key-1",
        authority_decision=AmendmentAuthorityDecision.AUTO_AMENDABLE,
    )

    assert amendment.content_hash
    assert "original_intent" not in amendment.model_dump(mode="json")

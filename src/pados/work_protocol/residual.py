# SPDX-License-Identifier: AGPL-3.0-only
"""Residual-only task formation and policy-gated candidate filtering."""
from __future__ import annotations

from collections.abc import Callable, Sequence

from pados.work_protocol.coverage import assert_coverage_matches_results
from pados.work_protocol.models import (
    CapabilityResult,
    CoverageMap,
    CoverageStatus,
    ResidualAssignmentCandidate,
    ResidualGoal,
    ResidualTask,
    ResourceCandidate,
    TaskEnvelope,
    UnresolvedReason,
)

_NON_RESIDUAL = {CoverageStatus.COMPLETE, CoverageStatus.REJECTED}
_NO_AUTOMATIC_ROUTE = {
    UnresolvedReason.POLICY_RESTRICTION,
    UnresolvedReason.HUMAN_DECISION_REQUIRED,
    UnresolvedReason.DEPENDENCY_BLOCKED,
}


def create_residual_tasks(
    task: TaskEnvelope,
    coverage: CoverageMap,
    results: tuple[CapabilityResult, ...],
) -> tuple[ResidualTask, ...]:
    if coverage.task_id != task.task_id or coverage.task_envelope_hash != task.content_hash:
        raise ValueError("COVERAGE_TASK_BINDING_MISMATCH")
    assert_coverage_matches_results(
        task, coverage, results, error_code="COVERAGE_RESULTS_MISMATCH"
    )
    source_ids = {result.result_id for result in results}
    if any(ref not in source_ids for entry in coverage.entries for ref in entry.result_refs):
        raise ValueError("COVERAGE_RESULT_REFERENCE_MISSING")
    remaining_candidates = tuple(
        ResidualGoal(
            goal_id=entry.goal_id,
            status=entry.status,
            required_capabilities=next(
                goal.required_capabilities for goal in task.normalized_goals if goal.goal_id == entry.goal_id
            ),
            unresolved_reasons=entry.unresolved_reasons,
            dependencies=entry.dependencies,
        )
        for entry in coverage.entries
        if entry.status not in _NON_RESIDUAL
    )
    remaining_goal_ids = {goal.goal_id for goal in remaining_candidates}
    parent_ids = {
        relation.source_goal_id
        for relation in task.goal_relations
        if relation.relation.value == "PARENT_OF" and relation.target_goal_id in remaining_goal_ids
    }
    children_by_parent: dict[str, set[str]] = {}
    for relation in task.goal_relations:
        if relation.relation.value == "PARENT_OF":
            children_by_parent.setdefault(relation.source_goal_id, set()).add(relation.target_goal_id)
    complete_goal_ids = {
        entry.goal_id for entry in coverage.entries if entry.status is CoverageStatus.COMPLETE
    }
    parents_satisfied_by_children = {
        parent_id
        for parent_id, child_ids in children_by_parent.items()
        if child_ids and child_ids.issubset(complete_goal_ids)
    }
    remaining = tuple(
        goal
        for goal in remaining_candidates
        if goal.goal_id not in parent_ids and goal.goal_id not in parents_satisfied_by_children
    )
    if not remaining:
        return ()
    result_by_id = {result.result_id: result for result in results}
    output: list[ResidualTask] = []
    for goal in remaining:
        reasons = set(goal.unresolved_reasons)
        refusal_for_goal = any(
            result_by_id[result_ref].policy_refusal is not None
            for entry in coverage.entries
            if entry.goal_id == goal.goal_id
            for result_ref in entry.result_refs
        )
        routable = not refusal_for_goal and not bool(reasons & _NO_AUTOMATIC_ROUTE)
        key = f"residual:{task.task_id}:{goal.goal_id}:{coverage.content_hash[:12]}"
        output.append(
            ResidualTask(
                protocol_version="1.0",
                schema_version=ResidualTask.SCHEMA,
                residual_task_id=key,
                task_id=task.task_id,
                goals=(goal,),
                preserved_result_refs=tuple(
                    dict.fromkeys(ref for entry in coverage.entries for ref in entry.result_refs)
                ),
                preserved_artifacts=tuple(
                    dict.fromkeys(ref for entry in coverage.entries for ref in entry.artifact_refs)
                ),
                preserved_evidence=tuple(
                    dict.fromkeys(ref for entry in coverage.entries for ref in entry.evidence_refs)
                ),
                automatic_routing_allowed=routable,
                source_coverage_hash=coverage.content_hash,
            )
        )
    return tuple(output)


def select_residual_candidates(
    residual: ResidualTask,
    candidates: Sequence[ResourceCandidate],
    *,
    authorize: Callable[[ResourceCandidate, str], bool],
) -> tuple[ResidualAssignmentCandidate, ...]:
    """Return only normally authorized candidates; never bypass on a refusal."""
    if not residual.automatic_routing_allowed:
        return ()
    if not residual.goals:
        return ()
    assignments: list[ResidualAssignmentCandidate] = []
    for goal in residual.goals:
        for capability in goal.required_capabilities:
            matching = sorted(
                (candidate for candidate in candidates if capability in candidate.capabilities),
                key=lambda candidate: candidate.resource_id,
            )
            for candidate in matching:
                if authorize(candidate, capability):
                    assignments.append(
                        ResidualAssignmentCandidate(
                            goal_id=goal.goal_id,
                            capability=capability,
                            resource_id=candidate.resource_id,
                            worker_id=candidate.worker_id,
                        )
                    )
                    break
    return tuple(assignments)

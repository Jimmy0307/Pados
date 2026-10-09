# SPDX-License-Identifier: AGPL-3.0-only
"""Discrete task coverage derived from goals and normalized capability results."""
from __future__ import annotations

from pados.work_protocol.models import (
    CapabilityResult,
    CoverageEntry,
    CoverageMap,
    CoverageStatus,
    TaskEnvelope,
    UnresolvedReason,
)


def build_coverage(task: TaskEnvelope, results: tuple[CapabilityResult, ...]) -> CoverageMap:
    goal_ids = {goal.goal_id for goal in task.normalized_goals}
    result_ids = [result.result_id for result in results]
    if len(result_ids) != len(set(result_ids)):
        raise ValueError("CAPABILITY_RESULT_ID_DUPLICATE")
    by_goal: dict[str, list[tuple[CoverageStatus, CapabilityResult]]] = {
        goal_id: [] for goal_id in goal_ids
    }
    for result in results:
        if result.task_id != task.task_id:
            raise ValueError("CAPABILITY_RESULT_TASK_MISMATCH")
        if result.authorization.project_ref != task.project_ref:
            raise ValueError("CAPABILITY_RESULT_PROJECT_MISMATCH")
        if result.authorization.classification.rank > task.classification.rank:
            raise ValueError("CAPABILITY_RESULT_CLASSIFICATION_EXCEEDS_TASK")
        if result.authorization.scope_version_ref != task.scope_version_ref:
            raise ValueError("CAPABILITY_RESULT_SCOPE_VERSION_MISMATCH")
        if result.authorization.capability != result.capability:
            raise ValueError("CAPABILITY_RESULT_AUTHORIZATION_MISMATCH")
        for artifact in result.artifacts:
            if artifact.project_ref != task.project_ref:
                raise ValueError("CAPABILITY_RESULT_ARTIFACT_PROJECT_MISMATCH")
            if not task.classification.permits(artifact.classification):
                raise ValueError("CAPABILITY_RESULT_ARTIFACT_CLASSIFICATION_EXCEEDS_TASK")
        for status, ids in (
            (CoverageStatus.COMPLETE, result.completed_goals),
            (CoverageStatus.PARTIAL, result.partial_goals),
            (CoverageStatus.UNRESOLVED, result.unresolved_goals),
            (CoverageStatus.BLOCKED, result.blocked_goals),
            (CoverageStatus.REJECTED, result.rejected_goals),
        ):
            for goal_id in ids:
                if goal_id not in goal_ids:
                    raise ValueError("CAPABILITY_RESULT_GOAL_UNKNOWN")
                by_goal[goal_id].append((status, result))

    entries: list[CoverageEntry] = []
    for goal in task.normalized_goals:
        contributions = by_goal[goal.goal_id]
        statuses = {status for status, _ in contributions}
        if goal.state is not None and goal.state.value == "REJECTED":
            status = CoverageStatus.REJECTED
        elif CoverageStatus.REJECTED in statuses:
            status = CoverageStatus.REJECTED
        elif CoverageStatus.BLOCKED in statuses:
            status = CoverageStatus.BLOCKED
        elif CoverageStatus.PARTIAL in statuses or len(statuses) > 1:
            status = CoverageStatus.PARTIAL
        elif CoverageStatus.COMPLETE in statuses:
            status = CoverageStatus.COMPLETE
        elif CoverageStatus.UNRESOLVED in statuses:
            status = CoverageStatus.UNRESOLVED
        else:
            dependency_entries = [
                next((entry for entry in entries if entry.goal_id == dependency), None)
                for dependency in goal.dependencies
            ]
            if any(entry is None or entry.status is not CoverageStatus.COMPLETE for entry in dependency_entries):
                status = CoverageStatus.BLOCKED
            else:
                status = CoverageStatus.UNRESOLVED
        result_refs = tuple(dict.fromkeys(result.result_id for _, result in contributions))
        artifact_refs = tuple(
            dict.fromkeys(
                artifact.artifact_id
                for _, result in contributions
                for artifact in result.artifacts
            )
        )
        evidence_refs = tuple(
            dict.fromkeys(evidence.evidence_id for _, result in contributions for evidence in result.evidence)
        )
        reasons = tuple(
            dict.fromkeys(
                result.failure_reason
                for _, result in contributions
                if result.failure_reason is not None
            )
        )
        if status is CoverageStatus.BLOCKED and not reasons:
            reasons = (UnresolvedReason.DEPENDENCY_BLOCKED,)
        entries.append(
            CoverageEntry(
                goal_id=goal.goal_id,
                status=status,
                result_refs=result_refs,
                artifact_refs=artifact_refs,
                evidence_refs=evidence_refs,
                unresolved_reasons=reasons,
                dependencies=goal.dependencies,
                next_candidate_action=_next_action(status, reasons),
            )
        )

    return CoverageMap(
        protocol_version="1.0",
        schema_version=CoverageMap.SCHEMA,
        task_id=task.task_id,
        task_envelope_hash=task.content_hash,
        entries=tuple(entries),
        source_result_hashes=tuple(result.content_hash for result in results),
    )


def assert_coverage_matches_results(
    task: TaskEnvelope,
    coverage: CoverageMap,
    results: tuple[CapabilityResult, ...],
    *,
    error_code: str,
) -> None:
    """Fail closed unless coverage is the deterministic projection of these exact inputs."""
    if coverage.content_hash != build_coverage(task, results).content_hash:
        raise ValueError(error_code)


def _next_action(
    status: CoverageStatus, reasons: tuple[UnresolvedReason, ...]
) -> str | None:
    if status is CoverageStatus.COMPLETE or status is CoverageStatus.REJECTED:
        return None
    if UnresolvedReason.POLICY_RESTRICTION in reasons:
        return "HUMAN_REVIEW_REQUIRED"
    if UnresolvedReason.HUMAN_DECISION_REQUIRED in reasons:
        return "REQUEST_HUMAN_DECISION"
    if status is CoverageStatus.BLOCKED:
        return "WAIT_FOR_DEPENDENCIES"
    return "PLAN_RESIDUAL_CAPABILITY"

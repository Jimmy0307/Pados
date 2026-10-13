# SPDX-License-Identifier: AGPL-3.0-only
"""Explainable scheduling candidates delegated to the existing PADOS scheduler."""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Protocol

from pados.enterprise.classification import DataClassification
from pados.execution.context import canonical_hash
from pados.work_protocol.models import (
    CoverageMap,
    CoverageStatus,
    ScheduleReason,
    SchedulingCandidate,
    TaskEnvelope,
)


class ExistingScheduler(Protocol):
    """The existing loop scheduler adapter; this module does not own a queue."""

    def offer(self, candidate: SchedulingCandidate) -> str: ...


def plan_scheduling_candidates(
    task: TaskEnvelope,
    coverage: CoverageMap,
    *,
    human_approval_goals: frozenset[str] = frozenset(),
    network_requirements: Mapping[str, str] | None = None,
    resource_requirements: Mapping[str, tuple[str, ...]] | None = None,
    estimates: Mapping[str, tuple[int | None, int | None]] | None = None,
    available_resource_goals: frozenset[str] = frozenset(),
) -> tuple[SchedulingCandidate, ...]:
    if coverage.task_id != task.task_id or coverage.task_envelope_hash != task.content_hash:
        raise ValueError("SCHEDULING_TASK_BINDING_MISMATCH")
    network = network_requirements or {}
    resource_needs = resource_requirements or {}
    estimates_by_goal = estimates or {}
    by_goal = {entry.goal_id: entry for entry in coverage.entries}
    unresolved_ids = {
        entry.goal_id
        for entry in coverage.entries
        if entry.status not in {CoverageStatus.COMPLETE, CoverageStatus.REJECTED}
    }
    parent_ids = {
        relation.source_goal_id
        for relation in task.goal_relations
        if relation.relation.value == "PARENT_OF" and relation.target_goal_id in unresolved_ids
    }
    candidates: list[SchedulingCandidate] = []
    for goal in task.normalized_goals:
        entry = by_goal[goal.goal_id]
        if entry.status in {CoverageStatus.COMPLETE, CoverageStatus.REJECTED} or goal.goal_id in parent_ids:
            continue
        dependency_ready = all(
            by_goal[dependency].status is CoverageStatus.COMPLETE for dependency in goal.dependencies
        )
        if goal.goal_id in human_approval_goals:
            reason = ScheduleReason.HUMAN_APPROVAL_REQUIRED
            explanation = "A governed human decision is required before this goal can run."
        elif not dependency_ready:
            reason = ScheduleReason.WAITING_FOR_ARTIFACT
            explanation = "A declared prerequisite has not completed."
        elif task.classification.rank >= DataClassification.CONFIDENTIAL.rank:
            reason = ScheduleReason.LOCAL_ONLY_CLASSIFICATION
            explanation = "The task classification requires the local policy lane."
        elif goal.goal_id in available_resource_goals:
            reason = ScheduleReason.REQUIRED_RESOURCE_ONLINE
            explanation = "A required resource is reported available; policy is still checked at dispatch."
        else:
            reason = ScheduleReason.DEPENDENCY_READY
            explanation = "Dependencies are complete and the goal is ready for the existing scheduler."
        latency, cost = estimates_by_goal.get(goal.goal_id, (None, None))
        key = canonical_hash(
            {
                "task": task.task_id,
                "goal": goal.goal_id,
                "coverage": coverage.content_hash,
                "capabilities": goal.required_capabilities,
                "reason": reason.value,
            }
        )
        candidates.append(
            SchedulingCandidate(
                protocol_version="1.0",
                schema_version=SchedulingCandidate.SCHEMA,
                candidate_id=f"schedule:{key[:24]}",
                task_id=task.task_id,
                goal_id=goal.goal_id,
                dependencies=goal.dependencies,
                priority=task.priority,
                deadline=task.deadline,
                required_capabilities=goal.required_capabilities,
                resource_requirements=resource_needs.get(goal.goal_id, ()),
                network_requirement=network.get(goal.goal_id, "LOCAL_ONLY"),
                classification=task.classification,
                estimated_latency_ms=latency,
                estimated_cost_microunits=cost,
                human_approval_required=goal.goal_id in human_approval_goals,
                reason=reason,
                explanation=explanation,
            )
        )
    return tuple(candidates)


def submit_to_existing_scheduler(
    scheduler: ExistingScheduler, candidates: Sequence[SchedulingCandidate]
) -> tuple[str, ...]:
    """Hand candidates to the single existing queue/order authority."""
    submitted: list[str] = []
    seen: set[str] = set()
    for candidate in candidates:
        if candidate.candidate_id in seen:
            continue
        seen.add(candidate.candidate_id)
        submitted.append(scheduler.offer(candidate))
    return tuple(submitted)

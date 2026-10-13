# SPDX-License-Identifier: AGPL-3.0-only
"""Provenance-preserving result composition; never performs acceptance or CAS."""
from __future__ import annotations

from collections import defaultdict

from pados.execution.context import canonical_hash
from pados.work_protocol.coverage import assert_coverage_matches_results
from pados.work_protocol.models import (
    CapabilityConflict,
    CapabilityResult,
    CompositionSection,
    CoverageMap,
    ResultCandidate,
    TaskEnvelope,
    VerificationStatus,
)


def compose_results(
    task: TaskEnvelope,
    results: tuple[CapabilityResult, ...],
    coverage: CoverageMap,
) -> ResultCandidate:
    if coverage.task_id != task.task_id or coverage.task_envelope_hash != task.content_hash:
        raise ValueError("COMPOSITION_TASK_BINDING_MISMATCH")
    if any(result.task_id != task.task_id for result in results):
        raise ValueError("COMPOSITION_RESULT_TASK_MISMATCH")
    assert_coverage_matches_results(
        task, coverage, results, error_code="COMPOSITION_COVERAGE_RESULTS_MISMATCH"
    )
    result_ids = [result.result_id for result in results]
    if len(result_ids) != len(set(result_ids)):
        raise ValueError("COMPOSITION_RESULT_ID_DUPLICATE")
    known_goals = {goal.goal_id for goal in task.normalized_goals}
    sections: list[CompositionSection] = []
    section_ids: set[str] = set()
    for result in results:
        for contribution in result.contributions:
            if contribution.goal_id not in known_goals:
                raise ValueError("COMPOSITION_GOAL_UNKNOWN")
            if contribution.contribution_id in section_ids:
                raise ValueError("COMPOSITION_SECTION_ID_DUPLICATE")
            section_ids.add(contribution.contribution_id)
            sections.append(
                CompositionSection(
                    section_id=contribution.contribution_id,
                    goal_id=contribution.goal_id,
                    result_ref=result.result_id,
                    worker_id=result.provenance.worker_id,
                    capability=result.capability,
                    resource_id=result.resource_id,
                    content=contribution.content,
                    artifact_refs=tuple(item.artifact_id for item in result.artifacts),
                    evidence_refs=tuple(item.evidence_id for item in result.evidence),
                    verification_status=VerificationStatus.NOT_VERIFIED,
                    conflict_status="CLEAR",
                )
            )

    grouped: dict[tuple[str, str], list[tuple[int, str]]] = defaultdict(list)
    section_by_id = {section.section_id: section for section in sections}
    for result in results:
        for contribution in result.contributions:
            if contribution.conflict_key is not None:
                grouped[(contribution.goal_id, contribution.conflict_key)].append(
                    (len(grouped[(contribution.goal_id, contribution.conflict_key)]), contribution.contribution_id)
                )

    conflicts: list[CapabilityConflict] = []
    conflicted_sections: set[str] = set()
    for (goal_id, key), indexed in sorted(grouped.items()):
        refs = tuple(ref for _, ref in indexed)
        contents = {section_by_id[ref].content for ref in refs}
        if len(contents) > 1:
            conflicts.append(CapabilityConflict(conflict_key=key, section_refs=refs, goal_id=goal_id))
            conflicted_sections.update(refs)
    marked = tuple(
        section.model_copy(update={"conflict_status": "CONFLICT"})
        if section.section_id in conflicted_sections
        else section
        for section in sections
    )
    provenance_hash = canonical_hash(
        {
            "task": task.content_hash,
            "coverage": coverage.content_hash,
            "results": [
                {
                    "result_id": result.result_id,
                    "result_hash": result.content_hash,
                    "receipt": result.execution_receipt_ref.model_dump(mode="json"),
                    "provenance": result.provenance.model_dump(mode="json"),
                }
                for result in results
            ],
        }
    )
    return ResultCandidate(
        protocol_version="1.0",
        schema_version=ResultCandidate.SCHEMA,
        candidate_id=f"candidate:{provenance_hash[:24]}",
        task_id=task.task_id,
        project_ref=task.project_ref,
        classification=task.classification,
        scope_version_ref=task.scope_version_ref,
        sections=marked,
        conflicts=tuple(conflicts),
        verification_status=VerificationStatus.NOT_VERIFIED,
        semantic_acceptance="PENDING_GOVERNED_ACCEPTANCE",
        provenance_hash=provenance_hash,
    )

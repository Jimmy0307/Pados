# SPDX-License-Identifier: AGPL-3.0-only
"""Capability gap recommendations. Suggestions never install or authorize."""
from __future__ import annotations

from collections.abc import Mapping

from pados.execution.context import canonical_hash
from pados.work_protocol.models import (
    CapabilityRecommendation,
    Goal,
    RecommendationStatus,
    InstallationStatus,
    RegistrationStatus,
    AuthorizationStatus,
    SelectionStatus,
    TaskEnvelope,
)

_KNOWN_CANDIDATES: dict[str, tuple[str, str, str]] = {
    "CAD_EDIT": (
        "solidworks-mcp-adapter",
        "The goal needs a CAD editing capability that is not in the registered capability inventory.",
        "CAPABILITY_CATEGORY_MATCH",
    ),
    "OCR_TEXT": (
        "ocr-capability-pack-backend",
        "The goal needs OCR for scanned content; no OCR backend is registered as ready.",
        "CAPABILITY_CATEGORY_MATCH",
    ),
}


def recommend_capability_gap(
    task: TaskEnvelope,
    goal: Goal,
    *,
    registered_capabilities: frozenset[str],
    candidates: Mapping[str, tuple[str, str, str]] | None = None,
) -> tuple[CapabilityRecommendation, ...]:
    if goal.goal_id not in {item.goal_id for item in task.normalized_goals}:
        raise ValueError("RECOMMENDATION_GOAL_NOT_IN_TASK")
    catalog = dict(_KNOWN_CANDIDATES)
    if candidates:
        catalog.update(candidates)
    output: list[CapabilityRecommendation] = []
    for capability in goal.required_capabilities:
        if capability in registered_capabilities:
            continue
        candidate = catalog.get(capability)
        if candidate is None:
            integration, reason, relevance = (
                "capability-pack-search",
                "No currently registered implementation matches the required capability.",
                "UNASSESSED",
            )
        else:
            integration, reason, relevance = candidate
        recommendation_hash = canonical_hash(
            {"task": task.task_id, "goal": goal.goal_id, "capability": capability,
             "integration": integration}
        )
        output.append(
            CapabilityRecommendation(
                protocol_version="1.0",
                schema_version=CapabilityRecommendation.SCHEMA,
                recommendation_id=f"recommendation:{recommendation_hash[:24]}",
                task_id=task.task_id,
                goal_id=goal.goal_id,
                required_capability=capability,
                why_unavailable=reason,
                candidate_integration=integration,
                relevance=relevance,
                match_rationale=reason,
                license_status="UNVERIFIED",
                provenance_status="UNVERIFIED",
                installation_status=InstallationStatus.NOT_INSTALLED,
                registration_status=RegistrationStatus.NOT_REGISTERED,
                authorization_status=AuthorizationStatus.NOT_AUTHORIZED,
                selection_status=SelectionStatus.NOT_SELECTED,
                status=RecommendationStatus.RECOMMENDED,
                auto_install=False,
            )
        )
    return tuple(output)

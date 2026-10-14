# SPDX-License-Identifier: AGPL-3.0-only
"""The human-AI capability loop (P7.3): classify a goal's capabilities into READY / LEARN / DISCOVER.

This connects existing PADOS planning with P7.1 discovery and P7.2 learning. It does NOT create a second
scheduler, replace TaskEnvelope, or add a second semantic-acceptance path. It classifies, deterministically
and with evidence, and records the decision. Execution of a READY capability stays with existing PADOS
planning/execution; this module only reports that the path is READY.

Classification per required capability (deterministic, first match wins):
* READY     -- a validated, executable capability exists -> execute the existing pack (Core path).
* LEARN     -- the gap is a human-skill gap the operator marked learnable -> P7.2 learning.
* DISCOVER  -- the capability is missing from the system -> P7.1 discovery.

Outcome learning stays advisory: this module cannot grant authorization, dispatch work, override policy or
auto-promote a capability. Reaching VALIDATED still requires a recorded validation outcome (see lifecycle).
"""
from __future__ import annotations

from collections.abc import Mapping
from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

from pados.capability_loop.learning_contracts import MasteryLevel
from pados.capability_loop.lifecycle import (
    CapabilityDimensions,
    SystemAvailability,
    ValidationStatus,
)
from pados.execution.context import canonical_hash
from pados.work_protocol.models import Goal, TaskEnvelope
from pados.work_protocol.recommendations import recommend_capability_gap


class CapabilityPath(StrEnum):
    READY = "READY"  # validated capability exists; execute via the existing PADOS pack path
    LEARN = "LEARN"  # human-skill gap; route to the adaptive-learning provider
    DISCOVER = "DISCOVER"  # system capability gap; route to the discovery provider


class PathDecision(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    required_capability: str
    path: CapabilityPath
    reason_code: str
    dimensions: CapabilityDimensions
    recommendation_ref: str | None = None  # a capability-gap recommendation id, when one applies
    semantic_authority: Literal["NONE"] = "NONE"

    @field_validator("reason_code")
    @classmethod
    def _reason(cls, value: str) -> str:
        if not value or len(value) > 63:
            raise ValueError("REASON_CODE_INVALID")
        return value


class GoalCapabilityAssessment(BaseModel):
    """The deterministic classification of one goal's required capabilities into the three paths."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    task_id: str
    goal_id: str
    decisions: tuple[PathDecision, ...]
    semantic_authority: Literal["NONE"] = "NONE"

    @property
    def ready(self) -> tuple[str, ...]:
        return tuple(d.required_capability for d in self.decisions if d.path is CapabilityPath.READY)

    @property
    def learn(self) -> tuple[str, ...]:
        return tuple(d.required_capability for d in self.decisions if d.path is CapabilityPath.LEARN)

    @property
    def discover(self) -> tuple[str, ...]:
        return tuple(d.required_capability for d in self.decisions if d.path is CapabilityPath.DISCOVER)

    @property
    def assessment_hash(self) -> str:
        return canonical_hash(self.model_dump(mode="json"))


def _bare_capability(required: str) -> str:
    """A required capability is grant-scoped (``CAPABILITY:OCR_TEXT``); the bare id is used for dimensions."""
    return required.split(":", 1)[1] if required.startswith("CAPABILITY:") else required


def assess_goal(task: TaskEnvelope, goal: Goal, *, validated_capabilities: frozenset[str],
                learnable_capabilities: frozenset[str], registered_capabilities: frozenset[str],
                human_mastery: Mapping[str, CapabilityDimensions] | None = None) -> GoalCapabilityAssessment:
    """Classify each of ``goal``'s required capabilities. Deterministic; produces evidence, grants nothing.

    ``validated_capabilities`` and ``registered_capabilities`` come from the operator-driven registry;
    ``learnable_capabilities`` is the operator's marking of which gaps a human can close by learning.
    ``human_mastery`` optionally supplies measured per-capability dimensions (from the learning progress
    store); absent entries default to UNASSESSED.
    """
    recommendations = {r.required_capability: r for r in
                       recommend_capability_gap(task, goal, registered_capabilities=registered_capabilities)}
    mastery = dict(human_mastery or {})
    decisions: list[PathDecision] = []
    for required in goal.required_capabilities:
        bare = _bare_capability(required)
        recommendation = recommendations.get(required)
        base = mastery.get(bare)
        if required in validated_capabilities:
            path, reason = CapabilityPath.READY, "VALIDATED_CAPABILITY_AVAILABLE"
            availability, validation = SystemAvailability.AVAILABLE, ValidationStatus.VALIDATED
        elif required in learnable_capabilities:
            path, reason = CapabilityPath.LEARN, "HUMAN_SKILL_GAP"
            availability, validation = SystemAvailability.UNKNOWN, ValidationStatus.UNVALIDATED
        else:
            path, reason = CapabilityPath.DISCOVER, "SYSTEM_CAPABILITY_GAP"
            availability = (SystemAvailability.CANDIDATE if required in registered_capabilities
                            else SystemAvailability.UNKNOWN)
            validation = ValidationStatus.VALIDATION_REQUIRED
        dimensions = base or CapabilityDimensions(
            capability_id=bare, human_mastery=MasteryLevel.UNASSESSED,
            system_availability=availability, validation_status=validation)
        decisions.append(PathDecision(
            required_capability=bare, path=path, reason_code=reason, dimensions=dimensions,
            recommendation_ref=recommendation.recommendation_id if recommendation is not None else None))
    return GoalCapabilityAssessment(task_id=task.task_id, goal_id=goal.goal_id, decisions=tuple(decisions))

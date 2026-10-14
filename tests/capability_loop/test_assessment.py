# SPDX-License-Identifier: AGPL-3.0-only
"""P7.3 human-AI capability loop: deterministic READY / LEARN / DISCOVER classification and lifecycle."""
from __future__ import annotations

import pytest
from tests.work_protocol.test_completion import _envelope

from pados.capability_loop.assessment import CapabilityPath, assess_goal
from pados.capability_loop.contracts import CapabilityState
from pados.capability_loop.learning_contracts import MasteryLevel
from pados.capability_loop.lifecycle import (
    CapabilityDimensions,
    LifecycleError,
    SystemAvailability,
    ValidationStatus,
    propose_transition,
    transition_allowed,
)


def _goal_with(*capabilities: str):
    task = _envelope()
    goal = task.normalized_goals[0].model_copy(update={"required_capabilities": capabilities})
    task = task.model_copy(update={"normalized_goals": (goal, *task.normalized_goals[1:])})
    return task, task.normalized_goals[0]


# -- Scenario A: Ready capability ------------------------------------------
def test_ready_path_for_a_validated_capability() -> None:
    task, goal = _goal_with("DOC_DRAFT")
    result = assess_goal(task, goal, validated_capabilities=frozenset({"DOC_DRAFT"}),
                         learnable_capabilities=frozenset(), registered_capabilities=frozenset({"DOC_DRAFT"}))
    assert result.ready == ("DOC_DRAFT",) and not result.learn and not result.discover
    decision = result.decisions[0]
    assert decision.path is CapabilityPath.READY
    assert decision.dimensions.system_availability is SystemAvailability.AVAILABLE
    assert decision.dimensions.validation_status is ValidationStatus.VALIDATED
    assert decision.semantic_authority == "NONE"


# -- Scenario B: Human gap -------------------------------------------------
def test_learn_path_for_a_human_skill_gap() -> None:
    task, goal = _goal_with("CAD_EDIT")
    result = assess_goal(task, goal, validated_capabilities=frozenset(),
                         learnable_capabilities=frozenset({"CAD_EDIT"}), registered_capabilities=frozenset())
    assert result.learn == ("CAD_EDIT",) and not result.ready and not result.discover
    decision = result.decisions[0]
    assert decision.path is CapabilityPath.LEARN and decision.reason_code == "HUMAN_SKILL_GAP"
    assert decision.recommendation_ref is not None  # a capability-gap recommendation was attached
    assert decision.dimensions.human_mastery is MasteryLevel.UNASSESSED


# -- Scenario C: System gap ------------------------------------------------
def test_discover_path_for_a_missing_system_capability() -> None:
    task, goal = _goal_with("BINARY_INSPECTION")
    result = assess_goal(task, goal, validated_capabilities=frozenset(),
                         learnable_capabilities=frozenset(), registered_capabilities=frozenset())
    assert result.discover == ("BINARY_INSPECTION",)
    decision = result.decisions[0]
    assert decision.path is CapabilityPath.DISCOVER and decision.reason_code == "SYSTEM_CAPABILITY_GAP"
    assert decision.dimensions.validation_status is ValidationStatus.VALIDATION_REQUIRED


def test_mixed_goal_classifies_every_capability_deterministically() -> None:
    task, goal = _goal_with("DOC_DRAFT", "CAD_EDIT", "BINARY_INSPECTION")
    kwargs = dict(validated_capabilities=frozenset({"DOC_DRAFT"}),
                  learnable_capabilities=frozenset({"CAD_EDIT"}), registered_capabilities=frozenset())
    first = assess_goal(task, goal, **kwargs)
    second = assess_goal(task, goal, **kwargs)
    assert first.assessment_hash == second.assessment_hash  # deterministic
    assert first.ready == ("DOC_DRAFT",)
    assert first.learn == ("CAD_EDIT",)
    assert first.discover == ("BINARY_INSPECTION",)


def test_measured_human_mastery_is_carried_into_the_decision() -> None:
    task, goal = _goal_with("CAD_EDIT")
    dims = CapabilityDimensions(capability_id="CAD_EDIT", human_mastery=MasteryLevel.PASSED,
                                system_availability=SystemAvailability.UNKNOWN,
                                validation_status=ValidationStatus.UNVALIDATED)
    result = assess_goal(task, goal, validated_capabilities=frozenset(),
                         learnable_capabilities=frozenset({"CAD_EDIT"}), registered_capabilities=frozenset(),
                         human_mastery={"CAD_EDIT": dims})
    assert result.decisions[0].dimensions.human_mastery is MasteryLevel.PASSED


# -- Lifecycle -------------------------------------------------------------
def test_discovery_cannot_jump_to_validated() -> None:
    assert not transition_allowed(CapabilityState.DISCOVERED, CapabilityState.VALIDATED)
    assert transition_allowed(CapabilityState.VALIDATION_REQUIRED, CapabilityState.VALIDATED)


def test_promotion_to_validated_requires_a_validation_outcome() -> None:
    with pytest.raises(LifecycleError) as excinfo:
        propose_transition("BINARY_INSPECTION", current=CapabilityState.VALIDATION_REQUIRED,
                           target=CapabilityState.VALIDATED, reason_code="READY")
    assert excinfo.value.code == "VALIDATION_OUTCOME_REQUIRED"


def test_promotion_with_outcome_is_an_inert_proposal() -> None:
    proposal = propose_transition("BINARY_INSPECTION", current=CapabilityState.VALIDATION_REQUIRED,
                                  target=CapabilityState.VALIDATED, reason_code="VALIDATED_BY_RECEIPT",
                                  validation_outcome_ref="b" * 64)
    assert proposal.auto_apply is False and proposal.semantic_authority == "NONE"
    assert proposal.to_state is CapabilityState.VALIDATED


def test_illegal_transition_is_refused() -> None:
    with pytest.raises(LifecycleError) as excinfo:
        propose_transition("BINARY_INSPECTION", current=CapabilityState.DISCOVERED,
                           target=CapabilityState.VALIDATED, reason_code="X", validation_outcome_ref="b" * 64)
    assert excinfo.value.code == "TRANSITION_NOT_ALLOWED"

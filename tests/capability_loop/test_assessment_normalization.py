# SPDX-License-Identifier: AGPL-3.0-only
"""Finding 6: capability classification must use one canonical (bare) id for every lookup.

A grant-scoped ``CAPABILITY:OCR_TEXT`` and a bare ``OCR_TEXT`` name the same capability. Classification must
canonicalize to the bare id for every membership check (validated / learnable / registered / recommendation
/ mastery) so both forms yield the same decision; the prefix must never change the outcome.
"""
from __future__ import annotations

import pytest
from tests.work_protocol.test_completion import _envelope

from pados.capability_loop.assessment import CapabilityPath, assess_goal
from pados.capability_loop.learning_contracts import MasteryLevel
from pados.capability_loop.lifecycle import CapabilityDimensions, SystemAvailability, ValidationStatus


def _goal_with(*capabilities: str):
    task = _envelope()
    goal = task.normalized_goals[0].model_copy(update={"required_capabilities": capabilities})
    task = task.model_copy(update={"normalized_goals": (goal, *task.normalized_goals[1:])})
    return task, task.normalized_goals[0]


def test_prefixed_and_bare_capability_sets_classify_identically() -> None:
    task, goal = _goal_with("OCR_TEXT")
    bare = assess_goal(task, goal, validated_capabilities=frozenset({"OCR_TEXT"}),
                       learnable_capabilities=frozenset(), registered_capabilities=frozenset({"OCR_TEXT"}))
    prefixed = assess_goal(task, goal, validated_capabilities=frozenset({"CAPABILITY:OCR_TEXT"}),
                           learnable_capabilities=frozenset(),
                           registered_capabilities=frozenset({"CAPABILITY:OCR_TEXT"}))
    assert bare.ready == ("OCR_TEXT",) == prefixed.ready
    assert bare.assessment_hash == prefixed.assessment_hash  # the prefix changes nothing


def test_prefixed_learnable_set_classifies_as_learn() -> None:
    task, goal = _goal_with("CAD_EDIT")
    result = assess_goal(task, goal, validated_capabilities=frozenset(),
                         learnable_capabilities=frozenset({"CAPABILITY:CAD_EDIT"}),
                         registered_capabilities=frozenset())
    assert result.learn == ("CAD_EDIT",)
    assert result.decisions[0].path is CapabilityPath.LEARN


def test_prefixed_registered_set_marks_discover_candidate() -> None:
    task, goal = _goal_with("BINARY_INSPECTION")
    result = assess_goal(task, goal, validated_capabilities=frozenset(),
                         learnable_capabilities=frozenset(),
                         registered_capabilities=frozenset({"CAPABILITY:BINARY_INSPECTION"}))
    assert result.discover == ("BINARY_INSPECTION",)
    # Registered (even prefixed) means the discover candidate is a CANDIDATE, not UNKNOWN availability.
    from pados.capability_loop.lifecycle import SystemAvailability
    assert result.decisions[0].dimensions.system_availability is SystemAvailability.CANDIDATE


def test_prefixed_human_mastery_key_is_canonicalized() -> None:
    # Round-9 Finding 3: measured mastery supplied under the grant-scoped key must still be found by the
    # bare-id lookup, not silently replaced by UNASSESSED.
    task, goal = _goal_with("CAD_EDIT")
    dims = CapabilityDimensions(capability_id="CAD_EDIT", human_mastery=MasteryLevel.PASSED,
                                system_availability=SystemAvailability.UNKNOWN,
                                validation_status=ValidationStatus.UNVALIDATED)
    result = assess_goal(task, goal, validated_capabilities=frozenset(),
                         learnable_capabilities=frozenset({"CAD_EDIT"}), registered_capabilities=frozenset(),
                         human_mastery={"CAPABILITY:CAD_EDIT": dims})  # grant-scoped mastery key
    assert result.decisions[0].dimensions.human_mastery is MasteryLevel.PASSED


def test_conflicting_human_mastery_aliases_are_rejected() -> None:
    # Round-10: a bare key and its prefixed alias with DIFFERENT values is ambiguous -> reject (deterministic).
    task, goal = _goal_with("CAD_EDIT")
    passed = CapabilityDimensions(capability_id="CAD_EDIT", human_mastery=MasteryLevel.PASSED,
                                  system_availability=SystemAvailability.UNKNOWN,
                                  validation_status=ValidationStatus.UNVALIDATED)
    failed = passed.model_copy(update={"human_mastery": MasteryLevel.FAILED})
    with pytest.raises(ValueError, match="HUMAN_MASTERY_ALIAS_CONFLICT"):
        assess_goal(task, goal, validated_capabilities=frozenset(),
                    learnable_capabilities=frozenset({"CAD_EDIT"}), registered_capabilities=frozenset(),
                    human_mastery={"CAD_EDIT": passed, "CAPABILITY:CAD_EDIT": failed})


def test_matching_human_mastery_aliases_are_accepted() -> None:
    task, goal = _goal_with("CAD_EDIT")
    dims = CapabilityDimensions(capability_id="CAD_EDIT", human_mastery=MasteryLevel.PASSED,
                                system_availability=SystemAvailability.UNKNOWN,
                                validation_status=ValidationStatus.UNVALIDATED)
    result = assess_goal(task, goal, validated_capabilities=frozenset(),
                         learnable_capabilities=frozenset({"CAD_EDIT"}), registered_capabilities=frozenset(),
                         human_mastery={"CAD_EDIT": dims, "CAPABILITY:CAD_EDIT": dims})  # identical -> ok
    assert result.decisions[0].dimensions.human_mastery is MasteryLevel.PASSED

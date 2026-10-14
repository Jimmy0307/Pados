# SPDX-License-Identifier: AGPL-3.0-only
"""Finding 6: capability classification must use one canonical (bare) id for every lookup.

A grant-scoped ``CAPABILITY:OCR_TEXT`` and a bare ``OCR_TEXT`` name the same capability. Classification must
canonicalize to the bare id for every membership check (validated / learnable / registered / recommendation
/ mastery) so both forms yield the same decision; the prefix must never change the outcome.
"""
from __future__ import annotations

from tests.work_protocol.test_completion import _envelope

from pados.capability_loop.assessment import CapabilityPath, assess_goal


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

# SPDX-License-Identifier: AGPL-3.0-only
"""PR #16 R17 [43]: a human-mastery value must belong to the capability its (canonical) key names.

Otherwise ``{"CAPABILITY:OCR_TEXT": <CAD_EDIT dimensions>}`` yields a decision whose ``required_capability`` is
OCR_TEXT while its mastery, evidence and embedded capability identity belong to CAD_EDIT, and the assessment
hash misattributes another capability's evidence.
"""
from __future__ import annotations

import pytest
from tests.work_protocol.test_completion import _envelope

from pados.capability_loop.assessment import assess_goal
from pados.capability_loop.learning_contracts import MasteryLevel
from pados.capability_loop.lifecycle import CapabilityDimensions


def _goal_with(*capabilities: str):
    task = _envelope()
    goal = task.normalized_goals[0].model_copy(update={"required_capabilities": capabilities})
    task = task.model_copy(update={"normalized_goals": (goal, *task.normalized_goals[1:])})
    return task, task.normalized_goals[0]


@pytest.mark.parametrize("key", ["OCR_TEXT", "CAPABILITY:OCR_TEXT"])
def test_mastery_value_for_another_capability_is_rejected(key: str) -> None:
    task, goal = _goal_with("OCR_TEXT")
    foreign = CapabilityDimensions(capability_id="CAD_EDIT", human_mastery=MasteryLevel.PASSED)
    with pytest.raises(ValueError, match="HUMAN_MASTERY_KEY_IDENTITY_MISMATCH"):
        assess_goal(task, goal, validated_capabilities=frozenset(),
                    learnable_capabilities=frozenset({"OCR_TEXT"}), registered_capabilities=frozenset(),
                    human_mastery={key: foreign})


def test_mastery_value_naming_its_own_capability_is_used() -> None:
    task, goal = _goal_with("OCR_TEXT")
    own = CapabilityDimensions(capability_id="OCR_TEXT", human_mastery=MasteryLevel.PASSED)
    result = assess_goal(task, goal, validated_capabilities=frozenset(),
                         learnable_capabilities=frozenset({"OCR_TEXT"}), registered_capabilities=frozenset(),
                         human_mastery={"CAPABILITY:OCR_TEXT": own})
    decision = result.decisions[0]
    assert decision.required_capability == decision.dimensions.capability_id == "OCR_TEXT"
    assert decision.dimensions.human_mastery is MasteryLevel.PASSED

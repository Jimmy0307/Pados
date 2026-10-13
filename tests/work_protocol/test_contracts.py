from __future__ import annotations

import pytest

from pados.work_protocol.compatibility import parse_protocol
from pados.work_protocol.models import Goal, GoalRelation, GoalRelationType, TaskEnvelope


def envelope_data() -> dict[str, object]:
    return {
        "protocol_version": "1.0",
        "schema_version": "pados.task-envelope.v1",
        "task_id": "task-1",
        "original_intent": "Prepare the release brief and verify its sources.",
        "normalized_goals": [
            {
                "protocol_version": "1.0",
                "schema_version": "pados.goal.v1",
                "goal_id": "goal-a",
                "title": "Prepare the brief",
                "objective": "Draft the release brief.",
                "required_capabilities": ["DOC_DRAFT"],
            },
            {
                "protocol_version": "1.0",
                "schema_version": "pados.goal.v1",
                "goal_id": "goal-b",
                "title": "Verify sources",
                "objective": "Verify source claims.",
                "required_capabilities": ["SOURCE_VERIFY"],
            },
        ],
        "goal_relations": [
            {
                "protocol_version": "1.0",
                "schema_version": "pados.goal-relation.v1",
                "source_goal_id": "goal-b",
                "target_goal_id": "goal-a",
                "relation": "DEPENDS_ON",
            }
        ],
        "constraints": ["Use project-approved sources."],
        "project_ref": "project-1",
        "classification": "INTERNAL",
        "priority": "NORMAL",
        "deadline": None,
        "scope_version_ref": "scope:7",
        "created_by": "human:jimmy",
        "created_at": "2026-10-05T08:00:00+00:00",
    }


def test_task_envelope_round_trips_known_and_additive_fields() -> None:
    raw = {**envelope_data(), "display_hint": {"theme": "panoramic"}}
    envelope = parse_protocol(raw, TaskEnvelope)

    assert envelope.original_intent == raw["original_intent"]
    assert envelope.classification.value == "INTERNAL"
    assert envelope.project_ref == "project-1"
    assert envelope.scope_version_ref == "scope:7"
    assert envelope.model_dump(mode="json")["display_hint"] == {"theme": "panoramic"}


def test_goal_graph_rejects_unknown_relation_endpoint_and_dependency_cycle() -> None:
    raw = envelope_data()
    raw["goal_relations"] = [
        {
            "protocol_version": "1.0",
            "schema_version": "pados.goal-relation.v1",
            "source_goal_id": "goal-a",
            "target_goal_id": "goal-b",
            "relation": "DEPENDS_ON",
        },
        {
            "protocol_version": "1.0",
            "schema_version": "pados.goal-relation.v1",
            "source_goal_id": "goal-b",
            "target_goal_id": "goal-a",
            "relation": "DEPENDS_ON",
        },
    ]

    with pytest.raises(ValueError, match="GOAL_GRAPH_CYCLE"):
        TaskEnvelope.model_validate(raw)

    relation = GoalRelation(
        protocol_version="1.0",
        schema_version="pados.goal-relation.v1",
        source_goal_id="goal-a",
        target_goal_id="unknown",
        relation=GoalRelationType.DERIVED_FROM,
    )
    with pytest.raises(ValueError, match="GOAL_RELATION_TARGET_UNKNOWN"):
        TaskEnvelope.model_validate({**envelope_data(), "goal_relations": [relation.model_dump()]})


def test_task_envelope_rejects_missing_authority_fields() -> None:
    raw = envelope_data()
    del raw["classification"]
    with pytest.raises(ValueError):
        TaskEnvelope.model_validate(raw)

    raw = envelope_data()
    del raw["scope_version_ref"]
    with pytest.raises(ValueError):
        TaskEnvelope.model_validate(raw)


def test_goal_schema_is_explicit_and_provider_neutral() -> None:
    goal = Goal.model_validate(envelope_data()["normalized_goals"][0])
    assert goal.required_capabilities == ("DOC_DRAFT",)
    assert "provider" not in goal.model_dump(mode="json")

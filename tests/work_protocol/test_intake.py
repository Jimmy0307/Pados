from __future__ import annotations

from datetime import UTC, datetime

import pytest

from pados.work_protocol.intake import capture_task_envelope


def test_intake_persists_original_intent_as_pending_root_goal() -> None:
    created = datetime(2026, 10, 5, 8, 0, tzinfo=UTC)
    deadline = datetime(2026, 10, 6, 8, 0, tzinfo=UTC)
    envelope = capture_task_envelope(
        task_id="task-intake-1",
        original_intent="Prepare the launch memo and verify each citation.",
        project_ref="project-1",
        classification="INTERNAL",
        priority="HIGH",
        deadline=deadline,
        scope_version_ref="scope:7",
        created_by="human:jimmy",
        created_at=created,
        constraints=("Do not publish without approval.",),
    )

    assert envelope.original_intent == envelope.normalized_goals[0].objective
    assert envelope.decomposition_status.value == "PENDING"
    assert envelope.priority.value == "HIGH"
    assert envelope.deadline == deadline
    assert envelope.scope_version_ref == "scope:7"


def test_intake_capability_hint_is_not_an_authorization_grant() -> None:
    envelope = capture_task_envelope(
        task_id="task-intake-2",
        original_intent="Read the scanned invoice.",
        project_ref="project-1",
        classification="INTERNAL",
        priority="NORMAL",
        deadline=None,
        scope_version_ref="scope:7",
        created_by="human:jimmy",
        created_at=datetime(2026, 10, 5, 8, 0, tzinfo=UTC),
        capability_hint="OCR_TEXT",
    )

    assert envelope.normalized_goals[0].required_capabilities == ("OCR_TEXT",)
    assert not hasattr(envelope.normalized_goals[0], "authorized")

    with pytest.raises(ValueError, match="TASK_CAPABILITY_HINT_INVALID"):
        capture_task_envelope(
            task_id="task-intake-3",
            original_intent="Read the scanned invoice.",
            project_ref="project-1",
            classification="INTERNAL",
            priority="NORMAL",
            deadline=None,
            scope_version_ref="scope:7",
            created_by="human:jimmy",
            created_at=datetime(2026, 10, 5, 8, 0, tzinfo=UTC),
            capability_hint="anything;please",
        )

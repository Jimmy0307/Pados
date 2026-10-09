# SPDX-License-Identifier: AGPL-3.0-only
"""Build a first-class task envelope from trusted server-side intent intake."""
from __future__ import annotations

import hashlib
import re
from datetime import datetime

from pados.enterprise.classification import DataClassification, parse_classification
from pados.work_protocol.models import (
    DecompositionStatus,
    Goal,
    GoalState,
    RemoteIntent,
    RemoteIntentType,
    TaskEnvelope,
    TaskPriority,
)

_CAPABILITY = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")


def capture_task_envelope(
    *,
    task_id: str,
    original_intent: str,
    project_ref: str,
    classification: DataClassification | str,
    priority: TaskPriority | str,
    deadline: datetime | None,
    scope_version_ref: str,
    created_by: str,
    created_at: datetime,
    constraints: tuple[str, ...] = (),
    capability_hint: str | None = None,
) -> TaskEnvelope:
    """Capture exact intent and a pending root goal; do not invent decomposition.

    Capability hints are requirements for planning only. They grant no
    authorization and still go through the existing registry/router/policy.
    """
    if not original_intent.strip():
        raise ValueError("TASK_INTENT_REQUIRED")
    if capability_hint is not None and not _CAPABILITY.fullmatch(capability_hint):
        raise ValueError("TASK_CAPABILITY_HINT_INVALID")
    root_id = "goal-root:" + hashlib.sha256(task_id.encode("utf-8")).hexdigest()[:24]
    title = next((line.strip() for line in original_intent.splitlines() if line.strip()), "Original intent")
    goal = Goal(
        protocol_version="1.0",
        schema_version=Goal.SCHEMA,
        goal_id=root_id,
        title=title[:160],
        objective=original_intent,
        required_capabilities=() if capability_hint is None else (capability_hint,),
        dependencies=(),
        state=GoalState.PENDING,
    )
    return TaskEnvelope(
        protocol_version="1.0",
        schema_version=TaskEnvelope.SCHEMA,
        task_id=task_id,
        original_intent=original_intent,
        decomposition_status=DecompositionStatus.PENDING,
        normalized_goals=(goal,),
        goal_relations=(),
        constraints=constraints,
        project_ref=project_ref,
        classification=parse_classification(classification),
        priority=TaskPriority(priority),
        deadline=deadline,
        scope_version_ref=scope_version_ref,
        created_by=created_by,
        created_at=created_at,
    )


def project_create_task_intent(*, intent_id: str, task: TaskEnvelope) -> RemoteIntent:
    """Bind the authenticated create-task request to the captured task and scope."""
    return RemoteIntent(
        protocol_version="1.0",
        schema_version=RemoteIntent.SCHEMA,
        intent_id=intent_id,
        project_ref=task.project_ref,
        intent_type=RemoteIntentType.CREATE_TASK,
        task_id=task.task_id,
        goal_id=task.normalized_goals[0].goal_id,
        priority=task.priority,
        human_message=task.original_intent,
        approval_ref=None,
        target_scope_version_ref=task.scope_version_ref,
    )

# SPDX-License-Identifier: AGPL-3.0-only
# ruff: noqa: F811 - the D1 fixtures are imported by name and requested as test parameters
"""A Control Center work request with a requested capability carries a server-captured TaskEnvelope, so a plan
bound to its product goal (the ResultCandidate path) can be created; a plain-text request still cannot.

Uses the D1 fixtures (PADOS_RC2_APPROVAL_BINDING_TEST_DATABASE_URL).
"""
from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.orm import Session

from pados.control import service as control_service
from pados.control.schemas import WorkRequestCreate
from pados.control.work_request_capture import captured_task_id, create_captured_work_request
from pados.loop import scope_authority
from pados.loop import service as loop_service
from pados.loop.schemas import LoopCreate, PlanStepCreate, StepType, TaskGoalCreate, TaskPlanCreate
from pados.work_protocol.models import TaskEnvelope
from pados.worker.schemas import CapabilityId, WorkerType
from tests.integration.approval_subjects import work_route_subject
from tests.integration.test_rc2_approval_binding_postgres import _project, session  # noqa: F401

NOW = datetime(2026, 11, 10, 9, 0, tzinfo=UTC)
PROBE_STEP = {"step_sequence": 1, "capability_id": "RUN_STATIC_ANALYSIS", "command_id": "markdown_reference_check",
              "max_risk_level": "LOW"}


def _request(project_id: str, key: str, *, text_value: str = "Run the read-only reference check.",
             capability: CapabilityId | None = CapabilityId.RUN_STATIC_ANALYSIS) -> WorkRequestCreate:
    return WorkRequestCreate(project_id=project_id, title="Check the docs links", body=text_value,
                             idempotency_key=key, requested_capability=capability)


def test_a_captured_request_stores_a_hashed_envelope_and_replays_by_key(session: Session, tmp_path: Path) -> None:
    project_id = _project(session, tmp_path / "ws")
    key = f"capture-{uuid4()}"
    first = create_captured_work_request(session, _request(project_id, key), scope_version=0, now=NOW)
    body = json.loads(str(first["body"]))
    envelope = TaskEnvelope.model_validate(body["task_envelope"])
    assert body["task_envelope_hash"] == envelope.content_hash
    assert envelope.task_id == captured_task_id(project_id, key)
    assert envelope.classification.value == "INTERNAL"  # read from the project row
    assert envelope.scope_version_ref == "scope:0"
    # A retry later (another capture time, even another scope version) is the same record, not a new capture.
    again = create_captured_work_request(session, _request(project_id, key), scope_version=4,
                                         now=datetime(2026, 11, 11, tzinfo=UTC))
    assert again["id"] == first["id"] and again["body"] == first["body"]
    events = session.execute(text("SELECT count(*) FROM control_sync_events WHERE project_id = CAST(:p AS uuid) "
                                  "AND event_type = 'WORK_REQUEST_CAPTURED'"), {"p": project_id}).scalar_one()
    assert events == 1
    with pytest.raises(ValueError, match="^WORK_REQUEST_IDEMPOTENCY_CONTENT_CONFLICT$"):
        create_captured_work_request(session, _request(project_id, key, text_value="Something else."),
                                     scope_version=0, now=NOW)
    with pytest.raises(ValueError, match="^WORK_REQUEST_IDEMPOTENCY_CONTENT_CONFLICT$"):
        create_captured_work_request(session, _request(project_id, key, capability=CapabilityId.RUN_TESTS),
                                     scope_version=0, now=NOW)


def test_capture_without_a_readable_scope_version_writes_nothing(session: Session, tmp_path: Path) -> None:
    project_id = _project(session, tmp_path / "ws")
    with pytest.raises(ValueError, match="^TASK_ENVELOPE_SCOPE_VERSION_UNAVAILABLE$"):
        create_captured_work_request(session, _request(project_id, f"capture-{uuid4()}"), scope_version=None,
                                     now=NOW)
    rows = session.execute(text("SELECT count(*) FROM control_work_requests WHERE project_id = CAST(:p AS uuid)"),
                           {"p": project_id}).scalar_one()
    assert rows == 0


def _plan_bound_to_the_product_goal(session: Session, project_id: str, request: dict[str, object], goal_id: str,
                                    capability: CapabilityId = CapabilityId.RUN_STATIC_ANALYSIS,
                                    ) -> tuple[dict[str, object], str, str]:
    """Approve a route for one probe step with ``capability``, then plan that step bound to ``goal_id``."""
    subject = work_route_subject(project_id, str(request["id"]), ({**PROBE_STEP, "capability_id": capability.value},))
    approval = control_service.create_approval(session, {"project_id": project_id, "proposal_summary": "capture",
                                                         "idempotency_key": f"capture-ap-{uuid4()}",
                                                         "subject": subject})  # type: ignore[arg-type]
    approved = control_service.decide_approval(session, str(approval["id"]), version=1, status="APPROVED",
                                               reason="capture integration test",
                                               idempotency_key=f"capture-ok-{uuid4()}")  # type: ignore[arg-type]
    goal = loop_service.create_task_goal(session, TaskGoalCreate(
        project_id=project_id, request_id=str(request["id"]), approval_id=str(approved["id"]),
        objective="Check that every Markdown reference resolves", success_criteria=["the probe completes"],
        idempotency_key=f"capture-goal-{uuid4()}"))
    plan = loop_service.create_task_plan(session, TaskPlanCreate(
        task_goal_id=str(goal["id"]), approval_id=str(approved["id"]), idempotency_key=f"capture-plan-{uuid4()}",
        steps=[PlanStepCreate(sequence=1, product_goal_id=goal_id, title="Check Markdown references",
                              step_type=StepType.RUN_VALIDATION, required_capability=capability,
                              preferred_worker_types=[WorkerType.DETERMINISTIC_TOOL],
                              command_id="markdown_reference_check",
                              output_contract={"pados_capability_outcome": "pados.capability-outcome.v1"})]),
        settings=None)
    return plan, str(approved["id"]), str(goal["id"])


def test_a_captured_request_supports_a_plan_bound_to_its_product_goal(
        session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scope_authority, "read_current_scope_version", lambda _settings, _project_id: 0)
    project_id = _project(session, tmp_path / "ws")
    request = create_captured_work_request(session, _request(project_id, f"capture-{uuid4()}"), scope_version=0,
                                           now=NOW)
    envelope = TaskEnvelope.model_validate(json.loads(str(request["body"]))["task_envelope"])
    (root_goal,) = envelope.normalized_goals
    plan, _, _ = _plan_bound_to_the_product_goal(session, project_id, request, root_goal.goal_id)
    binding = session.execute(text("SELECT task_id, goal_id FROM goal_runtime_bindings "
                                   "WHERE plan_id = CAST(:plan AS uuid)"), {"plan": str(plan["id"])}).one()
    assert (binding.task_id, binding.goal_id) == (envelope.task_id, root_goal.goal_id)


def test_a_plain_text_request_still_cannot_bind_a_product_goal(
        session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(scope_authority, "read_current_scope_version", lambda _settings, _project_id: 0)
    project_id = _project(session, tmp_path / "ws")
    request = control_service.create_work_request(session, {
        "project_id": project_id, "title": "Check the docs links", "body": "Run the read-only reference check.",
        "idempotency_key": f"plain-{uuid4()}"})
    with pytest.raises(ValueError, match="^TASK_ENVELOPE_REQUIRED_FOR_RUNTIME_BINDING$"):
        _plan_bound_to_the_product_goal(session, project_id, request, "goal-root:000000000000000000000000")


def test_the_captured_capability_only_narrows_what_a_bound_step_may_use(
        session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The capability names what the goal requires; a step needing another one is refused before it routes."""
    monkeypatch.setattr(scope_authority, "read_current_scope_version", lambda _settings, _project_id: 0)
    project_id = _project(session, tmp_path / "ws")
    request = create_captured_work_request(session, _request(project_id, f"capture-{uuid4()}"), scope_version=0,
                                           now=NOW)
    (root_goal,) = TaskEnvelope.model_validate(json.loads(str(request["body"]))["task_envelope"]).normalized_goals
    plan, approval_id, goal_id = _plan_bound_to_the_product_goal(session, project_id, request, root_goal.goal_id,
                                                                 CapabilityId.RUN_TESTS)
    loop = loop_service.create_loop(session, LoopCreate(task_goal_id=goal_id, active_plan_id=str(plan["id"]),
                                                        approval_id=approval_id, max_iterations=2,
                                                        idempotency_key=f"capture-loop-{uuid4()}"))
    step = session.execute(text("SELECT * FROM task_plan_steps WHERE task_plan_id = CAST(:plan AS uuid)"),
                           {"plan": str(plan["id"])}).mappings().one()
    failure = loop_service._product_step_revalidation_failure(session, loop=dict(loop), step=dict(step),
                                                              settings=None)
    assert failure == "CAPABILITY_BINDING_INVALID"


def test_a_concurrent_first_submission_with_the_same_key_is_answered_as_a_replay(
        session: Session, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The other submission committed its own capture (another capture time) after this one looked for it."""
    from pados.control import work_request_capture

    project_id = _project(session, tmp_path / "ws")
    key = f"capture-{uuid4()}"
    winner = create_captured_work_request(session, _request(project_id, key), scope_version=0, now=NOW)
    real_lookup = work_request_capture._stored_body
    lookups: list[object] = []

    def not_yet_visible(*args: object) -> object:
        lookups.append(args)
        return None if len(lookups) == 1 else real_lookup(*args)  # type: ignore[arg-type]

    monkeypatch.setattr(work_request_capture, "_stored_body", not_yet_visible)
    loser = create_captured_work_request(session, _request(project_id, key), scope_version=0,
                                         now=datetime(2026, 11, 10, 9, 0, 1, tzinfo=UTC))
    assert loser["id"] == winner["id"] and loser["body"] == winner["body"]
    with pytest.raises(ValueError, match="^WORK_REQUEST_IDEMPOTENCY_CONTENT_CONFLICT$"):
        lookups.clear()
        create_captured_work_request(session, _request(project_id, key, text_value="Other work."), scope_version=0,
                                     now=NOW)

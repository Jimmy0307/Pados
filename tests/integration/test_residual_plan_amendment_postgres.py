from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from pados.control import service as control_service
from pados.loop import scope_authority
from pados.loop import service as loop_service
from pados.loop.amendments import rebuild_p6_10_lineage_projection, reconcile_residual_work
from pados.loop.schemas import (
    LoopCreate,
    PlanStepCreate,
    StepType,
    TaskGoalCreate,
    TaskPlanCreate,
)
from pados.memory.service import bootstrap_project
from pados.work_protocol.coverage import build_coverage
from pados.work_protocol.graph import (
    project_p6_10_lineage,
    project_work_graph,
)
from pados.work_protocol.models import (
    CapabilityResult,
    Goal,
    GoalRelation,
    GoalRelationType,
    TaskEnvelope,
    WorkEdgeKind,
)
from pados.work_protocol.residual import create_residual_tasks
from pados.worker import service as worker_service
from pados.worker.schemas import (
    CapabilityId,
    RiskLevel,
    WorkerAvailability,
    WorkerSpec,
    WorkerType,
)

from tests.integration.approval_subjects import work_route_subject

_TEST_DATABASE_ENV = "PADOS_RESIDUAL_AMENDMENT_TEST_DATABASE_URL"


@pytest.fixture
def disposable_session() -> Iterator[Session]:
    database_url = os.environ.get(_TEST_DATABASE_ENV)
    if not database_url:
        pytest.skip(f"set {_TEST_DATABASE_ENV} to the dedicated local disposable test database")
    parsed_url = make_url(database_url)
    if parsed_url.database != "pados_residual_plan_test" or parsed_url.host not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        pytest.fail("residual amendment integration tests require the dedicated loopback test database")
    engine = create_engine(database_url, pool_pre_ping=True, connect_args={"connect_timeout": 2})
    session = Session(engine, expire_on_commit=False)
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


# The approval names exactly the plan steps it authorizes (RC2 D1); residual children run
# under their root step's operation.
LOOP_OPERATIONS: tuple[dict[str, object], ...] = tuple(
    {"step_sequence": sequence, "capability_id": "READ_REPOSITORY", "command_id": "python_version",
     "max_risk_level": "LOW"}
    for sequence in (1, 2)
)


def _approve(
    session: object,
    project_id: str,
    suffix: str,
    *,
    request_id: str,
    operations: tuple[dict[str, object], ...] = LOOP_OPERATIONS,
) -> str:
    subject = work_route_subject(project_id, request_id, operations)
    row = control_service.create_approval(
        session,
        {
            "project_id": project_id,
            "proposal_summary": suffix,
            "idempotency_key": f"residual-approval-{suffix}",
            "subject": subject,
        },
    )  # type: ignore[arg-type]
    approved = control_service.decide_approval(
        session,
        str(row["id"]),
        version=1,
        status="APPROVED",
        reason="bounded residual plan integration test",
        idempotency_key=f"residual-decision-{suffix}",
    )  # type: ignore[arg-type]
    return str(approved["id"])


def _task_envelope(project_id: str, scope_version: int, *, child_capability: str) -> TaskEnvelope:
    goals = (
        Goal(protocol_version="1.0", schema_version=Goal.SCHEMA, goal_id="goal-a", title="Read sources", objective="Read source notes", required_capabilities=("READ_REPOSITORY",)),
        Goal(protocol_version="1.0", schema_version=Goal.SCHEMA, goal_id="goal-b", title="Check references", objective="Check references", required_capabilities=("READ_REPOSITORY",)),
        Goal(protocol_version="1.0", schema_version=Goal.SCHEMA, goal_id="goal-b2", title="Resolve missing reference", objective="Resolve one missing reference", required_capabilities=(child_capability,)),
    )
    return TaskEnvelope(
        protocol_version="1.0",
        schema_version=TaskEnvelope.SCHEMA,
        task_id=f"task:{uuid4().hex}",
        original_intent="Read the source notes and verify the references.",
        normalized_goals=goals,
        goal_relations=(
            GoalRelation(
                protocol_version="1.0",
                schema_version=GoalRelation.SCHEMA,
                source_goal_id="goal-b",
                target_goal_id="goal-b2",
                relation=GoalRelationType.PARENT_OF,
            ),
        ),
        project_ref=project_id,
        classification="INTERNAL",
        priority="NORMAL",
        deadline=None,
        scope_version_ref=f"scope:{scope_version}",
        created_by="human:owner",
        created_at=datetime.now(UTC),
    )


def _seed_runtime(session: object, root: Path, *, child_capability: str) -> tuple[str, str, TaskEnvelope, str, str, str]:
    project_id = bootstrap_project(
        session,  # type: ignore[arg-type]
        name=f"residual-amendment-{uuid4()}",
        root_path=root,
        classification="internal",
    )
    envelope = _task_envelope(project_id, 0, child_capability=child_capability)
    request_body = json.dumps(
        {"task_envelope": envelope.model_dump(mode="json"), "task_envelope_hash": envelope.content_hash},
        sort_keys=True,
    )
    request = control_service.create_work_request(
        session,  # type: ignore[arg-type]
        {
            "project_id": project_id,
            "title": "Residual amendment integration",
            "body": request_body,
            "idempotency_key": f"residual-request-{uuid4()}",
        },
    )
    approval_id = _approve(session, project_id, uuid4().hex, request_id=str(request["id"]))
    goal = loop_service.create_task_goal(
        session,  # type: ignore[arg-type]
        TaskGoalCreate(
            project_id=project_id,
            request_id=str(request["id"]),
            objective="Execute the approved task plan and report discrete goal outcomes.",
            success_criteria=["each goal has a normalized result"],
            approval_id=approval_id,
            idempotency_key=f"p610-goal-{uuid4()}",
        ),
    )
    plan = loop_service.create_task_plan(
        session,  # type: ignore[arg-type]
        TaskPlanCreate(
            task_goal_id=str(goal["id"]),
            approval_id=approval_id,
            steps=[
                PlanStepCreate(
                    sequence=1,
                    product_goal_id="goal-a",
                    title="Read source notes",
                    step_type=StepType.INSPECT,
                    required_capability=CapabilityId.READ_REPOSITORY,
                    preferred_worker_types=[WorkerType.DETERMINISTIC_TOOL],
                    allowed_paths=["docs/"],
                    allowed_actions=["READ_REPOSITORY"],
                    command_id="python_version",
                    output_contract={"pados_capability_outcome": "pados.capability-outcome.v1"},
                ),
                PlanStepCreate(
                    sequence=2,
                    product_goal_id="goal-b",
                    title="Check references",
                    step_type=StepType.INSPECT,
                    required_capability=CapabilityId.READ_REPOSITORY,
                    preferred_worker_types=[WorkerType.DETERMINISTIC_TOOL],
                    dependencies=[1],
                    allowed_paths=["docs/"],
                    allowed_actions=["READ_REPOSITORY"],
                    command_id="python_version",
                    output_contract={"pados_capability_outcome": "pados.capability-outcome.v1"},
                ),
            ],
            idempotency_key=f"p610-plan-{uuid4()}",
        ),
        settings=None,
    )
    loop = loop_service.create_loop(
        session,  # type: ignore[arg-type]
        LoopCreate(
            task_goal_id=str(goal["id"]),
            active_plan_id=str(plan["id"]),
            approval_id=approval_id,
            max_iterations=6,
            idempotency_key=f"p610-loop-{uuid4()}",
        ),
    )
    loop_service.start_loop(session, str(loop["id"]), owner_instance="residual-integration")  # type: ignore[arg-type]
    return project_id, str(request["id"]), envelope, str(plan["id"]), str(loop["id"]), approval_id


def _install_deterministic_router(monkeypatch: pytest.MonkeyPatch, outcomes: list[str]) -> None:
    worker = WorkerSpec(
        worker_id="deterministic-tool",
        worker_type=WorkerType.DETERMINISTIC_TOOL,
        display_name="Deterministic integration worker",
        version="test",
        availability=WorkerAvailability.AVAILABLE,
        capabilities=[CapabilityId.READ_REPOSITORY],
        risk_ceiling=RiskLevel.LOW,
        supports_structured_output=True,
        supports_file_changes=False,
        supports_tool_calls=True,
        supports_streaming=False,
        max_context_tokens=1000,
        default_timeout_seconds=30,
    )
    monkeypatch.setattr(worker_service, "discover_workers", lambda _settings, **_kwargs: {worker.worker_id: worker})
    iterator = iter(outcomes)

    def run_command(_invocation: object, *, project_root: Path) -> dict[str, object]:
        del project_root
        return {
            "stdout": json.dumps(
                {"schema_version": "pados.capability-outcome.v1", "outcome": next(iterator)}
            ),
            "stderr": "",
            "status": "SUCCEEDED",
            "exit_code": 0,
            "error_code": None,
        }

    monkeypatch.setattr(worker_service, "run_registered_command", run_command)


def _run_iteration(session: object, loop_id: str, root: Path, key: str) -> dict[str, object]:
    return loop_service.run_one_iteration(
        session,  # type: ignore[arg-type]
        loop_id=loop_id,
        settings=None,
        artifact_root=root / "artifacts",
        idempotency_key=key,
    )


def test_p6_10_executes_governed_residual_and_emits_receipt_lineage(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, disposable_session: Session
) -> None:
    monkeypatch.setattr(scope_authority, "read_current_scope_version", lambda _settings, _project_id: 0)
    _install_deterministic_router(monkeypatch, ["COMPLETE", "PARTIAL", "COMPLETE"])
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with disposable_session as session:
        project_id, request_id, envelope, plan_id, loop_id, _approval_id = _seed_runtime(
            session, workspace, child_capability="READ_REPOSITORY"
        )
        first = _run_iteration(session, loop_id, tmp_path, f"run-{uuid4()}")
        second = _run_iteration(session, loop_id, tmp_path, f"run-{uuid4()}")

        assert first["status"] == "RUNNING"
        assert second["status"] == "RUNNING"
        amendments = session.execute(
            text("SELECT * FROM residual_plan_amendments WHERE loop_id = CAST(:id AS uuid)"),
            {"id": loop_id},
        ).mappings().all()
        assert len(amendments) == 1
        assert amendments[0]["authority_decision"] == "AUTO_AMENDABLE"
        assert amendments[0]["goal_id"] == "goal-b2"
        assert amendments[0]["step_id"] is not None
        assert amendments[0]["project_ref"] == project_id
        assert amendments[0]["classification"] == "INTERNAL"
        assert amendments[0]["expected_scope_version_ref"] == "scope:0"
        assert str(_approval_id) in amendments[0]["authorization_refs"]
        assert amendments[0]["source_result_hash"]
        assert amendments[0]["coverage_map_hash"]
        residual_dependencies = session.execute(
            text("SELECT dependencies FROM task_plan_steps WHERE id = CAST(:id AS uuid)"),
            {"id": str(amendments[0]["step_id"])},
        ).scalar_one()
        assert residual_dependencies == [2]
        session.execute(
            text("UPDATE task_plan_steps SET status = 'BLOCKED' WHERE task_plan_id = CAST(:plan AS uuid) AND sequence = 2"),
            {"plan": plan_id},
        )
        assert loop_service._select_ready_step(session, plan_id) is None
        session.execute(
            text("UPDATE task_plan_steps SET status = 'PASSED' WHERE task_plan_id = CAST(:plan AS uuid) AND sequence = 2"),
            {"plan": plan_id},
        )
        ready_residual = loop_service._select_ready_step(session, plan_id)
        assert ready_residual is not None
        assert str(ready_residual["id"]) == str(amendments[0]["step_id"])
        before_retry = session.execute(
            text("SELECT count(*) FROM task_plan_steps WHERE task_plan_id = CAST(:id AS uuid)"),
            {"id": plan_id},
        ).scalar_one()
        assert before_retry == 3

        replay = reconcile_residual_work(session, loop_id)
        after_retry = session.execute(
            text("SELECT count(*) FROM task_plan_steps WHERE task_plan_id = CAST(:id AS uuid)"),
            {"id": plan_id},
        ).scalar_one()
        assert len(replay) == 1
        assert replay[0]["id"] == amendments[0]["id"]
        assert after_retry == before_retry

        third = _run_iteration(session, loop_id, tmp_path, f"run-{uuid4()}")
        assert third["status"] == "COMPLETED"
        assert third["decision"] == "COMPLETE"

        result_events = session.execute(
            text(
                """
                SELECT payload_json FROM task_loop_events
                WHERE loop_id = CAST(:id AS uuid) AND event_type = 'CAPABILITY_RESULT_NORMALIZED'
                ORDER BY sequence_number
                """
            ),
            {"id": loop_id},
        ).scalars().all()
        normalized_events = [event if isinstance(event, dict) else json.loads(str(event)) for event in result_events]
        result_values = [event["capability_result"] for event in normalized_events]
        assert [value["goal_id"] for value in result_values] == ["goal-a", "goal-b", "goal-b2"]
        assert [value["completed_goals"] or value["partial_goals"] for value in result_values] == [
            ["goal-a"], ["goal-b"], ["goal-b2"]
        ]
        assert all(value["execution_receipt_ref"]["digest"] for value in result_values)
        assert all(value["artifacts"] for value in result_values)
        residual_execution = normalized_events[2]["execution_receipt"]["execution_binding"]
        assert residual_execution["task_id"] == envelope.task_id
        assert residual_execution["goal_id"] == "goal-b2"
        assert residual_execution["residual_id"] == amendments[0]["residual_id"]
        assert residual_execution["project_ref"] == project_id
        assert residual_execution["classification"] == "INTERNAL"
        assert residual_execution["scope_version_ref"] == "scope:0"
        assert residual_execution["authorization_ref"] == _approval_id

        coverage = build_coverage(
            envelope,
            tuple(
                CapabilityResult.model_validate(value)
                for value in result_values
            ),
        )
        assert coverage.by_goal("goal-a").status.value == "COMPLETE"
        assert coverage.by_goal("goal-b").status.value == "PARTIAL"
        assert coverage.by_goal("goal-b2").status.value == "COMPLETE"

        initial_results = tuple(
            CapabilityResult.model_validate(value)
            for value in result_values[:2]
        )
        initial_coverage = build_coverage(envelope, initial_results)
        residual = create_residual_tasks(envelope, initial_coverage, initial_results)[0]
        base_graph = project_work_graph(envelope, initial_results, coverage=initial_coverage, residuals=(residual,))
        lineages = rebuild_p6_10_lineage_projection(
            session,
            task_envelope=envelope,
            plan_id=plan_id,
            loop_id=loop_id,
        )
        residual_lineage = next(item for item in lineages if item.residual_id is not None)
        graph = project_p6_10_lineage(base_graph, lineages)
        assert graph.authority == "PROJECTION"
        assert graph.node(f"plan-step:{residual_lineage.step_id}").status == "PASSED"
        assert any(edge.kind is WorkEdgeKind.RESIDUAL_OF and edge.target_id == "goal-b" for edge in graph.edges)
        residual_artifact_ids = {f"artifact:{artifact['artifact_id']}" for artifact in result_values[2]["artifacts"]}
        assert residual_artifact_ids.issubset({node.node_id for node in graph.nodes})
        assert project_p6_10_lineage(base_graph, lineages).projection_id == graph.projection_id

        counts = session.execute(
            text(
                """
                SELECT
                  (SELECT count(*) FROM task_loop_iterations WHERE loop_id = CAST(:loop_id AS uuid)),
                  (SELECT count(*) FROM worker_jobs WHERE request_id = CAST(:request_id AS uuid)),
                  (SELECT count(*) FROM residual_plan_amendments WHERE loop_id = CAST(:loop_id AS uuid)),
                  (SELECT count(*) FROM goal_runtime_bindings WHERE plan_id = CAST(:plan_id AS uuid))
                """
            ),
            {"loop_id": loop_id, "request_id": request_id, "plan_id": plan_id},
        ).one()
        assert tuple(int(value) for value in counts) == (3, 3, 1, 3)


def test_authority_expansion_persists_waiting_approval_without_appending_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, disposable_session: Session
) -> None:
    monkeypatch.setattr(scope_authority, "read_current_scope_version", lambda _settings, _project_id: 0)
    _install_deterministic_router(monkeypatch, ["COMPLETE", "PARTIAL"])
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with disposable_session as session:
        _project_id, _request_id, _envelope, plan_id, loop_id, _approval_id = _seed_runtime(
            session, workspace, child_capability="MODIFY_CODE"
        )
        _run_iteration(session, loop_id, tmp_path, f"run-{uuid4()}")
        result = _run_iteration(session, loop_id, tmp_path, f"run-{uuid4()}")
        amendment = session.execute(
            text("SELECT * FROM residual_plan_amendments WHERE loop_id = CAST(:id AS uuid)"),
            {"id": loop_id},
        ).mappings().one()
        step_count = session.execute(
            text("SELECT count(*) FROM task_plan_steps WHERE task_plan_id = CAST(:id AS uuid)"),
            {"id": plan_id},
        ).scalar_one()

        assert amendment["authority_decision"] == "REAPPROVAL_REQUIRED"
        assert amendment["step_id"] is None
        assert "CAPABILITY_EXPANSION" in amendment["reason_codes"]
        assert step_count == 2
        assert result["status"] == "WAITING_HUMAN"


def test_scope_version_change_immediately_before_append_requires_reapproval(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, disposable_session: Session
) -> None:
    reads = 0

    def current_scope_version(_settings: object, _project_id: str) -> int:
        nonlocal reads
        reads += 1
        return 1 if reads >= 5 else 0

    monkeypatch.setattr(scope_authority, "read_current_scope_version", current_scope_version)
    _install_deterministic_router(monkeypatch, ["COMPLETE", "PARTIAL"])
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    with disposable_session as session:
        _project_id, _request_id, _envelope, plan_id, loop_id, _approval_id = _seed_runtime(
            session, workspace, child_capability="READ_REPOSITORY"
        )
        _run_iteration(session, loop_id, tmp_path, f"run-{uuid4()}")
        result = _run_iteration(session, loop_id, tmp_path, f"run-{uuid4()}")
        amendment = session.execute(
            text("SELECT * FROM residual_plan_amendments WHERE loop_id = CAST(:id AS uuid)"),
            {"id": loop_id},
        ).mappings().one()
        step_count = session.execute(
            text("SELECT count(*) FROM task_plan_steps WHERE task_plan_id = CAST(:id AS uuid)"),
            {"id": plan_id},
        ).scalar_one()

        assert reads == 5
        assert amendment["authority_decision"] == "REAPPROVAL_REQUIRED"
        assert amendment["step_id"] is None
        assert "SCOPE_VERSION_CHANGED_BEFORE_PLAN_APPEND" in amendment["reason_codes"]
        assert step_count == 2
        assert result["status"] == "WAITING_HUMAN"

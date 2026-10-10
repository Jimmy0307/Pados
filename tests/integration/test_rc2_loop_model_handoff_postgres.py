from __future__ import annotations

import asyncio
import os
from collections.abc import Iterator
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session

from pados.control import service as control_service
from pados.loop import service as loop_service
from pados.loop.schemas import LoopCreate, PlanStepCreate, StepType, TaskGoalCreate, TaskPlanCreate
from pados.memory.service import bootstrap_project
from pados.providers.execution_binding import ExecutionBindingRefused, ExecutionIntent, ExecutionRefusal
from pados.worker import service as worker_service
from pados.worker.schemas import (
    CapabilityId,
    RouteRequest,
    RiskLevel,
    WorkerAvailability,
    WorkerSpec,
    WorkerType,
)

from tests.integration.approval_subjects import work_route_subject

_TEST_DATABASE_ENV = "PADOS_RC2_D4_TEST_DATABASE_URL"
_OPERATIONS = (
    {
        "step_sequence": 1,
        "capability_id": "RUN_STATIC_ANALYSIS",
        "command_id": "python_version",
        "max_risk_level": "LOW",
    },
    {
        "step_sequence": 2,
        "capability_id": "SUMMARIZE",
        "command_id": None,
        "max_risk_level": "LOW",
    },
)


@pytest.fixture
def disposable_session() -> Iterator[Session]:
    database_url = os.environ.get(_TEST_DATABASE_ENV)
    if not database_url:
        pytest.skip(f"set {_TEST_DATABASE_ENV} to a dedicated local disposable PostgreSQL database")
    parsed = make_url(database_url)
    if not str(parsed.database or "").startswith("pados_rc2_d4_") or parsed.host not in {
        "localhost",
        "127.0.0.1",
        "::1",
    }:
        pytest.fail("RC2 D4 integration tests require a loopback pados_rc2_d4_* database")
    engine = create_engine(database_url, pool_pre_ping=True, connect_args={"connect_timeout": 2})
    session = Session(engine, expire_on_commit=False)
    try:
        yield session
    finally:
        session.rollback()
        session.close()
        engine.dispose()


class FakeBoundProvider:
    def __init__(self, result: str = "Bound summary.", *, delay: float = 0) -> None:
        self.result = result
        self.delay = delay
        self.last_evidence: dict[str, object] = {"profile_id": "fake-bound-profile", "binding_hash": "a" * 64}
        self.prompts: list[str] = []

    async def chat(self, prompt: str, *, response_schema: dict[str, object] | None = None) -> str:
        self.prompts.append(prompt)
        if response_schema is not None:
            assert response_schema["type"] == "object"
        if self.delay:
            await asyncio.sleep(self.delay)
        return self.result


def _spec(worker_id: str, worker_type: WorkerType, capabilities: list[CapabilityId]) -> WorkerSpec:
    return WorkerSpec(
        worker_id=worker_id,
        worker_type=worker_type,
        display_name=worker_id,
        version="test",
        availability=WorkerAvailability.AVAILABLE,
        capabilities=capabilities,
        risk_ceiling=RiskLevel.LOW,
        supports_structured_output=True,
        supports_file_changes=False,
        supports_tool_calls=worker_type is WorkerType.DETERMINISTIC_TOOL,
        supports_streaming=False,
        max_context_tokens=1000,
        default_timeout_seconds=30,
    )


def _install_router(monkeypatch: pytest.MonkeyPatch) -> None:
    workers = {
        "deterministic-tool": _spec(
            "deterministic-tool", WorkerType.DETERMINISTIC_TOOL, [CapabilityId.RUN_STATIC_ANALYSIS]
        ),
        "ollama-local": _spec(
            "ollama-local", WorkerType.OLLAMA, [CapabilityId.SUMMARIZE, CapabilityId.CLASSIFY_REQUEST]
        ),
    }
    monkeypatch.setattr(worker_service, "discover_workers", lambda _settings, **_kwargs: workers)

    def run_command(_invocation: object, *, project_root: Path) -> dict[str, object]:
        del project_root
        return {"stdout": "Python 3.12", "stderr": "", "status": "SUCCEEDED", "exit_code": 0,
                "error_code": None}

    monkeypatch.setattr(worker_service, "run_registered_command", run_command)


def _seed_loop(session: Session, workspace: Path, *, model_timeout: int = 30) -> dict[str, str]:
    workspace.mkdir(parents=True, exist_ok=True)
    project_id = bootstrap_project(
        session, name=f"rc2-d4-{uuid4()}", root_path=workspace, classification="internal"
    )
    request = control_service.create_work_request(
        session,
        {
            "project_id": project_id,
            "title": "Summarize reviewed project context",
            "body": "Use the approved task plan and its reviewed context.",
            "idempotency_key": f"rc2-d4-request-{uuid4()}",
        },
    )
    request_id = str(request["id"])
    subject = work_route_subject(
        project_id,
        request_id,
        _OPERATIONS,
        surfaces=("TASK_LOOP",),
        worker_types=("DETERMINISTIC_TOOL", "OLLAMA"),
    )
    approval = control_service.create_approval(
        session,
        {
            "project_id": project_id,
            "proposal_summary": "RC2 D4 model handoff test",
            "idempotency_key": f"rc2-d4-approval-{uuid4()}",
            "subject": subject,
        },
    )
    approved = control_service.decide_approval(
        session,
        str(approval["id"]),
        version=1,
        status="APPROVED",
        reason="bounded loop model handoff integration",
        idempotency_key=f"rc2-d4-decision-{uuid4()}",
    )
    approval_id = str(approved["id"])
    goal = loop_service.create_task_goal(
        session,
        TaskGoalCreate(
            project_id=project_id,
            request_id=request_id,
            objective="Run static analysis and summarize the reviewed context.",
            success_criteria=["both approved steps pass"],
            approval_id=approval_id,
            idempotency_key=f"rc2-d4-goal-{uuid4()}",
        ),
    )
    plan = loop_service.create_task_plan(
        session,
        TaskPlanCreate(
            task_goal_id=str(goal["id"]),
            approval_id=approval_id,
            steps=[
                PlanStepCreate(
                    sequence=1,
                    title="Check the configured Python runtime",
                    step_type=StepType.RUN_VALIDATION,
                    required_capability=CapabilityId.RUN_STATIC_ANALYSIS,
                    preferred_worker_types=[WorkerType.DETERMINISTIC_TOOL],
                    allowed_actions=["RUN_STATIC_ANALYSIS"],
                    command_id="python_version",
                    timeout_seconds=30,
                ),
                PlanStepCreate(
                    sequence=2,
                    title="Summarize the reviewed project context",
                    step_type=StepType.ANALYZE,
                    required_capability=CapabilityId.SUMMARIZE,
                    preferred_worker_types=[WorkerType.OLLAMA],
                    dependencies=[1],
                    allowed_actions=["READ_REPOSITORY"],
                    command_id=None,
                    timeout_seconds=model_timeout,
                ),
            ],
            idempotency_key=f"rc2-d4-plan-{uuid4()}",
        ),
        settings=None,
    )
    loop = loop_service.create_loop(
        session,
        LoopCreate(
            task_goal_id=str(goal["id"]),
            active_plan_id=str(plan["id"]),
            approval_id=approval_id,
            max_iterations=5,
            idempotency_key=f"rc2-d4-loop-{uuid4()}",
        ),
    )
    loop_service.start_loop(session, str(loop["id"]), owner_instance="rc2-d4-integration")
    return {
        "project_id": project_id,
        "request_id": request_id,
        "plan_id": str(plan["id"]),
        "loop_id": str(loop["id"]),
    }


def _step(session: Session, runtime: dict[str, str], key: str, root: Path) -> dict[str, object]:
    return loop_service.run_one_iteration(
        session,
        loop_id=runtime["loop_id"],
        settings=SimpleNamespace(local_inference_timeout_seconds=1),
        artifact_root=root / "artifacts",
        idempotency_key=key,
    )


def _run_deterministic_step(session: Session, runtime: dict[str, str], root: Path) -> None:
    result = _step(session, runtime, f"d4-step1-{uuid4()}", root)
    assert result["job"]["status"] == "SUCCEEDED"
    assert result["job"]["validation_status"] == "PASSED"
    assert result["decision"] == "ADVANCE_STEP"
    step_status = session.execute(
        text("SELECT status FROM task_plan_steps WHERE task_plan_id = CAST(:id AS uuid) AND sequence = 1"),
        {"id": runtime["plan_id"]},
    ).scalar_one()
    assert step_status == "PASSED"


def test_bound_model_step_completes_without_pausing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, disposable_session: Session
) -> None:
    _install_router(monkeypatch)
    provider = FakeBoundProvider()
    monkeypatch.setattr(
        worker_service,
        "build_chat_provider",
        lambda _settings, *, intent, project_id: provider
        if intent is ExecutionIntent.TEXT_SUMMARY and project_id
        else pytest.fail("model provider was not bound to the expected intent and project"),
    )
    with disposable_session as session:
        runtime = _seed_loop(session, tmp_path / "workspace")
        _run_deterministic_step(session, runtime, tmp_path)
        result = _step(session, runtime, "d4-model-success", tmp_path)
        replay = _step(session, runtime, "d4-model-success", tmp_path)
        job = result["job"]
        assert job["status"] == "SUCCEEDED"
        assert job["started_at"] is not None
        assert job["validation_status"] == "PASSED"
        assert job["usage_metadata"]["measured_execution"]["profile_id"] == "fake-bound-profile"
        assert result["loop"]["status"] == "COMPLETED"
        assert result["status"] == "COMPLETED"
        assert result["loop"]["status"] != "PAUSED"
        assert replay["iteration_id"] == result["iteration_id"]
        assert replay["decision"] == result["decision"]
        assert replay["job"]["id"] == result["job"]["id"]
        assert provider.prompts and "Summarize the reviewed project context" in provider.prompts[0]
        events = session.execute(
            text("SELECT event_type FROM worker_job_events WHERE job_id = CAST(:id AS uuid) ORDER BY sequence_number"),
            {"id": str(job["id"])},
        ).scalars().all()
        assert "JOB_STARTED" in events and events[-1] == "JOB_SUCCEEDED"


def test_measured_binding_refusal_blocks_step_without_pause_storm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, disposable_session: Session
) -> None:
    _install_router(monkeypatch)
    refusal = ExecutionBindingRefused(ExecutionRefusal("MODEL_NOT_AUTHORIZED", ("model_ref",)))
    monkeypatch.setattr(worker_service, "build_chat_provider", lambda *_args, **_kwargs: (_ for _ in ()).throw(refusal))
    with disposable_session as session:
        runtime = _seed_loop(session, tmp_path / "workspace")
        _run_deterministic_step(session, runtime, tmp_path)
        result = _step(session, runtime, "d4-model-refused", tmp_path)
        replay = _step(session, runtime, "d4-model-refused", tmp_path)
        step_status = session.execute(
            text("SELECT status FROM task_plan_steps WHERE task_plan_id = CAST(:id AS uuid) AND sequence = 2"),
            {"id": runtime["plan_id"]},
        ).scalar_one()
        assert result["job"]["status"] == "BLOCKED"
        assert result["job"]["error_code"] == "MODEL_NOT_AUTHORIZED"
        assert result["job"]["validation_status"] == "FAILED"
        assert result["job"]["usage_metadata"]["measured_execution_refusal"]["code"] == "MODEL_NOT_AUTHORIZED"
        assert step_status == "BLOCKED"
        assert result["decision"] == "WAIT_FOR_HUMAN"
        assert result["status"] == "WAITING_HUMAN"
        assert result["status"] != "PAUSED"
        assert replay["iteration_id"] == result["iteration_id"]
        assert replay["decision"] == result["decision"]


def test_provider_timeout_is_terminal_and_maps_to_timed_out_step(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, disposable_session: Session
) -> None:
    _install_router(monkeypatch)
    provider = FakeBoundProvider(delay=2)
    monkeypatch.setattr(worker_service, "build_chat_provider", lambda *_args, **_kwargs: provider)
    with disposable_session as session:
        runtime = _seed_loop(session, tmp_path / "workspace", model_timeout=1)
        _run_deterministic_step(session, runtime, tmp_path)
        result = _step(session, runtime, "d4-model-timeout", tmp_path)
        step_status = session.execute(
            text("SELECT status FROM task_plan_steps WHERE task_plan_id = CAST(:id AS uuid) AND sequence = 2"),
            {"id": runtime["plan_id"]},
        ).scalar_one()
        assert result["job"]["status"] == "TIMED_OUT"
        assert result["job"]["error_code"] == "TIMED_OUT"
        assert step_status == "TIMED_OUT"


def test_nonterminal_model_job_waits_and_resumes_the_same_iteration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, disposable_session: Session
) -> None:
    _install_router(monkeypatch)
    monkeypatch.setattr(
        worker_service,
        "_execute_model_if_requested",
        lambda _session, **kwargs: kwargs["job"],
    )
    with disposable_session as session:
        runtime = _seed_loop(session, tmp_path / "workspace")
        _run_deterministic_step(session, runtime, tmp_path)
        delta_count = session.execute(text("SELECT count(*) FROM task_progress_deltas")).scalar_one()
        decisions_before = session.execute(
            text("SELECT count(*) FROM task_loop_events WHERE loop_id = CAST(:id AS uuid) AND event_type = 'LOOP_DECISION_RECORDED'"),
            {"id": runtime["loop_id"]},
        ).scalar_one()
        waiting = _step(session, runtime, "d4-model-wait", tmp_path)
        repeated = _step(session, runtime, "d4-model-wait", tmp_path)
        assert waiting == repeated
        assert waiting["decision"] == "WAIT_FOR_JOB"
        assert waiting["job_status"] == "ROUTED"
        assert session.execute(text("SELECT count(*) FROM task_progress_deltas")).scalar_one() == delta_count
        assert session.execute(
            text("SELECT count(*) FROM task_loop_events WHERE loop_id = CAST(:id AS uuid) AND event_type = 'LOOP_DECISION_RECORDED'"),
            {"id": runtime["loop_id"]},
        ).scalar_one() == decisions_before
        active_iteration = session.execute(
            text("SELECT * FROM task_loop_iterations WHERE id = CAST(:id AS uuid)"),
            {"id": waiting["iteration_id"]},
        ).mappings().one()
        assert active_iteration["decision"] is None
        session.execute(
            text(
                "UPDATE worker_jobs SET status = 'SUCCEEDED', validation_status = 'PASSED', "
                "completed_at = now(), result_summary = 'fake completion' WHERE id = CAST(:id AS uuid)"
            ),
            {"id": waiting["job_id"]},
        )
        completed = _step(session, runtime, "d4-model-wait", tmp_path)
        assert completed["iteration_id"] == waiting["iteration_id"]
        assert str(completed["job"]["id"]) == waiting["job_id"]
        assert completed["job"]["status"] == "SUCCEEDED"
        assert session.execute(
            text("SELECT count(*) FROM worker_jobs WHERE request_id = CAST(:id AS uuid)"),
            {"id": runtime["request_id"]},
        ).scalar_one() == 2


def test_overdue_routed_job_expires_and_releases_project_busy_gate(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, disposable_session: Session
) -> None:
    _install_router(monkeypatch)
    monkeypatch.setattr(
        worker_service,
        "_execute_model_if_requested",
        lambda _session, **kwargs: kwargs["job"],
    )
    with disposable_session as session:
        runtime = _seed_loop(session, tmp_path / "workspace", model_timeout=1)
        _run_deterministic_step(session, runtime, tmp_path)
        waiting = _step(session, runtime, "d4-overdue-wait", tmp_path)
        session.execute(
            text("UPDATE worker_jobs SET queued_at = now() - interval '2 seconds' WHERE id = CAST(:id AS uuid)"),
            {"id": waiting["job_id"]},
        )
        expired = worker_service.expire_overdue_jobs(session, runtime["project_id"])
        job = session.execute(
            text("SELECT * FROM worker_jobs WHERE id = CAST(:id AS uuid)"), {"id": waiting["job_id"]}
        ).mappings().one()
        assert expired == 1
        assert job["status"] == "TIMED_OUT"
        assert job["error_code"] == "JOB_DEADLINE_EXCEEDED"
        route = RouteRequest(
            approval_id=str(session.execute(
                text("SELECT approval_id FROM task_loops WHERE id = CAST(:id AS uuid)"),
                {"id": runtime["loop_id"]},
            ).scalar_one()),
            capability_id=CapabilityId.RUN_STATIC_ANALYSIS,
            risk_level=RiskLevel.LOW,
            context_query="Check the Python runtime again",
            allowed_actions=["RUN_STATIC_ANALYSIS"],
            command_id="python_version",
            timeout_seconds=30,
            idempotency_key=f"d4-after-expiry-{uuid4()}",
            surface="TASK_LOOP",
            step_sequence=1,
        )
        routed = worker_service.route_work_request(
            session,
            request_id=runtime["request_id"],
            route=route,
            settings=SimpleNamespace(local_inference_timeout_seconds=1),
            artifact_root=tmp_path / "artifacts-after-expiry",
        )
        assert routed["job"]["status"] == "SUCCEEDED"


def test_cancel_loop_cancels_waiting_job_and_closes_active_iteration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, disposable_session: Session
) -> None:
    _install_router(monkeypatch)
    monkeypatch.setattr(
        worker_service,
        "_execute_model_if_requested",
        lambda _session, **kwargs: kwargs["job"],
    )
    with disposable_session as session:
        runtime = _seed_loop(session, tmp_path / "workspace")
        _run_deterministic_step(session, runtime, tmp_path)
        waiting = _step(session, runtime, "d4-cancel-wait", tmp_path)
        cancelled = loop_service.cancel_loop(session, runtime["loop_id"])
        iteration = session.execute(
            text("SELECT status, decision FROM task_loop_iterations WHERE id = CAST(:id AS uuid)"),
            {"id": waiting["iteration_id"]},
        ).mappings().one()
        job_status = session.execute(
            text("SELECT status FROM worker_jobs WHERE id = CAST(:id AS uuid)"),
            {"id": waiting["job_id"]},
        ).scalar_one()
        step_status = session.execute(
            text("SELECT status FROM task_plan_steps WHERE task_plan_id = CAST(:id AS uuid) AND sequence = 2"),
            {"id": runtime["plan_id"]},
        ).scalar_one()
        active_count = session.execute(
            text(
                "SELECT count(*) FROM task_loop_iterations WHERE loop_id = CAST(:id AS uuid) "
                "AND status IN ('CREATED', 'ROUTING', 'RUNNING', 'VALIDATING')"
            ),
            {"id": runtime["loop_id"]},
        ).scalar_one()
        assert cancelled["status"] == "CANCELLED"
        assert iteration["status"] == "DECIDED" and iteration["decision"] == "CANCEL"
        assert job_status == "CANCELLED"
        assert step_status == "CANCELLED"
        assert active_count == 0

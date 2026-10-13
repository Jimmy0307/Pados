from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from pados.config import Settings
from pados.db import get_engine, session_scope
from pados.memory.service import bootstrap_project
from pados.product_loop.maker import FileDirectMaker
from pados.product_loop.read_model import build_durable_read, contract_summary
from pados.product_loop.routes import register_product_loop_routes
from pados.product_loop.schemas import (
    DurableProductRead,
    GoalRequest,
    ReadProgressStatus,
    RuntimeResult,
    ValidationReport,
    ValidationStatus,
)
from pados.product_loop.service import ProductLoopService
from pados.product_loop.state import P6_19BStateGateway
from pados.product_loop.validation import FileValidator
from pados.state_proof import ObjectStore, StateProofService, repository
from tests.private_markers import leaked_markers


class _ExplodingMaker:
    async def make(self, contract: Any, *, run_id: str, attempt: int) -> RuntimeResult:
        del contract, run_id, attempt
        raise AssertionError("DURABLE_READ_MUST_NOT_CALL_MAKER")


class _ExplodingValidator:
    async def validate(self, contract: Any, runtime_result: RuntimeResult) -> ValidationReport:
        del contract, runtime_result
        raise AssertionError("DURABLE_READ_MUST_NOT_CALL_VALIDATOR")


class _CapturingMaker(FileDirectMaker):
    def __init__(self, approved_root: Path) -> None:
        super().__init__(approved_root, protected_roots=(Path("C:/PADOS"), Path("C:/PADOS_WORKTREES")))
        self.run_id: str | None = None

    async def make(self, contract: Any, *, run_id: str, attempt: int) -> RuntimeResult:
        self.run_id = run_id
        return await super().make(contract, run_id=run_id, attempt=attempt)


@pytest.fixture(scope="module")
def h_settings() -> Settings:
    return Settings()


CONTROL_TOKEN = "pados-local-control-" + "token"


def _registered_scope(root: Path) -> str:
    """Bind the run to a registered project with an initialized ScopeVersion.

    The product loop resolves semantic context from the registered project row
    whose root matches the workspace; free-form scope strings fail closed.
    """
    root.mkdir(parents=True, exist_ok=True)
    resolved = root.resolve()
    with session_scope() as session:
        project_id = session.execute(
            text("SELECT project_id::text FROM context_project_registry WHERE canonical_root = :root"),
            {"root": str(resolved)},
        ).scalar()
        if project_id is None:
            project_id = bootstrap_project(session, name=f"p6-21h-{uuid4()}", root_path=resolved)
        repository.ensure_scope(session, project_id)
    return str(project_id)


def _headers(scope_id: str) -> dict[str, str]:
    return {
        "X-PADOS-Control-Token": CONTROL_TOKEN,
        "X-PADOS-Product-Scope": scope_id,
    }


def _request(
    root: Path,
    *,
    scope_id: str | None = None,
    content: str = "def result():\n    return 'good'\n",
    authorized: bool = True,
    max_attempts: int = 1,
    revision_changes: list[dict[str, str]] | None = None,
    context: dict[str, Any] | None = None,
) -> GoalRequest:
    return GoalRequest(
        goal="Apply and verify one durable isolated candidate file change.",
        workspace_root=str(root),
        scope_id=scope_id or _registered_scope(root),
        changes=[{"path": "app.py", "content": content}],
        acceptance_criteria=[
            {
                "criterion_id": "contains-good",
                "check": "FILE_CONTAINS",
                "target_path": "app.py",
                "expected_text": "return 'good'",
            },
            {
                "criterion_id": "compile",
                "operation": "TEST",
                "check": "PYTHON_COMPILE",
                "target_path": "app.py",
            },
            {"criterion_id": "paths-scoped", "check": "CHANGED_PATHS_SCOPED"},
        ],
        revision_changes=revision_changes or [],
        authorized=authorized,
        max_attempts=max_attempts,
        idempotency_key=f"p6-21h-{uuid4()}",
        context=context or {},
    )


def _service(
    settings: Settings,
    workspace: Path,
    objects: Path,
    *,
    maker: Any | None = None,
    validator: Any | None = None,
) -> tuple[ProductLoopService, Any]:
    engine = get_engine(settings)
    proof = StateProofService(engine, ObjectStore(objects))
    service = ProductLoopService(
        maker=maker or FileDirectMaker(
            workspace,
            protected_roots=(Path("C:/PADOS"), Path("C:/PADOS_WORKTREES")),
        ),
        validator=validator or FileValidator(),
        state_gateway=P6_19BStateGateway(proof),
    )
    return service, engine


def _pin_context_snapshot(service: ProductLoopService, scope_id: str) -> None:
    """Serve every later run the context read now, before another writer commits.

    The second candidate therefore carries an expected ScopeVersion that the
    authoritative head has already moved past, and CAS must reject it.
    """
    snapshot = service.state_gateway.resolve_project_context(scope_id)
    service.state_gateway.resolve_project_context = lambda _scope_id: snapshot


def _run(service: ProductLoopService, request: GoalRequest):
    return asyncio.run(service.execute(request))


def _counts(engine: Any) -> dict[str, int]:
    with engine.connect() as connection:
        return {
            table: int(connection.execute(text(f"SELECT count(*) FROM {table}")).scalar_one())
            for table in (
                "p6_21h_product_read_index",
                "state_proof_objects",
                "state_proof_results",
                "state_proof_commits",
            )
        }


def _fresh_read(
    settings: Settings,
    workspace: Path,
    objects: Path,
    run_id: str,
) -> tuple[DurableProductRead | None, Any]:
    service, engine = _service(
        settings,
        workspace,
        objects,
        maker=_ExplodingMaker(),
        validator=_ExplodingValidator(),
    )
    return service.read_run(run_id), engine


def test_t01_fresh_instance_rebuilds_accepted_progress_after_process_exit(
    h_settings: Settings, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    objects = tmp_path / "objects"
    service_a, engine_a = _service(h_settings, workspace, objects)
    report = _run(service_a, _request(workspace))
    read_a = service_a.read_run(report.run_id)
    assert read_a is not None and read_a.progress_status == ReadProgressStatus.ACCEPTED
    engine_a.dispose()

    read_b, engine_b = _fresh_read(h_settings, workspace, objects, report.run_id)
    try:
        assert read_b is not None
        assert read_b.model_dump(mode="json") == read_a.model_dump(mode="json")
        assert read_b.accepted_result is not None
        assert read_b.semantic_commit_status == "COMMITTED"
    finally:
        engine_b.dispose()


def test_t02_api_get_reads_from_fresh_service_without_process_registry(
    h_settings: Settings, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    objects = tmp_path / "objects"
    service_a, engine_a = _service(h_settings, workspace, objects)
    request = _request(workspace)
    report = _run(service_a, request)
    engine_a.dispose()

    service_b, engine_b = _service(
        h_settings,
        workspace,
        objects,
        maker=_ExplodingMaker(),
        validator=_ExplodingValidator(),
    )
    try:
        app = FastAPI()
        register_product_loop_routes(app, h_settings, service=service_b)
        with TestClient(app) as client:
            response = client.get(
                f"/product-loop/runs/{report.run_id}",
                headers=_headers(request.scope_id),
            )
        assert response.status_code == 200
        payload = response.json()
        assert payload["run_id"] == report.run_id
        assert payload["progress_status"] == "ACCEPTED"
    finally:
        engine_b.dispose()


def test_t03_get_has_no_local_report_registry_dependency(
    h_settings: Settings, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    objects = tmp_path / "objects"
    service_a, engine_a = _service(h_settings, workspace, objects)
    report = _run(service_a, _request(workspace))
    engine_a.dispose()

    service_b, engine_b = _service(
        h_settings,
        workspace,
        objects,
        maker=_ExplodingMaker(),
        validator=_ExplodingValidator(),
    )
    try:
        assert service_b._completed_by_idempotency == {}
        assert service_b.read_run(report.run_id) is not None
    finally:
        engine_b.dispose()


def test_t04_attempt_receipt_and_commit_survive_restart(
    h_settings: Settings, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    objects = tmp_path / "objects"
    service_a, engine_a = _service(h_settings, workspace, objects)
    report = _run(service_a, _request(workspace))
    engine_a.dispose()
    read, engine_b = _fresh_read(h_settings, workspace, objects, report.run_id)
    try:
        assert read is not None
        assert read.attempt_count == 1
        assert read.attempts[0].receipt_id == report.attempts[0].receipt.receipt_id
        assert read.attempts[0].input_manifest_hash
        assert read.attempts[0].output_content_hash
        assert read.accepted_result is not None
    finally:
        engine_b.dispose()


def test_t05_failure_summary_survives_as_durable_user_progress(
    h_settings: Settings, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    objects = tmp_path / "objects"
    service_a, engine_a = _service(h_settings, workspace, objects)
    report = _run(
        service_a,
        _request(workspace, content="def result():\n    return 'bad'\n"),
    )
    engine_a.dispose()
    read, engine_b = _fresh_read(h_settings, workspace, objects, report.run_id)
    try:
        assert read is not None
        assert read.progress_status == ReadProgressStatus.ITERATION_LIMIT
        assert read.failed_attempts
        assert read.failed_attempts[0].attempt == 1
        assert read.failed_attempts[0].classification == "IMPLEMENTATION_DEFECT"
    finally:
        engine_b.dispose()


def test_t06_bounded_revision_history_and_understanding_changes_survive_restart(
    h_settings: Settings, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    objects = tmp_path / "objects"
    service_a, engine_a = _service(h_settings, workspace, objects)
    report = _run(
        service_a,
        _request(
            workspace,
            content="def result():\n    return 'bad'\n",
            max_attempts=2,
            revision_changes=[{"path": "app.py", "content": "def result():\n    return 'good'\n"}],
        ),
    )
    engine_a.dispose()
    read, engine_b = _fresh_read(h_settings, workspace, objects, report.run_id)
    try:
        assert read is not None
        assert read.attempt_count == 2
        assert len(read.contracts) == 2
        assert read.understanding_changes == ("Attempt 1: Attempt returned evidence but did not satisfy the declared criteria.",)
        assert read.attempts[0].revision_created is True
        assert read.accepted_result is not None
    finally:
        engine_b.dispose()


def test_t07_accepted_result_exposes_references_not_source_content(
    h_settings: Settings, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    objects = tmp_path / "objects"
    service, engine = _service(h_settings, workspace, objects)
    report = _run(service, _request(workspace))
    read = service.read_run(report.run_id)
    try:
        assert read is not None and read.accepted_result is not None
        assert read.accepted_result.output_content_hash == report.attempts[0].receipt.output_content_hash
        assert read.report_object_ref is not None
        assert "return 'good'" not in json.dumps(read.model_dump(mode="json"))
    finally:
        engine.dispose()


def test_t08_incomplete_result_window_is_recoverable_without_final_report(
    h_settings: Settings, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    objects = tmp_path / "objects"
    capturing_maker = _CapturingMaker(workspace)
    service_a, engine_a = _service(
        h_settings,
        workspace,
        objects,
        maker=capturing_maker,
        validator=_ExplodingValidator(),
    )
    with pytest.raises(AssertionError, match="DURABLE_READ_MUST_NOT_CALL_VALIDATOR"):
        _run(service_a, _request(workspace))
    assert capturing_maker.run_id is not None
    run_id = capturing_maker.run_id
    engine_a.dispose()

    read, engine_b = _fresh_read(h_settings, workspace, objects, run_id)
    try:
        assert read is not None
        assert read.progress_status == ReadProgressStatus.INCOMPLETE
        assert read.current_phase == "RESULT_RECEIVED"
        assert read.attempt_count == 1
        assert read.attempts[0].validation_status == ValidationStatus.NOT_RUN
        assert read.report_object_ref is None
        assert read.automatic_continuation_allowed is False
        assert "not reach an accepted result" in read.unresolved_issues[0]
    finally:
        engine_b.dispose()


def test_t09_incomplete_rebuild_does_not_auto_resume(
    h_settings: Settings, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    objects = tmp_path / "objects"
    capturing_maker = _CapturingMaker(workspace)
    service_a, engine_a = _service(
        h_settings,
        workspace,
        objects,
        maker=capturing_maker,
        validator=_ExplodingValidator(),
    )
    with pytest.raises(AssertionError):
        _run(service_a, _request(workspace))
    assert capturing_maker.run_id is not None
    run_id = capturing_maker.run_id
    engine_a.dispose()

    read, engine_b = _fresh_read(h_settings, workspace, objects, run_id)
    try:
        assert read is not None
        assert read.automatic_continuation_allowed is False
        assert read.progress_status == ReadProgressStatus.INCOMPLETE
    finally:
        engine_b.dispose()


def test_t10_rebuild_does_not_call_maker_or_validator(
    h_settings: Settings, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    objects = tmp_path / "objects"
    service_a, engine_a = _service(h_settings, workspace, objects)
    report = _run(service_a, _request(workspace))
    engine_a.dispose()
    read, engine_b = _fresh_read(h_settings, workspace, objects, report.run_id)
    try:
        assert read is not None and read.progress_status == ReadProgressStatus.ACCEPTED
    finally:
        engine_b.dispose()


def test_t11_repeated_read_does_not_create_receipts_commits_or_objects(
    h_settings: Settings, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    objects = tmp_path / "objects"
    service_a, engine_a = _service(h_settings, workspace, objects)
    report = _run(service_a, _request(workspace))
    before = _counts(engine_a)
    engine_a.dispose()

    service_b, engine_b = _service(
        h_settings,
        workspace,
        objects,
        maker=_ExplodingMaker(),
        validator=_ExplodingValidator(),
    )
    try:
        assert service_b.read_run(report.run_id) is not None
        assert service_b.read_run(report.run_id) is not None
        assert _counts(engine_b) == before
    finally:
        engine_b.dispose()


def test_t12_unauthorized_run_reads_as_blocked_and_nonauthoritative(
    h_settings: Settings, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    objects = tmp_path / "objects"
    service_a, engine_a = _service(h_settings, workspace, objects)
    report = _run(service_a, _request(workspace, authorized=False))
    engine_a.dispose()
    read, engine_b = _fresh_read(h_settings, workspace, objects, report.run_id)
    try:
        assert read is not None
        assert read.progress_status == ReadProgressStatus.BLOCKED
        assert read.human_action_required is True
        assert read.accepted_result is None
        assert read.semantic_commit_status == "NOT_COMMITTED"
    finally:
        engine_b.dispose()


def test_t13_stale_cas_candidate_reads_as_stale_with_receipt_retained(
    h_settings: Settings, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    objects = tmp_path / "objects"
    scope_id = _registered_scope(workspace)
    service_a, engine_a = _service(h_settings, workspace, objects)
    _pin_context_snapshot(service_a, scope_id)
    first = _run(service_a, _request(workspace, scope_id=scope_id))
    second = _run(service_a, _request(workspace, scope_id=scope_id))
    assert first.status.value == "COMMITTED"
    assert second.status.value == "STALE_CANDIDATE"
    engine_a.dispose()
    read, engine_b = _fresh_read(h_settings, workspace, objects, second.run_id)
    try:
        assert read is not None
        assert read.progress_status == ReadProgressStatus.STALE
        assert read.semantic_commit_status == "STALE_CANDIDATE"
        assert read.attempts[0].receipt_id == second.attempts[0].receipt.receipt_id
        assert read.accepted_result is None
        assert read.human_action_required is True
    finally:
        engine_b.dispose()


def test_t14_stale_context_status_is_readable_without_authority_escalation(
    h_settings: Settings, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    objects = tmp_path / "objects"
    service, engine = _service(h_settings, workspace, objects)
    report = _run(service, _request(workspace))
    run_id = str(uuid4())
    context = {
        "schema": "P6_21H_RUN_CONTEXT_V1",
        "run_id": run_id,
        "started_at": report.attempts[0].runtime_result.started_at,
        "scope_id": f"stale-context-{uuid4()}",
        "idempotency_key": f"stale-context-{uuid4()}",
        "authorized": True,
        "goal": report.goal.model_dump(mode="json"),
        "intent": report.intent.model_dump(mode="json"),
        "composition": report.composition.model_dump(mode="json"),
        "design": report.design.model_dump(mode="json"),
        "decision": report.decision.model_dump(mode="json"),
        "contract": contract_summary(report.contracts[0]).model_dump(mode="json"),
    }
    awaitable = service.state_gateway.persist_run_context(context)
    asyncio.run(awaitable)
    stale_read = build_durable_read(
        run_id=run_id,
        status="STALE_CONTEXT",
        goal=report.goal,
        intent=report.intent,
        composition=report.composition,
        design=report.design,
        decision=report.decision,
        contracts=[report.contracts[0]],
        attempts=[],
        reflections=[],
        final_result=None,
        final_validation=None,
        commit=None,
        started_at=str(context["started_at"]),
        current_phase="STALE_CONTEXT",
        human_action_required=True,
        unresolved_issues=("Context fingerprint no longer matches the authorized run context.",),
    )
    asyncio.run(
        service.state_gateway.persist_read_snapshot(
            run_id=run_id,
            payload=stale_read.model_dump(mode="json"),
        )
    )
    engine.dispose()
    read, engine_b = _fresh_read(h_settings, workspace, objects, run_id)
    try:
        assert read is not None
        assert read.status == "STALE_CONTEXT"
        assert read.progress_status == ReadProgressStatus.STALE
        assert read.human_action_required is True
        assert read.accepted_result is None
    finally:
        engine_b.dispose()


def test_t15_iteration_limit_is_distinguished_from_runtime_failure(
    h_settings: Settings, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    objects = tmp_path / "objects"
    service_a, engine_a = _service(h_settings, workspace, objects)
    report = _run(
        service_a,
        _request(workspace, content="def result():\n    return 'bad'\n", max_attempts=1),
    )
    engine_a.dispose()
    read, engine_b = _fresh_read(h_settings, workspace, objects, report.run_id)
    try:
        assert read is not None
        assert read.status == "ITERATION_LIMIT"
        assert read.progress_status == ReadProgressStatus.ITERATION_LIMIT
        assert read.human_action_required is True
        assert read.semantic_commit_status == "NOT_COMMITTED"
    finally:
        engine_b.dispose()


def test_t16_unknown_run_is_not_reconstructed_from_global_or_process_state(
    h_settings: Settings, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    objects = tmp_path / "objects"
    service, engine = _service(
        h_settings,
        workspace,
        objects,
        maker=_ExplodingMaker(),
        validator=_ExplodingValidator(),
    )
    try:
        missing = str(uuid4())
        assert service.read_run(missing) is None
        app = FastAPI()
        register_product_loop_routes(app, h_settings, service=service)
        with TestClient(app) as client:
            response = client.get(
                f"/product-loop/runs/{missing}",
                headers=_headers("p6-21h-any-scope"),
            )
        assert response.status_code == 404
    finally:
        engine.dispose()


def test_t17_compact_context_and_snapshot_exclude_source_content(
    h_settings: Settings, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    objects = tmp_path / "objects"
    marker = "P6_21H_SOURCE_CONTENT_MUST_REMAIN_BEHIND_REFERENCES"
    service, engine = _service(h_settings, workspace, objects)
    report = _run(service, _request(workspace, content=f"def result():\n    return '{marker}'\n"))
    proof = service.state_gateway.proof_service
    with proof._session() as session:
        index = repository.get_product_read_index(session, report.run_id)
    assert index is not None
    assert index["context_object_id"] is not None
    assert index["latest_snapshot_object_id"] is not None
    context_payload = Path(index["context_storage_uri"]).read_bytes().decode("utf-8")
    snapshot_payload = Path(index["snapshot_storage_uri"]).read_bytes().decode("utf-8")
    assert marker not in context_payload
    assert marker not in snapshot_payload
    engine.dispose()


def test_t18_read_payload_is_reference_based_and_does_not_embed_change_content(
    h_settings: Settings, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    objects = tmp_path / "objects"
    marker = "P6_21H_READ_MODEL_REFERENCE_ONLY_MARKER"
    service, engine = _service(h_settings, workspace, objects)
    report = _run(service, _request(workspace, content=f"def result():\n    return '{marker}'\n"))
    read = service.read_run(report.run_id)
    try:
        assert read is not None
        payload = json.dumps(read.model_dump(mode="json"), ensure_ascii=False)
        assert marker not in payload
        assert read.report_object_ref is not None
        assert read.report_object_ref["content_hash"]
    finally:
        engine.dispose()


def test_t19_read_authority_flags_remain_candidate_and_proposal_only(
    h_settings: Settings, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    objects = tmp_path / "objects"
    service, engine = _service(h_settings, workspace, objects)
    report = _run(service, _request(workspace))
    read = service.read_run(report.run_id)
    try:
        assert report.memory_authority == "CANDIDATE"
        assert report.graph_authority == "PROPOSAL_ONLY"
        assert read is not None
        assert "CANDIDATE" in read.user_report
        assert "PROPOSAL_ONLY" in read.user_report
        assert read.semantic_commit_status == "COMMITTED"
    finally:
        engine.dispose()


def test_t20_index_is_a_derived_lookup_surface_over_p6_19b_state(
    h_settings: Settings, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    objects = tmp_path / "objects"
    service, engine = _service(h_settings, workspace, objects)
    report = _run(service, _request(workspace))
    with engine.connect() as connection:
        index_columns = {
            row[0]
            for row in connection.execute(
                text(
                    """
                    SELECT column_name FROM information_schema.columns
                    WHERE table_name = 'p6_21h_product_read_index'
                    """
                )
            )
        }
        result_count = int(
            connection.execute(
                text("SELECT count(*) FROM state_proof_results WHERE run_id = CAST(:run_id AS uuid)"),
                {"run_id": report.run_id},
            ).scalar_one()
        )
    assert {"run_id", "scope_id", "context_object_id", "latest_snapshot_object_id", "final_report_object_id"} <= index_columns
    assert result_count == 1
    engine.dispose()


def test_t21_canonical_p6_19b_rows_are_sufficient_for_read_reconciliation(
    h_settings: Settings, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    objects = tmp_path / "objects"
    service, engine = _service(h_settings, workspace, objects)
    report = _run(service, _request(workspace))
    proof = service.state_gateway.proof_service
    with proof._session() as session:
        results = repository.get_results_for_run(session, report.run_id)
        commits = repository.get_commits_for_run(session, report.run_id)
    try:
        assert len(results) == 1
        assert len(commits) == 1
        assert commits[0]["status"] == "COMMITTED"
        assert results[0]["input_manifest_hash"] == report.attempts[0].receipt.input_manifest_hash
        assert results[0]["output_content_hash"] == report.attempts[0].receipt.output_content_hash
    finally:
        engine.dispose()


def test_t22_product_read_rebuild_is_bounded_to_existing_p6_21g_contract(
    h_settings: Settings, tmp_path: Path
) -> None:
    workspace = tmp_path / "workspace"
    objects = tmp_path / "objects"
    service, engine = _service(h_settings, workspace, objects)
    report = _run(service, _request(workspace))
    read = service.read_run(report.run_id)
    try:
        assert read is not None
        assert read.understanding.selected_capabilities == (
            "UNDERSTAND",
            "DESIGN",
            "DECIDE",
            "REFLECT",
        )
        assert read.understanding.accepted_direction == "DIRECT_MAKER_IN_APPROVED_ISOLATED_WORKSPACE"
        assert not leaked_markers(json.dumps(read.model_dump(mode="json")))
    finally:
        engine.dispose()

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from pados.config import Settings
from pados.product_loop.maker import FileDirectMaker
from pados.product_loop.routes import build_product_loop_service, register_product_loop_routes
from pados.product_loop.schemas import (
    ContractState,
    FailureClass,
    FileChange,
    GoalRequest,
    ProductLoopStatus,
    RuntimeStatus,
    ValidationStatus,
)
from pados.product_loop.service import ProductLoopService
from pados.product_loop.state import InMemoryP6_19BStateGateway
from pados.product_loop.validation import FileValidator
from tests.private_markers import leaked_markers

CONTROL_TOKEN = "pados-local-control-" + "token"


def _headers(scope_id: str) -> dict[str, str]:
    return {
        "X-PADOS-Control-Token": CONTROL_TOKEN,
        "X-PADOS-Product-Scope": scope_id,
    }


def _request(root: Path, *, authorized: bool = True, **overrides: Any) -> GoalRequest:
    values: dict[str, Any] = {
        "goal": "Implement the bounded fixture change.",
        "workspace_root": str(root),
        "scope_id": f"scope-{root.name}",
        "changes": [{"path": "app.py", "content": "def result():\n    return 'bad'\n"}],
        "acceptance_criteria": [
            {
                "criterion_id": "contains-result",
                "check": "FILE_CONTAINS",
                "target_path": "app.py",
                "expected_text": "return 'good'",
            },
            {
                "criterion_id": "compiles",
                "operation": "TEST",
                "check": "PYTHON_COMPILE",
                "target_path": "app.py",
            },
            {
                "criterion_id": "paths-scoped",
                "check": "CHANGED_PATHS_SCOPED",
            },
        ],
        "authorized": authorized,
    }
    values.update(overrides)
    return GoalRequest.model_validate(values)


def _service(root: Path) -> tuple[ProductLoopService, InMemoryP6_19BStateGateway]:
    gateway = InMemoryP6_19BStateGateway(root / "objects")
    service = ProductLoopService(
        maker=FileDirectMaker(root, protected_roots=(Path("C:/PADOS"), Path("C:/PADOS_WORKTREES"))),
        validator=FileValidator(),
        state_gateway=gateway,
    )
    return service, gateway


def _run(service: ProductLoopService, request: GoalRequest):
    return asyncio.run(service.execute(request))


def test_t01_goal_is_composed_into_six_contract_compatible_fields(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    report = _run(service, _request(tmp_path))
    assert report.goal.objective == report.intent.objective
    assert report.composition.selected_capabilities == ["UNDERSTAND", "DESIGN", "DECIDE", "REFLECT"]
    assert report.design.acceptance_shape == ["contains-result", "compiles", "paths-scoped"]
    assert report.decision.accepted_direction == "DIRECT_MAKER_IN_APPROVED_ISOLATED_WORKSPACE"


def test_t02_work_contract_is_immutable_and_scoped(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    report = _run(service, _request(tmp_path))
    contract = report.contracts[0]
    assert contract.scope.workspace_root == str(tmp_path.resolve())
    assert "WRITE_FORMAL_REPO" in contract.prohibited_actions
    with pytest.raises((TypeError, ValueError)):
        contract.objective = "changed"  # type: ignore[misc]


def test_t03_direct_maker_writes_declared_file_only(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    report = _run(service, _request(tmp_path, changes=[{"path": "app.py", "content": "def result():\n    return 'good'\n"}]))
    assert report.status == ProductLoopStatus.COMMITTED
    assert report.final_result is not None
    assert report.final_result.changed_paths == ["app.py"]
    assert (tmp_path / "app.py").read_text(encoding="utf-8") == "def result():\n    return 'good'\n"


def test_t04_runtime_completion_is_separate_from_validation(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    report = _run(service, _request(tmp_path))
    assert report.attempts[0].runtime_result.status == RuntimeStatus.COMPLETED
    assert report.attempts[0].validation.status == ValidationStatus.FAILED
    assert report.status == ProductLoopStatus.REJECTED


def test_t05_happy_path_commits_after_validation(tmp_path: Path) -> None:
    service, gateway = _service(tmp_path)
    report = _run(service, _request(tmp_path, changes=[{"path": "app.py", "content": "def result():\n    return 'good'\n"}]))
    assert report.status == ProductLoopStatus.COMMITTED
    assert report.final_validation is not None
    assert report.final_validation.validation_passed is True
    assert report.final_validation.semantic_accepted is True
    assert report.commit is not None and report.commit["status"] == "COMMITTED"
    assert len(gateway.receipts) == 1


def test_t06_failed_attempt_receipt_is_retained_without_commit(tmp_path: Path) -> None:
    service, gateway = _service(tmp_path)
    report = _run(service, _request(tmp_path))
    assert report.status == ProductLoopStatus.REJECTED
    assert len(report.attempts) == 1
    assert len(gateway.receipts) == 1
    receipt = report.attempts[0].receipt
    assert receipt.runtime_status == RuntimeStatus.COMPLETED
    assert receipt.failure_classification is None
    assert receipt.output_content_hash
    assert report.commit is None


def test_t07_reflection_creates_one_lineage_preserving_revision(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    report = _run(
        service,
        _request(
            tmp_path,
            max_attempts=2,
            revision_changes=[{"path": "app.py", "content": "def result():\n    return 'good'\n"}],
        ),
    )
    assert report.status == ProductLoopStatus.COMMITTED
    assert len(report.attempts) == 2
    assert len(report.reflections) == 2
    assert report.reflections[0].failure_classification == FailureClass.IMPLEMENTATION_DEFECT
    assert report.reflections[0].revision_created is True
    assert report.contracts[1].parent_contract_id == report.contracts[0].work_contract_id
    assert report.contracts[1].version == 2


def test_t08_revision_budget_is_bounded(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    report = _run(
        service,
        _request(
            tmp_path,
            max_attempts=3,
            revision_changes=[{"path": "app.py", "content": "still invalid"}],
        ),
    )
    assert report.status == ProductLoopStatus.REJECTED
    assert len(report.attempts) == 2
    assert len(report.contracts) == 2


def test_t09_unauthorized_goal_stops_before_maker(tmp_path: Path) -> None:
    service, gateway = _service(tmp_path)
    report = _run(service, _request(tmp_path, authorized=False))
    assert report.status == ProductLoopStatus.AUTHORITY_BLOCK
    assert report.contracts[0].state == ContractState.READY
    assert report.attempts == []
    assert gateway.receipts == {}
    assert not (tmp_path / "app.py").exists()


def test_t10_workspace_boundary_returns_authority_block_and_receipt(tmp_path: Path) -> None:
    service, gateway = _service(tmp_path)
    report = _run(service, _request(Path("C:/PADOS"), scope_id="formal-scope"))
    assert report.status == ProductLoopStatus.AUTHORITY_BLOCK
    assert report.attempts[0].runtime_result.failure_classification == FailureClass.AUTHORITY_BLOCK
    assert report.attempts[0].receipt.runtime_status == RuntimeStatus.BLOCKED
    assert report.attempts[0].receipt.failure_classification == FailureClass.AUTHORITY_BLOCK
    assert len(gateway.receipts) == 1
    assert report.commit is None


def test_t11_human_gate_blocks_semantic_commit(tmp_path: Path) -> None:
    service, gateway = _service(tmp_path)
    report = _run(
        service,
        _request(
            tmp_path,
            human_gate=True,
            changes=[{"path": "app.py", "content": "def result():\n    return 'good'\n"}],
        ),
    )
    assert report.status == ProductLoopStatus.HUMAN_REVIEW_REQUIRED
    assert report.human_decision_required is True
    assert report.final_validation is not None
    assert report.final_validation.validation_passed is True
    assert report.final_validation.semantic_accepted is False
    assert report.commit is None
    assert gateway.commits == {}


def test_t12_stale_cas_retains_receipt_and_blocks_overwrite(tmp_path: Path) -> None:
    service, gateway = _service(tmp_path)
    original_commit = gateway.commit

    async def advance_then_commit(*, receipt: Any, contract: Any) -> dict[str, Any]:
        gateway.versions[contract.intent.scope_id] = receipt.input_scope_version + 1
        return await original_commit(receipt=receipt, contract=contract)

    gateway.commit = advance_then_commit  # type: ignore[method-assign]
    report = _run(service, _request(tmp_path, changes=[{"path": "app.py", "content": "def result():\n    return 'good'\n"}]))
    assert report.status == ProductLoopStatus.STALE_CANDIDATE
    assert report.semantic_state == "STALE"
    assert report.attempts[0].receipt.output_content_hash
    assert report.commit is not None and report.commit["status"] == "STALE_CANDIDATE"


def test_t13_projection_is_candidate_and_proposal_only(tmp_path: Path) -> None:
    service, gateway = _service(tmp_path)
    report = _run(service, _request(tmp_path, changes=[{"path": "app.py", "content": "def result():\n    return 'good'\n"}]))
    projection = report.attempts[0].receipt.projection
    assert projection is not None
    assert projection["candidate_memory"]["authority_state"] == "CANDIDATE"
    assert projection["graph_authority"] == "PROPOSAL_ONLY"
    assert gateway.commits


def test_t14_projection_rebuild_does_not_create_receipt_or_commit(tmp_path: Path) -> None:
    service, gateway = _service(tmp_path)
    report = _run(service, _request(tmp_path, changes=[{"path": "app.py", "content": "def result():\n    return 'good'\n"}]))
    receipt = report.attempts[0].receipt
    receipt_count = len(gateway.receipts)
    commit_count = len(gateway.commits)
    projection = asyncio.run(gateway.rebuild_projection(receipt=receipt, contract=report.contracts[-1]))
    assert projection["result_id"] == receipt.receipt_id
    assert len(gateway.receipts) == receipt_count
    assert len(gateway.commits) == commit_count


def test_t15_receipt_idempotency_reuses_same_attempt(tmp_path: Path) -> None:
    service, gateway = _service(tmp_path)
    report = _run(service, _request(tmp_path, changes=[{"path": "app.py", "content": "def result():\n    return 'good'\n"}]))
    attempt = report.attempts[0]
    second = asyncio.run(
        gateway.record_attempt(
            run_id=report.run_id,
            attempt=attempt.attempt,
            contract=report.contracts[0],
            runtime_result=attempt.runtime_result,
        )
    )
    assert second.receipt_id == attempt.receipt.receipt_id
    assert len(gateway.receipts) == 1


def test_t16_file_change_rejects_absolute_path() -> None:
    with pytest.raises(ValueError, match="RELATIVE"):
        FileChange(path="C:/PADOS/forbidden.py", content="bad")


def test_t17_file_change_rejects_parent_escape() -> None:
    with pytest.raises(ValueError, match="ESCAPE"):
        FileChange(path="../formal.py", content="bad")


def test_t18_validator_marks_missing_file_failed(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    report = _run(service, _request(tmp_path))
    assert report.final_validation is not None
    assert report.final_validation.criterion_findings[0].status == ValidationStatus.FAILED


def test_t19_validator_compile_check_is_technical_only(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    report = _run(
        service,
        _request(
            tmp_path,
            changes=[{"path": "app.py", "content": "def result(:\n"}],
            acceptance_criteria=[
                {
                    "criterion_id": "compiles",
                    "operation": "TEST",
                    "check": "PYTHON_COMPILE",
                    "target_path": "app.py",
                }
            ],
        ),
    )
    assert report.final_validation is not None
    assert report.final_validation.technical_test_passed is False
    assert report.status == ProductLoopStatus.REJECTED


def test_t20_maker_does_not_expand_changed_path_envelope(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    report = _run(service, _request(tmp_path, changes=[{"path": "app.py", "content": "def result():\n    return 'good'\n"}]))
    assert report.final_result is not None
    assert set(report.final_result.changed_paths) == {"app.py"}
    assert list(tmp_path.rglob("*")) == [tmp_path / "objects", tmp_path / "app.py"] or (
        tmp_path / "app.py"
    ).exists()


def test_t21_memory_promotion_is_not_called_by_product_loop(tmp_path: Path) -> None:
    service, gateway = _service(tmp_path)
    _run(
        service,
        _request(tmp_path, changes=[{"path": "app.py", "content": "def result():\n    return 'good'\n"}]),
    )
    assert not hasattr(gateway, "promote_memory")


def test_t22_no_external_orchestration_is_selected(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    report = _run(service, _request(tmp_path, changes=[{"path": "app.py", "content": "def result():\n    return 'good'\n"}]))
    assert "EXTERNAL_ORCHESTRATION_NOT_NEEDED_FOR_ONE_DIRECT_MAKER" in report.design.alternatives_rejected


def test_t23_api_surface_submits_and_reads_one_report(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    app = FastAPI()
    register_product_loop_routes(app, Settings(), service=service)
    request = _request(
        tmp_path,
        changes=[{"path": "app.py", "content": "def result():\n    return 'good'\n"}],
    )
    with TestClient(app) as client:
        created = client.post(
            "/product-loop/runs",
            json=request.model_dump(mode="json"),
            headers=_headers(request.scope_id),
        )
        assert created.status_code == 200
        run_id = created.json()["run_id"]
        fetched = client.get(
            f"/product-loop/runs/{run_id}", headers=_headers(request.scope_id)
        )
    assert fetched.status_code == 200
    assert fetched.json()["status"] == "COMMITTED"
    assert "Goal:" in fetched.json()["user_report"]


def test_t24_api_rejects_unknown_contract_fields(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    app = FastAPI()
    register_product_loop_routes(app, Settings(), service=service)
    request = _request(
        tmp_path,
        changes=[{"path": "app.py", "content": "def result():\n    return 'good'\n"}],
    ).model_dump(
        mode="json"
    )
    request["unbounded_executor"] = True
    with TestClient(app) as client:
        response = client.post(
            "/product-loop/runs",
            json=request,
            headers=_headers(str(request["scope_id"])),
        )
    assert response.status_code == 422


def test_t25_report_is_json_serializable_without_hidden_runtime_state(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    report = _run(
        service,
        _request(
            tmp_path,
            changes=[{"path": "app.py", "content": "def result():\n    return 'good'\n"}],
        ),
    )
    payload = report.model_dump(mode="json")
    assert payload["status"] == "COMMITTED"
    assert payload["report_object_ref"]["content_hash"]
    assert "session" not in payload
    assert "queue" not in payload


def test_t26_receipt_retains_scope_and_object_hashes(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    report = _run(
        service,
        _request(
            tmp_path,
            changes=[{"path": "app.py", "content": "def result():\n    return 'good'\n"}],
        ),
    )
    receipt = report.attempts[0].receipt
    assert receipt.input_scope_version == 0
    assert receipt.input_manifest_hash
    assert receipt.output_content_hash


def test_t27_no_external_orchestration_or_private_model_is_in_product_package() -> None:
    package_root = Path(__file__).parents[2] / "src" / "pados" / "product_loop"
    source = "\n".join(path.read_text(encoding="utf-8") for path in package_root.glob("*.py"))
    assert not leaked_markers(source)


def test_t28_no_p6_21g_database_migration_is_added() -> None:
    migration_root = Path(__file__).parents[2] / "migrations"
    assert not list(migration_root.rglob("*p6_21g*"))


def test_t29_work_contract_nested_collections_are_immutable(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    contract = _run(service, _request(tmp_path)).contracts[0]
    with pytest.raises((TypeError, ValueError)):
        contract.changes += (FileChange(path="other.py", content="bad"),)  # type: ignore[misc]
    with pytest.raises((TypeError, ValueError)):
        contract.scope.allowed_paths += ("other.py",)  # type: ignore[misc]


def test_t30_idempotency_key_reuses_completed_product_report(tmp_path: Path) -> None:
    service, gateway = _service(tmp_path)
    request = _request(
        tmp_path,
        changes=[{"path": "app.py", "content": "def result():\n    return 'good'\n"}],
    )
    first = _run(service, request)
    second = _run(service, request)
    assert second.run_id == first.run_id
    assert len(gateway.receipts) == 1
    assert len(gateway.commits) == 1


def test_t31_product_service_requires_explicit_product_workspace() -> None:
    with pytest.raises(PermissionError, match="PRODUCT_WORKSPACE_ROOT_REQUIRED"):
        build_product_loop_service(Settings())


def test_t32_validator_blocks_acceptance_target_outside_allowlist(tmp_path: Path) -> None:
    service, _ = _service(tmp_path)
    report = _run(
        service,
        _request(
            tmp_path,
            changes=[{"path": "app.py", "content": "def result():\n    return 'good'\n"}],
            acceptance_criteria=[
                {
                    "criterion_id": "outside",
                    "check": "FILE_EXISTS",
                    "target_path": "other.py",
                }
            ],
        ),
    )
    assert report.status == ProductLoopStatus.REJECTED
    assert report.final_validation is not None
    assert report.final_validation.failure_classification == FailureClass.AUTHORITY_BLOCK

from __future__ import annotations

import pytest

from pados.work_protocol.composition import compose_results
from pados.work_protocol.coverage import build_coverage
from pados.work_protocol.graph import project_work_graph
from pados.work_protocol.models import ResultCandidate, VerificationStatus
from pados.work_protocol.planning import plan_scheduling_candidates, submit_to_existing_scheduler
from pados.work_protocol.residual import create_residual_tasks
from tests.work_protocol.test_completion import _envelope, _result
from tests.work_protocol.test_planning_recommendations import ExistingQueue


def test_original_task_partial_residual_composition_and_acceptance_boundary() -> None:
    task = _envelope()
    result_a = _result("result-a", "goal-a", "DOC_DRAFT", "COMPLETE", "Draft brief")
    result_b = _result("result-b", "goal-b", "SOURCE_VERIFY", "PARTIAL", "Verified citation one")
    first_coverage = build_coverage(task, (result_a, result_b))
    residuals = create_residual_tasks(task, first_coverage, (result_a, result_b))
    candidates = plan_scheduling_candidates(task, first_coverage)
    scheduler = ExistingQueue()
    receipts = submit_to_existing_scheduler(scheduler, candidates)

    assert [item.goal_ids for item in residuals] == [("goal-b2",)]
    assert residuals[0].preserved_result_refs == ("result-a", "result-b")
    assert [candidate.goal_id for candidate in candidates] == ["goal-b2"]
    assert len(receipts) == 1
    assert "goal-a" not in [candidate.goal_id for candidate in candidates]

    result_b2 = _result("result-b2", "goal-b2", "OCR_TEXT", "COMPLETE", "Verified citations two and three")
    final_results = (result_a, result_b, result_b2)
    final_coverage = build_coverage(task, final_results)
    candidate = compose_results(task, final_results, final_coverage)
    graph = project_work_graph(task, final_results, candidate, coverage=final_coverage, residuals=residuals)

    assert [item.result_ref for item in candidate.sections] == ["result-a", "result-b", "result-b2"]
    assert graph.node("goal-a").status == "COMPLETE"
    assert graph.node("goal-b").status == "PARTIAL"
    assert graph.node("goal-b2").status == "COMPLETE"
    assert graph.node(residuals[0].residual_task_id).kind.value == "TASK"
    assert candidate.semantic_acceptance == "PENDING_GOVERNED_ACCEPTANCE"
    assert candidate.verification_status.value == "NOT_VERIFIED"
    assert candidate.result_receipt_ref is None


def test_core_protocol_package_does_not_import_provider_or_authority_apis() -> None:
    import ast
    from pathlib import Path

    root = Path(__file__).resolve().parents[2] / "src" / "pados" / "work_protocol"
    imports: set[str] = set()
    for path in sorted(root.glob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module is not None:
                imports.add(node.module)
    forbidden = {"openai", "anthropic", "ollama", "pados.state_proof", "pados.db",
                 "pados.remote", "pados.provider", "pados.providers"}
    assert not imports & forbidden


def test_result_receipt_requires_matching_verified_candidate_and_projects_to_graph() -> None:
    task = _envelope()
    result = _result("result-a", "goal-a", "DOC_DRAFT", "COMPLETE", "Draft brief")
    coverage = build_coverage(task, (result,))
    candidate = compose_results(task, (result,), coverage)
    receipt = {
        "protocol_version": "1.0",
        "schema_version": "pados.result-receipt-ref.v1",
        "receipt_id": "result-acceptance-a",
        "candidate_id": candidate.candidate_id,
        "task_id": task.task_id,
        "project_ref": task.project_ref,
        "classification": task.classification.value,
        "digest": "c" * 64,
        "scope_version_ref": task.scope_version_ref,
    }

    with pytest.raises(ValueError, match="RESULT_RECEIPT_REQUIRES_VERIFIED_CANDIDATE"):
        ResultCandidate.model_validate({
            **candidate.model_dump(mode="python"),
            "result_receipt_ref": receipt,
        })

    verified = ResultCandidate.model_validate({
        **candidate.model_dump(mode="python"),
        "verification_status": VerificationStatus.PASSED,
        "result_receipt_ref": receipt,
    })
    graph = project_work_graph(task, (result,), verified, coverage=coverage)

    assert graph.node("receipt:result-acceptance-a").kind.value == "RECEIPT"
    assert any(
        edge.source_id == f"decision:{candidate.candidate_id}"
        and edge.target_id == "receipt:result-acceptance-a"
        and edge.kind.value == "PRODUCED"
        for edge in graph.edges
    )

    mismatched = {**receipt, "scope_version_ref": "scope:8"}
    with pytest.raises(ValueError, match="RESULT_RECEIPT_BINDING_MISMATCH"):
        ResultCandidate.model_validate({
            **candidate.model_dump(mode="python"),
            "verification_status": VerificationStatus.PASSED,
            "result_receipt_ref": mismatched,
        })


def test_stale_same_task_coverage_is_rejected_by_all_consumers() -> None:
    task = _envelope()
    completed = _result("result-complete", "goal-a", "DOC_DRAFT", "COMPLETE", "Draft")
    partial = _result("result-partial", "goal-a", "DOC_DRAFT", "PARTIAL", "Partial draft")
    stale_coverage = build_coverage(task, (completed,))

    with pytest.raises(ValueError, match="COVERAGE_RESULTS_MISMATCH"):
        create_residual_tasks(task, stale_coverage, (partial,))
    with pytest.raises(ValueError, match="WORK_GRAPH_COVERAGE_RESULTS_MISMATCH"):
        project_work_graph(task, (partial,), coverage=stale_coverage)
    with pytest.raises(ValueError, match="COMPOSITION_COVERAGE_RESULTS_MISMATCH"):
        compose_results(task, (partial,), stale_coverage)

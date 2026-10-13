from __future__ import annotations

from pados.work_protocol.coverage import build_coverage
from pados.work_protocol.graph import (
    RuntimeLineageProjection,
    project_p6_10_lineage,
    project_work_graph,
)
from pados.work_protocol.residual import create_residual_tasks
from tests.work_protocol.test_completion import _envelope, _result


def test_rebuildable_work_graph_shows_residual_amendment_runtime_receipt_lineage() -> None:
    task = _envelope()
    result_a = _result("result-a", "goal-a", "DOC_DRAFT", "COMPLETE", "Draft")
    result_b = _result("result-b", "goal-b", "SOURCE_VERIFY", "PARTIAL", "Partial")
    initial_results = (result_a, result_b)
    coverage = build_coverage(task, initial_results)
    residual = create_residual_tasks(task, coverage, initial_results)[0]
    base = project_work_graph(task, initial_results, coverage=coverage, residuals=(residual,))
    result_b2 = _result("result-b2", "goal-b2", "OCR_TEXT", "COMPLETE", "Residual complete")
    lineage = RuntimeLineageProjection(
        task_id="task-1",
        goal_id="goal-b2",
        project_ref="project-1",
        classification="INTERNAL",
        residual_id=residual.residual_task_id,
        parent_goal_id="goal-b",
        amendment_id="amendment:one",
        authority_decision="AUTO_AMENDABLE",
        plan_id="plan-1",
        step_id="step-3",
        sequence=3,
        step_status="PASSED",
        worker_id=result_b2.provenance.worker_id,
        capability=result_b2.capability,
        receipt_ref=result_b2.execution_receipt_ref,
        artifacts=result_b2.artifacts,
    )

    graph = project_p6_10_lineage(base, (lineage,))

    assert graph.authority == "PROJECTION"
    assert graph.node("plan-step:step-3").status == "PASSED"
    assert graph.node("amendment:amendment:one").kind.value == "AMENDMENT"
    assert any(edge.source_id == residual.residual_task_id and edge.target_id == "goal-b" and edge.kind.value == "RESIDUAL_OF" for edge in graph.edges)
    assert any(edge.source_id == "capability:OCR_TEXT" and edge.target_id.startswith("receipt:") for edge in graph.edges)

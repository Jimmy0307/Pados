"""Requirement 1: every stage is a registered, authorized, policy-evaluated capability run by the EXISTING
fabric executor (plan reproduction CF-01, trusted input provenance CF-02, guarded invocation CF-03)."""
from __future__ import annotations

import dataclasses
import inspect
from pathlib import Path

import pytest
from pipeline_support import STAGE_IDS, pipeline, spy_adapters

from pados.capability.executor import NOT_EXECUTED, ExecutionRefused, ExecutionStatus
from pados.capability.planner import PlanStatus, plan_document
from pados.capability.policy import PolicyCode
from pados.enterprise.classification import DataClassification
from pados.execution.context import canonical_hash

EXPECTED = {
    "preprocess": ("DOCUMENT_PREPROCESS", "ocr.preprocess.reference"),
    "recognize": ("DOCUMENT_OCR", "ocr.pdf-text-layer"),
    "layout": ("DOCUMENT_LAYOUT_ANALYSIS", "ocr.layout.line-grouping"),
    "tables": ("TABLE_EXTRACTION", "ocr.table.whitespace-aligned"),
    "structure": ("DOCUMENT_STRUCTURE_PARSE", "ocr.structure.heuristic"),
    "extract": ("STRUCTURED_EXTRACTION", "extraction.table-dataset"),
    "retrieve": ("PASSAGE_RETRIEVAL", "retrieval.lexical-bm25"),
    "analyze": ("ROUGH_SET_RULE_LEARNING", "algorithms.drsa.domlem"),
    "explain": ("RESULT_EXPLANATION_TEMPLATE", "algorithms.explain.template"),
}
# Every stage's selected adapter is DETERMINISTIC, so the fabric's DETERMINISTIC_REPLAY re-runs each once.
REPLAYED = {"preprocess", "recognize", "layout", "tables", "structure", "extract", "retrieve", "analyze", "explain"}


def test_every_stage_is_planned_and_executed_by_the_fabric(tmp_path: Path) -> None:
    fab = pipeline(tmp_path)
    task, plan, result = fab.run()
    assert plan.status is PlanStatus.FEASIBLE and plan.stage_ids == STAGE_IDS
    for stage in plan.stages:
        capability_id, adapter_id = EXPECTED[stage.stage_id]
        assert (stage.capability_id, stage.selected_adapter_id) == (capability_id, adapter_id)
        assert stage.decision_hash is not None and stage.alternatives == ()
        selected = next(c for c in stage.candidates if c.adapter_id == adapter_id)
        assert selected.code == PolicyCode.ALLOWED.value and selected.decision_hash == stage.decision_hash
    assert result.status is ExecutionStatus.COMPLETED and result.failure_code is None
    receipts = result.plan_receipt.stages
    assert tuple(r.stage_id for r in receipts) == STAGE_IDS
    for receipt, stage in zip(receipts, plan.stages, strict=True):
        assert (receipt.capability_id, receipt.adapter_id) == EXPECTED[stage.stage_id]
        assert receipt.policy_decision_hash == stage.decision_hash and receipt.failure_code is None
        assert receipt.outputs and receipt.attempt_id == f"run00001.{stage.stage_id}.1"
    assert [(a.stage_id, a.adapter_id, a.failure_code) for a in result.attempts] == [
        (sid, EXPECTED[sid][1], None) for sid in STAGE_IDS]
    assert result.plan_receipt.verify_structure()


def test_verified_quality_task_completes_with_every_stage_passed(tmp_path: Path) -> None:
    """Quality VERIFIED requires PASSED at every stage; the OCR stages pass by adapter-determinism replay."""
    from pados.capability.receipts import VerificationStatus
    from pados.capability.task import QualityRequirement

    fab = pipeline(tmp_path)
    task = fab.task(quality=QualityRequirement.VERIFIED)
    plan = fab.planner().plan(task)
    assert plan.status is PlanStatus.FEASIBLE
    result = fab.execute(plan, task)
    assert result.status is ExecutionStatus.COMPLETED
    assert all(r.verification is VerificationStatus.PASSED for r in result.plan_receipt.stages)


def test_adapters_run_only_through_the_guarded_bound_stage_path(tmp_path: Path) -> None:
    fab = pipeline(tmp_path)
    calls = spy_adapters(fab.wiring)
    _, _, result = fab.run()
    assert result.status is ExecutionStatus.COMPLETED
    assert calls and all(call.guarded and call.bound and call.scoped for call in calls)
    counts = {sid: sum(1 for call in calls if call.stage_id == sid) for sid in STAGE_IDS}
    assert counts == {sid: 2 if sid in REPLAYED else 1 for sid in STAGE_IDS}
    unused = {"retrieval.embedding-engine", "retrieval.reranker-engine", "algorithms.explain.model",
              "intelligence.pipeline-report"}
    assert {call.adapter_id for call in calls} == set(fab.wiring.adapters) - unused


def test_cf01_a_rewritten_plan_is_refused_before_any_adapter_runs(tmp_path: Path) -> None:
    fab = pipeline(tmp_path)
    task = fab.task()
    plan = fab.planner().plan(task)
    calls = spy_adapters(fab.wiring)
    index = plan.stage_ids.index("retrieve")
    stages = list(plan.stages)
    stages[index] = dataclasses.replace(stages[index], parameters={**stages[index].parameters, "top_k": 64})
    document = plan_document(plan.status, plan.task_id, plan.task_hash, plan.template_id, plan.template_hash,
                             plan.graph_hash, stages, plan.infeasible_codes, plan.advice_enabled, plan.human_gate,
                             plan.quality)
    digest = canonical_hash(document)
    forged = dataclasses.replace(plan, stages=tuple(stages), plan_hash=digest, plan_id=f"plan-{digest[:24]}")
    with pytest.raises(ExecutionRefused, match="PLAN_NOT_REPRODUCIBLE"):
        fab.execute(forged, task)
    with pytest.raises(ExecutionRefused, match="PLAN_HASH_MISMATCH"):
        fab.executor().execute(dataclasses.replace(plan, stages=tuple(stages)), task)
    assert calls == []


def test_cf02_inputs_resolve_only_through_operator_provenance(tmp_path: Path) -> None:
    fab = pipeline(tmp_path)
    task = fab.task()
    plan = fab.planner().plan(task)
    calls = spy_adapters(fab.wiring)
    with pytest.raises(ExecutionRefused, match="INPUT_PROVENANCE_RESOLVER_MISSING"):
        fab.execute(plan, task, provenance=None)
    unregistered = pipeline(tmp_path / "unregistered", register_source=False)
    refused = unregistered.planner().plan(unregistered.task())
    assert refused.status is PlanStatus.INFEASIBLE and refused.infeasible_codes == ("INPUT_PROVENANCE_UNKNOWN",)
    relabelled = pipeline(tmp_path / "relabelled", classification=DataClassification.RESTRICTED)
    mislabelled = relabelled.task(classification=DataClassification.RESTRICTED, inputs=(
        relabelled.task().inputs[0].model_copy(update={"classification": DataClassification.INTERNAL}),))
    assert relabelled.planner().plan(mislabelled).infeasible_codes == ("INPUT_CLASSIFICATION_LABEL_MISMATCH",)
    assert calls == []


def test_cf03_revocation_inside_the_readiness_probe_is_caught_before_the_adapter_body(tmp_path: Path) -> None:
    fab = pipeline(tmp_path)
    task = fab.task()
    plan = fab.planner().plan(task)
    calls = spy_adapters(fab.wiring)
    extraction = fab.wiring.adapters["extraction.table-dataset"]
    probe = extraction.available

    def revoking_probe() -> bool:
        if any(f.function == "execute" and Path(f.filename).as_posix().endswith("pados/capability/registry.py")
               for f in inspect.stack(0)):
            fab.grants.revoke("CAPABILITY:STRUCTURED_EXTRACTION")
        return probe()

    extraction.available = revoking_probe  # type: ignore[method-assign]
    result = fab.execute(plan, task)
    receipts = {r.stage_id: r for r in result.plan_receipt.stages}
    assert result.status is ExecutionStatus.FAILED
    assert receipts["extract"].failure_code == "AUTHORIZATION_REVOKED_BEFORE_EXECUTION"
    assert receipts["extract"].outputs == ()
    assert all(receipts[sid].failure_code == NOT_EXECUTED for sid in ("retrieve", "analyze", "explain"))
    assert not any(call.stage_id in {"extract", "retrieve", "analyze", "explain"} for call in calls)
    assert result.plan_receipt.verify_structure()

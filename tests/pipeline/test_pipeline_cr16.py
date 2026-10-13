"""CR-16 fixer cycle 1: report authenticity (H-01) and provenance (H-02); H-03 is in test_pipeline_cr16_extraction.

H-01: the report builder and adapter accept only an execution the executor recorded in the trusted ledger, and
replay verdicts only when bound to the exact recorded receipt. H-02: outputs inherit the highest authenticated
input classification and never overwrite, relabel or adopt a foreign registration; the OCR reference check
matches the invocation the executor publishes (CR-16b H-06)."""
from __future__ import annotations

import dataclasses
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest
from pipeline_support import PROJECT, dispatching, pipeline

from pados.capability.artifacts import ArtifactRef
from pados.capability.executor import CapabilityExecutor, ExecutionRefused, ExecutionStatus
from pados.capability.planner import PlanStatus
from pados.capability.policy import PolicyCode
from pados.capability.receipts import chain
from pados.capability.task import TaskInput
from pados.enterprise.classification import DataClassification
from pados.packs.algorithms.contracts import canonical_json
from pados.packs.intelligence.report import (
    EVIDENCE_KIND,
    PipelineEvidence,
    ReportError,
    load_evidence,
    load_report,
    replay_match_evidence,
)
from pados.packs.intelligence.template import DOCUMENT_INPUT
from pados.packs.intelligence.wiring import WiringError, document_reference_check, wire_pipeline
from pados.packs.ocr.contracts import JSON_MEDIA_TYPE, KIND_DOCUMENT, KIND_SOURCE, PDF_MEDIA_TYPE, OcrDocument

FOREIGN = "proj-foreign"


def _stage(result: Any, stage_id: str) -> Any:
    return next(stage for stage in result.plan_receipt.stages if stage.stage_id == stage_id)


def _forged_receipt(plan: Any, receipt: Any, index: int, **changes: Any) -> Any:
    """An internally consistent chain (re-linked from the plan genesis) with one stage edited."""
    stages = list(receipt.stages)
    stages[index] = stages[index].model_copy(update=changes)
    forged = receipt.model_copy(update={"stages": tuple(chain(plan.plan_id, plan.plan_hash, stages))})
    assert forged.verify_structure()
    return forged


def _run_report_with(fab: Any, built: Any, evidence: PipelineEvidence) -> Any:
    """Store forged evidence (registered like real evidence) and run the report task over it."""
    ref = fab.store.put(canonical_json(evidence.canonical()), media_type=JSON_MEDIA_TYPE, kind=EVIDENCE_KIND)
    fab.provenance.register(ref.sha256, project_id=PROJECT, classification=fab.classification)
    task = built.task.model_copy(update={"inputs": (built.task.inputs[0].model_copy(update={"ref": ref}),
                                                    *built.task.inputs[1:])})
    plan = fab.planner().plan(task)
    return fab.execute(plan, task).plan_receipt.stages[-1]


# --- H-01: report authenticity ------------------------------------------------------------------------

def test_a_genuine_run_is_resolved_from_the_ledger_and_reported(tmp_path: Path) -> None:
    fab = pipeline(tmp_path)
    task, plan, result = fab.run()
    entry = fab.ledger.resolve(result.plan_receipt.envelope_hash)
    assert entry is not None and entry.matches(result.plan_receipt) and entry.task_hash == task.task_hash
    built, _, report_result = fab.report(task, plan, result)
    assert report_result.status is ExecutionStatus.COMPLETED
    (verdict,) = built.replay
    analyze = _stage(result, "analyze")
    assert verdict.receipt_hash == analyze.receipt_hash == entry.stage("analyze").receipt_hash  # type: ignore[union-attr]
    assert verdict.evidence_hash == replay_match_evidence(analyze)
    report = load_report(fab.store.get(report_result.plan_receipt.stages[-1].outputs[0]))
    assert report.receipt_envelope_hash == entry.envelope_hash
    assert fab.ledger.resolve(report_result.plan_receipt.envelope_hash) is not None  # the report run is recorded too


def test_a_fabricated_internally_consistent_chain_is_refused_by_the_builder(tmp_path: Path) -> None:
    fab = pipeline(tmp_path)
    task, plan, result = fab.run()
    index = plan.stage_ids.index("extract")
    forged = _forged_receipt(plan, result.plan_receipt, index, verifier_id="forged.verifier")
    with pytest.raises(ReportError, match="^REPORT_EXECUTION_NOT_IN_LEDGER$"):
        fab.build_report(task, plan, dataclasses.replace(result, plan_receipt=forged))
    unrecorded = pipeline(tmp_path / "other")  # a genuine run by an executor without the ledger
    task2, plan2, result2 = unrecorded.run(ledger=None)
    assert result2.status is ExecutionStatus.COMPLETED
    with pytest.raises(ReportError, match="^REPORT_EXECUTION_NOT_IN_LEDGER$"):
        unrecorded.build_report(task2, plan2, result2)
    assert all(fab.provenance.resolve(ref.sha256) is None for stage in result.plan_receipt.stages
               for ref in stage.outputs)  # nothing was registered for a refused report


def test_the_report_adapter_refuses_evidence_whose_chain_is_not_recorded(tmp_path: Path) -> None:
    fab = pipeline(tmp_path)
    task, plan, result = fab.run()
    built = fab.build_report(task, plan, result)
    evidence = load_evidence(fab.store.get(built.evidence))
    index = plan.stage_ids.index("retrieve")
    forged = _forged_receipt(plan, evidence.receipt, index, verification_evidence_hash="e" * 64)
    receipt = _run_report_with(fab, built, evidence.model_copy(update={
        "receipt": forged, "envelope_hash": forged.envelope_hash}))
    assert (receipt.failure_code, receipt.outputs) == ("REPORT_EXECUTION_NOT_IN_LEDGER", ())
    graph = list(evidence.graph)
    first = graph[1].inputs[1].model_copy(update={"artifact_kind": None})
    graph[1] = graph[1].model_copy(update={"inputs": (graph[1].inputs[0], first)})
    receipt = _run_report_with(fab, built, evidence.model_copy(update={"graph": tuple(graph)}))
    assert (receipt.failure_code, receipt.outputs) == ("REPORT_GRAPH_NOT_RECORDED", ())


@pytest.mark.parametrize(("changes", "code"), [
    ({"evidence_hash": "0" * 64}, "REPORT_REPLAY_NOT_BOUND"),
    ({"receipt_hash": "1" * 64}, "REPORT_REPLAY_NOT_BOUND"),
    ({"code": "REPLAY_DIVERGED"}, "REPORT_REPLAY_NOT_PASSED"),
    ({"verifier_id": "someone.else"}, "REPORT_REPLAY_NOT_PASSED"),
])
def test_a_tampered_replay_verdict_is_refused(tmp_path: Path, changes: dict[str, Any], code: str) -> None:
    fab = pipeline(tmp_path)
    task, plan, result = fab.run()
    built = fab.build_report(task, plan, result)
    evidence = load_evidence(fab.store.get(built.evidence))
    tampered = tuple(item.model_copy(update=changes) for item in evidence.replay)
    receipt = _run_report_with(fab, built, evidence.model_copy(update={"replay": tampered}))
    assert (receipt.failure_code, receipt.outputs) == (code, ())


def test_a_verdict_for_another_receipt_is_refused(tmp_path: Path) -> None:
    fab = pipeline(tmp_path)
    task, plan, result = fab.run()
    built = fab.build_report(task, plan, result)
    evidence = load_evidence(fab.store.get(built.evidence))
    explain = _stage(result, "explain")  # a genuine receipt hash, but of a different stage
    tampered = tuple(item.model_copy(update={"receipt_hash": explain.receipt_hash}) for item in evidence.replay)
    receipt = _run_report_with(fab, built, evidence.model_copy(update={"replay": tampered}))
    assert (receipt.failure_code, receipt.outputs) == ("REPORT_REPLAY_NOT_BOUND", ())


def test_a_rewritten_or_torn_ledger_fails_closed(tmp_path: Path) -> None:
    fab = pipeline(tmp_path)
    task, plan, result = fab.run()
    original = fab.ledger.path.read_bytes()
    entry = json.loads(original.splitlines()[0])
    fab.ledger.path.write_bytes(canonical_json({**entry, "task_hash": "f" * 64}) + b"\n")
    with pytest.raises(ReportError, match="^EXECUTION_LEDGER_CORRUPT$"):
        fab.build_report(task, plan, result)
    fab.ledger.path.write_bytes(original + b'{"schema":"pados.capability.execution')
    with pytest.raises(ReportError, match="^EXECUTION_LEDGER_TORN_ENTRY$"):
        fab.build_report(task, plan, result)
    fab.ledger.path.write_bytes(original)
    built = fab.build_report(task, plan, result)
    fab.ledger.path.write_bytes(original + b"{")
    report_plan = fab.planner().plan(built.task)
    with pytest.raises(ExecutionRefused, match="^EXECUTION_LEDGER_TORN_ENTRY$"):
        fab.execute(report_plan, built.task)  # the adapter refuses, and the run cannot be recorded either


def test_the_report_capability_is_unavailable_without_a_ledger(tmp_path: Path) -> None:
    fab = pipeline(tmp_path)
    task, plan, result = fab.run()
    built = fab.build_report(task, plan, result)
    unwired = wire_pipeline(fab.provenance, operator="operator.pipeline")
    planner = dataclasses.replace(fab, wiring=unwired).planner()
    report_plan = planner.plan(built.task)
    assert report_plan.status is PlanStatus.INFEASIBLE
    (stage,) = report_plan.stages
    assert [c.code for c in stage.candidates] == [PolicyCode.ADAPTER_UNAVAILABLE.value]
    with pytest.raises(ValueError, match="EXECUTION_LEDGER_INVALID"):
        CapabilityExecutor(fab.wiring.registry, fab.store, fab.grants, templates=fab.wiring.templates,
                           provenance=fab.provenance, ledger=fab.provenance)  # type: ignore[arg-type]


# --- H-02: provenance -------------------------------------------------------------------------------------

def _outputs(result: Any) -> dict[str, tuple[ArtifactRef, ...]]:
    return {stage.stage_id: stage.outputs for stage in result.plan_receipt.stages}


def test_an_output_registered_for_a_foreign_project_is_refused_before_anything_registers(tmp_path: Path) -> None:
    fab = pipeline(tmp_path)
    task, plan, result = fab.run()
    ranking = _outputs(result)["retrieve"][0]
    fab.provenance.register(ranking.sha256, project_id=FOREIGN, classification=DataClassification.CONFIDENTIAL)
    with pytest.raises(ReportError, match="^REPORT_OUTPUT_PROJECT_CONFLICT$"):
        fab.build_report(task, plan, result)
    others = [ref for refs in _outputs(result).values() for ref in refs if ref != ranking]
    assert all(fab.provenance.resolve(ref.sha256) is None for ref in others)
    assert fab.provenance.resolve(ranking.sha256) == (FOREIGN, DataClassification.CONFIDENTIAL)


@pytest.mark.parametrize(("existing", "code"), [
    (DataClassification.PUBLIC, "REPORT_OUTPUT_CLASSIFICATION_DOWNGRADE"),
    (DataClassification.INTERNAL, "REPORT_OUTPUT_CLASSIFICATION_DOWNGRADE"),
    (DataClassification.RESTRICTED, "REPORT_OUTPUT_REGISTERED_ELSEWHERE"),
])
def test_a_relabel_of_an_already_registered_output_is_refused(tmp_path: Path, existing: DataClassification,
                                                              code: str) -> None:
    fab = pipeline(tmp_path)
    task, plan, result = fab.run()
    algorithm_result = _outputs(result)["analyze"][0]
    fab.provenance.register(algorithm_result.sha256, project_id=PROJECT, classification=existing)
    with pytest.raises(ReportError, match=f"^{code}$"):
        fab.build_report(task, plan, result)


def test_outputs_take_the_highest_authenticated_input_classification(tmp_path: Path) -> None:
    fab = pipeline(tmp_path, classification=DataClassification.INTERNAL)  # the document is INTERNAL
    task = fab.task(classification=DataClassification.CONFIDENTIAL)  # the task ceiling is higher
    task, plan, result = fab.run(task)
    built = fab.build_report(task, plan, result)
    hashes = {ref.sha256 for refs in _outputs(result).values() for ref in refs} | {built.evidence.sha256}
    assert {fab.provenance.resolve(sha) for sha in hashes} == {(PROJECT, DataClassification.INTERNAL)}
    assert {item.classification for item in built.task.inputs} == {DataClassification.INTERNAL}
    repeat = fab.build_report(task, plan, result)  # identical registrations are idempotent
    assert repeat.evidence == built.evidence


def _document_outputs(fab: Any, result: Any) -> list[tuple[ArtifactRef, bytes]]:
    ref = _stage(result, "recognize").outputs[0]
    return [(ref, fab.store.get(ref))]


def test_the_reference_check_matches_the_executors_dispatch(tmp_path: Path) -> None:
    fab = pipeline(tmp_path)
    task, _, result = fab.run()
    hook = document_reference_check(fab.provenance, fab.wiring.invocations)
    outputs = _document_outputs(fab, result)
    with dispatching(task, (fab.source,)):
        assert hook(outputs) is True
        with fab.wiring.invocations.bind(task):
            assert hook(outputs) is True  # the operator's optional cross-check agrees
            with pytest.raises(WiringError, match="PIPELINE_INVOCATION_ALREADY_BOUND"):
                with fab.wiring.invocations.bind(task):
                    pass
    assert hook(outputs) is False  # outside an executor dispatch
    with fab.wiring.invocations.bind(task):
        assert hook(outputs) is False  # a caller binding alone never makes it pass
    foreign = fab.task(project_id=FOREIGN, inputs=(TaskInput(name=DOCUMENT_INPUT, ref=fab.source, project_id=FOREIGN,
                                                               classification=fab.classification),))
    with dispatching(foreign, (fab.source,)):
        assert hook(outputs) is False  # registered, but for another project
    relabelled = fab.task(inputs=(TaskInput(name=DOCUMENT_INPUT, ref=fab.source, project_id=PROJECT,
                                            classification=DataClassification.INTERNAL),))
    with dispatching(relabelled, (fab.source,)):
        assert hook(outputs) is False  # registered, but at another classification than the input declares
    other = fab.store.put(b"%PDF-1.4 another registered document", media_type=PDF_MEDIA_TYPE, kind=KIND_SOURCE)
    fab.provenance.register(other.sha256, project_id=PROJECT, classification=fab.classification)
    elsewhere = fab.task(inputs=(TaskInput(name=DOCUMENT_INPUT, ref=other, project_id=PROJECT,
                                           classification=fab.classification),))
    with dispatching(elsewhere, (other,)):
        assert hook(outputs) is False  # registered in this project, but not the actual stage input
    with dispatching(task, (other,)):
        assert hook(outputs) is False  # the task names the source, but the stage did not consume it
    document = OcrDocument.from_bytes(outputs[0][1])
    moved = document.model_copy(update={"source": other}).to_bytes()
    moved_ref = ArtifactRef(sha256=hashlib.sha256(moved).hexdigest(), media_type=JSON_MEDIA_TYPE,
                            size_bytes=len(moved), kind=KIND_DOCUMENT)
    with dispatching(task, (fab.source,)):
        assert hook([(moved_ref, moved)]) is False  # a document claiming another registered source


def test_running_under_a_mismatched_invocation_fails_recognition_closed(tmp_path: Path) -> None:
    fab = pipeline(tmp_path)
    task = fab.task()
    plan = fab.planner().plan(task)
    foreign = fab.task(project_id=FOREIGN, inputs=(TaskInput(name=DOCUMENT_INPUT, ref=fab.source, project_id=FOREIGN,
                                                               classification=fab.classification),))
    with fab.wiring.invocations.bind(foreign):
        result = fab.executor().execute(plan, task)
    recognize = _stage(result, "recognize")
    assert result.status is ExecutionStatus.FAILED
    assert (recognize.failure_code, recognize.outputs) == ("VERIFICATION_FAILED", ())

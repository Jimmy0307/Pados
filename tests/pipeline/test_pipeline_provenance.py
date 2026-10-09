"""Requirement 2: per-stage provenance, graph-exact data flow, verification evidence and the report
artifact whose hash is an output of the final (report) receipt chain."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pipeline_support import STAGE_IDS, pipeline

from pados.capability.executor import ExecutionStatus
from pados.capability.graph import InputSource
from pados.capability.model import VerificationMethod, descriptor_digest
from pados.capability.receipts import VerificationStatus, parameters_hash
from pados.packs.algorithms.contracts import RECORD_KIND, RESULT_KIND, load_record
from pados.packs.algorithms.replay import VERIFIER_ID as REPLAY_VERIFIER
from pados.packs.intelligence.report import REPORT_KIND, load_evidence, load_report
from pados.packs.ocr.contracts import OcrDocument

# OCR capabilities are PROBABILISTIC (the weakest determinism their adapters may declare), but the selected
# reference adapters declare DETERMINISTIC (digest-bound), so the fabric replays them too: every stage PASSES.
EXPECTED_VERIFICATION = {sid: VerificationStatus.PASSED for sid in (
    "preprocess", "recognize", "layout", "tables", "structure", "extract", "retrieve", "analyze", "explain")}


def test_each_receipt_binds_identities_digests_parameters_and_verification(tmp_path: Path) -> None:
    fab = pipeline(tmp_path)
    _, plan, result = fab.run()
    registry = fab.wiring.registry
    planned = {stage.stage_id: stage for stage in plan.stages}
    for receipt in result.plan_receipt.stages:
        stage = planned[receipt.stage_id]
        assert receipt.capability_digest == descriptor_digest(registry.capability(receipt.capability_id))
        assert receipt.adapter_digest == descriptor_digest(registry.adapter(receipt.adapter_id).recorded_descriptor())
        assert receipt.capability_digest == stage.capability_digest and receipt.adapter_digest == stage.adapter_digest
        assert receipt.parameters_hash == parameters_hash(stage.parameters)
        assert receipt.policy_decision_hash == stage.decision_hash
        assert receipt.verification is EXPECTED_VERIFICATION[receipt.stage_id]
        assert receipt.verification_method is VerificationMethod.SCHEMA
        assert receipt.verification_evidence_hash and receipt.verifier_id == "pados.capability.verifier.v1"
        assert receipt.semantic_authority == "NONE" and receipt.seed is None
        for ref in (*receipt.inputs, *receipt.outputs):
            assert hashlib.sha256(fab.store.get(ref)).hexdigest() == ref.sha256


def test_outputs_of_each_stage_are_exactly_the_inputs_its_consumers_bind(tmp_path: Path) -> None:
    fab = pipeline(tmp_path)
    task, plan, result = fab.run()
    produced = fab.outputs(result)
    for stage, receipt in zip(plan.stages, result.plan_receipt.stages, strict=True):
        expected = []
        for binding in stage.inputs:
            if binding.source is InputSource.TASK_INPUT:
                expected.append(task.input_ref(binding.name))
            else:
                expected.extend(ref for ref in produced[binding.name]
                                if binding.artifact_kind is None or ref.kind == binding.artifact_kind)
        assert list(receipt.inputs) == expected, stage.stage_id
    assert produced["analyze"][0].kind == RESULT_KIND and produced["analyze"][1].kind == RECORD_KIND
    assert fab.outputs(result)["recognize"][0] in result.plan_receipt.stages[2].inputs
    document = OcrDocument.from_bytes(fab.store.get(produced["structure"][0]))
    assert document.source == fab.source  # every OCR derivative references the source by hash
    record = load_record(fab.store.get(produced["analyze"][1]))
    assert record.dataset_artifact_sha256 == produced["extract"][0].sha256
    assert record.result_hash == produced["analyze"][0].sha256
    explanation = json.loads(fab.store.get(produced["explain"][0]))
    assert explanation["source_result_sha256"] == produced["analyze"][0].sha256


def test_report_lists_every_stage_and_its_hash_is_in_the_final_receipt_chain(tmp_path: Path) -> None:
    fab = pipeline(tmp_path)
    task, plan, result = fab.run()
    built, report_plan, report_result = fab.report(task, plan, result)
    assert [(v.stage_id, v.status, v.code, v.verifier_id) for v in built.replay] == [
        ("analyze", VerificationStatus.PASSED, "REPLAY_MATCH", REPLAY_VERIFIER)]
    assert report_plan.feasible and report_result.status is ExecutionStatus.COMPLETED
    final = report_result.plan_receipt
    assert final.verify_structure() and final.stages[-1].verification is VerificationStatus.PASSED
    report_ref = final.stages[-1].outputs[0]
    assert report_ref.kind == REPORT_KIND and final.stages[-1].inputs[0] == built.evidence
    report = load_report(fab.store.get(report_ref))
    evidence = load_evidence(fab.store.get(built.evidence))
    analysis = result.plan_receipt
    assert report.evidence_sha256 == built.evidence.sha256 and evidence.receipt == analysis
    assert (report.plan_id, report.plan_hash, report.receipt_envelope_hash) == (
        analysis.plan_id, analysis.plan_hash, analysis.envelope_hash)
    assert tuple(stage.stage_id for stage in report.stages) == STAGE_IDS
    for row, receipt in zip(report.stages, analysis.stages, strict=True):
        assert (row.capability_id, row.capability_digest, row.adapter_id, row.adapter_digest) == (
            receipt.capability_id, receipt.capability_digest, receipt.adapter_id, receipt.adapter_digest)
        assert [i.sha256 for i in row.inputs] == [i.sha256 for i in receipt.inputs]
        assert [o.sha256 for o in row.outputs] == [o.sha256 for o in receipt.outputs]
        assert (row.verification.method, row.verification.status, row.verification.evidence_hash) == (
            receipt.verification_method, receipt.verification, receipt.verification_evidence_hash)
        assert row.receipt_hash == receipt.receipt_hash and row.parameters_hash == receipt.parameters_hash
    sources = {row.stage_id: [i.source for i in row.inputs] for row in report.stages}
    assert sources["recognize"] == ["task:document", "stage:preprocess"] and sources["explain"] == ["stage:analyze"]
    assert {row.stage_id: row.consumed_by for row in report.stages}["structure"] == ("extract", "retrieve")
    assert report.verification_summary == {"PASSED": 9, "FAILED": 0, "NOT_VERIFIED": 0}
    assert {item.name: item.ref for item in report.final_outputs} == {
        "explanation": fab.outputs(result)["explain"][0], "ranking": fab.outputs(result)["retrieve"][0],
        "result": fab.outputs(result)["analyze"][0]}
    assert all(fab.provenance.resolve(ref.sha256) is not None
               for receipt in analysis.stages for ref in receipt.outputs)

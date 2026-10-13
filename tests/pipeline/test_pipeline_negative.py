"""Requirement 7: negative paths fail closed - tampered intermediates, revoked grants, a RESTRICTED document
with a WAN-only adapter, forged report evidence - and the fixture OCR adapter is never part of the pipeline."""
from __future__ import annotations

import dataclasses
import sys
import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest
from pipeline_support import OPERATOR, TamperingStore, dispatching, full_grant, pipeline, spy_adapters

from pados.capability.adapter import CapabilityAdapterError, StageContext, StageOutput, StageRequest
from pados.capability.artifacts import ArtifactRef
from pados.capability.executor import NOT_EXECUTED, ExecutionRefused, ExecutionStatus
from pados.capability.planner import PlanStatus
from pados.capability.policy import PolicyCode
from pados.capability.receipts import VerificationStatus
from pados.capability.seams import InferenceRequest, InferenceSelection
from pados.connectivity.requirements import NetworkRequirement
from pados.enterprise.classification import DataClassification
from pados.execution.context import canonical_hash
from pados.packs.algorithms.contracts import DATASET_KIND, canonical_json
from pados.packs.algorithms.explanation import ModelExplanationAdapter, explanation_capability
from pados.packs.intelligence import report as report_module
from pados.packs.intelligence.report import (
    EVIDENCE_KIND,
    ReportError,
    load_evidence,
    replay_algorithm_stages,
)
from pados.packs.intelligence.template import analysis_template
from pados.packs.intelligence.wiring import WiringError, document_reference_check, register_production_adapter
from pados.packs.ocr.contracts import JSON_MEDIA_TYPE, KIND_DOCUMENT, EngineRecord, OcrDocument
from pados.packs.ocr.fixture import fixture_descriptor

CLOUD = "document_decision_analysis_cloud"


def _receipts(result: Any) -> dict[str, Any]:
    return {stage.stage_id: stage for stage in result.plan_receipt.stages}


def test_intermediate_tampered_before_verification_withholds_outputs(tmp_path: Path) -> None:
    store = TamperingStore(tmp_path / "objects", kind=DATASET_KIND)
    fab = pipeline(tmp_path, store=store)
    _, _, result = fab.run()
    receipts = _receipts(result)
    assert store.tampered and result.status is ExecutionStatus.FAILED
    assert receipts["extract"].failure_code == "VERIFICATION_FAILED"
    assert receipts["extract"].verification is VerificationStatus.FAILED and receipts["extract"].outputs == ()
    assert all(receipts[sid].failure_code == NOT_EXECUTED for sid in ("retrieve", "analyze", "explain"))
    assert "extract" not in result.outputs and result.plan_receipt.verify_structure()


def test_intermediate_tampered_after_verification_refuses_its_consumer(tmp_path: Path) -> None:
    store = TamperingStore(tmp_path / "objects", kind=KIND_DOCUMENT, nth=4, after_reads=1)  # structure output
    fab = pipeline(tmp_path, store=store)
    _, _, result = fab.run()
    receipts = _receipts(result)
    assert receipts["structure"].failure_code is None and receipts["structure"].outputs[0].sha256 == store.tampered[0]
    assert receipts["extract"].failure_code == "EXTRACTION_INPUT_UNREADABLE" and receipts["extract"].outputs == ()
    assert all(receipts[sid].failure_code == NOT_EXECUTED for sid in ("retrieve", "analyze", "explain"))


def test_dataset_tampered_after_verification_refuses_the_algorithm(tmp_path: Path) -> None:
    store = TamperingStore(tmp_path / "objects", kind=DATASET_KIND, after_reads=1)
    fab = pipeline(tmp_path, store=store)
    _, _, result = fab.run()
    receipts = _receipts(result)
    assert receipts["extract"].failure_code is None and receipts["retrieve"].failure_code is None
    assert receipts["analyze"].failure_code == "ALGORITHM_UNEXPECTED_ERROR" and receipts["analyze"].outputs == ()
    assert receipts["explain"].failure_code == NOT_EXECUTED


def test_report_refuses_a_replay_mismatch_and_unreadable_artifacts(tmp_path: Path,
                                                                   monkeypatch: pytest.MonkeyPatch) -> None:
    fab = pipeline(tmp_path)
    task, plan, result = fab.run()
    index = plan.stage_ids.index("analyze")
    stages = list(plan.stages)
    stages[index] = dataclasses.replace(stages[index], parameters={**stages[index].parameters, "max_rules": 63})
    forged = dataclasses.replace(plan, stages=tuple(stages))
    verdicts = replay_algorithm_stages(forged, result.plan_receipt, fab.store)
    assert [(v.stage_id, v.status, v.code) for v in verdicts] == [
        ("analyze", VerificationStatus.FAILED, "REPLAY_RECORD_REQUEST_PARAMETERS_HASH_MISMATCH")]
    with pytest.raises(ReportError, match="REPORT_PLAN_MISMATCH"):  # a rewritten plan no longer matches its hash
        fab.build_report(task, forged, result)
    real = report_module.verify_replay

    def changed_parameters(expected: Any, record: bytes, result_bytes: bytes, dataset: bytes,
                           parameters: Mapping[str, Any]) -> Any:
        return real(expected, record, result_bytes, dataset, {**parameters, "max_rules": 63})

    monkeypatch.setattr(report_module, "verify_replay", changed_parameters)  # the replay itself disagrees
    with pytest.raises(ReportError, match="REPORT_REPLAY_FAILED"):
        fab.build_report(task, plan, result)
    monkeypatch.setattr(report_module, "verify_replay", real)
    dataset = fab.outputs(result)["extract"][0]
    path = tmp_path / "objects" / dataset.sha256[:2] / dataset.sha256[2:4] / dataset.sha256
    path.write_bytes(path.read_bytes() + b" ")
    with pytest.raises(ReportError, match="REPORT_ARTIFACT_UNREADABLE"):
        fab.build_report(task, plan, result)


def test_report_stage_refuses_forged_evidence_and_foreign_final_outputs(tmp_path: Path) -> None:
    fab = pipeline(tmp_path)
    task, plan, result = fab.run()
    built = fab.build_report(task, plan, result)
    evidence = load_evidence(fab.store.get(built.evidence))
    stages = list(evidence.receipt.stages)
    stages[5] = stages[5].model_copy(update={"parameters_hash": "0" * 64})
    forged = evidence.model_copy(update={"receipt": evidence.receipt.model_copy(update={"stages": tuple(stages)})})
    forged_ref = fab.store.put(canonical_json(forged.canonical()), media_type=JSON_MEDIA_TYPE, kind=EVIDENCE_KIND)
    fab.provenance.register(forged_ref.sha256, project_id=task.project_id, classification=task.classification)
    dataset = fab.outputs(result)["extract"][0]
    cases = {
        "REPORT_RECEIPT_STRUCTURE_INVALID": (built.task.inputs[0].model_copy(update={"ref": forged_ref}),
                                             *built.task.inputs[1:]),
        "REPORT_FINAL_OUTPUT_MISMATCH": (*built.task.inputs[:1], built.task.inputs[1].model_copy(
            update={"ref": dataset}), *built.task.inputs[2:]),
    }
    for code, inputs in cases.items():
        forged_task = built.task.model_copy(update={"inputs": inputs})
        report_plan = fab.planner().plan(forged_task)
        report_result = fab.execute(report_plan, forged_task)
        receipt = report_result.plan_receipt.stages[-1]
        assert (receipt.failure_code, receipt.outputs) == (code, ()), code


def test_revoked_grant_refuses_the_plan_before_any_adapter_runs(tmp_path: Path) -> None:
    fab = pipeline(tmp_path)
    task = fab.task()
    plan = fab.planner().plan(task)
    calls = spy_adapters(fab.wiring)
    fab.grants.revoke("CAPABILITY:ROUGH_SET_RULE_LEARNING")
    with pytest.raises(ExecutionRefused, match="PLAN_NOT_REPRODUCIBLE"):
        fab.execute(plan, task)
    replanned = fab.planner().plan(task)
    assert replanned.status is PlanStatus.INFEASIBLE
    assert replanned.infeasible_codes == ("analyze:NO_FEASIBLE_CANDIDATE",)
    analyze = next(stage for stage in replanned.stages if stage.stage_id == "analyze")
    assert [(c.adapter_id, c.code) for c in analyze.candidates] == [
        ("algorithms.drsa.domlem", PolicyCode.NOT_AUTHORIZED.value)]
    with pytest.raises(ExecutionRefused, match="PLAN_NOT_FEASIBLE"):
        fab.execute(replanned, task)
    assert calls == []


class _Generator:
    def generate(self, facts: Mapping[str, Any], *, router_selection_hash: str) -> str:
        return "generated prose"


class _Delegate:
    def select(self, request: InferenceRequest) -> InferenceSelection:
        return InferenceSelection("worker-local", "ROUTED", "router decision",
                                  canonical_hash({"stage": request.stage_id, "grant": request.grant_hash}))


def test_restricted_document_with_a_wan_only_adapter_is_infeasible(tmp_path: Path) -> None:
    fab = pipeline(tmp_path, classification=DataClassification.RESTRICTED, include_model_explanation=False)
    local_plan = fab.planner().plan(fab.task())
    if sys.platform == "win32":  # OCR containment is intentionally unavailable pending Windows UAT.
        assert local_plan.infeasible_codes == (
            "preprocess:NO_FEASIBLE_CANDIDATE", "recognize:NO_FEASIBLE_CANDIDATE")
    else:
        assert local_plan.feasible  # the local pipeline accepts RESTRICTED input
    fab.wiring.registry.register_capability(explanation_capability(NetworkRequirement.WAN_REQUIRED),
                                            operator=OPERATOR)
    cloud = ModelExplanationAdapter(_Generator(), max_classification=DataClassification.INTERNAL,
                                    network_requirement=NetworkRequirement.WAN_REQUIRED)
    register_production_adapter(fab.wiring.registry, cloud, operator=OPERATOR)
    fab.wiring.templates.register(analysis_template({"explain": "RESULT_EXPLANATION"}, task_type=CLOUD))
    fab.grants.grant(*full_grant(), "CAPABILITY:RESULT_EXPLANATION")
    calls = spy_adapters(fab.wiring)
    for network in (NetworkRequirement.LOCAL_ONLY, NetworkRequirement.WAN_REQUIRED):
        task = fab.task(task_type=CLOUD, network_policy=network)
        plan = fab.planner(inference=_Delegate()).plan(task)
        expected_infeasible = ("explain:NO_FEASIBLE_CANDIDATE",)
        if sys.platform == "win32":
            expected_infeasible = ("preprocess:NO_FEASIBLE_CANDIDATE", "recognize:NO_FEASIBLE_CANDIDATE",
                                   *expected_infeasible)
        assert plan.status is PlanStatus.INFEASIBLE and plan.infeasible_codes == expected_infeasible
        explain = next(stage for stage in plan.stages if stage.stage_id == "explain")
        assert [(c.adapter_id, c.code) for c in explain.candidates] == [
            ("algorithms.explain.model", PolicyCode.CLASSIFICATION_EXCEEDS_ADAPTER.value)]
        failing = set(explain.candidates[0].failing_checks)
        assert PolicyCode.CLASSIFICATION_EXCEEDS_ADAPTER.value in failing
        assert (PolicyCode.NETWORK_NOT_PERMITTED.value in failing) == (network is NetworkRequirement.LOCAL_ONLY)
        with pytest.raises(ExecutionRefused, match="PLAN_NOT_FEASIBLE"):
            fab.execute(plan, task, inference=_Delegate())
    assert calls == []


class _FixtureLike:
    """Stands in for the OCR pack's test-only fixture adapter (its descriptor, not its engine)."""

    production_eligible = False

    def __init__(self, descriptor: Any) -> None:
        self.descriptor = descriptor

    def available(self) -> bool:
        return True

    def execute(self, request: StageRequest, *, context: StageContext) -> StageOutput:
        raise CapabilityAdapterError("FIXTURE_TRUTH_NOT_FOUND")


def test_fixture_ocr_adapter_is_refused_and_only_the_text_layer_recognizes(tmp_path: Path) -> None:
    fab = pipeline(tmp_path)
    registry = fab.wiring.registry
    fixture = fixture_descriptor()
    renamed = _FixtureLike(fixture.model_copy(update={"adapter_id": "ocr.renamed-truth"}))
    renamed.production_eligible = True  # the fixture backend alone is enough to refuse it
    for adapter in (_FixtureLike(fixture), renamed):
        with pytest.raises(WiringError, match="PIPELINE_ADAPTER_NOT_PRODUCTION_ELIGIBLE"):
            register_production_adapter(registry, adapter, operator=OPERATOR)  # type: ignore[arg-type]
    assert not registry.is_installed(fixture.adapter_id) and not registry.is_installed("ocr.renamed-truth")
    assert [a.adapter_id for a in registry.adapters_for("DOCUMENT_OCR")] == ["ocr.pdf-text-layer"]
    _, _, result = fab.run()
    recognize = _receipts(result)["recognize"]
    assert recognize.adapter_id == "ocr.pdf-text-layer"
    document = OcrDocument.from_bytes(fab.store.get(recognize.outputs[0]))
    assert [(e.adapter_id, e.backend) for e in document.evidence.engines] == [("ocr.pdf-text-layer", "pypdf")]
    hook = document_reference_check(fab.provenance, fab.wiring.invocations)

    def outputs(doc: OcrDocument) -> list[tuple[ArtifactRef, bytes]]:
        payload = doc.to_bytes()
        return [(ArtifactRef(sha256=hashlib.sha256(payload).hexdigest(), media_type=JSON_MEDIA_TYPE,
                             size_bytes=len(payload), kind=KIND_DOCUMENT), payload)]

    fixture_engine = EngineRecord(adapter_id=fixture.adapter_id, adapter_version="1", backend="fixture",
                                  engine_version="n/a", license_spdx="AGPL-3.0-only", distribution="NONE",
                                  operation=document.evidence.engines[0].operation, attempted_pages=(1,), pages=(1,))
    faked = document.model_copy(update={"evidence": document.evidence.model_copy(
        update={"engines": (fixture_engine,)})})
    assert hook(outputs(document)) is False  # outside an executor dispatch: fails, never passes
    with dispatching(fab.task(), (fab.source,)):
        assert hook(outputs(document)) is True and hook(outputs(faked)) is False

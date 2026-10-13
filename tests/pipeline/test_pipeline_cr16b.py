"""CR-16b fixer cycle 2 (pipeline side): H-05 replay re-performed by the report adapter, H-04 read-only anchored
ledger readers, H-06 executor-bound invocation, H-08 all-or-nothing provenance registration.

The ledger-file items (H-04 reader surface and MAC, H-07 size bound) are in
``tests/capability/test_execution_ledger.py``; H-03 is in ``test_pipeline_cr16_extraction.py``."""
from __future__ import annotations

import dataclasses
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pytest
from pipeline_support import PROJECT, MemoryAnchor, pipeline

from pados.capability.artifacts import ArtifactRef, ObjectStoreArtifacts
from pados.capability.execution_ledger import ExecutionLedgerReader, FileExecutionLedger
from pados.capability import executor as executor_module
from pados.capability.adapter import StageContext, StageOutput, StageRequest
from pados.capability.executor import DispatchContext, ExecutionStatus, current_dispatch
from pados.capability.planner import PlanStatus
from pados.capability.policy import PolicyCode
from pados.capability.provenance import InMemoryInputProvenance, ProvenanceRefused
from pados.enterprise.classification import DataClassification
from pados.enterprise.secrets import EnvironmentSecretResolver, SecretBoundary
from pados.packs.algorithms import drsa
from pados.packs.algorithms.contracts import JSON_MEDIA, canonical_json
from pados.packs.intelligence.provenance import AtomicInputProvenance
from pados.packs.intelligence.report import (
    EVIDENCE_KIND,
    PipelineEvidence,
    PipelineReportAdapter,
    ReportError,
    build_report_task,
    load_evidence,
    load_report,
    replay_match_evidence,
)
from pados.packs.intelligence import wiring as wiring_module
from pados.packs.intelligence.wiring import wire_pipeline

KEY_NAME = "PADOS_TEST_LEDGER_MAC"
KEY_REF = f"secretref://env/{KEY_NAME}"


def _stage(result: Any, stage_id: str) -> Any:
    return next(stage for stage in result.plan_receipt.stages if stage.stage_id == stage_id)


def _run_report_with(fab: Any, built: Any, evidence: PipelineEvidence) -> Any:
    """Store crafted evidence (registered like real evidence) and run the report task over it."""
    ref = fab.store.put(canonical_json(evidence.canonical()), media_type=JSON_MEDIA, kind=EVIDENCE_KIND)
    fab.provenance.register(ref.sha256, project_id=PROJECT, classification=fab.classification)
    task = built.task.model_copy(update={"inputs": (built.task.inputs[0].model_copy(update={"ref": ref}),
                                                    *built.task.inputs[1:])})
    return fab.execute(fab.planner().plan(task), task).plan_receipt.stages[-1]


# --- H-05: the report adapter re-performs the replay ------------------------------------------------------

def test_the_report_task_carries_the_replay_artifacts_and_records_the_adapters_own_verdict(tmp_path: Path) -> None:
    fab = pipeline(tmp_path)
    task, plan, result = fab.run()
    built, _, report_result = fab.report(task, plan, result)
    analyze = _stage(result, "analyze")
    names = {item.name: item.ref for item in built.task.inputs}
    assert names["dataset"] == analyze.inputs[0] and names["result"] == analyze.outputs[0]
    assert names["record"] == analyze.outputs[1]
    report_stage = report_result.plan_receipt.stages[-1]
    assert {ref.sha256 for ref in report_stage.inputs} == {item.ref.sha256 for item in built.task.inputs}
    report = load_report(fab.store.get(report_stage.outputs[0]))
    (verdict,) = report.replay
    assert (verdict.receipt_hash, verdict.code, verdict.evidence_hash) == (
        analyze.receipt_hash, "REPLAY_MATCH", replay_match_evidence(analyze))


def test_a_forged_passed_verdict_for_a_run_that_does_not_replay_is_refused(tmp_path: Path,
                                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    """The analysis ran (and the builder replayed) with a DRSA implementation whose result bytes the current
    implementation does not reproduce. The fabric's own replay matched (it is repeatable), the run is recorded
    as COMPLETED and the builder's verdict is a PASSED REPLAY_MATCH with the correct evidence hash. The report
    adapter replays itself with the real implementation, diverges and refuses."""
    original = drsa.compute

    def drifted(*args: Any, **kwargs: Any) -> tuple[dict[str, Any], dict[str, float]]:
        result, metrics = original(*args, **kwargs)
        return {**result, "rules": result["rules"][:-1]}, metrics

    fab = pipeline(tmp_path)
    monkeypatch.setattr(drsa, "compute", drifted)
    task, plan, result = fab.run()
    assert result.status is ExecutionStatus.COMPLETED
    built = fab.build_report(task, plan, result)
    monkeypatch.setattr(drsa, "compute", original)
    analyze = _stage(result, "analyze")
    (verdict,) = built.replay
    assert verdict.code == "REPLAY_MATCH" and verdict.evidence_hash == replay_match_evidence(analyze)
    assert fab.reader.resolve(result.plan_receipt.envelope_hash) is not None
    report_plan = fab.planner().plan(built.task)
    report_result = fab.execute(report_plan, built.task)
    receipt = report_result.plan_receipt.stages[-1]
    assert report_result.status is ExecutionStatus.FAILED
    assert (receipt.failure_code, receipt.outputs) == ("REPORT_REPLAY_FAILED", ())


@pytest.mark.parametrize(("change", "code"), [
    ("parameters", "REPORT_REPLAY_PARAMETERS_NOT_BOUND"),
    ("no_parameters", "REPORT_REPLAY_PARAMETERS_MISSING"),
    ("inputs", "REPORT_REPLAY_INPUTS_INVALID"),
])
def test_replay_inputs_and_parameters_are_bound_to_the_recorded_receipt(tmp_path: Path, change: str,
                                                                        code: str) -> None:
    fab = pipeline(tmp_path)
    task, plan, result = fab.run()
    built = fab.build_report(task, plan, result)
    evidence = load_evidence(fab.store.get(built.evidence))
    if change == "parameters":
        (item,) = evidence.replay_parameters
        edited = item.model_copy(update={"parameters": {**item.parameters, "max_rules": 1}})
        evidence = evidence.model_copy(update={"replay_parameters": (edited,)})
    elif change == "no_parameters":
        evidence = evidence.model_copy(update={"replay_parameters": ()})
    else:
        dataset, record = evidence.replay_inputs
        evidence = evidence.model_copy(update={"replay_inputs": (dataset.model_copy(update={"name": "record"}),
                                                                 record.model_copy(update={"name": "dataset"}))})
    receipt = _run_report_with(fab, built, evidence)
    assert (receipt.failure_code, receipt.outputs) == (code, ())


def test_a_replay_that_cannot_read_its_inputs_fails_closed(tmp_path: Path) -> None:
    fab = pipeline(tmp_path)
    task, plan, result = fab.run()
    built = fab.build_report(task, plan, result)
    dataset = _stage(result, "analyze").inputs[0]
    path = tmp_path / "objects" / dataset.sha256[:2] / dataset.sha256[2:4] / dataset.sha256
    path.write_bytes(path.read_bytes()[:-1] + b" ")
    report_result = fab.execute(fab.planner().plan(built.task), built.task)
    receipt = report_result.plan_receipt.stages[-1]
    assert report_result.status is ExecutionStatus.FAILED and receipt.outputs == ()
    assert receipt.failure_code == "REPORT_INPUT_UNREADABLE"


# --- H-04: read-only, anchored readers only ------------------------------------------------------------

def test_a_writer_handed_to_report_code_is_refused(tmp_path: Path) -> None:
    fab = pipeline(tmp_path)
    task, plan, result = fab.run()
    with pytest.raises(ReportError, match="^EXECUTION_LEDGER_READER_REQUIRED$"):
        build_report_task(task, plan, result, store=fab.store, provenance=fab.provenance,
                          ledger=fab.ledger)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="^EXECUTION_LEDGER_READER_REQUIRED$"):
        PipelineReportAdapter(fab.ledger)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="^EXECUTION_LEDGER_READER_REQUIRED$"):
        wire_pipeline(fab.provenance, operator="operator.pipeline", ledger=fab.ledger)  # type: ignore[arg-type]
    assert all(fab.provenance.resolve(ref.sha256) is None for stage in result.plan_receipt.stages
               for ref in stage.outputs)


def test_report_building_without_an_anchor_is_refused(tmp_path: Path) -> None:
    fab = pipeline(tmp_path)
    task, plan, result = fab.run()
    unanchored = ExecutionLedgerReader(fab.ledger.path)
    assert unanchored.resolve(result.plan_receipt.envelope_hash) is not None  # readable, but not anchored
    with pytest.raises(ReportError, match="^EXECUTION_LEDGER_ANCHOR_REQUIRED$"):
        build_report_task(task, plan, result, store=fab.store, provenance=fab.provenance, ledger=unanchored)
    built = fab.build_report(task, plan, result)
    unwired = wire_pipeline(fab.provenance, operator="operator.pipeline", ledger=unanchored)
    report_plan = dataclasses.replace(fab, wiring=unwired).planner().plan(built.task)
    assert report_plan.status is PlanStatus.INFEASIBLE
    assert [c.code for c in report_plan.stages[0].candidates] == [PolicyCode.ADAPTER_UNAVAILABLE.value]


def test_a_keyed_ledger_reports_end_to_end_and_its_reader_verifies_every_mac(tmp_path: Path) -> None:
    boundary = SecretBoundary(EnvironmentSecretResolver({KEY_NAME: "k" * 48}))
    fab = pipeline(tmp_path)
    anchor = MemoryAnchor()
    writer = FileExecutionLedger(tmp_path / "keyed" / "executions.jsonl", anchor=anchor, mac_key_ref=KEY_REF,
                                 boundary=boundary)
    reader = writer.reader()
    assert reader.anchored and reader.authenticated and not hasattr(reader, "_append_entry")
    fab = dataclasses.replace(fab, ledger=writer, reader=reader, anchor=anchor,
                              wiring=wire_pipeline(fab.provenance, operator="operator.pipeline", ledger=reader))
    task, plan, result = fab.run()
    _, _, report_result = fab.report(task, plan, result)
    assert report_result.status is ExecutionStatus.COMPLETED
    assert len(reader.entries()) == 2 and "entry_mac" in writer.path.read_text(encoding="ascii")
    wrong = ExecutionLedgerReader(writer.path, anchor=anchor, mac_key_ref=KEY_REF,
                                  boundary=SecretBoundary(EnvironmentSecretResolver({KEY_NAME: "x" * 48})))
    with pytest.raises(ReportError, match="^EXECUTION_LEDGER_MAC_INVALID$"):
        build_report_task(task, plan, result, store=fab.store, provenance=fab.provenance, ledger=wrong)


# --- H-06: the executor binds the invocation -----------------------------------------------------------

def test_a_caller_binding_task_b_around_task_a_is_refused(tmp_path: Path) -> None:
    fab = pipeline(tmp_path)
    task_a = fab.task()
    task_b = fab.task(task_id="decision-b")  # same project, same registered document: only the task differs
    plan_a = fab.planner().plan(task_a)
    with fab.wiring.invocations.bind(task_b):
        result = fab.executor().execute(plan_a, task_a)
    recognize = _stage(result, "recognize")
    assert result.status is ExecutionStatus.FAILED
    assert (recognize.failure_code, recognize.outputs) == ("VERIFICATION_FAILED", ())


def test_the_executor_supplies_the_invocation_without_any_caller_binding(tmp_path: Path) -> None:
    fab = pipeline(tmp_path)
    task = fab.task()
    result = fab.executor().execute(fab.planner().plan(task), task)  # nothing bound by the caller
    assert result.status is ExecutionStatus.COMPLETED
    assert _stage(result, "recognize").verification.value == "PASSED"
    assert current_dispatch() is None  # published only while a stage is verified


def test_adapter_code_cannot_change_the_dispatch_context_the_hooks_read(tmp_path: Path,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """The recognize adapter (run again by the fabric's replay, before the REFERENCE_CHECK) sets the dispatch
    context to another task; the replay runs in a copied context, so the hook still sees the executor's."""
    fab = pipeline(tmp_path)
    task = fab.task()
    forged = DispatchContext(fab.task(task_id="decision-forged"), "0" * 64, "recognize", "DOCUMENT_OCR",
                             (fab.source,))
    adapter = fab.wiring.adapters["ocr.pdf-text-layer"]
    original = adapter.execute

    def tampering(request: StageRequest, *, context: StageContext) -> StageOutput:
        executor_module._DISPATCH.set(forged)
        result: StageOutput = original(request, context=context)
        return result

    adapter.execute = tampering  # type: ignore[method-assign]
    seen: list[DispatchContext | None] = []

    def recording() -> DispatchContext | None:
        seen.append(current_dispatch())
        return seen[-1]

    monkeypatch.setattr(wiring_module, "current_dispatch", recording)
    result = fab.executor().execute(fab.planner().plan(task), task)
    assert result.status is ExecutionStatus.COMPLETED
    assert seen and all(item is not None and item.task.task_id == task.task_id and item.stage_id == "recognize"
                        for item in seen)
    assert current_dispatch() is None  # nothing the adapter set survives its call


# --- H-08: all-or-nothing provenance registration --------------------------------------------------------

def test_a_mid_batch_conflict_leaves_no_registration() -> None:
    registry = AtomicInputProvenance()
    a, b, c = "a" * 64, "b" * 64, "c" * 64
    registry.register(b, project_id="proj-other", classification=DataClassification.INTERNAL)
    batch = [(a, PROJECT, DataClassification.CONFIDENTIAL), (b, PROJECT, DataClassification.CONFIDENTIAL),
             (c, PROJECT, DataClassification.CONFIDENTIAL)]
    with pytest.raises(ProvenanceRefused, match="^INPUT_PROVENANCE_ENTRY_CONFLICT$"):
        registry.register_many(batch)
    assert registry.resolve(a) is None and registry.resolve(c) is None
    assert registry.resolve(b) == ("proj-other", DataClassification.INTERNAL)
    with pytest.raises(ProvenanceRefused, match="^INPUT_PROVENANCE_ENTRY_CONFLICT$"):  # a conflict inside a batch
        registry.register_many([(a, PROJECT, DataClassification.CONFIDENTIAL), (a, PROJECT,
                                                                              DataClassification.INTERNAL)])
    assert registry.resolve(a) is None
    full = AtomicInputProvenance(capacity=2)
    with pytest.raises(ProvenanceRefused, match="^INPUT_PROVENANCE_FULL$"):
        full.register_many([(sha * 64, PROJECT, DataClassification.INTERNAL) for sha in "abc"])
    assert all(full.resolve(sha * 64) is None for sha in "abc")
    assert registry.register_many([(a, PROJECT, DataClassification.CONFIDENTIAL)] * 2) == 1
    assert registry.register_many([(a, PROJECT, DataClassification.CONFIDENTIAL)]) == 0  # idempotent


class _Racing(AtomicInputProvenance):
    """Another writer registers one output for a foreign project between the builder's check and its batch."""

    def __init__(self, victim: str) -> None:
        self.victim = victim
        self.raced = False
        super().__init__()

    def register_many(self, entries: Iterable[tuple[str, str, DataClassification]]) -> int:
        batch = list(entries)
        if not self.raced and any(sha == self.victim for sha, _, _ in batch):
            self.raced = True
            super().register_many([(self.victim, "proj-foreign", DataClassification.CONFIDENTIAL)])
        return super().register_many(batch)


class _RecordingStore(ObjectStoreArtifacts):
    def __init__(self, root: Path) -> None:
        super().__init__(root)
        self.kinds: list[str] = []

    def put(self, payload: bytes, *, media_type: str, kind: str) -> ArtifactRef:
        self.kinds.append(kind)
        return super().put(payload, media_type=media_type, kind=kind)


def test_a_registration_race_registers_nothing_and_writes_no_evidence(tmp_path: Path) -> None:
    fab = pipeline(tmp_path, store=_RecordingStore(tmp_path / "objects"))
    task, plan, result = fab.run()
    victim = _stage(result, "explain").outputs[0].sha256
    racing = _Racing(victim)
    racing.register_many([(fab.source.sha256, PROJECT, fab.classification)])
    with pytest.raises(ReportError, match="^INPUT_PROVENANCE_ENTRY_CONFLICT$"):
        build_report_task(task, plan, result, store=fab.store, provenance=racing, ledger=fab.reader)
    others = [ref.sha256 for stage in result.plan_receipt.stages for ref in stage.outputs if ref.sha256 != victim]
    assert others and all(racing.resolve(sha) is None for sha in others)
    assert EVIDENCE_KIND not in fab.store.kinds  # type: ignore[attr-defined]


def test_a_registrar_without_an_atomic_batch_is_refused(tmp_path: Path) -> None:
    fab = pipeline(tmp_path)
    task, plan, result = fab.run()
    legacy = InMemoryInputProvenance({fab.source.sha256: (PROJECT, fab.classification)})
    with pytest.raises(ReportError, match="^REPORT_PROVENANCE_NOT_ATOMIC$"):
        build_report_task(task, plan, result, store=fab.store, provenance=legacy,  # type: ignore[arg-type]
                          ledger=fab.reader)


def test_an_endless_registration_iterable_is_refused_without_materializing_it() -> None:
    """CR-16c H-10: at most MAX_BATCH + 1 items are consumed before an oversized batch is refused."""
    from itertools import count

    from pados.capability.provenance import ProvenanceRefused
    from pados.packs.intelligence.provenance import MAX_BATCH

    consumed = 0

    def endless() -> Any:
        nonlocal consumed
        for index in count():
            consumed += 1
            yield (f"{index:064x}", "proj", DataClassification.INTERNAL)

    registry = AtomicInputProvenance()
    with pytest.raises(ProvenanceRefused, match="INPUT_PROVENANCE_ENTRY_INVALID"):
        registry.register_many(endless())
    assert consumed == MAX_BATCH + 1

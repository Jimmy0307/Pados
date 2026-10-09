# SPDX-License-Identifier: AGPL-3.0-only
"""PIPELINE_REPORT: the content-free report of one analysis run, produced by its own governed stage.

An adapter sees artifacts only, and stage receipts exist only after the executor returns, so a report
that lists every stage's capability, adapter, digests, inputs, outputs and verification cannot be a stage
of the run it describes. It is its own fabric task over trusted evidence:

1. ``build_report_task`` (operator side, trusted code) takes the analysis task, its plan, the executor's
   ``ExecutionResult``, a READ-ONLY, ANCHORED ``ExecutionLedgerReader`` (CR-16b H-04: a writer-capable
   object is refused with ``EXECUTION_LEDGER_READER_REQUIRED``, an unanchored reader with
   ``EXECUTION_LEDGER_ANCHOR_REQUIRED``) and an atomic provenance registrar (``register_many``). Receipts
   handed to it are claims: it requires a COMPLETED run whose ``PlanReceipt`` passes ``verify_structure``
   AND is exactly the execution the executor recorded in the ledger under that envelope hash (plan, task,
   every stage receipt hash and verification row, the stage bindings); otherwise
   ``REPORT_EXECUTION_NOT_IN_LEDGER`` (a damaged ledger refuses with its own closed code). It runs the
   algorithm pack's ``verify_replay`` for every algorithm stage and records the verdicts as ADVISORY: the
   report adapter never trusts them (CR-16b H-05). It builds the evidence artifact (the receipt chain, the
   plan's input bindings, the advisory verdicts, the algorithm stage's replay inputs and request parameters;
   content-free and ``assert_minimized`` with an explicit schema). Provenance (CR-16 H-02, CR-16b H-08): the
   outputs take the HIGHEST classification of the analysis task's inputs as resolved by the operator's
   ``InputProvenance`` (never the caller's labels, never lower); an output already registered for another
   project, at a lower classification or under any other registration is refused. The evidence hash and
   every output are then registered in ONE all-or-nothing ``register_many`` batch, and only after it
   succeeds is the evidence artifact written. It returns the report ``TaskContract``.
2. That task runs through the same planner and executor (policy, grant, provenance, guarded invocation).
   ``PipelineReportAdapter`` (wired with the same read-only anchored reader; unavailable otherwise)
   re-validates the evidence: receipt chain structure, the ledger entry for its envelope hash (receipts,
   task id/type/hash, template and stage bindings via ``stage_bindings_hash``), the graph (every stage's
   inputs are exactly the task inputs and upstream outputs its bindings name, in binding order) and the
   final outputs it was handed. It then RE-PERFORMS the algorithm replay itself, inside its own stage, on
   its authorized inputs only (the dataset, the result and the reproducibility record are inputs of the
   report task): ``verify_replay(ExpectedStage.from_receipt(<the ledger-recorded receipt>), ...)`` with
   the request parameters bound by that receipt's parameters hash. Only the verdict it computed goes into
   the report; a builder verdict that differs is refused (``REPORT_REPLAY_ADVISORY_MISMATCH``), a replay that
   does not match fails closed (``REPORT_REPLAY_FAILED``), and a replay that cannot run refuses. Evidence it
   cannot resolve is refused, never labelled authentic. It writes ``intelligence.pipeline-report/v1``,
   whose hash is an output of the report plan's final receipt.

The report is labelled ``PIPELINE_REPORT_CANDIDATE`` with ``authoritative = false`` and
``semantic_authority = NONE``; it has no acceptance field and accepts nothing. It holds ids, digests,
codes and artifact references only, never document text.
"""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from pados.capability.adapter import CapabilityAdapterError, StageContext, StageOutput, StageRequest
from pados.capability.artifacts import ArtifactAccessDenied, ArtifactIntegrityError, ArtifactRef, ArtifactStore
from pados.capability.execution_ledger import (
    ExecutionLedgerError,
    ExecutionLedgerReader,
    LedgerEntry,
    require_reader,
    stage_bindings_hash,
)
from pados.capability.executor import ExecutionResult, ExecutionStatus
from pados.capability.graph import InputBinding, InputSource
from pados.capability.model import (
    AdapterDescriptor,
    CapabilityDescriptor,
    CapabilityFamily,
    Determinism,
    FailureSemantics,
    FallbackPolicy,
    LicenseInfo,
    Modality,
    VerificationMethod,
)
from pados.capability.planner import ExecutionPlan, plan_hash_of
from pados.capability.provenance import ArtifactProvenance, ProvenanceRefused, check_task_input, resolved_provenance
from pados.capability.receipts import PlanReceipt, StageReceipt, VerificationStatus, parameters_hash
from pados.capability.task import Scalar, TaskContract, TaskInput, validated_task
from pados.connectivity.requirements import NetworkRequirement
from pados.enterprise.classification import DataClassification
from pados.execution.context import canonical_hash
from pados.lifecycle.minimization import DataMinimizationViolation, assert_minimized
from pados.packs.algorithms.contracts import (
    JSON_MEDIA,
    AlgorithmError,
    canonical_json,
    parse_json_document,
    sha256_hex,
    validated,
)
from pados.packs.algorithms.registry import ALGORITHMS
from pados.packs.algorithms.replay import VERIFIER_ID as REPLAY_VERIFIER_ID
from pados.packs.algorithms.replay import ExpectedStage, verify_replay
from pados.packs.intelligence.template import (
    EVIDENCE_INPUT,
    FINAL_OUTPUT_KINDS,
    PIPELINE_REPORT,
    REPLAY_INPUT_KINDS,
    REPORT_TASK_TYPE,
)

EVIDENCE_SCHEMA: Final = "pados.intelligence.pipeline-evidence.v1"
EVIDENCE_KIND = "intelligence.pipeline-evidence"
EVIDENCE_CONTRACT = "intelligence.pipeline-evidence/v1"
REPORT_SCHEMA: Final = "pados.intelligence.pipeline-report.v1"
REPORT_KIND = "intelligence.pipeline-report"
REPORT_CONTRACT = "intelligence.pipeline-report/v1"
REPORT_ADAPTER_ID = "intelligence.pipeline-report"
REPORT_LABEL: Final = "PIPELINE_REPORT_CANDIDATE"
MAX_EVIDENCE_BYTES = 1024 * 1024
MAX_REPORT_BYTES = 1024 * 1024
EVIDENCE_KEYS = frozenset({"schema", "semantic_authority", "task_id", "task_type", "task_hash", "template_id",
                           "template_hash", "plan_id", "plan_hash", "task_inputs", "graph", "receipt",
                           "envelope_hash", "replay", "replay_inputs", "replay_parameters", "final_outputs"})
REPORT_KEYS = frozenset({"schema", "label", "authoritative", "semantic_authority", "evidence_sha256", "task_id",
                         "task_type", "task_hash", "template_id", "template_hash", "plan_id", "plan_hash",
                         "receipt_envelope_hash", "receipt_structure_verified", "graph_consistent",
                         "human_decision_ref", "stages", "verification_summary", "replay", "final_outputs"})
_SHA256 = r"^[0-9a-f]{64}$"
_ID = r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,79}$"
_NAME = r"^[a-z][a-z0-9_]{0,39}$"
_CODE = r"^[A-Z][A-Z0-9_]{2,79}$"
_FROZEN = ConfigDict(frozen=True, extra="forbid", populate_by_name=True)
_REPLAYABLE = frozenset(spec.capability.capability_id for spec in ALGORITHMS.values() if spec.compute is not None)
REPLAY_MATCH = "REPLAY_MATCH"
COMPLETED = ExecutionStatus.COMPLETED.value

CAPABILITY = CapabilityDescriptor(
    capability_id=PIPELINE_REPORT, version="1", family=CapabilityFamily.VERIFICATION,
    operation="SUMMARIZE_PIPELINE_EVIDENCE", input_contract=EVIDENCE_CONTRACT, output_contract=REPORT_CONTRACT,
    determinism=Determinism.DETERMINISTIC, modalities=(Modality.STRUCTURED,),
    max_classification=DataClassification.RESTRICTED, network_requirement=NetworkRequirement.LOCAL_ONLY,
    verification=(VerificationMethod.SCHEMA, VerificationMethod.DETERMINISTIC_REPLAY),
    failure=FailureSemantics.FAIL_CLOSED, fallback=FallbackPolicy.NONE,
    description="Content-free report of one capability pipeline run (evidence, never acceptance)")


class ReportError(ValueError):
    """Report failure with a closed code."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def minimized(document: Mapping[str, Any], keys: frozenset[str], code: str, *, max_total_bytes: int) -> None:
    """``assert_minimized`` with an explicit schema before any write; a closed code with no exception chain."""
    try:
        assert_minimized(document, allowed_keys=keys, max_string=256, max_items=256, max_total_bytes=max_total_bytes)
        return
    except DataMinimizationViolation:
        pass
    raise ReportError(code)


def report_descriptor() -> AdapterDescriptor:
    return AdapterDescriptor(
        adapter_id=REPORT_ADAPTER_ID, capability_id=PIPELINE_REPORT, version="1", backend="python",
        determinism=Determinism.DETERMINISTIC, max_classification=DataClassification.RESTRICTED,
        network_requirement=NetworkRequirement.LOCAL_ONLY, license=LicenseInfo(spdx="AGPL-3.0-only"))


# --- evidence (written by trusted operator code, re-validated by the report adapter) ---------------

class NamedRef(BaseModel):
    model_config = _FROZEN

    name: str = Field(pattern=_NAME)
    ref: ArtifactRef


class StageGraph(BaseModel):
    model_config = _FROZEN

    stage_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    capability_id: str = Field(pattern=r"^[A-Z][A-Z0-9_]{2,63}$")
    inputs: tuple[InputBinding, ...] = Field(max_length=16)


class ReplayVerdict(BaseModel):
    """The algorithm pack's replay verdict for one stage, bound to that stage's exact receipt hash."""

    model_config = _FROZEN

    stage_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    receipt_hash: str = Field(pattern=_SHA256)
    method: VerificationMethod
    status: VerificationStatus
    code: str = Field(pattern=_CODE)
    verifier_id: str = Field(pattern=_ID)
    evidence_hash: str = Field(pattern=_SHA256)


class ReplayParameters(BaseModel):
    """The request parameters of one algorithm stage; the adapter binds them to the receipt's parameters hash."""

    model_config = _FROZEN

    stage_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{0,39}$")
    parameters: dict[str, Scalar] = Field(max_length=32)


class PipelineEvidence(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["pados.intelligence.pipeline-evidence.v1"] = Field(default=EVIDENCE_SCHEMA,
                                                                                 alias="schema")
    semantic_authority: Literal["NONE"] = "NONE"
    task_id: str = Field(pattern=_ID)
    task_type: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,63}$")
    task_hash: str = Field(pattern=_SHA256)
    template_id: str = Field(pattern=r"^[a-z][a-z0-9_.-]{2,63}$")
    template_hash: str = Field(pattern=_SHA256)
    plan_id: str = Field(pattern=_ID)
    plan_hash: str = Field(pattern=_SHA256)
    task_inputs: tuple[NamedRef, ...] = Field(max_length=64)
    graph: tuple[StageGraph, ...] = Field(min_length=1, max_length=64)
    receipt: PlanReceipt
    envelope_hash: str = Field(pattern=_SHA256)
    replay: tuple[ReplayVerdict, ...] = Field(max_length=64)  # the builder's verdicts: ADVISORY only
    replay_inputs: tuple[NamedRef, ...] = Field(max_length=16)  # the algorithm stage's dataset and record
    replay_parameters: tuple[ReplayParameters, ...] = Field(max_length=64)
    final_outputs: tuple[NamedRef, ...] = Field(max_length=16)

    def canonical(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


# --- report ---------------------------------------------------------------------------------------

class ReportArtifact(BaseModel):
    model_config = _FROZEN

    sha256: str = Field(pattern=_SHA256)
    kind: str
    media_type: str
    size_bytes: int = Field(ge=0)
    source: str | None = Field(default=None, pattern=r"^(task|stage):[a-z][a-z0-9_-]{0,39}$")


class ReportVerification(BaseModel):
    model_config = _FROZEN

    method: VerificationMethod
    status: VerificationStatus
    verifier_id: str | None = None
    evidence_hash: str | None = Field(default=None, pattern=_SHA256)


class ReportStage(BaseModel):
    model_config = _FROZEN

    stage_id: str
    attempt_id: str
    capability_id: str
    capability_family: CapabilityFamily
    capability_version: str
    capability_digest: str = Field(pattern=_SHA256)
    adapter_id: str
    adapter_version: str
    adapter_digest: str = Field(pattern=_SHA256)
    determinism: Determinism
    resource_id: str
    parameters_hash: str = Field(pattern=_SHA256)
    policy_decision_hash: str = Field(pattern=_SHA256)
    receipt_hash: str = Field(pattern=_SHA256)
    inputs: tuple[ReportArtifact, ...]
    outputs: tuple[ReportArtifact, ...]
    consumed_by: tuple[str, ...]
    verification: ReportVerification
    semantic_authority: Literal["NONE"] = "NONE"


class PipelineReport(BaseModel):
    """``intelligence.pipeline-report/v1``: a candidate summary of verified evidence, never acceptance."""

    model_config = _FROZEN

    schema_version: Literal["pados.intelligence.pipeline-report.v1"] = Field(default=REPORT_SCHEMA, alias="schema")
    label: Literal["PIPELINE_REPORT_CANDIDATE"] = REPORT_LABEL
    authoritative: Literal[False] = False
    semantic_authority: Literal["NONE"] = "NONE"
    evidence_sha256: str = Field(pattern=_SHA256)
    task_id: str
    task_type: str
    task_hash: str = Field(pattern=_SHA256)
    template_id: str
    template_hash: str = Field(pattern=_SHA256)
    plan_id: str
    plan_hash: str = Field(pattern=_SHA256)
    receipt_envelope_hash: str = Field(pattern=_SHA256)
    receipt_structure_verified: Literal[True] = True
    graph_consistent: Literal[True] = True
    human_decision_ref: str | None = None
    stages: tuple[ReportStage, ...] = Field(min_length=1, max_length=64)
    verification_summary: dict[str, int]
    replay: tuple[ReplayVerdict, ...]
    final_outputs: tuple[NamedRef, ...]

    @model_validator(mode="after")
    def summary_matches(self) -> PipelineReport:
        counts = {status.value: 0 for status in VerificationStatus}
        for stage in self.stages:
            counts[stage.verification.status.value] += 1
        if self.verification_summary != counts:
            raise ValueError("REPORT_SUMMARY_INVALID")
        return self

    def canonical(self) -> dict[str, Any]:
        return self.model_dump(mode="json", by_alias=True)


def _artifact(ref: ArtifactRef, source: str | None = None) -> ReportArtifact:
    return ReportArtifact(sha256=ref.sha256, kind=ref.kind, media_type=ref.media_type, size_bytes=ref.size_bytes,
                          source=source)


def expected_inputs(stage: StageGraph, task_inputs: Mapping[str, ArtifactRef],
                    produced: Mapping[str, tuple[ArtifactRef, ...]]) -> list[tuple[ArtifactRef, str]]:
    """The inputs the fabric derives for ``stage`` from its bindings (binding order, de-duplicated)."""
    refs: dict[str, tuple[ArtifactRef, str]] = {}
    for binding in stage.inputs:
        if binding.source is InputSource.TASK_INPUT:
            if binding.name not in task_inputs:
                raise ReportError("REPORT_GRAPH_INCONSISTENT")
            found = [(task_inputs[binding.name], f"task:{binding.name}")]
        else:
            if binding.name not in produced:
                raise ReportError("REPORT_GRAPH_INCONSISTENT")
            found = [(ref, f"stage:{binding.name}") for ref in produced[binding.name]
                     if binding.artifact_kind is None or ref.kind == binding.artifact_kind]
        for ref, source in found:
            refs.setdefault(ref.sha256, (ref, source))
    return list(refs.values())


def recorded_execution(ledger: ExecutionLedgerReader | None, receipt: PlanReceipt, *, task_id: str,
                       task_type: str, task_hash: str) -> LedgerEntry:
    """The trusted ledger entry for exactly this receipt chain and task (CR-16 H-01); anything else refuses.

    The receipts are claims until the executor-written ledger holds an entry under their envelope hash that
    records the same plan, task and every stage row (receipt hash, verification method, status, verifier
    and evidence hash). Only a read-only, anchored ``ExecutionLedgerReader`` is accepted (CR-16b H-04:
    ``EXECUTION_LEDGER_READER_REQUIRED`` / ``EXECUTION_LEDGER_ANCHOR_REQUIRED``). A damaged ledger refuses with
    its own closed code (fail closed)."""
    if ledger is None:
        raise ReportError("REPORT_EXECUTION_LEDGER_MISSING")
    code = "REPORT_EXECUTION_NOT_IN_LEDGER"
    try:
        entry = require_reader(ledger).resolve(receipt.envelope_hash)
    except ExecutionLedgerError as exc:
        entry, code = None, exc.code
    if (entry is None or not entry.matches(receipt) or entry.status != COMPLETED or entry.task_hash != task_hash
            or entry.task_id != task_id or entry.task_type != task_type):
        raise ReportError(code)
    return entry


def graph_bindings_hash(template_id: str | None, template_hash: str | None, graph: Sequence[StageGraph]) -> str:
    return stage_bindings_hash(template_id, template_hash,
                               [(stage.stage_id, stage.capability_id, stage.inputs) for stage in graph])


def _check_chain(evidence: PipelineEvidence,
                 ledger: ExecutionLedgerReader | None) -> tuple[PlanReceipt, LedgerEntry]:
    receipt = evidence.receipt
    if not receipt.verify_structure():
        raise ReportError("REPORT_RECEIPT_STRUCTURE_INVALID")
    if (receipt.envelope_hash != evidence.envelope_hash or receipt.plan_id != evidence.plan_id
            or receipt.plan_hash != evidence.plan_hash):
        raise ReportError("REPORT_EVIDENCE_INCONSISTENT")
    entry = recorded_execution(ledger, receipt, task_id=evidence.task_id, task_type=evidence.task_type,
                               task_hash=evidence.task_hash)
    if graph_bindings_hash(evidence.template_id, evidence.template_hash, evidence.graph) != entry.bindings_hash:
        raise ReportError("REPORT_GRAPH_NOT_RECORDED")
    if tuple(stage.stage_id for stage in evidence.graph) != receipt.expected_stage_ids:
        raise ReportError("REPORT_GRAPH_INCONSISTENT")
    if any(stage.failure_code is not None or not stage.outputs or stage.partial for stage in receipt.stages):
        raise ReportError("REPORT_ANALYSIS_NOT_COMPLETED")
    return receipt, entry


def replay_match_evidence(stage: StageReceipt) -> str | None:
    """The algorithm pack's ``verify_replay`` evidence hash of a ``REPLAY_MATCH`` for exactly this receipt.

    A match proves the recomputed result hash equals the recorded one, and the replay binding requires the
    receipt's outputs to be (result, record) with those exact hashes, so the evidence is a function of the
    receipt alone. ``build_report_task`` checks this against the verdict ``verify_replay`` actually returned
    (a change in the algorithm pack's evidence format fails closed, never open)."""
    if len(stage.outputs) != 2:
        return None
    return canonical_hash({"verifier_id": REPLAY_VERIFIER_ID,
                           "method": VerificationMethod.DETERMINISTIC_REPLAY.value, "code": REPLAY_MATCH,
                           "status": VerificationStatus.PASSED.value, "record_sha256": stage.outputs[1].sha256,
                           "recomputed_result_hash": stage.outputs[0].sha256})


def _check_replay(evidence: PipelineEvidence, receipt: PlanReceipt, entry: LedgerEntry) -> None:
    """Every (advisory) builder verdict is a PASSED ``REPLAY_MATCH`` bound to the ledger's receipt hash of its
    stage. This alone authenticates nothing: ``replay_in_stage`` re-performs the replay."""
    verdicts: dict[str, ReplayVerdict] = {}
    for verdict in evidence.replay:
        if verdict.stage_id in verdicts:
            raise ReportError("REPORT_REPLAY_DUPLICATE")
        verdicts[verdict.stage_id] = verdict
    replayable = {stage.stage_id: stage for stage in receipt.stages if stage.capability_id in _REPLAYABLE}
    if set(verdicts) != set(replayable):
        raise ReportError("REPORT_REPLAY_MISSING")
    for stage_id, verdict in verdicts.items():
        if (verdict.method is not VerificationMethod.DETERMINISTIC_REPLAY
                or verdict.status is not VerificationStatus.PASSED or verdict.code != REPLAY_MATCH
                or verdict.verifier_id != REPLAY_VERIFIER_ID):
            raise ReportError("REPORT_REPLAY_NOT_PASSED")
        stage, recorded = replayable[stage_id], entry.stage(stage_id)
        if (recorded is None or verdict.receipt_hash != recorded.receipt_hash
                or verdict.receipt_hash != stage.receipt_hash
                or verdict.evidence_hash != replay_match_evidence(stage)):
            raise ReportError("REPORT_REPLAY_NOT_BOUND")


def _verdict(stage: StageReceipt, method: VerificationMethod, status: VerificationStatus, code: str,
             verifier_id: str, evidence_hash: str) -> ReplayVerdict:
    return ReplayVerdict(stage_id=stage.stage_id, receipt_hash=stage.receipt_hash, method=method, status=status,
                         code=code, verifier_id=verifier_id, evidence_hash=evidence_hash)


def replay_in_stage(evidence: PipelineEvidence, receipt: PlanReceipt, entry: LedgerEntry,
                    read: Callable[[ArtifactRef], bytes]) -> tuple[ReplayVerdict, ...]:
    """Re-perform the algorithm replay inside the report stage (CR-16b H-05); never trust a supplied verdict.

    The stage is the LEDGER-RECORDED receipt (``entry.matches`` holds, so every field, including its digests,
    inputs, outputs and parameters hash, is the recorded one). Its replay artifacts must be exactly the
    evidence's named replay inputs and are read through ``read`` (the stage-scoped artifact store: only the
    report task's authorized inputs, hash-checked). The request parameters must hash to the receipt's
    parameters hash. The verdict this function computes is the only one the report records; a replay that
    does not match fails closed, and one that cannot run refuses."""
    parameters: dict[str, Mapping[str, Scalar]] = {}
    for item in evidence.replay_parameters:
        if item.stage_id in parameters:
            raise ReportError("REPORT_REPLAY_DUPLICATE")
        parameters[item.stage_id] = item.parameters
    replayable = [stage for stage in receipt.stages if stage.capability_id in _REPLAYABLE]
    if set(parameters) != {stage.stage_id for stage in replayable}:
        raise ReportError("REPORT_REPLAY_PARAMETERS_MISSING")
    if len(replayable) != 1:  # V1: one algorithm stage per report task (its inputs are named once)
        raise ReportError("REPORT_REPLAY_BINDING_INVALID")
    (stage,) = replayable
    recorded = entry.stage(stage.stage_id)
    if recorded is None or recorded.receipt_hash != stage.receipt_hash:
        raise ReportError("REPORT_EXECUTION_NOT_IN_LEDGER")
    if len(stage.inputs) != 1 or len(stage.outputs) != 2:
        raise ReportError("REPORT_REPLAY_BINDING_INVALID")
    named = {item.name: item.ref for item in evidence.replay_inputs}
    if len(named) != len(evidence.replay_inputs) or named != {"dataset": stage.inputs[0], "record": stage.outputs[1]}:
        raise ReportError("REPORT_REPLAY_INPUTS_INVALID")
    request = parameters[stage.stage_id]
    try:
        bound = parameters_hash(request) == stage.parameters_hash
    except (TypeError, ValueError):
        bound = False
    if not bound:
        raise ReportError("REPORT_REPLAY_PARAMETERS_NOT_BOUND")
    dataset, result, record = read(stage.inputs[0]), read(stage.outputs[0]), read(stage.outputs[1])
    outcome = verify_replay(ExpectedStage.from_receipt(stage), record, result, dataset, request)
    computed = _verdict(stage, outcome.method, outcome.status, outcome.code, outcome.verifier_id,
                        outcome.evidence_hash)
    if not outcome.passed:
        raise ReportError("REPORT_REPLAY_FAILED")
    advisory = [item for item in evidence.replay if item.stage_id == stage.stage_id]
    if advisory != [computed]:  # the builder's verdict is a cross-check only, and it must agree
        raise ReportError("REPORT_REPLAY_ADVISORY_MISMATCH")
    return (computed,)


def build_report(evidence: PipelineEvidence, evidence_sha256: str, *, ledger: ExecutionLedgerReader | None,
                 read: Callable[[ArtifactRef], bytes]) -> PipelineReport:
    """Authenticate the evidence against the trusted ledger, re-validate it, re-perform the algorithm replay
    through ``read`` and derive the report (closed ``REPORT_*`` codes, or the ledger's own code when the
    ledger is unusable)."""
    receipt, entry = _check_chain(evidence, ledger)
    _check_replay(evidence, receipt, entry)
    replay = replay_in_stage(evidence, receipt, entry, read)
    task_inputs = {item.name: item.ref for item in evidence.task_inputs}
    if set(task_inputs) != {binding.name for stage in evidence.graph for binding in stage.inputs
                            if binding.source is InputSource.TASK_INPUT}:
        raise ReportError("REPORT_GRAPH_INCONSISTENT")  # every named task input is consumed by a recorded stage
    produced: dict[str, tuple[ArtifactRef, ...]] = {}
    sources: dict[str, list[tuple[ArtifactRef, str]]] = {}
    consumers: dict[str, list[str]] = {stage.stage_id: [] for stage in receipt.stages}
    for graph, stage in zip(evidence.graph, receipt.stages, strict=True):
        if graph.capability_id != stage.capability_id:
            raise ReportError("REPORT_GRAPH_INCONSISTENT")
        expected = expected_inputs(graph, task_inputs, produced)
        if [ref for ref, _ in expected] != list(stage.inputs):
            raise ReportError("REPORT_GRAPH_INCONSISTENT")
        sources[stage.stage_id] = expected
        for upstream in sorted({b.name for b in graph.inputs if b.source is InputSource.STAGE_OUTPUT}):
            consumers[upstream].append(stage.stage_id)
        produced[stage.stage_id] = stage.outputs
    outputs = {ref.sha256: ref for stage in receipt.stages for ref in stage.outputs}
    names = [item.name for item in evidence.final_outputs]
    if sorted(names) != sorted(FINAL_OUTPUT_KINDS) or any(
            outputs.get(item.ref.sha256) != item.ref or item.ref.kind != FINAL_OUTPUT_KINDS[item.name]
            for item in evidence.final_outputs):
        raise ReportError("REPORT_FINAL_OUTPUT_INVALID")
    stages = tuple(_report_stage(stage, sources[stage.stage_id], consumers[stage.stage_id])
                   for stage in receipt.stages)
    counts = {status.value: 0 for status in VerificationStatus}
    for stage in receipt.stages:
        counts[stage.verification.value] += 1
    return PipelineReport(
        evidence_sha256=evidence_sha256, task_id=evidence.task_id, task_type=evidence.task_type,
        task_hash=evidence.task_hash, template_id=evidence.template_id, template_hash=evidence.template_hash,
        plan_id=receipt.plan_id, plan_hash=receipt.plan_hash, receipt_envelope_hash=receipt.envelope_hash,
        human_decision_ref=receipt.human_decision_ref, stages=stages, verification_summary=counts,
        replay=tuple(sorted(replay, key=lambda item: item.stage_id)),
        final_outputs=tuple(sorted(evidence.final_outputs, key=lambda item: item.name)))


def _report_stage(stage: StageReceipt, inputs: Sequence[tuple[ArtifactRef, str]],
                  consumed_by: Sequence[str]) -> ReportStage:
    return ReportStage(
        stage_id=stage.stage_id, attempt_id=stage.attempt_id, capability_id=stage.capability_id,
        capability_family=stage.capability_family, capability_version=stage.capability_version,
        capability_digest=stage.capability_digest, adapter_id=stage.adapter_id,
        adapter_version=stage.adapter_version, adapter_digest=stage.adapter_digest, determinism=stage.determinism,
        resource_id=stage.resource_id, parameters_hash=stage.parameters_hash,
        policy_decision_hash=stage.policy_decision_hash, receipt_hash=stage.receipt_hash,
        inputs=tuple(_artifact(ref, source) for ref, source in inputs),
        outputs=tuple(_artifact(ref) for ref in stage.outputs), consumed_by=tuple(consumed_by),
        verification=ReportVerification(method=stage.verification_method, status=stage.verification,
                                        verifier_id=stage.verifier_id,
                                        evidence_hash=stage.verification_evidence_hash))


def load_evidence(payload: bytes) -> PipelineEvidence:
    try:
        document = parse_json_document(payload, max_bytes=MAX_EVIDENCE_BYTES, code="EVIDENCE")
        evidence = validated(PipelineEvidence, document, "REPORT_EVIDENCE_INVALID")
    except AlgorithmError:
        evidence = None
    if evidence is None or canonical_json(evidence.canonical()) != payload:
        raise ReportError("REPORT_EVIDENCE_INVALID")
    return evidence


def load_report(payload: bytes) -> PipelineReport:
    try:
        document = parse_json_document(payload, max_bytes=MAX_REPORT_BYTES, code="REPORT")
        report = validated(PipelineReport, document, "REPORT_SCHEMA_INVALID")
    except AlgorithmError:
        report = None
    if report is None or canonical_json(report.canonical()) != payload:
        raise ReportError("REPORT_SCHEMA_INVALID")
    return report


class PipelineReportAdapter:
    """Deterministic PIPELINE_REPORT adapter (invoked only through ``run_bound_stage``).

    The operator wires it with a read-only, anchored ``ExecutionLedgerReader`` (never the writer: a
    writer-capable object is refused with ``EXECUTION_LEDGER_READER_REQUIRED``); without an anchored reader it
    is unavailable, so a report task is never planned on unauthenticated evidence."""

    def __init__(self, ledger: ExecutionLedgerReader | None = None) -> None:
        if ledger is not None and type(ledger) is not ExecutionLedgerReader:
            raise ValueError("EXECUTION_LEDGER_READER_REQUIRED")
        self._descriptor = report_descriptor()
        self._ledger = ledger

    @property
    def descriptor(self) -> AdapterDescriptor:
        return self._descriptor

    def available(self) -> bool:
        return type(self._ledger) is ExecutionLedgerReader and self._ledger.anchored

    def execute(self, request: StageRequest, *, context: StageContext) -> StageOutput:
        try:
            return self._execute(request, context)
        except ArtifactAccessDenied:
            raise
        except ReportError as exc:
            code = exc.code
        except ArtifactIntegrityError:
            code = "REPORT_INPUT_UNREADABLE"
        except Exception:  # noqa: BLE001 - closed-error boundary
            code = "REPORT_UNEXPECTED_ERROR"
        raise CapabilityAdapterError(code) from None  # outside the handler: empty exception chain

    def _execute(self, request: StageRequest, context: StageContext) -> StageOutput:
        if request.capability_id != PIPELINE_REPORT or request.seed is not None or request.parameters:
            raise ReportError("REPORT_REQUEST_INVALID")
        sources = [ref for ref in request.inputs if ref.kind == EVIDENCE_KIND]
        if len(sources) != 1 or sources[0].media_type != JSON_MEDIA:
            raise ReportError("REPORT_EVIDENCE_INPUT_INVALID")
        evidence = load_evidence(context.artifacts.get(sources[0]))
        supplied = {ref.sha256: ref for ref in request.inputs if ref.kind != EVIDENCE_KIND}
        expected = {item.ref.sha256: item.ref for item in (*evidence.final_outputs, *evidence.replay_inputs)}
        if supplied != expected:
            raise ReportError("REPORT_FINAL_OUTPUT_MISMATCH")
        for ref in supplied.values():
            context.artifacts.get(ref)  # the store re-checks hash and size: a tampered output refuses
        report = build_report(evidence, sources[0].sha256, ledger=self._ledger, read=context.artifacts.get)
        document = report.canonical()
        minimized(document, REPORT_KEYS, "REPORT_NOT_MINIMIZED", max_total_bytes=MAX_REPORT_BYTES)
        payload = canonical_json(document)
        if len(payload) > MAX_REPORT_BYTES:
            raise ReportError("REPORT_TOO_LARGE")
        ref = context.artifacts.put(payload, media_type=JSON_MEDIA, kind=REPORT_KIND)
        evidence_fields: dict[str, str | int | float | bool | None] = {
            "output_contract": REPORT_CONTRACT, "evidence_sha256": sources[0].sha256, "report_sha256": ref.sha256,
            "analysis_plan_hash": report.plan_hash, "stages": len(report.stages),
            "replay_verdicts": len(report.replay), "adapter_id": REPORT_ADAPTER_ID,
        }
        return StageOutput(outputs=(ref,), metrics={"stages": float(len(report.stages))}, evidence=evidence_fields)


def report_output_valid(outputs: Sequence[tuple[ArtifactRef, bytes]]) -> bool:
    """SCHEMA check for ``intelligence.pipeline-report/v1``."""
    if len(outputs) != 1 or outputs[0][0].kind != REPORT_KIND or outputs[0][0].media_type != JSON_MEDIA:
        return False
    try:
        load_report(outputs[0][1])
    except ReportError:
        return False
    return True


# --- operator side: evidence builder ---------------------------------------------------------------

class ProvenanceRegistrar(Protocol):
    """The operator's governed provenance registry: resolve, and register a batch ALL OR NOTHING
    (insert-if-absent with conflict checking under one lock), e.g.
    ``pados.packs.intelligence.provenance.AtomicInputProvenance``."""

    def resolve(self, sha256: str) -> ArtifactProvenance | tuple[str, DataClassification] | None: ...

    def register_many(self, entries: Sequence[tuple[str, str, DataClassification]]) -> int: ...


@dataclass(frozen=True)
class ReportTask:
    task: TaskContract
    evidence: ArtifactRef
    replay: tuple[ReplayVerdict, ...]


def _read_all(store: ArtifactStore, refs: Sequence[ArtifactRef]) -> list[bytes] | None:
    try:
        return [store.get(ref) for ref in refs]
    except ArtifactIntegrityError:
        return None


def replay_algorithm_stages(plan: ExecutionPlan, receipt: PlanReceipt,
                            store: ArtifactStore) -> tuple[ReplayVerdict, ...]:
    """The algorithm pack's ``verify_replay`` for every algorithm stage, bound to its receipt."""
    parameters = {stage.stage_id: stage.parameters for stage in plan.stages}
    verdicts: list[ReplayVerdict] = []
    for stage in receipt.stages:
        if stage.capability_id not in _REPLAYABLE:
            continue
        if len(stage.inputs) != 1 or len(stage.outputs) != 2 or stage.stage_id not in parameters:
            raise ReportError("REPORT_REPLAY_BINDING_INVALID")
        loaded = _read_all(store, (stage.inputs[0], *stage.outputs))
        if loaded is None:
            raise ReportError("REPORT_ARTIFACT_UNREADABLE")
        dataset, result, record = loaded
        verdict = verify_replay(ExpectedStage.from_receipt(stage), record, result, dataset,
                                parameters[stage.stage_id])
        verdicts.append(ReplayVerdict(stage_id=stage.stage_id, receipt_hash=stage.receipt_hash,
                                      method=verdict.method, status=verdict.status, code=verdict.code,
                                      verifier_id=verdict.verifier_id, evidence_hash=verdict.evidence_hash))
    return tuple(verdicts)


def derived_classification(task: TaskContract, provenance: ProvenanceRegistrar) -> DataClassification:
    """The highest classification of the task's inputs as the trusted provenance resolves them (never lower;
    the caller's labels must agree with it and the task ceiling must permit it)."""
    levels: list[DataClassification] = []
    for item in task.inputs:
        try:
            levels.append(check_task_input(task, item, provenance).classification)
        except ProvenanceRefused as exc:
            code = exc.code
        else:
            continue
        raise ReportError(code)
    return max(levels, key=lambda level: level.rank) if levels else task.classification


def _existing(provenance: ProvenanceRegistrar, sha256: str) -> ArtifactProvenance | None:
    code = "REPORT_PROVENANCE_UNAVAILABLE"
    try:
        return resolved_provenance(provenance, sha256)
    except ProvenanceRefused as exc:
        if exc.code == "INPUT_PROVENANCE_UNKNOWN":
            return None
        code = exc.code
    raise ReportError(code)


def _check_registrations(provenance: ProvenanceRegistrar, hashes: Sequence[str], *, project_id: str,
                         classification: DataClassification) -> list[str]:
    """The hashes still to register; an existing registration must be exactly (project, classification)."""
    pending: list[str] = []
    for sha256 in dict.fromkeys(hashes):
        existing = _existing(provenance, sha256)
        if existing is None:
            pending.append(sha256)
        elif existing.project_id != project_id:
            raise ReportError("REPORT_OUTPUT_PROJECT_CONFLICT")
        elif existing.classification.rank < classification.rank:
            raise ReportError("REPORT_OUTPUT_CLASSIFICATION_DOWNGRADE")
        elif existing.classification is not classification:
            raise ReportError("REPORT_OUTPUT_REGISTERED_ELSEWHERE")
    return pending


def _replay_inputs(receipt: PlanReceipt) -> tuple[NamedRef, ...]:
    """The algorithm stage's dataset and reproducibility record (the result is a final output)."""
    replayable = [stage for stage in receipt.stages if stage.capability_id in _REPLAYABLE]
    if len(replayable) != 1 or len(replayable[0].inputs) != 1 or len(replayable[0].outputs) != 2:
        raise ReportError("REPORT_REPLAY_BINDING_INVALID")
    (stage,) = replayable
    refs = {"dataset": stage.inputs[0], "record": stage.outputs[1]}
    if any(refs[name].kind != kind for name, kind in REPLAY_INPUT_KINDS.items()):
        raise ReportError("REPORT_REPLAY_BINDING_INVALID")
    return tuple(NamedRef(name=name, ref=refs[name]) for name in REPLAY_INPUT_KINDS)


def build_report_task(task: TaskContract, plan: ExecutionPlan, result: ExecutionResult, *, store: ArtifactStore,
                      provenance: ProvenanceRegistrar, ledger: ExecutionLedgerReader) -> ReportTask:
    """Evidence artifact + provenance registration + the report task for one COMPLETED, LEDGER-RECORDED run."""
    if not callable(getattr(provenance, "register_many", None)):
        raise ReportError("REPORT_PROVENANCE_NOT_ATOMIC")
    task = validated_task(task)
    receipt = result.plan_receipt
    if (plan_hash_of(plan) != plan.plan_hash or plan.task_hash != task.task_hash or receipt.plan_id != plan.plan_id
            or receipt.plan_hash != plan.plan_hash):
        raise ReportError("REPORT_PLAN_MISMATCH")
    if result.status is not ExecutionStatus.COMPLETED or not receipt.verify_structure():
        raise ReportError("REPORT_ANALYSIS_NOT_COMPLETED")
    if plan.template_id is None or plan.template_hash is None:
        raise ReportError("REPORT_PLAN_MISMATCH")
    entry = recorded_execution(ledger, receipt, task_id=task.task_id, task_type=task.task_type,
                               task_hash=task.task_hash)
    graph = tuple(StageGraph(stage_id=stage.stage_id, capability_id=stage.capability_id, inputs=stage.inputs)
                  for stage in plan.stages)
    if graph_bindings_hash(plan.template_id, plan.template_hash, graph) != entry.bindings_hash:
        raise ReportError("REPORT_GRAPH_NOT_RECORDED")
    replay = replay_algorithm_stages(plan, receipt, store)
    if any(item.status is not VerificationStatus.PASSED for item in replay):
        raise ReportError("REPORT_REPLAY_FAILED")
    stages = {stage.stage_id: stage for stage in receipt.stages}
    if any(item.evidence_hash != replay_match_evidence(stages[item.stage_id]) for item in replay):
        raise ReportError("REPORT_REPLAY_NOT_BOUND")
    classification = derived_classification(task, provenance)
    finals: list[NamedRef] = []
    for name, kind in FINAL_OUTPUT_KINDS.items():
        found = [ref for stage in receipt.stages for ref in stage.outputs if ref.kind == kind]
        if len(found) != 1:
            raise ReportError("REPORT_FINAL_OUTPUT_AMBIGUOUS")
        finals.append(NamedRef(name=name, ref=found[0]))
    replay_inputs = _replay_inputs(receipt)
    replay_parameters = tuple(ReplayParameters(stage_id=stage.stage_id, parameters=dict(stage.parameters))
                              for stage in plan.stages if stage.capability_id in _REPLAYABLE)
    evidence = PipelineEvidence(
        task_id=task.task_id, task_type=task.task_type, task_hash=task.task_hash, template_id=plan.template_id,
        template_hash=plan.template_hash, plan_id=plan.plan_id, plan_hash=plan.plan_hash,
        task_inputs=tuple(NamedRef(name=item.name, ref=item.ref) for item in task.inputs),
        graph=graph, receipt=receipt, envelope_hash=receipt.envelope_hash, replay=replay,
        replay_inputs=replay_inputs, replay_parameters=replay_parameters, final_outputs=tuple(finals))
    document = evidence.canonical()
    minimized(document, EVIDENCE_KEYS, "REPORT_EVIDENCE_NOT_MINIMIZED", max_total_bytes=MAX_EVIDENCE_BYTES)
    payload = canonical_json(document)
    evidence_sha256 = sha256_hex(payload)
    hashes = (evidence_sha256, *(out.sha256 for stage in receipt.stages for out in stage.outputs))
    _check_registrations(provenance, hashes, project_id=task.project_id, classification=classification)
    # CR-16b H-08: ONE all-or-nothing batch (insert-if-absent, conflicts refuse the whole batch), and the
    # evidence artifact is written only after it succeeded. If the write then fails, the registration names
    # a content hash nothing references yet; a retry writes exactly those bytes (idempotent).
    code: str | None = None
    try:
        provenance.register_many([(sha256, task.project_id, classification) for sha256 in dict.fromkeys(hashes)])
    except ProvenanceRefused as exc:
        code = exc.code
    if code is not None:
        raise ReportError(code)
    ref = store.put(payload, media_type=JSON_MEDIA, kind=EVIDENCE_KIND)
    if ref.sha256 != evidence_sha256:
        raise ReportError("REPORT_EVIDENCE_WRITE_MISMATCH")
    inputs = (TaskInput(name=EVIDENCE_INPUT, ref=ref, project_id=task.project_id, classification=classification),
              *(TaskInput(name=item.name, ref=item.ref, project_id=task.project_id,
                          classification=classification) for item in (*finals, *replay_inputs)))
    report_task = TaskContract(
        task_id=f"{task.task_id[:72]}.report", task_type=REPORT_TASK_TYPE, project_id=task.project_id,
        work_contract_id=task.work_contract_id, classification=task.classification,
        network_policy=NetworkRequirement.LOCAL_ONLY, required_outputs=(REPORT_CONTRACT,), quality=task.quality,
        risk_level=task.risk_level, inputs=inputs)
    return ReportTask(report_task, ref, replay)


__all__ = ["CAPABILITY", "EVIDENCE_KIND", "PIPELINE_REPORT", "REPORT_CONTRACT", "REPORT_KIND", "PipelineEvidence",
           "PipelineReport", "PipelineReportAdapter", "ProvenanceRegistrar", "ReplayParameters", "ReplayVerdict",
           "ReportError", "ReportTask", "build_report", "build_report_task", "derived_classification",
           "load_evidence", "load_report", "recorded_execution", "replay_algorithm_stages", "replay_in_stage",
           "replay_match_evidence", "report_descriptor", "report_output_valid"]

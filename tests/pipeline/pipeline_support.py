"""Hermetic harness for the Phase G pipeline tests (tmp stores, explicit grants, fixed clocks, no network).

Everything runs through the EXISTING fabric planner and executor built from ``wire_pipeline`` state; this
module only supplies what an integrator supplies: a trusted grant source, the governed input provenance (with
atomic batch registration), the trusted execution ledger (the WRITER held by the executor, an external
compare-and-set anchor, and the read-only anchored READER given to the report builder and adapter), a CPU-only
resource inventory and fixed clocks. ``Pipeline.execute`` also binds the executed task as the operator's
optional invocation cross-check (``PipelineWiring.invocations``); the reference hooks read the executor's own
dispatch context.
"""
from __future__ import annotations

import importlib.util
import inspect
import itertools
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pados.capability.adapter import StageContext, StageOutput, StageRequest
from pados.capability.artifacts import ArtifactRef, ObjectStoreArtifacts, ScopedArtifacts
from pados.capability.execution_ledger import ExecutionLedgerReader, FileExecutionLedger, LedgerCheckpoint
from pados.capability import executor as executor_module
from pados.capability.executor import CapabilityExecutor, DispatchContext, ExecutionResult
from pados.capability.planner import CapabilityPlanner, ExecutionPlan, PlannerConfig
from pados.capability.task import TaskContract, TaskInput
from pados.connectivity.requirements import NetworkRequirement
from pados.enterprise.classification import DataClassification
from pados.packs.intelligence.provenance import AtomicInputProvenance
from pados.packs.intelligence.report import ReportTask, build_report_task
from pados.packs.intelligence.template import (
    ANALYSIS_OUTPUTS,
    DOCUMENT_INPUT,
    TASK_TYPE,
    analysis_template,
    report_template,
    stage_grants,
)
from pados.packs.intelligence.wiring import PipelineWiring, wire_pipeline
from pados.packs.ocr.contracts import KIND_SOURCE, PDF_MEDIA_TYPE
from pados.resources.hardware import AcceleratorRuntime, Architecture, HardwareProfile, NodeRole, OperatingSystem
from pados.resources.inventory import StaticResourceInventory

OPERATOR = "operator.pipeline"
PROJECT = "proj-g"
WORK = "wc-g"
CPU_NODE = "cpu-node"
OBSERVED = datetime(2026, 10, 3, 8, 0, tzinfo=UTC)
FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "pipeline" / "decision_document.py"
STAGE_IDS = ("preprocess", "recognize", "layout", "tables", "structure", "extract", "retrieve", "analyze", "explain")
TASK_PARAMETERS: dict[str, str | int] = {
    "object_column": "Proposal", "decision_column": "Rating",
    "column_1": "Cost|cost|NUMERIC|COST",
    "column_2": "Quality|quality|ORDINAL|GAIN|low,medium,high",
    "column_3": "Delivery|delivery_days|NUMERIC|COST",
    "column_4": "Rating|rating|ORDINAL|GAIN|poor,fair,good",
    "query": "lower cost and shorter delivery", "top_k": 3,
}


def _builder() -> Any:
    spec = importlib.util.spec_from_file_location("pipeline_decision_document", FIXTURE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def document_bytes() -> bytes:
    data: bytes = _builder().decision_document_pdf()
    return data


def cpu_node(**overrides: Any) -> HardwareProfile:
    values: dict[str, Any] = dict(resource_id=CPU_NODE, node_role=NodeRole.LOCAL_COMPUTE_NODE,
                                  os=OperatingSystem.LINUX, architecture=Architecture.X86_64, cpu_cores=8,
                                  ram_mb=16_384, runtimes=(AcceleratorRuntime.CPU,), observed_at=OBSERVED)
    values.update(overrides)
    return HardwareProfile(**values)


def inventory(*profiles: HardwareProfile) -> StaticResourceInventory:
    return StaticResourceInventory(profiles or (cpu_node(),), as_of=OBSERVED + timedelta(minutes=1))


class FixedClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 3, 9, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.now += timedelta(milliseconds=5)
        return self.now


class MemoryAnchor:
    """A compare-and-set anchor kept outside the ledger's store (test stand-in for the operator's anchor)."""

    def __init__(self) -> None:
        self.head: LedgerCheckpoint | None = None

    def record(self, previous: LedgerCheckpoint | None, new: LedgerCheckpoint) -> None:
        if previous != self.head or new.seq != (1 if previous is None else previous.seq + 1):
            raise ValueError("ANCHOR_CONFLICT")
        self.head = new

    def latest(self) -> LedgerCheckpoint | None:
        return self.head


@dataclass
class GrantStore:
    """The trusted grant source: the CURRENT project grant document per (project, work contract)."""

    grants: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)

    def current_grant(self, *, project_id: str, work_contract_id: str) -> Mapping[str, Any]:
        try:
            return dict(self.grants[(project_id, work_contract_id)])
        except KeyError as exc:
            raise LookupError("GRANT_NOT_FOUND") from exc

    def grant(self, *capabilities: str, project_id: str = PROJECT, work: str = WORK) -> None:
        self.grants[(project_id, work)] = {"work_contract_id": work, "project_id": project_id,
                                           "capabilities": sorted(set(capabilities))}

    def revoke(self, capability: str, *, project_id: str = PROJECT, work: str = WORK) -> None:
        current = self.grants[(project_id, work)]
        current["capabilities"] = [item for item in current["capabilities"] if item != capability]


@dataclass
class Pipeline:
    root: Path
    store: ObjectStoreArtifacts
    provenance: AtomicInputProvenance
    wiring: PipelineWiring
    grants: GrantStore
    clock: FixedClock
    inventory: StaticResourceInventory | None
    source: ArtifactRef
    classification: DataClassification
    run_ids: Callable[[], str]
    ledger: FileExecutionLedger  # the WRITER: only the executor gets it
    reader: ExecutionLedgerReader  # read-only and anchored: the report builder and adapter get it
    anchor: MemoryAnchor

    def task(self, **overrides: Any) -> TaskContract:
        values: dict[str, Any] = dict(
            task_id="decision-1", task_type=TASK_TYPE, project_id=PROJECT, work_contract_id=WORK,
            classification=self.classification, network_policy=NetworkRequirement.LOCAL_ONLY,
            required_outputs=ANALYSIS_OUTPUTS, parameters=dict(TASK_PARAMETERS),
            inputs=(TaskInput(name=DOCUMENT_INPUT, ref=self.source, project_id=PROJECT,
                              classification=self.classification),))
        values.update(overrides)
        return TaskContract(**values)

    def planner(self, **kwargs: Any) -> CapabilityPlanner:
        kwargs.setdefault("config", self.wiring.planner_config)
        kwargs.setdefault("inventory", self.inventory)
        kwargs.setdefault("provenance", self.provenance)
        return CapabilityPlanner(self.wiring.registry, self.wiring.templates, self.grants, **kwargs)

    def executor(self, **kwargs: Any) -> CapabilityExecutor:
        """Built from the SAME trusted state as ``planner`` (the executor re-derives every plan)."""
        kwargs.setdefault("planner_config", self.wiring.planner_config)
        kwargs.setdefault("inventory", self.inventory)
        kwargs.setdefault("provenance", self.provenance)
        kwargs.setdefault("ledger", self.ledger)
        return CapabilityExecutor(self.wiring.registry, self.store, self.grants, templates=self.wiring.templates,
                                  verifier=self.wiring.verifier, clock=self.clock, run_ids=self.run_ids, **kwargs)

    def execute(self, plan: ExecutionPlan, task: TaskContract, *, executor: CapabilityExecutor | None = None,
                human_decision: Mapping[str, Any] | None = None, **executor_kwargs: Any) -> ExecutionResult:
        """``executor.execute`` with ``task`` bound as the invocation (what the operator does)."""
        chosen = executor or self.executor(**executor_kwargs)
        with self.wiring.invocations.bind(task):
            return chosen.execute(plan, task, human_decision=human_decision)

    def run(self, task: TaskContract | None = None, *, advisor: Any = None, config: PlannerConfig | None = None,
            **executor_kwargs: Any) -> tuple[TaskContract, ExecutionPlan, ExecutionResult]:
        task = task or self.task()
        config = config or self.wiring.planner_config
        plan = self.planner(advisor=advisor, config=config).plan(task)
        result = self.execute(plan, task, advisor=advisor, planner_config=config, **executor_kwargs)
        return task, plan, result

    def build_report(self, task: TaskContract, plan: ExecutionPlan, result: ExecutionResult) -> ReportTask:
        return build_report_task(task, plan, result, store=self.store, provenance=self.provenance,
                                 ledger=self.reader)

    def report(self, task: TaskContract, plan: ExecutionPlan, result: ExecutionResult
               ) -> tuple[ReportTask, ExecutionPlan, ExecutionResult]:
        built = self.build_report(task, plan, result)
        report_plan = self.planner().plan(built.task)
        return built, report_plan, self.execute(report_plan, built.task)

    def outputs(self, result: ExecutionResult) -> dict[str, tuple[ArtifactRef, ...]]:
        return {stage.stage_id: stage.outputs for stage in result.plan_receipt.stages}


def full_grant() -> tuple[str, ...]:
    return tuple(sorted({*stage_grants(analysis_template()), *stage_grants(report_template())}))


def pipeline(tmp_path: Path, *, classification: DataClassification = DataClassification.CONFIDENTIAL,
             resources: StaticResourceInventory | None = None, cpu_inventory: bool = True,
             include_model_explanation: bool = True, document: bytes | None = None, register_source: bool = True,
             store: ObjectStoreArtifacts | None = None) -> Pipeline:
    store = store or ObjectStoreArtifacts(tmp_path / "objects")
    provenance = AtomicInputProvenance()
    anchor = MemoryAnchor()
    ledger = FileExecutionLedger(tmp_path / "ledger" / "executions.jsonl", anchor=anchor)
    reader = ledger.reader()
    wiring = wire_pipeline(provenance, operator=OPERATOR, clock=FixedClock(),
                           include_model_explanation=include_model_explanation, ledger=reader)
    grants = GrantStore()
    grants.grant(*full_grant())
    source = store.put(document_bytes() if document is None else document, media_type=PDF_MEDIA_TYPE,
                       kind=KIND_SOURCE)
    if register_source:
        provenance.register(source.sha256, project_id=PROJECT, classification=classification)
    chosen = resources if resources is not None else (inventory() if cpu_inventory else None)
    counter = itertools.count(1)
    return Pipeline(tmp_path, store, provenance, wiring, grants, FixedClock(), chosen, source,
                    classification, lambda: f"run{next(counter):05d}", ledger, reader, anchor)


@dataclass(frozen=True)
class AdapterCall:
    adapter_id: str
    stage_id: str
    guarded: bool  # reached through RegisteredAdapter.guarded (pados.capability.registry.GuardedAdapter)
    bound: bool  # reached through pados.capability.adapter.run_bound_stage
    scoped: bool  # the adapter received only the stage-scoped artifact facade


def _frame_in(frames: list[tuple[str, str]], function: str, module_path: str) -> bool:
    return any(name == function and path.endswith(module_path) for name, path in frames)


def spy_adapters(wiring: PipelineWiring) -> list[AdapterCall]:
    """Record every adapter body invocation and the call path that reached it (the bodies still run)."""
    calls: list[AdapterCall] = []
    for adapter_id, adapter in wiring.adapters.items():
        original = adapter.execute

        def spy(request: StageRequest, *, context: StageContext, _original: Any = original,
                _adapter_id: str = adapter_id) -> StageOutput:
            frames = [(frame.function, Path(frame.filename).as_posix()) for frame in inspect.stack(0)]
            calls.append(AdapterCall(_adapter_id, request.stage_id,
                                     _frame_in(frames, "execute", "pados/capability/registry.py"),
                                     _frame_in(frames, "run_bound_stage", "pados/capability/adapter.py"),
                                     isinstance(context.artifacts, ScopedArtifacts)))
            result: StageOutput = _original(request, context=context)
            return result

        adapter.execute = spy  # type: ignore[method-assign]  # instance attribute; resolved per call
    return calls


class TamperingStore(ObjectStoreArtifacts):
    """Simulates at-rest tampering: flips one byte on disk of the ``nth`` artifact of ``kind`` written, right
    after it is written (``after_reads=0``) or immediately before its ``after_reads + 1``-th read."""

    def __init__(self, root: Path, *, kind: str, nth: int = 1, after_reads: int = 0) -> None:
        super().__init__(root)
        self.root = Path(root).resolve()
        self.kind = kind
        self.nth = nth
        self.after_reads = after_reads
        self.written: list[str] = []
        self.reads = 0
        self.tampered: list[str] = []

    def target(self) -> str | None:
        return self.written[self.nth - 1] if len(self.written) >= self.nth else None

    def put(self, payload: bytes, *, media_type: str, kind: str) -> ArtifactRef:
        ref = super().put(payload, media_type=media_type, kind=kind)
        if kind == self.kind and ref.sha256 not in self.written:
            self.written.append(ref.sha256)
            if self.after_reads == 0 and ref.sha256 == self.target() and not self.tampered:
                self.corrupt(ref)
        return ref

    def get(self, ref: ArtifactRef) -> bytes:
        if ref.sha256 == self.target() and not self.tampered:
            self.reads += 1
            if self.reads > self.after_reads:
                self.corrupt(ref)
        return super().get(ref)

    def corrupt(self, ref: ArtifactRef) -> None:
        path = self.root / ref.sha256[:2] / ref.sha256[2:4] / ref.sha256
        data = path.read_bytes()
        path.write_bytes(data[:-1] + bytes([data[-1] ^ 0x01]))
        self.tampered.append(ref.sha256)


@contextmanager
def dispatching(task: TaskContract, inputs: tuple[ArtifactRef, ...], *, stage_id: str = "recognize",
                capability_id: str = "DOCUMENT_OCR") -> Iterator[DispatchContext]:
    """TEST ONLY: simulate the executor's dispatch context, to exercise a verification hook in isolation
    (production code never sets it; only ``CapabilityExecutor`` does, around verification)."""
    context = DispatchContext(task, "0" * 64, stage_id, capability_id, inputs)
    entered = executor_module._DISPATCH.set(context)
    try:
        yield context
    finally:
        executor_module._DISPATCH.reset(entered)

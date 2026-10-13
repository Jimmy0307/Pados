"""Shared fixtures for the Capability Fabric V1 tests (unique module name; no network, no DB)."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import dataclasses
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from pados.capability.adapter import CapabilityAdapterError, StageContext, StageOutput, StageRequest
from pados.capability.artifacts import ArtifactRef, ObjectStoreArtifacts
from pados.capability.executor import CapabilityExecutor
from pados.capability.graph import DecompositionTemplate, TemplateRegistry
from pados.capability.model import (
    AdapterDescriptor,
    CapabilityDescriptor,
    CapabilityFamily,
    Determinism,
    FailureSemantics,
    FallbackPolicy,
    Modality,
    VerificationMethod,
)
from pados.capability.planner import CapabilityPlanner, ExecutionPlan, PlannerConfig, plan_document
from pados.capability.provenance import InMemoryInputProvenance
from pados.capability.registry import CapabilityRegistry
from pados.capability.seams import (
    Advice,
    InferenceRequest,
    InferenceSelection,
    ResourceMatch,
)
from pados.capability.task import TaskContract, TaskInput
from pados.capability.verification import ValidatorRegistry, VerificationEngine
from pados.connectivity.requirements import NetworkRequirement
from pados.enterprise.classification import DataClassification
from pados.execution.context import canonical_hash

OPERATOR = "operator.test"
PROJECT = "proj-a"
WORK = "wc-1"


def capability(capability_id: str, **overrides: Any) -> CapabilityDescriptor:
    values: dict[str, Any] = dict(
        capability_id=capability_id, version="1", family=CapabilityFamily.PERCEPTION, operation="RECOGNIZE_TEXT",
        input_contract="doc.scan/v1", output_contract=f"{capability_id.lower().replace('_', '.')}/v1",
        determinism=Determinism.DETERMINISTIC, modalities=(Modality.TEXT,),
        max_classification=DataClassification.RESTRICTED, network_requirement=NetworkRequirement.LOCAL_ONLY,
        verification=(VerificationMethod.SCHEMA,))
    values.update(overrides)
    return CapabilityDescriptor(**values)


def adapter_descriptor(adapter_id: str, capability_id: str, **overrides: Any) -> AdapterDescriptor:
    values: dict[str, Any] = dict(
        adapter_id=adapter_id, capability_id=capability_id, version="1", backend="python",
        determinism=Determinism.DETERMINISTIC, max_classification=DataClassification.RESTRICTED,
        network_requirement=NetworkRequirement.LOCAL_ONLY)
    values.update(overrides)
    return AdapterDescriptor(**values)


OCR = capability("DOCUMENT_OCR")
CHUNK = capability("TEXT_CHUNK", family=CapabilityFamily.ALGORITHM, operation="SPLIT_TEXT",
                   input_contract="document.ocr/v1", output_contract="text.chunks/v1",
                   verification=(VerificationMethod.DETERMINISTIC_REPLAY,))
MODEL = capability("MODEL_SUMMARY", family=CapabilityFamily.MODEL_RUNTIME, operation="GENERATE_TEXT",
                   input_contract="text.chunks/v1", output_contract="model.summary/v1",
                   determinism=Determinism.PROBABILISTIC, max_classification=DataClassification.INTERNAL,
                   network_requirement=NetworkRequirement.WAN_REQUIRED,
                   verification=(VerificationMethod.HUMAN_REVIEW,))


class TransformAdapter:
    """Reads every input, writes one transformed output per input (deterministic)."""

    def __init__(self, descriptor: AdapterDescriptor, *, suffix: bytes = b"", kind: str = "text.out",
                 fail_with: str | None = None, partial: bool = False, ready: bool = True) -> None:
        self.hook: Callable[[], None] | None = None  # runs inside execute (simulates concurrent operator action)
        self.probe_hook: Callable[[], None] | None = None  # runs inside available() (adapter-controlled code)
        self.descriptor = descriptor
        self.suffix = suffix
        self.kind = kind
        self.fail_with = fail_with
        self.partial = partial
        self.ready = ready
        self.calls: list[StageRequest] = []

    def available(self) -> bool:
        if self.probe_hook is not None:
            self.probe_hook()
        return self.ready

    def execute(self, request: StageRequest, *, context: StageContext) -> StageOutput:
        self.calls.append(request)
        if self.hook is not None:
            self.hook()
        if self.fail_with:
            raise CapabilityAdapterError(self.fail_with)
        outputs = tuple(context.artifacts.put(context.artifacts.get(ref).upper() + self.suffix,
                                              media_type="text/plain", kind=self.kind) for ref in request.inputs)
        return StageOutput(outputs=outputs, metrics={"items": float(len(outputs))}, partial=self.partial)


class NondeterministicAdapter(TransformAdapter):
    def execute(self, request: StageRequest, *, context: StageContext) -> StageOutput:
        self.calls.append(request)
        payload = b"run-%d" % len(self.calls)
        return StageOutput(outputs=(context.artifacts.put(payload, media_type="text/plain", kind=self.kind),))


@dataclass
class GrantStore:
    """The trusted grant source: the CURRENT grant document per (project, work contract)."""

    grants: dict[tuple[str, str], dict[str, Any]] = field(default_factory=dict)
    reads: int = 0

    def current_grant(self, *, project_id: str, work_contract_id: str) -> Mapping[str, Any]:
        self.reads += 1
        try:
            return dict(self.grants[(project_id, work_contract_id)])
        except KeyError as exc:
            raise LookupError("GRANT_NOT_FOUND") from exc

    def grant(self, *capability_ids: str, project_id: str = PROJECT, work: str = WORK) -> None:
        self.grants[(project_id, work)] = {"work_contract_id": work, "project_id": project_id,
                                           "capabilities": [f"CAPABILITY:{cid}" for cid in capability_ids]}


class FixedClock:
    def __init__(self) -> None:
        self.now = datetime(2026, 10, 3, tzinfo=UTC)

    def __call__(self) -> datetime:
        self.now += timedelta(milliseconds=5)
        return self.now


@dataclass
class Inventory:
    fits: dict[str, bool] = field(default_factory=lambda: {"gpu-node": False, "local-cpu": True})
    calls: int = 0

    def match(self, requirement: Any) -> Sequence[ResourceMatch]:
        self.calls += 1
        return [ResourceMatch(rid, ok, () if ok else ("RAM_INSUFFICIENT",)) for rid, ok in self.fits.items()]


@dataclass
class Advisor:
    """ScoreAdvisor test double. ``bind_to`` makes it bind advice to a DIFFERENT decision than requested."""

    scores: dict[str, float]
    calls: int = 0
    wrong_context: bool = False
    invalid_features: bool = False
    bind_to: dict[str, Any] = field(default_factory=dict)  # overrides: candidates / classification / plan binding
    seen: list[dict[str, Any]] = field(default_factory=list)
    log: dict[str, tuple[Advice, ...]] = field(default_factory=dict)  # what it issued, per plan binding
    issued_calls: int = 0

    def context_hash(self, *, task_type: str, capability_id: str, context_features: Mapping[str, str],
                     candidates: tuple[tuple[str, str], ...], classification: str,
                     plan_binding_hash: str | None = None) -> str:
        if self.invalid_features:
            raise ValueError("ADVICE_FEATURE_VOCABULARY_INVALID")
        return canonical_hash({"advisor": "test", "task_type": task_type, "capability_id": capability_id,
                               "features": dict(context_features), "candidates": [list(c) for c in candidates],
                               "classification": classification, "plan_binding_hash": plan_binding_hash})

    def advise(self, *, task_type: str, capability_id: str, candidates: Sequence[AdapterDescriptor],
               context_features: Mapping[str, str], classification: str,
               plan_binding_hash: str | None = None) -> Sequence[Advice]:
        self.calls += 1
        pairs = tuple(sorted((c.adapter_id, c.version) for c in candidates))
        self.seen.append({"candidates": pairs, "classification": classification,
                          "plan_binding_hash": plan_binding_hash})
        binding = {"candidates": pairs, "classification": classification, "plan_binding_hash": plan_binding_hash,
                   **self.bind_to}
        own = self.context_hash(task_type=task_type, capability_id=capability_id, context_features=context_features,
                                **binding)
        ctx = "0" * 64 if self.wrong_context else own
        advice = [Advice(aid, score, 12, "historical outcome rate", canonical_hash({"profile": aid}), ctx)
                  for aid, score in self.scores.items()]
        if plan_binding_hash is not None:
            self.log[plan_binding_hash] = tuple(advice)
        return advice

    def issued(self, plan_binding_hash: str, *, candidates: Sequence[tuple[str, str]] = ()) -> Sequence[Advice] | None:
        self.issued_calls += 1
        return self.log.get(plan_binding_hash)


@dataclass
class Delegate:
    """A ModelInferenceDelegate test double (the real bridge is tested separately)."""

    worker: str | None = "ollama-local"
    calls: list[InferenceRequest] = field(default_factory=list)

    def select(self, request: InferenceRequest) -> InferenceSelection:
        self.calls.append(request)
        status = "ROUTED" if self.worker else "BLOCKED"
        return InferenceSelection(self.worker, status, "router decision",
                                  canonical_hash({"worker": self.worker, "stage": request.stage_id,
                                                  "grant": request.grant_hash}))


def template(task_type: str = "doc.extract", stages: list[dict[str, Any]] | None = None) -> DecompositionTemplate:
    stages = stages if stages is not None else [
        {"stage_id": "ocr", "capability_id": "DOCUMENT_OCR",
         "inputs": [{"source": "TASK_INPUT", "name": "scan"}]},
        {"stage_id": "chunk", "capability_id": "TEXT_CHUNK",
         "inputs": [{"source": "STAGE_OUTPUT", "name": "ocr"}], "parameters": {"size": 64}},
    ]
    return DecompositionTemplate.model_validate({"template_id": f"tpl.{task_type}", "version": "1",
                                                 "task_type": task_type, "graph": {"stages": stages}})


@dataclass
class Fabric:
    root: Path
    store: ObjectStoreArtifacts
    registry: CapabilityRegistry
    templates: TemplateRegistry
    grants: GrantStore
    adapters: dict[str, TransformAdapter]
    scan: ArtifactRef
    schemas: ValidatorRegistry
    clock: FixedClock
    provenance: InMemoryInputProvenance

    def task(self, **overrides: Any) -> TaskContract:
        values: dict[str, Any] = dict(
            task_id="task-1", task_type="doc.extract", project_id=PROJECT, work_contract_id=WORK,
            classification=DataClassification.CONFIDENTIAL, network_policy=NetworkRequirement.LOCAL_ONLY,
            required_outputs=("text.chunks/v1",),
            inputs=(TaskInput(name="scan", ref=self.scan, project_id=PROJECT,
                              classification=DataClassification.CONFIDENTIAL),))
        values.update(overrides)
        return TaskContract(**values)

    def planner(self, **kwargs: Any) -> CapabilityPlanner:
        config = kwargs.pop("config", PlannerConfig())
        provenance = kwargs.pop("provenance", self.provenance)
        return CapabilityPlanner(self.registry, self.templates, self.grants, config=config, provenance=provenance,
                                 **kwargs)

    def executor(self, **kwargs: Any) -> CapabilityExecutor:
        """Built from the SAME trusted state as ``planner`` (``config`` maps to the executor's planner config)."""
        verifier = kwargs.pop("verifier", VerificationEngine(schemas=self.schemas))
        config = kwargs.pop("config", None)
        provenance = kwargs.pop("provenance", self.provenance)
        return CapabilityExecutor(self.registry, self.store, self.grants, templates=self.templates,
                                  provenance=provenance, planner_config=config, verifier=verifier, clock=self.clock,
                                  run_ids=lambda: "run00001", **kwargs)

    def artifact(self, data: bytes, classification: DataClassification, *, project_id: str = PROJECT) -> ArtifactRef:
        """Store a source artifact and register its governed provenance."""
        ref = self.store.put(data, media_type="text/plain", kind="doc.scan")
        self.provenance.register(ref.sha256, project_id=project_id, classification=classification)
        return ref

    def add(self, implementation: TransformAdapter, *, register: bool = True) -> TransformAdapter:
        self.registry.install(implementation, operator=OPERATOR)
        if register:
            self.registry.register_adapter(implementation.descriptor.adapter_id,
                                           capability_id=implementation.descriptor.capability_id, operator=OPERATOR)
        self.adapters[implementation.descriptor.adapter_id] = implementation
        return implementation


def fabric(tmp_path: Path, *, ocr_fallback: bool = False, ocr_failure: FailureSemantics = FailureSemantics.FAIL_CLOSED
           ) -> Fabric:
    store = ObjectStoreArtifacts(tmp_path / "objects")
    clock = FixedClock()
    registry = CapabilityRegistry(clock=clock)
    ocr = OCR.model_copy(update={"fallback": FallbackPolicy.EXPLICIT_SAME_POLICY if ocr_fallback
                                 else FallbackPolicy.NONE, "failure": ocr_failure})
    for descriptor in (ocr, CHUNK, MODEL):
        registry.register_capability(descriptor, operator=OPERATOR)
    templates = TemplateRegistry()
    templates.register(template())
    grants = GrantStore()
    grants.grant("DOCUMENT_OCR", "TEXT_CHUNK", "MODEL_SUMMARY")
    schemas = ValidatorRegistry()
    schemas.register("document.ocr/v1", "ocr-schema", lambda outputs: all(data.isupper() for _, data in outputs))
    scan = store.put(b"scanned page", media_type="text/plain", kind="doc.scan")
    provenance = InMemoryInputProvenance({scan.sha256: (PROJECT, DataClassification.CONFIDENTIAL)})
    built = Fabric(tmp_path, store, registry, templates, grants, {}, scan, schemas, clock, provenance)
    built.add(TransformAdapter(adapter_descriptor("ocr.alpha", "DOCUMENT_OCR"), kind="ocr.text"))
    built.add(TransformAdapter(adapter_descriptor("chunk.fixed", "TEXT_CHUNK"), suffix=b"|", kind="text.chunk"))
    return built


SUMMARY_STAGES: list[dict[str, Any]] = [
    {"stage_id": "ocr", "capability_id": "DOCUMENT_OCR", "inputs": [{"source": "TASK_INPUT", "name": "scan"}]},
    {"stage_id": "chunk", "capability_id": "TEXT_CHUNK", "inputs": [{"source": "STAGE_OUTPUT", "name": "ocr"}]},
    {"stage_id": "summary", "capability_id": "MODEL_SUMMARY",
     "inputs": [{"source": "STAGE_OUTPUT", "name": "chunk", "artifact_kind": "text.chunk"}]},
]


def with_model(fab: Fabric, *, delegates: bool = True) -> TaskContract:
    """Register a Worker-Router-delegating model adapter and return a summarize task for it."""
    fab.templates.register(template("doc.summarize", SUMMARY_STAGES))
    fab.add(TransformAdapter(adapter_descriptor(
        "llm.bridge", "MODEL_SUMMARY", determinism=Determinism.PROBABILISTIC,
        network_requirement=NetworkRequirement.WAN_REQUIRED, max_classification=DataClassification.INTERNAL,
        delegates_to_worker_router=delegates), kind="model.summary"))
    internal = fab.artifact(b"internal scanned page", DataClassification.INTERNAL)
    return fab.task(task_type="doc.summarize", classification=DataClassification.INTERNAL,
                    network_policy=NetworkRequirement.WAN_REQUIRED, required_outputs=("model.summary/v1",),
                    inputs=(TaskInput(name="scan", ref=internal, project_id=PROJECT,
                                      classification=DataClassification.INTERNAL),))


def rehash(plan: ExecutionPlan, stages: tuple[Any, ...]) -> ExecutionPlan:
    """A forger who rewrites stages and recomputes plan_hash / plan_id consistently."""
    document = plan_document(plan.status, plan.task_id, plan.task_hash, plan.template_id, plan.template_hash,
                             plan.graph_hash, stages, plan.infeasible_codes, plan.advice_enabled, plan.human_gate,
                             plan.quality)
    digest = canonical_hash(document)
    return dataclasses.replace(plan, stages=stages, plan_hash=digest, plan_id=f"plan-{digest[:24]}")


def forge(plan: ExecutionPlan, index: int, **changes: Any) -> ExecutionPlan:
    stages = list(plan.stages)
    stages[index] = dataclasses.replace(stages[index], **changes)
    return rehash(plan, tuple(stages))

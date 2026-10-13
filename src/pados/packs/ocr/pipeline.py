# SPDX-License-Identifier: AGPL-3.0-only
"""OCR decomposition templates (plain data) and a small local runner over the shared stage contract.

A ``DecompositionTemplate`` is data the Fabric planner can register: stages, the capability each one
needs, which artifacts it consumes (``source``, ``manifest``, latest ``document``) and what it emits.
It names capabilities, never adapters.

``run_pipeline`` is the pack's own minimal executor for tests and integration: for every stage it
takes the adapter the CALLER assigned (it never ranks, selects or substitutes adapters), evaluates the
shared policy, binds the stage and executes only through ``bind_stage``/``run_bound_stage`` with
grant re-validation. A refusal or failure stops the run (fail closed; no fallback). Each output is
schema-verified and then verified INDEPENDENTLY of what the producer claims: the preprocess manifest
must select exactly the task's pages; every document must cover exactly the manifest's pages, its
status/count/confidence evidence is recomputed from the pages (and from the previous verified
document), its stage and engine provenance must extend the previous document's by exactly this
stage, and non-recognition stages may not change any page's status, producer or geometry. A
recognition stage's engine record is rebuilt from the assigned descriptor (adapter, version, backend,
operation, license, distribution) and the exact attempted/perceived page sets, and must equal the one
emitted. Page coverage is trusted only from the pack's own isolated preprocess adapter
(``REGISTERED_PREPROCESS_DIGESTS``). Confidence measurements are taken from each recognition stage's
verified output and carried to the quality gate, whatever later stages do to the blocks. Every code
that leaves the runner is closed (``STAGE_CODES``, the shared binding codes or the runner's own). Every
stage gets a ``StageReceipt`` and the receipts are chained into a ``PlanReceipt`` from the plan
genesis. Fixture (non-production) adapters are refused unless the caller passes the explicit
test-mode token ``TEST_FIXTURE_MODE``; ``allow_experimental`` alone never admits them, and a
test-mode result is marked ``provenance = "FIXTURE"`` on the result, the document and every receipt.
Model-runtime stages obtain the Worker Router's selection hash from a ``ModelInferenceDelegate``.
Results are candidates (``semantic_authority = NONE``); acceptance happens only through the existing
receipt -> validation -> CAS path, outside this pack.
"""
from __future__ import annotations

import re
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

from pados.capability.adapter import (
    CapabilityAdapter,
    CapabilityAdapterError,
    StageBindingError,
    StageRequest,
    bind_stage,
    run_bound_stage,
)
from pados.capability.artifacts import ArtifactIntegrityError, ArtifactRef, ArtifactStore
from pados.capability.model import (
    AdapterDescriptor,
    CapabilityFamily,
    FailureSemantics,
    LifecycleState,
    VerificationMethod,
    descriptor_digest,
)
from pados.capability.policy import AuthorizedScope, PolicyCode, PolicyContext, evaluate
from pados.capability.receipts import PlanReceipt, StageReceipt, VerificationStatus, chain, parameters_hash
from pados.capability.seams import InferenceRequest, ModelInferenceDelegate
from pados.execution.context import canonical_hash
from pados.packs.ocr._isolated import ProcessBudget
from pados.packs.ocr._stage import OCR_STAGE_FAILED, closed_code, engine_record
from pados.packs.ocr.contracts import (
    IMAGE_MEDIA_TYPES,
    KIND_DOCUMENT,
    KIND_PREPROCESSED,
    OCR_DOCUMENT_CONTRACT,
    OCR_PREPROCESSED_CONTRACT,
    OCR_REQUEST_CONTRACT,
    PDF_MEDIA_TYPE,
    OcrContractError,
    OcrDocument,
    OcrTaskContract,
    PageConfidence,
    PageStatus,
    PreprocessManifest,
    StageMark,
    page_measurement,
    summarize_pages,
)
from pados.packs.ocr.descriptors import (
    DOCUMENT_LAYOUT_ANALYSIS,
    DOCUMENT_OCR,
    DOCUMENT_PREPROCESS,
    DOCUMENT_STRUCTURE_PARSE,
    FIGURE_EXTRACTION,
    OCR_CAPABILITIES,
    TABLE_EXTRACTION,
    VISION_MODEL_OCR,
    VISION_MODEL_OCR_CLOUD,
    determinism_conforms,
)
from pados.packs.ocr.fixture import FixtureMode
from pados.packs.ocr.reference import REGISTERED_PREPROCESS_DIGESTS, DocumentPreprocessAdapter

TEMPLATE_SCHEMA = "pados.capability.decomposition-template.v1"
SCHEMA_VERIFIER_ID = "ocr.schema-validator.v1"
SOURCE, MANIFEST, DOCUMENT = "source", "manifest", "document"
_OUTPUT_KIND = {MANIFEST: KIND_PREPROCESSED, DOCUMENT: KIND_DOCUMENT}
_OUTPUT_CONTRACT = {MANIFEST: OCR_PREPROCESSED_CONTRACT, DOCUMENT: OCR_DOCUMENT_CONTRACT}
_STAGE_ID = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")
_TEMPLATE_ID = re.compile(r"^[a-z][a-z0-9_.-]{2,79}$")
_FLAGS = frozenset({"require_tables"})
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
RECOGNITION_OPERATION = "RECOGNIZE_TEXT"
PROVENANCE_CANDIDATE = "ENGINE_CANDIDATE"
PROVENANCE_FIXTURE = "FIXTURE"
FIXTURE_RECEIPT_METRIC = "test_fixture_mode"
_PERCEIVED = frozenset({PageStatus.TEXT_LAYER, PageStatus.RECOGNIZED})
# Refusal codes of the shared bind/run path (pados.capability.adapter); anything else is STAGE_BINDING_REFUSED.
BINDING_REFUSAL_CODES = frozenset({
    "POLICY_DECISION_NOT_ALLOWED", "POLICY_DECISION_DESCRIPTOR_MISMATCH", "POLICY_DECISION_CONTEXT_MISMATCH",
    "STAGE_REQUEST_MISMATCH", "STAGE_INPUT_NOT_PERMITTED", "ADAPTER_DESCRIPTOR_CHANGED_SINCE_BINDING",
    "AUTHORIZATION_REVOKED_BEFORE_EXECUTION", "AUTHORIZATION_CHANGED_BEFORE_EXECUTION",
    "MODEL_INFERENCE_REQUIRES_WORKER_ROUTER_SELECTION", "ADAPTER_OUTPUT_INVALID",
    "ADAPTER_OUTPUT_NOT_WRITTEN_THROUGH_STAGE"})


class OcrPipelineRefused(PermissionError):
    """Refused before or at binding (policy, assignment, authorization). ``code`` is closed."""

    def __init__(self, code: str, stage_id: str | None = None) -> None:
        super().__init__(f"{code}:{stage_id}" if stage_id else code)
        self.code = code
        self.stage_id = stage_id


class OcrPipelineFailed(RuntimeError):
    """A bound stage failed or produced output that violates its contract. No output is accepted."""

    def __init__(self, code: str, stage_id: str) -> None:
        super().__init__(f"{code}:{stage_id}")
        self.code = code
        self.stage_id = stage_id


@dataclass(frozen=True)
class StageSpec:
    stage_id: str
    capability_id: str
    inputs: tuple[str, ...]
    output: str
    enabled_by: str | None = None  # a boolean task flag; the stage runs only when it is true

    def as_data(self) -> dict[str, Any]:
        return {"stage_id": self.stage_id, "capability_id": self.capability_id, "inputs": list(self.inputs),
                "output": self.output, "enabled_by": self.enabled_by}


@dataclass(frozen=True)
class DecompositionTemplate:
    """A capability graph as plain data (validated on construction)."""

    template_id: str
    version: str
    media_types: frozenset[str]
    stages: tuple[StageSpec, ...]
    input_contract: str = OCR_REQUEST_CONTRACT
    output_contract: str = OCR_DOCUMENT_CONTRACT

    def __post_init__(self) -> None:
        if not _TEMPLATE_ID.fullmatch(self.template_id) or not self.stages:
            raise ValueError("TEMPLATE_INVALID")
        produced = {SOURCE}
        seen: set[str] = set()
        for spec in self.stages:
            capability = OCR_CAPABILITIES.get(spec.capability_id)
            if not _STAGE_ID.fullmatch(spec.stage_id) or spec.stage_id in seen or capability is None:
                raise ValueError("TEMPLATE_STAGE_INVALID")
            if not set(spec.inputs) <= produced or spec.output not in _OUTPUT_KIND:
                raise ValueError("TEMPLATE_STAGE_INPUT_UNAVAILABLE")
            if capability.output_contract != _OUTPUT_CONTRACT[spec.output]:
                raise ValueError("TEMPLATE_STAGE_CONTRACT_MISMATCH")
            if spec.enabled_by is not None and spec.enabled_by not in _FLAGS:
                raise ValueError("TEMPLATE_STAGE_FLAG_UNKNOWN")
            seen.add(spec.stage_id)
            produced.add(spec.output)
        if self.stages[-1].output != DOCUMENT or self.stages[-1].enabled_by is not None:
            raise ValueError("TEMPLATE_FINAL_STAGE_INVALID")

    def as_data(self) -> dict[str, Any]:
        return {"schema": TEMPLATE_SCHEMA, "template_id": self.template_id, "version": self.version,
                "media_types": sorted(self.media_types), "input_contract": self.input_contract,
                "output_contract": self.output_contract, "stages": [spec.as_data() for spec in self.stages]}

    @property
    def template_hash(self) -> str:
        return canonical_hash(self.as_data())

    def resolve(self, task: OcrTaskContract) -> tuple[StageSpec, ...]:
        """The stages a task runs (flag-gated stages included only when the task asks for them)."""
        if task.media_type not in self.media_types:
            raise OcrPipelineRefused("TEMPLATE_MEDIA_TYPE_UNSUPPORTED")
        flags = {"require_tables": task.require_tables}
        stages = tuple(s for s in self.stages if s.enabled_by is None or flags[s.enabled_by])
        if task.require_tables and not any(s.capability_id == TABLE_EXTRACTION for s in stages):
            raise OcrPipelineRefused("TEMPLATE_CANNOT_EXTRACT_TABLES")
        return stages


def _graph(ocr_capability: str, *, figures: bool, hybrid: bool = False) -> tuple[StageSpec, ...]:
    stages = [StageSpec("preprocess", DOCUMENT_PREPROCESS, (SOURCE,), MANIFEST),
              StageSpec("recognize", ocr_capability, (SOURCE, MANIFEST), DOCUMENT)]
    if hybrid:  # an explicitly planned second recognition stage for NEEDS_OCR pages (never implicit)
        stages.append(StageSpec("recognize_scanned", DOCUMENT_OCR, (SOURCE, MANIFEST, DOCUMENT), DOCUMENT))
    stages += [StageSpec("layout", DOCUMENT_LAYOUT_ANALYSIS, (DOCUMENT,), DOCUMENT),
               StageSpec("tables", TABLE_EXTRACTION, (DOCUMENT,), DOCUMENT, enabled_by="require_tables")]
    if figures:
        stages.append(StageSpec("figures", FIGURE_EXTRACTION, (SOURCE, DOCUMENT), DOCUMENT))
    stages.append(StageSpec("structure", DOCUMENT_STRUCTURE_PARSE, (DOCUMENT,), DOCUMENT))
    return tuple(stages)


PDF_TEXT_LAYER_TEMPLATE = DecompositionTemplate("ocr.pdf-text-layer", "1", frozenset({PDF_MEDIA_TYPE}),
                                                _graph(DOCUMENT_OCR, figures=True))
PDF_HYBRID_TEMPLATE = DecompositionTemplate("ocr.pdf-hybrid", "1", frozenset({PDF_MEDIA_TYPE}),
                                            _graph(DOCUMENT_OCR, figures=True, hybrid=True))
IMAGE_TEMPLATE = DecompositionTemplate("ocr.image", "1", IMAGE_MEDIA_TYPES, _graph(DOCUMENT_OCR, figures=False))
IMAGE_VISION_TEMPLATE = DecompositionTemplate("ocr.image-vision-local", "1", IMAGE_MEDIA_TYPES,
                                              _graph(VISION_MODEL_OCR, figures=False))
IMAGE_VISION_CLOUD_TEMPLATE = DecompositionTemplate("ocr.image-vision-cloud", "1", IMAGE_MEDIA_TYPES,
                                                    _graph(VISION_MODEL_OCR_CLOUD, figures=False))
TEMPLATES: Mapping[str, DecompositionTemplate] = MappingProxyType({t.template_id: t for t in (
    PDF_TEXT_LAYER_TEMPLATE, PDF_HYBRID_TEMPLATE, IMAGE_TEMPLATE, IMAGE_VISION_TEMPLATE, IMAGE_VISION_CLOUD_TEMPLATE)})


# --- planning view -----------------------------------------------------------------------------

def policy_view(capability_id: str, candidates: Iterable[CapabilityAdapter],
                context: PolicyContext) -> tuple[tuple[str, PolicyCode], ...]:
    """Shared-policy outcome for each candidate adapter (feasibility only; never a ranking)."""
    capability = OCR_CAPABILITIES[capability_id]
    return tuple((adapter.descriptor.adapter_id,
                  evaluate(capability, adapter.descriptor, context, adapter_available=adapter.available).code)
                 for adapter in candidates)


# --- runner ------------------------------------------------------------------------------------

@dataclass(frozen=True)
class QualityVerdict:
    met: bool
    reasons: tuple[str, ...]


def evaluate_quality(task: OcrTaskContract, document: OcrDocument, *, expected_pages: tuple[int, ...],
                     measurements: Mapping[int, PageConfidence | None] | None = None) -> QualityVerdict:
    """``met`` means every requested page was perceived (statuses recomputed from the pages themselves).

    With a confidence floor, every ``RECOGNIZED`` page needs a verified measurement (``page_measurement``,
    ``MEASUREMENT_POLICY``: the unweighted mean and the minimum of the recognition stage's own LINE
    confidences, as ``run_pipeline`` records them), whatever its blocks look like now; a page without
    one is ``CONFIDENCE_UNMEASURED``. Without ``measurements`` they are recomputed from the document's
    current LINE blocks, never taken from its claimed evidence or from non-LINE blocks.
    """
    reasons: list[str] = []
    pages = document.pages
    if tuple(page.page_number for page in pages) != expected_pages:
        reasons.append("REQUESTED_PAGES_MISSING")
    if any(page.status is PageStatus.NEEDS_OCR for page in pages) and not task.quality.allow_needs_ocr_pages:
        reasons.append("NEEDS_OCR_PAGES_PRESENT")
    if any(page.status is PageStatus.FAILED for page in pages):
        reasons.append("FAILED_PAGES_PRESENT")
    floor = task.quality.min_mean_confidence
    if floor is not None:
        verified = measurements if measurements is not None else {
            page.page_number: page_measurement(page) for page in pages}
        measured = [verified.get(page.page_number) for page in pages if page.status is PageStatus.RECOGNIZED]
        if any(m is None or m.mean is None for m in measured):
            reasons.append("CONFIDENCE_UNMEASURED")
        if any(m is not None and m.mean is not None and m.mean < floor for m in measured):
            reasons.append("CONFIDENCE_BELOW_REQUIREMENT")
    return QualityVerdict(met=not reasons, reasons=tuple(reasons))


@dataclass(frozen=True)
class PipelineResult:
    plan_id: str
    plan_hash: str
    template_id: str
    document_ref: ArtifactRef
    document: OcrDocument
    stage_outputs: Mapping[str, ArtifactRef]
    decision_hashes: Mapping[str, str]
    plan_receipt: PlanReceipt
    quality: QualityVerdict
    provenance: str = PROVENANCE_CANDIDATE  # "FIXTURE" for a test-mode run: never production output
    # Per RECOGNIZED page: the measurement (MEASUREMENT_POLICY) taken from the recognition stage's own
    # verified output and carried past every later stage; this is what the quality gate compared.
    measurements: Mapping[int, PageConfidence | None] = field(default_factory=lambda: MappingProxyType({}))
    semantic_authority: str = field(default="NONE", init=False)


def _utcnow() -> datetime:
    return datetime.now(UTC)


def compute_plan_hash(template: DecompositionTemplate, task: OcrTaskContract, stages: Iterable[StageSpec],
                      assignments: Mapping[str, CapabilityAdapter]) -> str:
    return canonical_hash({"template_hash": template.template_hash, "task_hash": task.task_hash,
                           "stages": [{"stage_id": s.stage_id, "capability_id": s.capability_id,
                                       "adapter_id": assignments[s.stage_id].descriptor.adapter_id,
                                       "adapter_digest": descriptor_digest(assignments[s.stage_id].descriptor)}
                                      for s in stages]})


def is_fixture_adapter(adapter: CapabilityAdapter) -> bool:
    """Non-production adapters: declared ``production_eligible = False`` or a ``fixture`` backend."""
    return getattr(adapter, "production_eligible", True) is not True or adapter.descriptor.backend == "fixture"


def _check_assignment(spec: StageSpec, adapter: CapabilityAdapter | None, allow_experimental: bool,
                      test_fixtures: FixtureMode | None) -> AdapterDescriptor:
    if adapter is None:
        raise OcrPipelineRefused("STAGE_ADAPTER_UNASSIGNED", spec.stage_id)
    if is_fixture_adapter(adapter) and test_fixtures is None:  # independent of allow_experimental
        raise OcrPipelineRefused("FIXTURE_ADAPTER_REQUIRES_TEST_MODE", spec.stage_id)
    descriptor = adapter.descriptor
    capability = OCR_CAPABILITIES[spec.capability_id]
    experimental = LifecycleState.EXPERIMENTAL in {descriptor.lifecycle, capability.lifecycle}
    if experimental and not allow_experimental:
        raise OcrPipelineRefused("EXPERIMENTAL_ADAPTER_NOT_ALLOWED", spec.stage_id)
    if not determinism_conforms(descriptor.determinism, capability.determinism):
        raise OcrPipelineRefused("ADAPTER_DETERMINISM_NONCONFORMING", spec.stage_id)
    if spec.capability_id == DOCUMENT_PREPROCESS and not is_registered_preprocess(adapter):
        raise OcrPipelineRefused("PREPROCESS_ADAPTER_NOT_REGISTERED", spec.stage_id)
    return descriptor


def is_registered_preprocess(adapter: CapabilityAdapter) -> bool:
    """Page coverage is trusted only from the pack's own isolated preprocess IMPLEMENTATION.

    The adapter's type must be exactly ``DocumentPreprocessAdapter`` (a subclass can override
    ``_execute`` and keep the inherited descriptor, so ``isinstance`` is not enough), its descriptor
    digest must be the registered one (metadata, not proof of implementation), and the instance may
    not shadow any method of the class (``execute``, ``_execute``, ``_pdf``, ...) in its ``__dict__``.
    Its budget must be a real ``ProcessBudget``. Not what a manifest claims.
    """
    if type(adapter) is not DocumentPreprocessAdapter:
        return False
    own = vars(adapter)
    if any(name in own for name in ("execute", "_execute", "available", "unavailable_reason", "_ready", "_pdf")):
        return False
    if any(callable(getattr(DocumentPreprocessAdapter, name, None)) or callable(value) for name, value in own.items()):
        return False
    if not isinstance(own.get("_budget"), ProcessBudget):
        return False
    descriptor = adapter.descriptor
    return REGISTERED_PREPROCESS_DIGESTS.get(descriptor.adapter_id) == descriptor_digest(descriptor)


def _router_selection(delegate: ModelInferenceDelegate | None, context: PolicyContext, capability_id: str,
                      task: OcrTaskContract) -> str:
    if delegate is None:
        raise OcrPipelineRefused("INFERENCE_DELEGATE_REQUIRED", context.stage_id)
    selection = delegate.select(InferenceRequest(
        project_id=context.project_id, stage_id=context.stage_id, capability_id=capability_id, risk_level="MEDIUM",
        classification=task.classification, network_policy=task.network_policy.value,
        grant_hash=context.scope.grant_hash))
    if selection.selected_worker_id is None or not _SHA256.fullmatch(selection.selection_hash):
        raise OcrPipelineRefused("WORKER_ROUTER_NO_SELECTION", context.stage_id)
    return selection.selection_hash


def _parse_output(store: ArtifactStore, ref: ArtifactRef, spec: StageSpec, task: OcrTaskContract
                  ) -> OcrDocument | PreprocessManifest:
    """Schema verification of a stage output against its declared contract and the task's source."""
    parsed: OcrDocument | PreprocessManifest | None = None
    try:
        payload = store.get(ref)
        parsed = PreprocessManifest.from_bytes(payload) if spec.output == MANIFEST else OcrDocument.from_bytes(payload)
    except (OcrContractError, ArtifactIntegrityError):
        parsed = None
    if parsed is None:  # raised outside the handler: no chained exception (which may quote content)
        raise OcrPipelineFailed("STAGE_OUTPUT_SCHEMA_INVALID", spec.stage_id)
    if parsed.source != task.artifact_ref:
        raise OcrPipelineFailed("STAGE_OUTPUT_SOURCE_MISMATCH", spec.stage_id)
    return parsed


def expected_pages(task: OcrTaskContract, manifest: PreprocessManifest) -> tuple[int, ...]:
    """The pages the task asked for, given the document's page count (never clipped)."""
    if task.page_range is not None:
        return tuple(range(task.page_range.first, task.page_range.last + 1))
    return tuple(range(1, manifest.page_count + 1))


def _page_facts(document: OcrDocument) -> list[tuple[object, ...]]:
    return [(p.page_number, p.status, p.producer, p.width, p.height, p.unit) for p in document.pages]


def _verify_document(document: OcrDocument, *, spec: StageSpec, descriptor: AdapterDescriptor,
                     manifest: PreprocessManifest | None, prior: OcrDocument | None,
                     test_fixtures: FixtureMode | None) -> None:
    """Independent checks of a document output (the producer's claims are recomputed, never trusted)."""
    stage = spec.stage_id
    if manifest is None:
        raise OcrPipelineFailed("STAGE_OUTPUT_UNVERIFIABLE", stage)
    if tuple(page.page_number for page in document.pages) != manifest.selected_pages:
        raise OcrPipelineFailed("STAGE_OUTPUT_PAGES_MISMATCH", stage)
    earlier: dict[int, PageConfidence] | None = (
        {s.page_number: s for s in prior.evidence.page_confidence} if prior is not None else None)
    summary, needs, failed = summarize_pages(document.pages, earlier)
    evidence = document.evidence
    if (evidence.page_confidence, evidence.needs_ocr_pages, evidence.failed_pages, evidence.pages_total) != (
            summary, needs, failed, len(document.pages)):
        raise OcrPipelineFailed("STAGE_OUTPUT_EVIDENCE_MISMATCH", stage)
    mark = StageMark(stage_id=stage, capability_id=spec.capability_id, adapter_id=descriptor.adapter_id,
                     adapter_version=descriptor.version)
    if evidence.stages != (*(prior.evidence.stages if prior is not None else ()), mark):
        raise OcrPipelineFailed("STAGE_OUTPUT_PROVENANCE_INVALID", stage)
    fixture_marked = evidence.test_provenance is not None or any(e.backend == "fixture" for e in evidence.engines)
    if fixture_marked and test_fixtures is None:
        raise OcrPipelineFailed("STAGE_OUTPUT_FIXTURE_PROVENANCE", stage)
    if prior is not None and prior.evidence.test_provenance is not None and evidence.test_provenance is None:
        raise OcrPipelineFailed("STAGE_OUTPUT_PROVENANCE_INVALID", stage)
    if OCR_CAPABILITIES[spec.capability_id].operation == RECOGNITION_OPERATION:
        _verify_recognition(document, stage=stage, descriptor=descriptor, manifest=manifest, prior=prior)
        return
    if prior is None or evidence.engines != prior.evidence.engines:
        raise OcrPipelineFailed("STAGE_OUTPUT_PROVENANCE_INVALID", stage)
    if _page_facts(document) != _page_facts(prior):  # only recognition may change what a page is
        raise OcrPipelineFailed("STAGE_OUTPUT_PAGE_STATUS_CHANGED", stage)


def _verify_recognition(document: OcrDocument, *, stage: str, descriptor: AdapterDescriptor,
                        manifest: PreprocessManifest, prior: OcrDocument | None) -> None:
    """Rebuild the stage's complete engine record and require the emitted one to equal it.

    Policy: the first recognition stage attempts every selected page and may perceive any of them
    (``TEXT_LAYER`` or ``RECOGNIZED``); a later one attempts exactly the previous document's
    ``NEEDS_OCR`` pages and may only turn them ``RECOGNIZED`` or ``FAILED`` (or leave them unchanged).
    Perceived pages name this adapter as producer; every page it did not attempt is left exactly as it was.
    """
    engines = document.evidence.engines
    earlier = prior.evidence.engines if prior is not None else ()
    if len(engines) != len(earlier) + 1 or engines[:-1] != earlier:
        raise OcrPipelineFailed("STAGE_OUTPUT_PROVENANCE_INVALID", stage)
    before = {page.page_number: page for page in prior.pages} if prior is not None else {}
    if prior is None:
        attempted = set(manifest.selected_pages)
        allowed = _PERCEIVED
    else:
        attempted = {number for number, page in before.items() if page.status is PageStatus.NEEDS_OCR}
        allowed = frozenset({PageStatus.RECOGNIZED})
    perceived: list[int] = []
    for page in document.pages:
        previous = before.get(page.page_number)
        if page.page_number not in attempted:
            if page != previous:  # a page that was not attempted is left exactly as it was
                raise OcrPipelineFailed("STAGE_OUTPUT_PAGE_STATUS_CHANGED", stage)
            continue
        if page.status in _PERCEIVED:
            if page.status not in allowed:
                raise OcrPipelineFailed("STAGE_OUTPUT_PAGE_STATUS_CHANGED", stage)
            if page.producer != descriptor.adapter_id:
                raise OcrPipelineFailed("STAGE_OUTPUT_PROVENANCE_INVALID", stage)
            perceived.append(page.page_number)
        elif previous is not None and page.status is PageStatus.NEEDS_OCR and page != previous:
            raise OcrPipelineFailed("STAGE_OUTPUT_PAGE_STATUS_CHANGED", stage)
    if engines[-1] != engine_record(descriptor, attempted=attempted, pages=perceived):
        raise OcrPipelineFailed("STAGE_OUTPUT_PROVENANCE_INVALID", stage)


def _recognition_measurements(document: OcrDocument, prior: OcrDocument | None) -> dict[int, PageConfidence | None]:
    """Measurements of the pages this (verified) recognition stage newly made ``RECOGNIZED``."""
    before = {page.page_number: page.status for page in prior.pages} if prior is not None else {}
    return {page.page_number: page_measurement(page) for page in document.pages
            if page.status is PageStatus.RECOGNIZED and before.get(page.page_number) is not PageStatus.RECOGNIZED}


def run_pipeline(template: DecompositionTemplate, task: OcrTaskContract,
                 assignments: Mapping[str, CapabilityAdapter], *, store: ArtifactStore, scope: AuthorizedScope,
                 revalidate: Callable[[], AuthorizedScope], project_id: str, plan_id: str,
                 resource_id: str = "local", inference: ModelInferenceDelegate | None = None,
                 allow_experimental: bool = False, clock: Callable[[], datetime] = _utcnow,
                 test_fixtures: FixtureMode | None = None) -> PipelineResult:
    """Run ``template`` for ``task``; ``test_fixtures`` is the explicit test-mode token (tests only)."""
    if test_fixtures is not None and not isinstance(test_fixtures, FixtureMode):
        raise OcrPipelineRefused("TEST_MODE_TOKEN_INVALID")
    stages = template.resolve(task)
    descriptors = {s.stage_id: _check_assignment(s, assignments.get(s.stage_id), allow_experimental, test_fixtures)
                   for s in stages}
    plan_hash = compute_plan_hash(template, task, stages, assignments)
    available: dict[str, ArtifactRef] = {SOURCE: task.artifact_ref}
    outputs: dict[str, ArtifactRef] = {}
    decisions: dict[str, str] = {}
    receipts: list[StageReceipt] = []
    document: OcrDocument | None = None
    manifest: PreprocessManifest | None = None
    measurements: dict[int, PageConfidence | None] = {}
    fixture_metric = {FIXTURE_RECEIPT_METRIC: 1.0} if test_fixtures is not None else {}
    for spec in stages:
        capability, descriptor, adapter = (OCR_CAPABILITIES[spec.capability_id], descriptors[spec.stage_id],
                                           assignments[spec.stage_id])
        context = PolicyContext(scope=scope, project_id=project_id, stage_id=spec.stage_id,
                                classification=task.classification, network_policy=task.network_policy,
                                resource_id=resource_id)
        decision = evaluate(capability, descriptor, context, adapter_available=adapter.available)
        if not decision.allowed:
            raise OcrPipelineRefused(decision.code.value, spec.stage_id)
        router_hash = (_router_selection(inference, context, spec.capability_id, task)
                       if capability.family is CapabilityFamily.MODEL_RUNTIME else None)
        request = StageRequest(spec.stage_id, spec.capability_id, tuple(available[name] for name in spec.inputs),
                               task.stage_parameters(), task.classification, router_selection_hash=router_hash)
        started = clock()
        refused: str | None = None
        failed: str | None = None
        try:
            binding = bind_stage(capability, descriptor, context, decision, request,
                                 permitted_inputs=frozenset(ref.sha256 for ref in available.values()))
            output = run_bound_stage(binding, adapter, store, revalidate=revalidate)
        except StageBindingError as exc:
            refused = str(exc) if str(exc) in BINDING_REFUSAL_CODES else "STAGE_BINDING_REFUSED"
        except CapabilityAdapterError as exc:
            failed = closed_code(exc.code)  # an adapter's code leaves only if it is approved
        except Exception:  # a foreign adapter's own exception (which may quote content) never leaves
            failed = OCR_STAGE_FAILED
        if refused is not None:  # raised outside the handlers: no chained exception context
            raise OcrPipelineRefused(refused, spec.stage_id)
        if failed is not None:
            raise OcrPipelineFailed(failed, spec.stage_id)
        finished = clock()
        if len(output.outputs) != 1 or output.outputs[0].kind != _OUTPUT_KIND[spec.output]:
            raise OcrPipelineFailed("STAGE_OUTPUT_CONTRACT_VIOLATION", spec.stage_id)
        ref = output.outputs[0]
        parsed = _parse_output(store, ref, spec, task)
        if isinstance(parsed, PreprocessManifest):
            if parsed.selected_pages != expected_pages(task, parsed):
                raise OcrPipelineFailed("STAGE_OUTPUT_PAGES_MISMATCH", spec.stage_id)
            manifest = parsed
        else:
            _verify_document(parsed, spec=spec, descriptor=descriptor, manifest=manifest, prior=document,
                             test_fixtures=test_fixtures)
            if capability.operation == RECOGNITION_OPERATION:
                measurements.update(_recognition_measurements(parsed, document))
            document = parsed
        if output.partial and capability.failure is FailureSemantics.FAIL_CLOSED:
            raise OcrPipelineFailed("STAGE_PARTIAL_NOT_PERMITTED", spec.stage_id)
        evidence_hash = canonical_hash({"verifier": SCHEMA_VERIFIER_ID, "contract": _OUTPUT_CONTRACT[spec.output],
                                        "stage_id": spec.stage_id, "output_sha256": ref.sha256})
        receipts.append(StageReceipt(
            plan_id=plan_id, stage_id=spec.stage_id, attempt_id=f"{spec.stage_id}.1",
            capability_id=capability.capability_id, capability_family=capability.family,
            capability_version=capability.version, capability_digest=binding.capability_digest,
            adapter_id=descriptor.adapter_id, adapter_version=descriptor.version,
            adapter_digest=binding.adapter_digest, resource_id=resource_id, determinism=descriptor.determinism,
            inputs=request.inputs, outputs=(ref,), parameters_hash=parameters_hash(request.parameters),
            started_at=started.isoformat(), finished_at=finished.isoformat(),
            duration_ms=max(0, int((finished - started).total_seconds() * 1000)),
            verification_method=VerificationMethod.SCHEMA,
            verification=VerificationStatus.NOT_VERIFIED if output.partial else VerificationStatus.PASSED,
            verification_evidence_hash=None if output.partial else evidence_hash,
            verifier_id=None if output.partial else SCHEMA_VERIFIER_ID,
            policy_decision_hash=decision.decision_hash, router_selection_hash=router_hash,
            partial=output.partial, failure_code="STAGE_PARTIAL_OUTPUT" if output.partial else None,
            metrics={**output.metrics, **fixture_metric}, prev_hash="0" * 64))
        available[spec.output] = ref
        outputs[spec.stage_id] = ref
        decisions[spec.stage_id] = decision.decision_hash
    if document is None or manifest is None:  # the template validator guarantees a final document stage
        raise OcrPipelineFailed("PIPELINE_PRODUCED_NO_DOCUMENT", stages[-1].stage_id)
    plan_receipt = PlanReceipt(plan_id=plan_id, plan_hash=plan_hash,
                               expected_stage_ids=tuple(s.stage_id for s in stages),
                               stages=tuple(chain(plan_id, plan_hash, receipts)))
    if not plan_receipt.verify_structure():
        raise OcrPipelineFailed("PLAN_RECEIPT_STRUCTURE_INVALID", stages[-1].stage_id)
    return PipelineResult(plan_id=plan_id, plan_hash=plan_hash, template_id=template.template_id,
                          document_ref=available[DOCUMENT], document=document,
                          stage_outputs=MappingProxyType(outputs), decision_hashes=MappingProxyType(decisions),
                          plan_receipt=plan_receipt,
                          quality=evaluate_quality(task, document, expected_pages=expected_pages(task, manifest),
                                                   measurements=measurements),
                          measurements=MappingProxyType(dict(measurements)),
                          provenance=PROVENANCE_FIXTURE if test_fixtures is not None else PROVENANCE_CANDIDATE)

# SPDX-License-Identifier: AGPL-3.0-only
"""Adapter-side helpers shared by every OCR pack stage (no authority, no I/O beyond the stage facade).

Adapters read only the authorized inputs handed to them through ``StageContext.artifacts`` and write
their single output through the same facade. Every failure surfaces as a ``CapabilityAdapterError``
with a closed code; document text never appears in an error, a metric or evidence. At the public
``execute`` boundary the error is always a NEW exception raised outside any handler, so it carries no
``__cause__`` and no ``__context__`` (an internal exception, a pydantic ``ValidationError`` quoting
input values, or engine glue output can never ride along), and its code is one of the explicitly
approved ``STAGE_CODES``: any other code (an uppercase document identifier or a secret shaped like a
code included) becomes ``OCR_STAGE_FAILED``.

An adapter that needs the bounded child-process runner declares ``requires_isolation``; where the
runner cannot contain a child (Windows) it reports ``available() == False`` with
``unavailable_reason() == "OCR_ISOLATION_UNAVAILABLE"``.
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Iterable, Mapping, Sequence
from typing import Any, ClassVar, Literal

from pydantic import ValidationError

from pados.capability.adapter import CapabilityAdapterError, StageContext, StageOutput, StageRequest
from pados.capability.artifacts import ArtifactAccessDenied, ArtifactIntegrityError, ArtifactRef
from pados.capability.model import AdapterDescriptor
from pados.lifecycle.minimization import DataMinimizationViolation
from pados.packs.ocr._isolated import OCR_SOURCE_TOO_LARGE, ProcessBudget, isolation_unavailable_reason
from pados.packs.ocr.contracts import (
    JSON_MEDIA_TYPE,
    KIND_DOCUMENT,
    KIND_PREPROCESSED,
    KIND_SOURCE,
    OCR_DOCUMENT_CONTRACT,
    EngineRecord,
    OcrContractError,
    OcrDocument,
    OcrEvidence,
    OcrPage,
    PageStatus,
    PreprocessManifest,
    Section,
    StageMark,
    StageParameters,
    summarize_pages,
)
from pados.packs.ocr.descriptors import OCR_CAPABILITIES
from pados.packs.ocr.normalize import EngineResult, recognized_pages, unrecognized_page

FIXTURE_PROVENANCE: Literal["FIXTURE"] = "FIXTURE"
OCR_STAGE_FAILED = "OCR_STAGE_FAILED"
ADAPTER_UNAVAILABLE = "ADAPTER_UNAVAILABLE"

# Every code an OCR stage may surface (explicit registry; a test checks each code the pack raises is here).
STAGE_CODES: frozenset[str] = frozenset({
    # stage boundary and inputs
    "OCR_STAGE_FAILED", "OCR_OUTPUT_INVALID", "STAGE_ARTIFACT_ACCESS_FAILED", "STAGE_CAPABILITY_MISMATCH",
    "STAGE_INPUT_MISSING", "STAGE_INPUT_AMBIGUOUS", "STAGE_PARAMETERS_INVALID", "MEDIA_TYPE_MISMATCH",
    "MEDIA_TYPE_UNSUPPORTED", "MANIFEST_SOURCE_MISMATCH", "PRIOR_DOCUMENT_SOURCE_MISMATCH",
    "OCR_DOCUMENT_MEDIA_TYPE_INVALID", "OCR_DOCUMENT_INVALID", "OCR_DOCUMENT_TOO_LARGE", "OCR_MANIFEST_INVALID",
    "OCR_MANIFEST_TOO_LARGE", "PAGE_COUNT_UNSUPPORTED", "PAGE_RANGE_OUT_OF_BOUNDS", "ADAPTER_UNAVAILABLE",
    # isolated parsing (runner and worker)
    "OCR_SOURCE_TOO_LARGE", "OCR_PARSE_BUDGET_EXCEEDED", "OCR_DECODE_BUDGET_EXCEEDED", "OCR_PARSE_TIMEOUT",
    "OCR_PARSE_FAILED", "OCR_ISOLATION_UNAVAILABLE", "PDF_ENCRYPTED", "PDF_UNREADABLE", "FIGURES_TOO_MANY",
    "MANIFEST_PAGE_COUNT_MISMATCH",
    # images and reference stages
    "IMAGE_HEADER_INVALID", "IMAGE_DIMENSIONS_EXCEEDED", "IMAGE_PIXELS_EXCEEDED", "IMAGE_TIFF_UNSUPPORTED",
    "TABLES_TOO_MANY",
    # engines, bindings and engine results
    "ENGINE_FAILED", "ENGINE_START_FAILED", "ENGINE_EXECUTABLE_INVALID", "ENGINE_BINDING_IMPORT_FAILED",
    "ENGINE_TIMEOUT", "ENGINE_OUTPUT_INVALID", "ENGINE_OUTPUT_TOO_LARGE",
    "ENGINE_BUDGET_EXCEEDED", "LANGUAGE_UNSUPPORTED", "WORKER_ROUTER_SELECTION_REQUIRED", "ENGINE_RESULT_INVALID",
    "ENGINE_RESULT_PAGE_UNEXPECTED", "ENGINE_RESULT_GEOMETRY_INCOMPLETE", "ENGINE_RESULT_BBOX_WITHOUT_GEOMETRY",
    "ENGINE_RESULT_BBOX_OUTSIDE_PAGE",
    # fixture (test-only) adapter
    "FIXTURE_TRUTH_NOT_FOUND", "FIXTURE_TRUTH_INVALID", "FIXTURE_TRUTH_DUPLICATE", "FIXTURE_TRUTH_FILE_INVALID",
    "FIXTURE_TRUTH_TOO_MANY",
})


def closed_code(code: object) -> str:
    """``code`` if it is an approved stage code, else ``OCR_STAGE_FAILED`` (nothing else is echoed)."""
    return code if isinstance(code, str) and code in STAGE_CODES else OCR_STAGE_FAILED


def _closed_code(exc: Exception) -> tuple[str, bool]:
    if isinstance(exc, CapabilityAdapterError):
        code = closed_code(exc.code)
        return code, exc.retryable and code != OCR_STAGE_FAILED
    if isinstance(exc, OcrContractError):
        return closed_code(exc.code), False
    if isinstance(exc, (ArtifactAccessDenied, ArtifactIntegrityError)):
        return "STAGE_ARTIFACT_ACCESS_FAILED", False
    if isinstance(exc, (ValidationError, DataMinimizationViolation)):
        return "OCR_OUTPUT_INVALID", False
    return OCR_STAGE_FAILED, False


class OcrStageAdapter(ABC):
    """Base for pack adapters: maps contract/validation errors to closed adapter error codes."""

    requires_isolation: ClassVar[bool] = False  # runs untrusted parsing or an engine binary in a child

    def __init__(self, descriptor: AdapterDescriptor) -> None:
        self._descriptor = descriptor

    @property
    def descriptor(self) -> AdapterDescriptor:
        return self._descriptor

    def available(self) -> bool:
        return self.unavailable_reason() is None

    def unavailable_reason(self) -> str | None:
        """Why ``available()`` is False, as a closed code (``None`` when available)."""
        if self.requires_isolation:
            isolation = isolation_unavailable_reason()
            if isolation is not None:
                return isolation
        return None if self._ready() else ADAPTER_UNAVAILABLE

    def _ready(self) -> bool:
        """Adapter-specific readiness (engine configured, binding present, ...); side-effect free."""
        return True

    def execute(self, request: StageRequest, *, context: StageContext) -> StageOutput:
        if request.capability_id != self._descriptor.capability_id:
            raise CapabilityAdapterError("STAGE_CAPABILITY_MISMATCH")
        isolation = isolation_unavailable_reason() if self.requires_isolation else None
        if isolation is not None:  # fail closed before any input is read or any child is spawned
            raise CapabilityAdapterError(isolation)
        try:
            return self._execute(request, StageParameters.parse(request.parameters), context)
        except Exception as exc:  # every failure leaves as a closed code (below)
            code, retryable = _closed_code(exc)
        raise CapabilityAdapterError(code, retryable=retryable) from None  # outside the handler: no context

    @abstractmethod
    def _execute(self, request: StageRequest, params: StageParameters, context: StageContext) -> StageOutput:
        """The stage body; called only through ``execute`` (and so only through ``run_bound_stage``)."""


def single_input(request: StageRequest, kind: str) -> ArtifactRef:
    matches = [ref for ref in request.inputs if ref.kind == kind]
    if not matches:
        raise CapabilityAdapterError("STAGE_INPUT_MISSING")
    if len(matches) > 1:
        raise CapabilityAdapterError("STAGE_INPUT_AMBIGUOUS")
    return matches[0]


def optional_input(request: StageRequest, kind: str) -> ArtifactRef | None:
    matches = [ref for ref in request.inputs if ref.kind == kind]
    if len(matches) > 1:
        raise CapabilityAdapterError("STAGE_INPUT_AMBIGUOUS")
    return matches[0] if matches else None


def source_input(request: StageRequest, params: StageParameters) -> ArtifactRef:
    source = single_input(request, KIND_SOURCE)
    if source.media_type != params.media_type:
        raise CapabilityAdapterError("MEDIA_TYPE_MISMATCH")
    return source


def bounded_source(source: ArtifactRef, context: StageContext, budget: ProcessBudget) -> bytes:
    """The source bytes, refused before they are read or parsed if they exceed the budget."""
    if source.size_bytes > budget.max_input_bytes:
        raise CapabilityAdapterError(OCR_SOURCE_TOO_LARGE)
    data = context.artifacts.get(source)
    if len(data) > budget.max_input_bytes:
        raise CapabilityAdapterError(OCR_SOURCE_TOO_LARGE)
    return data


def read_manifest(request: StageRequest, context: StageContext, source: ArtifactRef) -> PreprocessManifest:
    manifest = PreprocessManifest.from_bytes(context.artifacts.get(single_input(request, KIND_PREPROCESSED)))
    if manifest.source != source:
        raise CapabilityAdapterError("MANIFEST_SOURCE_MISMATCH")
    return manifest


def read_document(request: StageRequest, context: StageContext, *, required: bool = True) -> OcrDocument | None:
    ref = single_input(request, KIND_DOCUMENT) if required else optional_input(request, KIND_DOCUMENT)
    if ref is None:
        return None
    if ref.media_type != JSON_MEDIA_TYPE:
        raise CapabilityAdapterError("OCR_DOCUMENT_MEDIA_TYPE_INVALID")
    return OcrDocument.from_bytes(context.artifacts.get(ref))


def engine_record(descriptor: AdapterDescriptor, *, attempted: Iterable[int], pages: Iterable[int]) -> EngineRecord:
    """The complete record of one recognition stage, built only from its descriptor and page outcome.

    Policy (the pipeline verifier rebuilds the same record independently): ``attempted_pages`` are the
    pages the stage was asked to perceive (every selected page for the first recognition stage; the
    previous document's ``NEEDS_OCR`` pages for a later one) and ``pages`` are those of them it
    perceived (``TEXT_LAYER`` or ``RECOGNIZED``, which always carry blocks).
    """
    return EngineRecord(adapter_id=descriptor.adapter_id, adapter_version=descriptor.version,
                        backend=descriptor.backend, engine_version=descriptor.license.engine_version,
                        license_spdx=descriptor.license.spdx, distribution=descriptor.license.distribution.value,
                        operation=OCR_CAPABILITIES[descriptor.capability_id].operation,
                        attempted_pages=tuple(sorted(set(attempted))), pages=tuple(sorted(set(pages))))


def stage_mark(descriptor: AdapterDescriptor, stage_id: str) -> StageMark:
    return StageMark(stage_id=stage_id, capability_id=descriptor.capability_id, adapter_id=descriptor.adapter_id,
                     adapter_version=descriptor.version)


def build_document(source: ArtifactRef, pages: Sequence[OcrPage], *, engines: Sequence[EngineRecord],
                   stages: Sequence[StageMark], sections: Sequence[Section] = (),
                   prior: OcrDocument | None = None, fixture: bool = False) -> OcrDocument:
    """A document whose evidence is computed from its pages; a FIXTURE mark is inherited, never dropped."""
    earlier = {s.page_number: s for s in prior.evidence.page_confidence} if prior is not None else None
    summary, needs, failed = summarize_pages(pages, earlier)
    marked = fixture or (prior is not None and prior.evidence.test_provenance == FIXTURE_PROVENANCE)
    evidence = OcrEvidence(input_sha256=source.sha256, input_media_type=source.media_type, engines=tuple(engines),
                           stages=tuple(stages), page_confidence=summary, pages_total=len(pages),
                           needs_ocr_pages=needs, failed_pages=failed,
                           test_provenance=FIXTURE_PROVENANCE if marked else None)
    return OcrDocument(source=source, pages=tuple(pages), sections=tuple(sections), evidence=evidence)


def derive_document(document: OcrDocument, descriptor: AdapterDescriptor, stage_id: str, *,
                    pages: Sequence[OcrPage] | None = None, sections: Sequence[Section] | None = None,
                    engines: Sequence[EngineRecord] | None = None, fixture: bool = False) -> OcrDocument:
    """A new document from a prior one, with this stage recorded in its evidence."""
    return build_document(document.source, document.pages if pages is None else pages,
                          engines=document.evidence.engines if engines is None else engines,
                          stages=(*document.evidence.stages, stage_mark(descriptor, stage_id)),
                          sections=document.sections if sections is None else sections, prior=document,
                          fixture=fixture)


def emit_document(context: StageContext, document: OcrDocument, descriptor: AdapterDescriptor, *,
                  partial: bool = False, extra: dict[str, str | int | float | bool | None] | None = None
                  ) -> StageOutput:
    payload = document.to_bytes()
    ref = context.artifacts.put(payload, media_type=JSON_MEDIA_TYPE, kind=KIND_DOCUMENT)
    pages = document.pages
    scores = [s.mean for s in document.evidence.page_confidence if s.mean is not None]
    evidence: dict[str, str | int | float | bool | None] = {
        "output_contract": OCR_DOCUMENT_CONTRACT,
        "input_sha256": document.source.sha256,
        "document_sha256": ref.sha256,
        "adapter_id": descriptor.adapter_id,
        "adapter_version": descriptor.version,
        "engine_version": descriptor.license.engine_version,
        "license_spdx": descriptor.license.spdx,
        "pages": len(pages),
        "pages_needs_ocr": len(document.evidence.needs_ocr_pages),
        "pages_failed": len(document.evidence.failed_pages),
        "blocks": sum(len(page.blocks) for page in pages),
        "tables": sum(len(page.tables) for page in pages),
        "figures": sum(len(page.figures) for page in pages),
        "sections": len(document.sections),
        "mean_confidence": round(sum(scores) / len(scores), 4) if scores else None,
        **(extra or {}),
    }
    metrics = {"pages": float(len(pages)), "output_bytes": float(len(payload))}
    return StageOutput(outputs=(ref,), metrics=metrics, evidence=evidence, partial=partial)


def recognition_output(descriptor: AdapterDescriptor, request: StageRequest, context: StageContext,
                       source: ArtifactRef, manifest: PreprocessManifest,
                       result: EngineResult | Mapping[str, Any], *,
                       extra: dict[str, str | int | float | bool | None] | None = None,
                       fixture: bool = False) -> StageOutput:
    """Normalize an engine result into a document. With a prior document input, only its NEEDS_OCR
    pages are filled (an explicitly planned second recognition stage, never an implicit fallback)."""
    recognized = recognized_pages(result, selected=manifest.selected_pages, producer=descriptor.adapter_id)
    prior = read_document(request, context, required=False)
    if prior is None:
        pages = [recognized.get(n) or unrecognized_page(n, manifest.geometry) for n in manifest.selected_pages]
        record = engine_record(descriptor, attempted=manifest.selected_pages, pages=recognized)
        document = build_document(source, pages, engines=(record,),
                                  stages=(stage_mark(descriptor, request.stage_id),), fixture=fixture)
    else:
        if prior.source != source:
            raise CapabilityAdapterError("PRIOR_DOCUMENT_SOURCE_MISMATCH")
        needs = {p.page_number for p in prior.pages if p.status is PageStatus.NEEDS_OCR}
        filled = needs & set(recognized)
        pages = [recognized[p.page_number] if p.page_number in filled else p for p in prior.pages]
        record = engine_record(descriptor, attempted=needs, pages=filled)
        document = derive_document(prior, descriptor, request.stage_id, pages=pages,
                                   engines=(*prior.evidence.engines, record), fixture=fixture)
    return emit_document(context, document, descriptor, partial=bool(document.evidence.failed_pages),
                         extra=extra)

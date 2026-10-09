"""CR-15b OCR-11 / OCR-12 / OCR-13 / OCR-05 (trusted preprocess): independent verification of stage claims.

* OCR-11: a recognition stage's engine record must equal the record rebuilt from the assigned descriptor
  (backend, operation, license SPDX and distribution, adapter id and version) and the exact
  attempted/perceived page sets.
* OCR-12: with a confidence floor every RECOGNIZED page needs a verified measurement (all its lines
  scored), whatever its blocks look like now; measurements survive later stages.
* OCR-13: only approved codes leave a stage or the runner; anything else is ``OCR_STAGE_FAILED``.
* OCR-05: page coverage is trusted only from the pack's own registered preprocess adapter.

New pack symbols are read through their modules at call time, so this file also collects against the
pre-fix source (the "before" run).
"""
from __future__ import annotations

import ast
import re
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

import pytest
from ocr_support import (
    PROJECT,
    fixed_clock,
    fixture_adapter,
    fixture_bytes,
    image_assignments,
    pdf_assignments,
    put_source,
    run_stage,
    scope,
    task,
)
from test_ocr_hardening import on_document, tampering

from pados.capability.adapter import (
    CapabilityAdapter,
    CapabilityAdapterError,
    StageContext,
    StageOutput,
    StageRequest,
)
from pados.capability.artifacts import ArtifactRef, ObjectStoreArtifacts
from pados.capability.model import descriptor_digest
from pados.packs.ocr import _isolated, _stage, engines, pipeline, reference
from pados.packs.ocr._stage import derive_document, emit_document
from pados.packs.ocr.contracts import (
    Block,
    BlockKind,
    OcrContractError,
    OcrDocument,
    OcrEvidence,
    OcrPage,
    PageConfidence,
    PageStatus,
    QualityRequirement,
    StageParameters,
    minimum_confidence,
    union_bbox,
)
from pados.packs.ocr.fixture import TEST_FIXTURE_MODE, FixtureOcrAdapter
from pados.packs.ocr.normalize import EngineLine, EnginePage, EngineResult
from pados.packs.ocr.pipeline import (
    IMAGE_TEMPLATE,
    PDF_HYBRID_TEMPLATE,
    PDF_TEXT_LAYER_TEMPLATE,
    DecompositionTemplate,
    OcrPipelineFailed,
    OcrPipelineRefused,
    PipelineResult,
    evaluate_quality,
    run_pipeline,
)
from pados.packs.ocr.reference import DocumentPreprocessAdapter, LineLayoutAdapter
from pados.packs.ocr.text_layer import PdfTextLayerAdapter

PACK = Path(__file__).resolve().parents[3] / "src" / "pados" / "packs" / "ocr"


def _run(store: ObjectStoreArtifacts, template: DecompositionTemplate, ocr_task: Any,
         assignments: Mapping[str, CapabilityAdapter], **kwargs: Any) -> PipelineResult:
    options: dict[str, Any] = {"store": store, "scope": scope(), "revalidate": scope, "project_id": PROJECT,
                               "plan_id": "plan-ocr-v", "allow_experimental": True, "clock": fixed_clock(),
                               "test_fixtures": TEST_FIXTURE_MODE}
    options.update(kwargs)
    return run_pipeline(template, ocr_task, assignments, **options)


def _pdf(store: ObjectStoreArtifacts, **overrides: Any) -> Any:
    return task(put_source(store, fixture_bytes("text_and_scan.pdf"), "application/pdf"), **overrides)


def _png(store: ObjectStoreArtifacts, **overrides: Any) -> Any:
    return task(put_source(store, fixture_bytes("scan_page.png"), "image/png"), **overrides)


def _last_engine(**changes: Any) -> Callable[[OcrDocument], OcrDocument]:
    def edit(document: OcrDocument) -> OcrDocument:
        engines_ = document.evidence.engines
        forged = engines_[-1].model_copy(update=changes)
        evidence = document.evidence.model_copy(update={"engines": (*engines_[:-1], forged)})
        return document.model_copy(update={"evidence": evidence})
    return edit


# --- OCR-11: the complete engine record is reconciled with the assigned descriptor -------------------------

RECORD_FORGERIES: dict[str, dict[str, Any]] = {
    "misleading-backend": {"backend": "tesseract-cli"},
    "misleading-license-spdx": {"license_spdx": "MIT"},
    "misleading-distribution": {"distribution": "RUNTIME_DEPENDENCY"},
    "misleading-engine-version": {"engine_version": "9.9"},
    "misleading-operation": {"operation": "ANALYZE_LAYOUT"},
    "misleading-adapter-version": {"adapter_version": "2"},
    "extra-claimed-pages": {"attempted_pages": (1, 2), "pages": (1, 2)},
    "attempted-pages-widened": {"attempted_pages": (1, 2)},
}


@pytest.mark.parametrize("forgery", sorted(RECORD_FORGERIES))
def test_second_recognition_engine_record_must_match_its_descriptor(forgery: str, tmp_path: Path) -> None:
    store = ObjectStoreArtifacts(tmp_path)
    forged = tampering(FixtureOcrAdapter, on_document(_last_engine(**RECORD_FORGERIES[forgery])),
                       fixture_adapter()._truths)  # noqa: SLF001
    with pytest.raises(OcrPipelineFailed) as failed:
        _run(store, PDF_HYBRID_TEMPLATE, _pdf(store), {**pdf_assignments(), "recognize_scanned": forged})
    assert (failed.value.code, failed.value.stage_id) == ("STAGE_OUTPUT_PROVENANCE_INVALID", "recognize_scanned")


@pytest.mark.parametrize("changes", [{"pages": (1, 2)}, {"backend": "tesseract-cli"}, {"license_spdx": "MIT"}],
                         ids=["needs-ocr-page-claimed", "backend", "license"])
def test_first_recognition_engine_record_must_match_its_outcome(changes: dict[str, Any], tmp_path: Path) -> None:
    store = ObjectStoreArtifacts(tmp_path)
    forged = tampering(PdfTextLayerAdapter, on_document(_last_engine(**changes)))
    with pytest.raises(OcrPipelineFailed) as failed:
        _run(store, PDF_TEXT_LAYER_TEMPLATE, _pdf(store), {**pdf_assignments(), "recognize": forged},
             test_fixtures=None)
    assert (failed.value.code, failed.value.stage_id) == ("STAGE_OUTPUT_PROVENANCE_INVALID", "recognize")


def test_engine_records_follow_the_explicit_page_policy(tmp_path: Path) -> None:
    store = ObjectStoreArtifacts(tmp_path)
    result = _run(store, PDF_HYBRID_TEMPLATE, _pdf(store), pdf_assignments(hybrid=True))
    text_layer, scanned = result.document.evidence.engines
    assert (text_layer.attempted_pages, text_layer.pages) == ((1, 2), (1,))  # page 2 had no text layer
    assert (scanned.attempted_pages, scanned.pages) == ((2,), (2,))  # only the NEEDS_OCR page was attempted
    assert (text_layer.backend, text_layer.license_spdx, text_layer.distribution) == (
        "pypdf", "BSD-3-Clause", "RUNTIME_DEPENDENCY")
    assert text_layer.operation == scanned.operation == "RECOGNIZE_TEXT"


# --- OCR-12: a confidence floor needs a verified measurement for every recognized page ---------------------

def _paragraph_only_document(claim: float) -> OcrDocument:
    """Model-valid: a RECOGNIZED page with paragraphs only that CLAIMS a confidence summary."""
    source = ArtifactRef(sha256="b" * 64, media_type="image/png", size_bytes=10, kind="ocr.source")
    page = OcrPage(page_number=1, status=PageStatus.RECOGNIZED, producer="ocr.fixture-truth",
                   blocks=(Block(block_id="p1.b1", kind=BlockKind.PARAGRAPH, text="Total due", confidence=claim),),
                   reading_order=("p1.b1",))
    summary = PageConfidence(page_number=1, status=PageStatus.RECOGNIZED, mean=claim, minimum=claim, scored_blocks=1)
    evidence = OcrEvidence(input_sha256=source.sha256, input_media_type="image/png", page_confidence=(summary,),
                           pages_total=1)
    return OcrDocument(source=source, pages=(page,), evidence=evidence)


def test_paragraph_only_recognized_page_without_line_confidence_is_unmeasured(tmp_path: Path) -> None:
    store = ObjectStoreArtifacts(tmp_path)
    strict = _png(store, quality=QualityRequirement(min_mean_confidence=0.5))
    verdict = evaluate_quality(strict, _paragraph_only_document(0.99), expected_pages=(1,))
    assert verdict.met is False and verdict.reasons == ("CONFIDENCE_UNMEASURED",)


def test_partly_scored_recognition_is_unmeasured(tmp_path: Path) -> None:
    store = ObjectStoreArtifacts(tmp_path)
    source = put_source(store, fixture_bytes("scan_page.png"), "image/png")
    partly = EngineResult(pages=(EnginePage(page_number=1, width=64, height=16, unit="px", lines=(
        EngineLine(text="Total due", bbox=(2, 0, 60, 4), confidence=0.99),
        EngineLine(text="Pen  2  3.50", bbox=(2, 12, 50, 15), confidence=None))),))
    strict = task(source, quality=QualityRequirement(min_mean_confidence=0.5))
    result = _run(store, IMAGE_TEMPLATE, strict, image_assignments(FixtureOcrAdapter({source.sha256: partly})))
    assert result.quality.reasons == ("CONFIDENCE_UNMEASURED",)


class LinelessLayout(LineLayoutAdapter):
    """A layout stage whose output keeps paragraphs only (its lines are dropped, as a model might)."""

    def _execute(self, request: StageRequest, params: StageParameters, context: StageContext) -> StageOutput:
        document = reference._document(request, context)  # noqa: SLF001
        pages = []
        for page in document.pages:
            groups = reference.group_paragraphs([b for b in page.blocks if b.kind is BlockKind.LINE])
            paragraphs = tuple(Block(block_id=f"p{page.page_number}.b{index}", kind=BlockKind.PARAGRAPH,
                                     text="\n".join(line.text for line in group),
                                     bbox=union_bbox(line.bbox for line in group),
                                     confidence=minimum_confidence(line.confidence for line in group))
                               for index, group in enumerate(groups, start=1))
            pages.append(reference._replace(page, blocks=paragraphs,  # noqa: SLF001
                                            reading_order=tuple(b.block_id for b in paragraphs)))
        return emit_document(context, derive_document(document, self.descriptor, request.stage_id, pages=pages,
                                                      sections=()), self.descriptor)


@pytest.mark.parametrize(("floor", "reasons"), [(0.9, ()), (0.95, ("CONFIDENCE_BELOW_REQUIREMENT",))])
def test_measurements_survive_a_layout_stage_that_drops_lines(floor: float, reasons: tuple[str, ...],
                                                               tmp_path: Path) -> None:
    store = ObjectStoreArtifacts(tmp_path)
    strict = _png(store, quality=QualityRequirement(min_mean_confidence=floor), preserve_layout=True)
    result = _run(store, IMAGE_TEMPLATE, strict, {**image_assignments(), "layout": LinelessLayout()})
    page = result.document.pages[0]
    assert page.status is PageStatus.RECOGNIZED and not any(b.kind is BlockKind.LINE for b in page.blocks)
    assert result.quality.reasons == reasons  # mean 0.92 measured by the recognition stage


# --- OCR-13: only approved codes leave a stage or the runner -----------------------------------------------

class _Raising(LineLayoutAdapter):
    def __init__(self, error: Exception) -> None:
        super().__init__()
        self.error = error

    def _execute(self, request: StageRequest, params: StageParameters, context: StageContext) -> StageOutput:
        raise self.error


UNAPPROVED = {
    "document-identifier": "INVOICE_4417_ACME_GMBH",
    "secret-shaped": "SK_" + "LIVE" + "Q" * 20,
    "token-shaped": "GHP_" + "A1" * 18,
}


@pytest.mark.parametrize("code", sorted(UNAPPROVED))
def test_unapproved_adapter_codes_become_ocr_stage_failed(code: str, tmp_path: Path) -> None:
    store = ObjectStoreArtifacts(tmp_path)
    ocr_task = _pdf(store)
    source = ocr_task.artifact_ref
    manifest = run_stage(DocumentPreprocessAdapter(), store, (source,), ocr_task).outputs[0]
    document = run_stage(PdfTextLayerAdapter(), store, (source, manifest), ocr_task).outputs[0]
    for error in (CapabilityAdapterError(UNAPPROVED[code], retryable=True),
                  OcrContractError(UNAPPROVED[code])):
        with pytest.raises(CapabilityAdapterError) as failed:
            run_stage(_Raising(error), store, (document,), ocr_task)
        assert failed.value.code == "OCR_STAGE_FAILED" and not failed.value.retryable
        assert UNAPPROVED[code] not in str(failed.value)


class _Foreign:
    """A non-pack adapter (no OcrStageAdapter boundary) that fails with content in its error."""

    def __init__(self, error: Exception) -> None:
        self.error = error
        self.descriptor = LineLayoutAdapter().descriptor

    def available(self) -> bool:
        return True

    def execute(self, request: StageRequest, *, context: StageContext) -> StageOutput:
        raise self.error


@pytest.mark.parametrize("error", [CapabilityAdapterError(UNAPPROVED["document-identifier"]),
                                   RuntimeError("INVOICE-4417 synthetic text")], ids=["code", "foreign-exception"])
def test_runner_closes_codes_of_foreign_adapters(error: Exception, tmp_path: Path) -> None:
    store = ObjectStoreArtifacts(tmp_path)
    with pytest.raises(OcrPipelineFailed) as failed:
        _run(store, PDF_TEXT_LAYER_TEMPLATE, _pdf(store), {**pdf_assignments(), "layout": _Foreign(error)},
             test_fixtures=None)
    assert (failed.value.code, failed.value.stage_id) == ("OCR_STAGE_FAILED", "layout")
    assert failed.value.__cause__ is None and failed.value.__context__ is None


def _raised_codes() -> set[str]:
    """Every string literal passed to CapabilityAdapterError(...) or OcrContractError(...) in the pack."""
    codes: set[str] = set()
    for module in PACK.glob("*.py"):
        for node in ast.walk(ast.parse(module.read_text(encoding="utf-8"))):
            if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id in {"CapabilityAdapterError", "OcrContractError"} and node.args
                    and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str)):
                codes.add(node.args[0].value)
    return codes


def test_every_code_the_pack_raises_is_registered() -> None:
    approved = getattr(_stage, "STAGE_CODES")
    assert _raised_codes() <= approved
    assert _isolated.WORKER_CODES <= approved and engines.BINDING_CODES <= approved
    assert set(engines._ENGINE_CODES.values()) <= approved  # noqa: SLF001
    assert set(_isolated._PARSE_CODES.values()) <= approved  # noqa: SLF001
    assert all(re.fullmatch(r"[A-Z][A-Z0-9_]{1,63}", code) for code in approved)


# --- OCR-05: page coverage is trusted only from the registered preprocess adapter --------------------------

class _OtherPreprocess:
    """Not the pack's preprocess adapter, even though it reuses the registered descriptor."""

    def __init__(self) -> None:
        self.inner = DocumentPreprocessAdapter()
        self.descriptor = self.inner.descriptor

    def available(self) -> bool:
        return True

    def execute(self, request: StageRequest, *, context: StageContext) -> StageOutput:
        return self.inner.execute(request, context=context)


def _changed_descriptor() -> DocumentPreprocessAdapter:
    adapter = DocumentPreprocessAdapter()
    adapter._descriptor = adapter.descriptor.model_copy(update={"version": "2"})  # noqa: SLF001
    return adapter


@pytest.mark.parametrize("make", [_OtherPreprocess, _changed_descriptor], ids=["impostor-class", "other-digest"])
def test_preprocess_must_be_the_registered_adapter(make: Callable[[], Any], tmp_path: Path) -> None:
    store = ObjectStoreArtifacts(tmp_path)
    with pytest.raises(OcrPipelineRefused) as refused:
        _run(store, PDF_TEXT_LAYER_TEMPLATE, _pdf(store), {**pdf_assignments(), "preprocess": make()},
             test_fixtures=None)
    assert (refused.value.code, refused.value.stage_id) == ("PREPROCESS_ADAPTER_NOT_REGISTERED", "preprocess")


def test_registered_preprocess_digest_is_the_pack_adapter() -> None:
    registered = getattr(reference, "REGISTERED_PREPROCESS_DIGESTS")
    assert dict(registered) == {"ocr.preprocess.reference": descriptor_digest(DocumentPreprocessAdapter().descriptor)}
    assert getattr(pipeline, "is_registered_preprocess")(DocumentPreprocessAdapter())

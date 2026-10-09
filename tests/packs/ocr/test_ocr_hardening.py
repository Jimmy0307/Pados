"""CR-15 OCR-03 .. OCR-07: image budgets, the test-mode boundary, independent output verification,
atomic trusted lineage and exception chains free of content."""
from __future__ import annotations

import json
import struct
import zlib
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
from pydantic import ValidationError

from pados.capability.adapter import CapabilityAdapter, CapabilityAdapterError, StageContext, StageOutput, StageRequest
from pados.capability.artifacts import ArtifactRef, ObjectStoreArtifacts
from pados.enterprise.lineage import LineageGraph, LineageKind
from pados.packs.ocr._stage import build_document
from pados.packs.ocr.contracts import (
    KIND_DOCUMENT,
    KIND_PREPROCESSED,
    OcrContractError,
    OcrDocument,
    OcrPage,
    PageGeometry,
    PageRange,
    PageStatus,
    PreprocessManifest,
    QualityRequirement,
)
from pados.packs.ocr.engines import VisionModelOcrAdapter
from pados.packs.ocr.fixture import TEST_FIXTURE_MODE, FixtureOcrAdapter
from pados.packs.ocr.lineage import OcrLineageError, register_ocr_lineage
from pados.packs.ocr.normalize import EngineLine, EnginePage, EngineResult
from pados.packs.ocr.pipeline import (
    IMAGE_TEMPLATE,
    PDF_TEXT_LAYER_TEMPLATE,
    FIXTURE_RECEIPT_METRIC,
    DecompositionTemplate,
    OcrPipelineFailed,
    OcrPipelineRefused,
    PipelineResult,
    evaluate_quality,
    run_pipeline,
)
from pados.packs.ocr.reference import DocumentPreprocessAdapter, HeuristicStructureAdapter, LineLayoutAdapter

CONTENT_MARKER = "INVOICE-4417 synthetic"  # document-like text that must never ride along in an exception chain


def _run(store: ObjectStoreArtifacts, template: DecompositionTemplate, ocr_task: Any,
         assignments: Mapping[str, CapabilityAdapter], **kwargs: Any) -> PipelineResult:
    options: dict[str, Any] = {"store": store, "scope": scope(), "revalidate": scope, "project_id": PROJECT,
                               "plan_id": "plan-ocr-h", "allow_experimental": True, "clock": fixed_clock()}
    options.update(kwargs)
    return run_pipeline(template, ocr_task, assignments, **options)


def _png_header(width: int, height: int) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    chunk = b"IHDR" + ihdr
    return b"\x89PNG\r\n\x1a\n" + struct.pack(">I", 13) + chunk + struct.pack(">I", zlib.crc32(chunk))


def _jpeg(width: int, height: int) -> bytes:
    app0 = b"\xff\xe0" + (16).to_bytes(2, "big") + b"JFIF\x00" + b"\x00" * 9
    sof0 = b"\xff\xc0" + (17).to_bytes(2, "big") + b"\x08" + height.to_bytes(2, "big") + width.to_bytes(2, "big")
    return b"\xff\xd8" + app0 + sof0 + b"\x03" + b"\x00" * 9 + b"\xff\xd9"


# --- OCR-03: image dimension / pixel budgets ---------------------------------------------------------

@pytest.mark.parametrize(("data", "media", "code"), [
    (_png_header(20_001, 10), "image/png", "IMAGE_DIMENSIONS_EXCEEDED"),
    (_png_header(10, 20_001), "image/png", "IMAGE_DIMENSIONS_EXCEEDED"),
    (_png_header(15_000, 15_000), "image/png", "IMAGE_PIXELS_EXCEEDED"),  # 225 MP > 100 MP
    (_jpeg(12_000, 9_000), "image/jpeg", "IMAGE_PIXELS_EXCEEDED"),  # 108 MP
    (b"\x89PNG\r\n\x1a\n" + b"\x00" * 30, "image/png", "IMAGE_HEADER_INVALID"),  # unparseable header
    (_png_header(0, 16), "image/png", "IMAGE_HEADER_INVALID"),
    (b"\xff\xd8\xff" + b"\x00" * 40, "image/jpeg", "IMAGE_HEADER_INVALID"),
    (b"II*\x00" + b"\x08\x00\x00\x00" + b"\x00" * 40, "image/tiff", "IMAGE_TIFF_UNSUPPORTED"),
    (b"MM\x00*" + b"\x00" * 40, "image/tiff", "IMAGE_TIFF_UNSUPPORTED"),
], ids=["png-wide", "png-tall", "png-225mp", "jpeg-108mp", "png-no-ihdr", "png-zero-width", "jpeg-no-sof",
        "tiff-le", "tiff-be"])
def test_image_inputs_need_validated_geometry_within_budget(data: bytes, media: str, code: str,
                                                            tmp_path: Path) -> None:
    store = ObjectStoreArtifacts(tmp_path)
    source = put_source(store, data, media)
    with pytest.raises(CapabilityAdapterError) as refused:
        run_stage(DocumentPreprocessAdapter(), store, (source,), task(source))
    assert refused.value.code == code


def test_image_at_the_budget_is_accepted_and_recorded_in_pixels(tmp_path: Path) -> None:
    store = ObjectStoreArtifacts(tmp_path)
    for data, media in [(_png_header(20_000, 5_000), "image/png"), (_jpeg(10_000, 10_000), "image/jpeg")]:
        source = put_source(store, data, media)
        output = run_stage(DocumentPreprocessAdapter(), store, (source,), task(source))
        manifest = PreprocessManifest.from_bytes(store.get(output.outputs[0]))
        assert manifest.geometry[0].unit == "px" and manifest.geometry[0].width * manifest.geometry[0].height <= 1e8


def test_a_recognition_stage_never_sees_an_unvalidated_image_manifest() -> None:
    source = ArtifactRef(sha256="b" * 64, media_type="image/png", size_bytes=10, kind="ocr.source")
    base = {"source": source, "detected_media_type": "image/png", "page_count": 1, "selected_pages": (1,)}
    for geometry, code in [((), "IMAGE_GEOMETRY_UNVALIDATED"),
                           ((PageGeometry(page_number=1),), "IMAGE_GEOMETRY_UNVALIDATED"),
                           ((PageGeometry(page_number=1, width=10, height=10, unit="pt"),), "IMAGE_GEOMETRY_UNVALIDATED"),
                           ((PageGeometry(page_number=1, width=30_000, height=10, unit="px"),),
                            "IMAGE_DIMENSIONS_EXCEEDED"),
                           ((PageGeometry(page_number=1, width=12_000, height=12_000, unit="px"),),
                            "IMAGE_PIXELS_EXCEEDED")]:
        with pytest.raises(ValidationError, match=code):
            PreprocessManifest(**base, geometry=geometry)
    forged = json.dumps({**{k: v for k, v in base.items() if k != "source"}, "source": source.model_dump(mode="json"),
                         "geometry": [], "schema_version": "ocr.preprocessed/v1", "semantic_authority": "NONE"})
    with pytest.raises(OcrContractError, match="OCR_MANIFEST_INVALID"):
        PreprocessManifest.from_bytes(forged.encode())


# --- OCR-04: fixture adapters need the explicit test-mode token --------------------------------------

def test_allow_experimental_alone_never_admits_a_fixture_adapter(tmp_path: Path) -> None:
    store = ObjectStoreArtifacts(tmp_path)
    png = task(put_source(store, fixture_bytes("scan_page.png"), "image/png"))
    with pytest.raises(OcrPipelineRefused, match="FIXTURE_ADAPTER_REQUIRES_TEST_MODE") as refused:
        _run(store, IMAGE_TEMPLATE, png, image_assignments(), allow_experimental=True)
    assert refused.value.stage_id == "recognize"
    with pytest.raises(OcrPipelineRefused, match="TEST_MODE_TOKEN_INVALID"):
        _run(store, IMAGE_TEMPLATE, png, image_assignments(), test_fixtures=True)
    hybrid = {**pdf_assignments(), "recognize": fixture_adapter()}  # also refused in a PDF plan
    pdf = task(put_source(store, fixture_bytes("text_and_scan.pdf"), "application/pdf"))
    with pytest.raises(OcrPipelineRefused, match="FIXTURE_ADAPTER_REQUIRES_TEST_MODE"):
        _run(store, PDF_TEXT_LAYER_TEMPLATE, pdf, hybrid)


def test_fixture_results_carry_test_provenance_everywhere(tmp_path: Path) -> None:
    store = ObjectStoreArtifacts(tmp_path)
    png = task(put_source(store, fixture_bytes("scan_page.png"), "image/png"))
    result = _run(store, IMAGE_TEMPLATE, png, image_assignments(), test_fixtures=TEST_FIXTURE_MODE)
    assert result.provenance == "FIXTURE"
    assert result.document.evidence.test_provenance == "FIXTURE"
    assert b'"test_provenance":"FIXTURE"' in store.get(result.document_ref)
    assert all(receipt.metrics[FIXTURE_RECEIPT_METRIC] == 1.0 for receipt in result.plan_receipt.stages)
    production = _run(store, PDF_TEXT_LAYER_TEMPLATE,
                      task(put_source(store, fixture_bytes("text_and_scan.pdf"), "application/pdf")),
                      pdf_assignments())
    assert production.provenance == "ENGINE_CANDIDATE" and production.document.evidence.test_provenance is None
    assert b"test_provenance" not in store.get(production.document_ref)
    assert all(FIXTURE_RECEIPT_METRIC not in receipt.metrics for receipt in production.plan_receipt.stages)


# --- tampering adapters (OCR-04 / OCR-05) ------------------------------------------------------------

class _Intercept:
    """Wraps the stage's artifact facade and rewrites the adapter's output before it is stored."""

    def __init__(self, inner: Any, mutate: Callable[[bytes, str], bytes]) -> None:
        self._inner, self._mutate = inner, mutate

    def get(self, ref: ArtifactRef) -> bytes:
        return bytes(self._inner.get(ref))

    def put(self, payload: bytes, *, media_type: str, kind: str) -> ArtifactRef:
        return self._inner.put(self._mutate(payload, kind), media_type=media_type, kind=kind)  # type: ignore[no-any-return]


def tampering(base: type[Any], mutate: Callable[[bytes, str], bytes], *args: Any) -> Any:
    class Tampered(base):  # type: ignore[valid-type,misc]
        def execute(self, request: StageRequest, *, context: StageContext) -> StageOutput:
            wrapped = StageContext(artifacts=_Intercept(context.artifacts, mutate),  # type: ignore[arg-type]
                                   project_id=context.project_id, stage_id=context.stage_id)
            return super().execute(request, context=wrapped)  # type: ignore[no-any-return]

    return Tampered(*args)


def on_document(edit: Callable[[OcrDocument], OcrDocument]) -> Callable[[bytes, str], bytes]:
    return lambda payload, kind: edit(OcrDocument.from_bytes(payload)).to_bytes() if kind == KIND_DOCUMENT else payload


def _rebuilt(document: OcrDocument, pages: tuple[OcrPage, ...]) -> OcrDocument:
    """A schema-valid document whose evidence is consistent with ``pages`` (a convincing forgery)."""
    return build_document(document.source, pages, engines=document.evidence.engines,
                          stages=document.evidence.stages, sections=document.sections)


def _evidence_copy(document: OcrDocument, **changes: Any) -> OcrDocument:
    return document.model_copy(update={"evidence": document.evidence.model_copy(update=changes)})


def _pdf_task(store: ObjectStoreArtifacts, **overrides: Any) -> Any:
    return task(put_source(store, fixture_bytes("text_and_scan.pdf"), "application/pdf"), **overrides)


FORGERIES: dict[str, tuple[Callable[[OcrDocument], OcrDocument], str]] = {
    "omits-a-requested-page": (lambda d: _rebuilt(d, d.pages[:1]), "STAGE_OUTPUT_PAGES_MISMATCH"),
    "needs-ocr-relabelled-failed": (lambda d: _rebuilt(d, (d.pages[0], OcrPage(
        page_number=2, status=PageStatus.FAILED))), "STAGE_OUTPUT_PAGE_STATUS_CHANGED"),
    "text-layer-relabelled-recognized": (lambda d: _rebuilt(d, (d.pages[0].model_copy(
        update={"status": PageStatus.RECOGNIZED}), d.pages[1])), "STAGE_OUTPUT_PAGE_STATUS_CHANGED"),
    "needs-ocr-count-hidden": (lambda d: _evidence_copy(d, needs_ocr_pages=()), "STAGE_OUTPUT_SCHEMA_INVALID"),
    "page-total-inflated": (lambda d: _evidence_copy(d, pages_total=3), "STAGE_OUTPUT_SCHEMA_INVALID"),
    "stage-provenance-rewritten": (lambda d: _evidence_copy(d, stages=d.evidence.stages[-1:]),
                                   "STAGE_OUTPUT_PROVENANCE_INVALID"),
    "engine-provenance-dropped": (lambda d: _evidence_copy(d, engines=()), "STAGE_OUTPUT_PROVENANCE_INVALID"),
    "page-geometry-rewritten": (lambda d: _rebuilt(d, (d.pages[0].model_copy(update={"width": 1.0}), d.pages[1])),
                                "STAGE_OUTPUT_PAGE_STATUS_CHANGED"),
    "fixture-mark-in-production": (lambda d: _evidence_copy(d, test_provenance="FIXTURE"),
                                   "STAGE_OUTPUT_FIXTURE_PROVENANCE"),
}


@pytest.mark.parametrize("forgery", sorted(FORGERIES))
def test_forged_layout_output_is_refused(forgery: str, tmp_path: Path) -> None:
    edit, code = FORGERIES[forgery]
    store = ObjectStoreArtifacts(tmp_path)
    assignments = {**pdf_assignments(), "layout": tampering(LineLayoutAdapter, on_document(edit))}
    with pytest.raises(OcrPipelineFailed) as failed:
        _run(store, PDF_TEXT_LAYER_TEMPLATE, _pdf_task(store), assignments)
    assert (failed.value.code, failed.value.stage_id) == (code, "layout")
    assert failed.value.__cause__ is None and failed.value.__context__ is None


def test_forged_confidence_summaries_are_refused(tmp_path: Path) -> None:
    store = ObjectStoreArtifacts(tmp_path)
    png = task(put_source(store, fixture_bytes("scan_page.png"), "image/png"))

    def inflate(document: OcrDocument) -> OcrDocument:
        summary = document.evidence.page_confidence[0].model_copy(update={"mean": 0.99, "minimum": 0.99})
        return _evidence_copy(document, page_confidence=(summary,))

    # Lines present: the model recomputes the summary from the block confidences.
    with pytest.raises(OcrPipelineFailed, match="STAGE_OUTPUT_SCHEMA_INVALID"):
        _run(store, IMAGE_TEMPLATE, png, {**image_assignments(), "layout": tampering(
            LineLayoutAdapter, on_document(inflate))}, test_fixtures=TEST_FIXTURE_MODE)
    # Lines dropped by structure (preserve_layout=False): the verifier checks the carried summary.
    with pytest.raises(OcrPipelineFailed, match="STAGE_OUTPUT_EVIDENCE_MISMATCH") as failed:
        _run(store, IMAGE_TEMPLATE, png, {**image_assignments(), "structure": tampering(
            HeuristicStructureAdapter, on_document(inflate))}, test_fixtures=TEST_FIXTURE_MODE)
    assert failed.value.stage_id == "structure"


def test_a_later_stage_cannot_drop_the_fixture_mark(tmp_path: Path) -> None:
    store = ObjectStoreArtifacts(tmp_path)
    png = task(put_source(store, fixture_bytes("scan_page.png"), "image/png"))
    launder = tampering(LineLayoutAdapter, on_document(lambda d: _evidence_copy(d, test_provenance=None)))
    with pytest.raises(OcrPipelineFailed, match="STAGE_OUTPUT_PROVENANCE_INVALID"):
        _run(store, IMAGE_TEMPLATE, png, {**image_assignments(), "layout": launder}, test_fixtures=TEST_FIXTURE_MODE)


def test_recognizer_cannot_claim_another_producer(tmp_path: Path) -> None:
    store = ObjectStoreArtifacts(tmp_path)
    png = task(put_source(store, fixture_bytes("scan_page.png"), "image/png"))
    impostor = tampering(FixtureOcrAdapter, on_document(lambda d: _rebuilt(d, (d.pages[0].model_copy(
        update={"producer": "ocr.tesseract-cli"}),))), fixture_adapter()._truths)  # noqa: SLF001
    with pytest.raises(OcrPipelineFailed, match="STAGE_OUTPUT_PROVENANCE_INVALID") as failed:
        _run(store, IMAGE_TEMPLATE, png, image_assignments(impostor), test_fixtures=TEST_FIXTURE_MODE)
    assert failed.value.stage_id == "recognize"


def test_preprocess_cannot_narrow_the_requested_pages(tmp_path: Path) -> None:
    def first_page_only(payload: bytes, kind: str) -> bytes:
        if kind != KIND_PREPROCESSED:
            return payload
        manifest = json.loads(payload)
        manifest.update(selected_pages=[1], geometry=manifest["geometry"][:1])
        return json.dumps(manifest, sort_keys=True, separators=(",", ":")).encode()

    store = ObjectStoreArtifacts(tmp_path)
    # A tampering subclass is not the registered implementation (CR-15c OCR-14): refused before any stage runs.
    assignments = {**pdf_assignments(), "preprocess": tampering(DocumentPreprocessAdapter, first_page_only)}
    with pytest.raises(OcrPipelineRefused) as refused:
        _run(store, PDF_TEXT_LAYER_TEMPLATE, _pdf_task(store), assignments)
    assert (refused.value.code, refused.value.stage_id) == ("PREPROCESS_ADAPTER_NOT_REGISTERED", "preprocess")
    # A manifest narrowed after the registered adapter wrote it still fails the independent page check.
    ocr_task = _pdf_task(store)
    with pytest.raises(OcrPipelineFailed) as failed:
        _run(_Intercept(store, first_page_only), PDF_TEXT_LAYER_TEMPLATE, ocr_task, pdf_assignments())
    assert (failed.value.code, failed.value.stage_id) == ("STAGE_OUTPUT_PAGES_MISMATCH", "preprocess")


def test_empty_recognition_is_needs_ocr_and_fails_quality(tmp_path: Path) -> None:
    with pytest.raises(ValidationError, match="RECOGNIZED_PAGE_HAS_NO_BLOCKS"):
        OcrPage(page_number=1, status=PageStatus.RECOGNIZED)
    store = ObjectStoreArtifacts(tmp_path)
    data = fixture_bytes("scan_page.png")
    source = put_source(store, data, "image/png")
    blank = EngineResult(pages=(EnginePage(page_number=1, width=64, height=16, unit="px",
                                           lines=(EngineLine(text="   "), EngineLine(text="\t"))),))
    result = _run(store, IMAGE_TEMPLATE, task(source), image_assignments(FixtureOcrAdapter({source.sha256: blank})),
                  test_fixtures=TEST_FIXTURE_MODE)
    assert result.document.pages[0].status is PageStatus.NEEDS_OCR
    assert result.document.evidence.engines[0].pages == ()
    assert result.quality.met is False and result.quality.reasons == ("NEEDS_OCR_PAGES_PRESENT",)


def test_quality_met_means_every_requested_page_was_perceived(tmp_path: Path) -> None:
    store = ObjectStoreArtifacts(tmp_path)
    accepting = QualityRequirement(allow_needs_ocr_pages=True)
    result = _run(store, PDF_TEXT_LAYER_TEMPLATE, _pdf_task(store, quality=accepting), pdf_assignments())
    assert result.quality.met
    partial = _rebuilt(result.document, result.document.pages[:1])
    verdict = evaluate_quality(_pdf_task(store, quality=accepting), partial, expected_pages=(1, 2))
    assert verdict.met is False and "REQUESTED_PAGES_MISSING" in verdict.reasons
    second = _run(store, PDF_TEXT_LAYER_TEMPLATE, _pdf_task(store, page_range=PageRange(first=2, last=2),
                                                            quality=accepting), pdf_assignments())
    assert [p.page_number for p in second.document.pages] == [2] and second.quality.met


# --- OCR-06: lineage is bound to a trusted source identity and is atomic ------------------------------

def _lineage_fixture(tmp_path: Path) -> tuple[PipelineResult, str]:
    store = ObjectStoreArtifacts(tmp_path)
    result = _run(store, PDF_TEXT_LAYER_TEMPLATE, _pdf_task(store), pdf_assignments())
    return result, "src:" + result.document.source.sha256[:32]


def _snapshot(graph: LineageGraph) -> tuple[dict[str, LineageKind], dict[str, set[str]], set[str]]:
    return dict(graph.nodes), {k: set(v) for k, v in graph.children.items()}, set(graph.revoked)


def test_lineage_refuses_an_unproven_source_without_touching_the_graph(tmp_path: Path) -> None:
    result, source_id = _lineage_fixture(tmp_path)
    graph = LineageGraph()
    graph.add(source_id, LineageKind.SOURCE)
    graph.add("src:other", LineageKind.SOURCE)
    before = _snapshot(graph)
    for sources, node, code in [({source_id: "f" * 64}, source_id, "LINEAGE_SOURCE_MISMATCH"),
                                ({"src:other": "f" * 64}, "src:other", "LINEAGE_SOURCE_MISMATCH"),
                                ({}, source_id, "LINEAGE_SOURCE_IDENTITY_UNKNOWN"),
                                ({source_id: "not-a-hash"}, source_id, "LINEAGE_SOURCE_IDENTITY_UNKNOWN")]:
        with pytest.raises(OcrLineageError, match=code):
            register_ocr_lineage(graph, source_id=node, result=result, trusted_sources=sources)
        assert _snapshot(graph) == before
    # Some other source registered with this document's hash still cannot adopt it under the wrong id.
    with pytest.raises(OcrLineageError, match="LINEAGE_SOURCE_IDENTITY_UNKNOWN"):
        register_ocr_lineage(graph, source_id="src:other", result=result,
                             trusted_sources={source_id: result.document.source.sha256})
    assert _snapshot(graph) == before


class _FailingGraph(LineageGraph):
    """Fails on the n-th ``add`` (after earlier adds succeeded) to simulate a mid-registration failure."""

    fail_at = 3

    def add(self, node_id: str, kind: LineageKind, parents: Any = ()) -> str:
        self.fail_at -= 1
        if self.fail_at == 0:
            raise RuntimeError(CONTENT_MARKER)
        return super().add(node_id, kind, parents)


def test_a_failure_mid_registration_leaves_the_graph_unchanged(tmp_path: Path) -> None:
    result, source_id = _lineage_fixture(tmp_path)
    graph = _FailingGraph()
    graph.fail_at = 1_000
    graph.add(source_id, LineageKind.SOURCE)
    graph.fail_at = 3
    before = _snapshot(graph)
    with pytest.raises(OcrLineageError, match="LINEAGE_REGISTRATION_ROLLED_BACK") as failed:
        register_ocr_lineage(graph, source_id=source_id, result=result,
                             trusted_sources={source_id: result.document.source.sha256})
    assert _snapshot(graph) == before
    assert failed.value.__cause__ is None and failed.value.__context__ is None
    conflict = LineageGraph()
    conflict.add(source_id, LineageKind.SOURCE)
    intermediate = register_ocr_lineage(conflict, source_id=source_id, result=result,
                                        trusted_sources={source_id: result.document.source.sha256})
    conflict.nodes[intermediate.chunk_nodes[0]] = LineageKind.CACHE  # an existing node of another kind
    before = _snapshot(conflict)
    with pytest.raises(OcrLineageError, match="LINEAGE_NODE_KIND_CONFLICT"):
        register_ocr_lineage(conflict, source_id=source_id, result=result,
                             trusted_sources={source_id: result.document.source.sha256})
    assert _snapshot(conflict) == before


# --- OCR-07: closed codes with no exception chain ------------------------------------------------------

def _chain(exc: BaseException) -> list[BaseException]:
    seen: list[BaseException] = []
    current: BaseException | None = exc
    while current is not None and current not in seen:
        seen.append(current)
        current = current.__cause__ or current.__context__
    return seen


class _LeakyBinding:
    def __init__(self, error: Exception) -> None:
        self.error = error

    def ready(self) -> bool:
        return True

    def recognize(self, payload: bytes, **_: object) -> Mapping[str, Any]:
        raise self.error

    def transcribe(self, payload: bytes, **_: object) -> Mapping[str, Any]:
        raise self.error


def _image_inputs(store: ObjectStoreArtifacts) -> tuple[tuple[ArtifactRef, ...], Any]:
    source = put_source(store, fixture_bytes("scan_page.png"), "image/png")
    ocr_task = task(source)
    manifest = run_stage(DocumentPreprocessAdapter(), store, (source,), ocr_task).outputs[0]
    return (source, manifest), ocr_task


# Operator engine bindings (PaddleOCR / MinerU) no longer run in-process (CR-15c OCR-03): their
# content-free failure codes are tested against the isolated binding worker in test_ocr_cr15c.py.


def test_vision_binding_failure_carries_no_content(tmp_path: Path) -> None:
    store = ObjectStoreArtifacts(tmp_path)
    inputs, ocr_task = _image_inputs(store)
    vision = VisionModelOcrAdapter(_LeakyBinding(RuntimeError(CONTENT_MARKER)))
    request = StageRequest("recognize", vision.descriptor.capability_id, inputs, ocr_task.stage_parameters(),
                           ocr_task.classification, router_selection_hash="e" * 64)
    context = StageContext(artifacts=_Intercept(_Readable(store, inputs), lambda p, k: p),  # type: ignore[arg-type]
                           project_id=PROJECT, stage_id="recognize")
    with pytest.raises(CapabilityAdapterError) as failed:
        vision._execute(request, _params(ocr_task), context)  # noqa: SLF001 - the binding boundary itself
    assert failed.value.code == "ENGINE_FAILED" and _chain(failed.value) == [failed.value]
    with pytest.raises(CapabilityAdapterError) as boundary:
        vision.execute(request, context=context)
    assert boundary.value.code == "ENGINE_FAILED" and _chain(boundary.value) == [boundary.value]


def test_stage_boundary_strips_internal_exception_chains(tmp_path: Path) -> None:
    class Exploding(LineLayoutAdapter):
        def _execute(self, request: StageRequest, params: Any, context: StageContext) -> StageOutput:
            try:
                raise KeyError(CONTENT_MARKER)
            except KeyError as inner:
                raise OcrContractError("OCR_DOCUMENT_INVALID") from inner

    class Crashing(LineLayoutAdapter):
        def _execute(self, request: StageRequest, params: Any, context: StageContext) -> StageOutput:
            raise ValueError(CONTENT_MARKER)

    store = ObjectStoreArtifacts(tmp_path)
    for adapter, code in [(Exploding(), "OCR_DOCUMENT_INVALID"), (Crashing(), "OCR_STAGE_FAILED")]:
        with pytest.raises(OcrPipelineFailed) as failed:
            _run(store, PDF_TEXT_LAYER_TEMPLATE, _pdf_task(store), {**pdf_assignments(), "layout": adapter})
        assert failed.value.code == code and _chain(failed.value) == [failed.value]
    # A schema-invalid document input (pydantic errors quote input values) leaves as a bare closed code.
    source = put_source(store, fixture_bytes("text_and_scan.pdf"), "application/pdf")
    bogus = store.put(json.dumps({"text": CONTENT_MARKER}).encode(), media_type="application/json", kind=KIND_DOCUMENT)
    with pytest.raises(CapabilityAdapterError) as invalid:
        run_stage(LineLayoutAdapter(), store, (bogus,), task(source))
    assert invalid.value.code == "OCR_DOCUMENT_INVALID" and _chain(invalid.value) == [invalid.value]


class _Readable:
    def __init__(self, store: ObjectStoreArtifacts, refs: tuple[ArtifactRef, ...]) -> None:
        self._store, self._refs = store, refs

    def get(self, ref: ArtifactRef) -> bytes:
        assert ref in self._refs
        return self._store.get(ref)

    def put(self, payload: bytes, *, media_type: str, kind: str) -> ArtifactRef:
        return self._store.put(payload, media_type=media_type, kind=kind)


def _params(ocr_task: Any) -> Any:
    from pados.packs.ocr.contracts import StageParameters

    return StageParameters.parse(ocr_task.stage_parameters())


def test_contract_parsers_never_chain_the_offending_input() -> None:
    from pados.packs.ocr.engines import parse_tesseract_tsv
    from pados.packs.ocr.normalize import parse_engine_result

    header = "\t".join(["level", "page_num", "block_num", "par_num", "line_num", "word_num", "left", "top", "width",
                        "height", "conf", "text"])
    attempts: list[Callable[[], object]] = [
        lambda: OcrDocument.from_bytes(json.dumps({"pages": [CONTENT_MARKER]}).encode()),
        lambda: PreprocessManifest.from_bytes(json.dumps({"source": CONTENT_MARKER}).encode()),
        lambda: parse_engine_result({"pages": [{"page_number": 1, "lines": [{"text": CONTENT_MARKER, "confidence": 7}]}]}),
        lambda: parse_tesseract_tsv(header + "\n" + "\t".join([CONTENT_MARKER] * 12)),
    ]
    for attempt in attempts:
        with pytest.raises(OcrContractError) as failed:
            attempt()
        assert _chain(failed.value) == [failed.value] and CONTENT_MARKER not in str(failed.value)

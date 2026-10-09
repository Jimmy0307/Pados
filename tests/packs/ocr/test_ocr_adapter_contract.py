"""Provider-agnostic adapter contract: every shipped adapter is checked by the same tests.

Available adapters are executed (only through bind_stage/run_bound_stage); unavailable ones - every
third-party engine, since none is installed or configured - are refused by the shared policy with
ADAPTER_UNAVAILABLE and are never executed.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from ocr_support import (
    every_adapter,
    fixture_adapter,
    fixture_bytes,
    permitted_context,
    put_source,
    run_stage,
    task,
)

from pados.capability.adapter import CapabilityAdapter, StageBindingError, StageOutput, StageRequest, bind_stage
from pados.capability.artifacts import ArtifactRef, ObjectStoreArtifacts
from pados.capability.model import Determinism
from pados.capability.policy import PolicyCode, evaluate
from pados.packs.ocr.contracts import (
    KIND_DOCUMENT,
    KIND_PREPROCESSED,
    OcrDocument,
    PageStatus,
    PreprocessManifest,
)
from pados.packs.ocr.descriptors import (
    DOCUMENT_LAYOUT_ANALYSIS,
    DOCUMENT_OCR,
    DOCUMENT_PREPROCESS,
    DOCUMENT_STRUCTURE_PARSE,
    FIGURE_EXTRACTION,
    OCR_CAPABILITIES,
    TABLE_EXTRACTION,
)
from pados.packs.ocr.reference import DocumentPreprocessAdapter, image_geometry
from pados.packs.ocr.text_layer import PdfTextLayerAdapter

AVAILABLE = {"ocr.preprocess.reference", "ocr.pdf-text-layer", "ocr.fixture-truth", "ocr.layout.line-grouping",
             "ocr.table.whitespace-aligned", "ocr.figure.pdf-xobject", "ocr.structure.heuristic"}
UNAVAILABLE = {"ocr.tesseract-cli", "ocr.paddleocr", "ocr.mineru-style", "ocr.vision-model.local",
               "ocr.vision-model.cloud"}
WINDOWS_ISOLATION_UNAVAILABLE = {"ocr.preprocess.reference", "ocr.pdf-text-layer", "ocr.figure.pdf-xobject"}
CONTENT_WORDS = ("Quarterly", "Region", "INVOICE", "Scanned", "synthetic")
ADAPTERS = every_adapter()


def _inputs(adapter: CapabilityAdapter, store: ObjectStoreArtifacts) -> tuple[ArtifactRef, ...]:
    """Valid inputs for the adapter's capability, produced through bound stages."""
    if sys.platform == "win32" and adapter.descriptor.adapter_id not in WINDOWS_ISOLATION_UNAVAILABLE:
        source = put_source(store, fixture_bytes("scan_page.png"), "image/png")
        manifest = PreprocessManifest(
            source=source,
            detected_media_type=source.media_type,
            page_count=1,
            selected_pages=(1,),
            geometry=(image_geometry(fixture_bytes("scan_page.png"), source.media_type),),
        )
        manifest_ref = store.put(manifest.to_bytes(), media_type="application/json", kind=KIND_PREPROCESSED)
        if adapter.descriptor.adapter_id == "ocr.fixture-truth":
            return (source, manifest_ref)
        fixture_document = run_stage(fixture_adapter(), store, (source, manifest_ref), task(source)).outputs[0]
        if adapter.descriptor.capability_id in {
            DOCUMENT_LAYOUT_ANALYSIS, TABLE_EXTRACTION, DOCUMENT_STRUCTURE_PARSE
        }:
            return (fixture_document,)

    use_png = adapter.descriptor.adapter_id == "ocr.fixture-truth"
    media = "image/png" if use_png else "application/pdf"
    source = put_source(store, fixture_bytes("scan_page.png" if use_png else "text_and_scan.pdf"), media)
    if adapter.descriptor.capability_id == DOCUMENT_PREPROCESS:
        return (source,)
    ocr_task = task(source)
    manifest = run_stage(DocumentPreprocessAdapter(), store, (source,), ocr_task).outputs[0]
    if adapter.descriptor.capability_id == DOCUMENT_OCR:
        return (source, manifest)
    document = run_stage(PdfTextLayerAdapter(), store, (source, manifest), ocr_task).outputs[0]
    return (source, document) if adapter.descriptor.capability_id == FIGURE_EXTRACTION else (document,)


def _execute(adapter: CapabilityAdapter, root: Path) -> tuple[ObjectStoreArtifacts, tuple[ArtifactRef, ...], StageOutput]:
    store = ObjectStoreArtifacts(root)
    inputs = _inputs(adapter, store)
    return store, inputs, run_stage(adapter, store, inputs, task(inputs[0] if inputs[0].kind == "ocr.source"
                                                                 else _source_of(store, inputs[0])))


def _source_of(store: ObjectStoreArtifacts, document: ArtifactRef) -> ArtifactRef:
    return OcrDocument.from_bytes(store.get(document)).source


def test_shipped_adapter_set_is_classified() -> None:
    ids = {adapter.descriptor.adapter_id for adapter in ADAPTERS}
    assert ids == AVAILABLE | UNAVAILABLE
    expected_available = AVAILABLE - WINDOWS_ISOLATION_UNAVAILABLE if sys.platform == "win32" else AVAILABLE
    assert {a.descriptor.adapter_id for a in ADAPTERS if a.available()} == expected_available


@pytest.mark.parametrize("adapter", [a for a in ADAPTERS if a.descriptor.adapter_id in AVAILABLE],
                         ids=lambda a: a.descriptor.adapter_id)
def test_available_adapter_satisfies_the_output_contract(adapter: CapabilityAdapter, tmp_path: Path) -> None:
    if sys.platform == "win32" and adapter.descriptor.adapter_id in WINDOWS_ISOLATION_UNAVAILABLE:
        pytest.skip("OCR_LIVE_CONTAINMENT_UAT=PENDING: safe preprocess stage is unavailable on this platform")
    store, inputs, output = _execute(adapter, tmp_path / "a")
    capability = OCR_CAPABILITIES[adapter.descriptor.capability_id]
    assert output.semantic_authority == "NONE" and len(output.outputs) == 1
    ref = output.outputs[0]
    payload = store.get(ref)
    assert output.evidence["adapter_id"] == adapter.descriptor.adapter_id
    source = inputs[0] if inputs[0].kind == "ocr.source" else _source_of(store, inputs[0])
    assert output.evidence["input_sha256"] == source.sha256
    assert not any(word in str(value) for value in output.evidence.values() for word in CONTENT_WORDS)
    if capability.output_contract == "ocr.preprocessed/v1":
        assert ref.kind == KIND_PREPROCESSED and PreprocessManifest.from_bytes(payload).source == source
    else:
        assert ref.kind == KIND_DOCUMENT and ref.media_type == "application/json"
        document = OcrDocument.from_bytes(payload)
        assert document.source == source and document.semantic_authority == "NONE"
        assert document.evidence.stages[-1].adapter_id == adapter.descriptor.adapter_id
        assert len(document.evidence.page_confidence) == len(document.pages)  # per-page confidence summary
        assert document.evidence.engines and document.evidence.engines[0].license_spdx
        assert output.evidence["document_sha256"] == ref.sha256
    if adapter.descriptor.determinism is Determinism.DETERMINISTIC:  # replay in a fresh store: same bytes
        _, _, replay = _execute(adapter, tmp_path / "b")
        assert replay.outputs[0].sha256 == ref.sha256


@pytest.mark.parametrize("adapter", [a for a in ADAPTERS if a.descriptor.adapter_id in UNAVAILABLE],
                         ids=lambda a: a.descriptor.adapter_id)
def test_unavailable_adapter_is_refused_and_never_executed(adapter: CapabilityAdapter, tmp_path: Path) -> None:
    context = permitted_context(adapter)
    capability = OCR_CAPABILITIES[adapter.descriptor.capability_id]
    decision = evaluate(capability, adapter.descriptor, context, adapter_available=adapter.available)
    assert decision.code is PolicyCode.ADAPTER_UNAVAILABLE
    assert all(ok for name, ok in decision.checks if name != PolicyCode.ADAPTER_UNAVAILABLE.value)
    ref = ArtifactRef(sha256="d" * 64, media_type="image/png", size_bytes=1, kind="ocr.source")
    request = StageRequest("stage", capability.capability_id, (ref,), {}, context.classification)
    with pytest.raises(StageBindingError, match="NOT_ALLOWED"):
        bind_stage(capability, adapter.descriptor, context, decision, request, permitted_inputs=frozenset({ref.sha256}))


def test_text_layer_marks_pages_without_text_as_needs_ocr(tmp_path: Path) -> None:
    preprocess = DocumentPreprocessAdapter()
    if not preprocess.available():
        reason = preprocess.unavailable_reason() or "UNKNOWN"
        if sys.platform != "win32":
            pytest.fail(f"preprocess adapter unexpectedly unavailable outside Windows: {reason}")
        pytest.skip(f"OCR_LIVE_CONTAINMENT_UAT=PENDING: {reason}")
    store = ObjectStoreArtifacts(tmp_path)
    adapter = PdfTextLayerAdapter()
    inputs = _inputs(adapter, store)
    output = run_stage(adapter, store, inputs, task(inputs[0]))
    document = OcrDocument.from_bytes(store.get(output.outputs[0]))
    first, second = document.pages
    assert first.status is PageStatus.TEXT_LAYER and first.blocks[0].text == "1. Quarterly Report"
    assert all(block.confidence is None and block.bbox is None for block in first.blocks)
    assert second.status is PageStatus.NEEDS_OCR and second.blocks == () and second.producer is None
    assert document.evidence.needs_ocr_pages == (2,) and output.evidence["pages_needs_ocr"] == 1
    assert output.partial is False

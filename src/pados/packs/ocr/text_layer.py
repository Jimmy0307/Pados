# SPDX-License-Identifier: AGPL-3.0-only
"""PDF text-layer adapter (REAL, local, deterministic): embedded text via pypdf, never recognition.

pypdf (BSD-3-Clause) is an existing PADOS runtime dependency. The adapter uses pypdf's public
``extract_text(extraction_mode="layout")``, which renders the embedded text on a fixed-width grid so
column alignment survives (the table stage relies on it). A page with no extractable text is marked
``NEEDS_OCR`` with no blocks: text is never invented, and nothing silently falls back to OCR or a model.
Blocks carry ``confidence = None`` (embedded text is not a recognition estimate) and ``bbox = None``
(pypdf's public API exposes no glyph geometry); ``line_index`` keeps the vertical grid position.

pypdf never runs in this process: page counting and text extraction happen in the isolated worker
(``_isolated.isolated_pdf``) under memory, CPU, wall-clock and output budgets, after the source size
is bounded. The worker applies the same line limits and a decoded-text budget; the result here is
rebuilt and re-validated. Where the runner cannot contain a child (Windows) the adapter is unavailable
(``OCR_ISOLATION_UNAVAILABLE``). Its engine record lists every selected page as attempted and the
``TEXT_LAYER`` pages as perceived.
"""
from __future__ import annotations

from importlib import metadata
from importlib.util import find_spec

from pados.capability.adapter import CapabilityAdapterError, StageContext, StageOutput, StageRequest
from pados.capability.model import AdapterDescriptor, Determinism, Distribution, LicenseInfo
from pados.connectivity.requirements import NetworkRequirement
from pados.enterprise.classification import DataClassification
from pados.packs.ocr._isolated import (
    OCR_PARSE_FAILED,
    PDF_PARSE_BUDGET,
    ProcessBudget,
    as_dimension,
    as_int,
    as_list,
    as_row,
    isolated_pdf,
    page_rows,
)
from pados.packs.ocr._pdf_worker import text_lines
from pados.packs.ocr._stage import (
    OcrStageAdapter,
    bounded_source,
    build_document,
    emit_document,
    engine_record,
    read_manifest,
    source_input,
    stage_mark,
)
from pados.packs.ocr.contracts import (
    MAX_BLOCK_TEXT,
    MAX_BLOCKS_PER_PAGE,
    PDF_MEDIA_TYPE,
    Block,
    BlockKind,
    OcrPage,
    PageStatus,
    StageParameters,
)
from pados.packs.ocr.descriptors import DOCUMENT_OCR

TEXT_LAYER_ADAPTER_ID = "ocr.pdf-text-layer"


def _pypdf_version() -> str:
    try:
        return metadata.version("pypdf")
    except metadata.PackageNotFoundError:
        return "n/a"


def text_layer_descriptor() -> AdapterDescriptor:
    return AdapterDescriptor(
        adapter_id=TEXT_LAYER_ADAPTER_ID, capability_id=DOCUMENT_OCR, version="1", backend="pypdf",
        determinism=Determinism.DETERMINISTIC, max_classification=DataClassification.RESTRICTED,
        network_requirement=NetworkRequirement.LOCAL_ONLY,
        license=LicenseInfo(spdx="BSD-3-Clause", engine_version=_pypdf_version(),
                            distribution=Distribution.RUNTIME_DEPENDENCY))


_PAGE_STATES = frozenset({"OK", "OVERSIZE", "ERROR"})


def _worker_lines(value: object) -> list[tuple[int, str]]:
    lines: list[tuple[int, str]] = []
    for row in as_list(value, max_items=MAX_BLOCKS_PER_PAGE):
        index, text = as_row(row, 2)
        if not isinstance(text, str) or not text or len(text) > MAX_BLOCK_TEXT:
            raise CapabilityAdapterError(OCR_PARSE_FAILED)
        lines.append((as_int(index, high=10_000_000), text))
    return lines


class PdfTextLayerAdapter(OcrStageAdapter):
    requires_isolation = True

    def __init__(self, *, budget: ProcessBudget = PDF_PARSE_BUDGET) -> None:
        super().__init__(text_layer_descriptor())
        self._budget = budget

    def _ready(self) -> bool:
        return find_spec("pypdf") is not None

    def _execute(self, request: StageRequest, params: StageParameters, context: StageContext) -> StageOutput:
        source = source_input(request, params)
        if source.media_type != PDF_MEDIA_TYPE:
            raise CapabilityAdapterError("MEDIA_TYPE_UNSUPPORTED")
        manifest = read_manifest(request, context, source)
        result = isolated_pdf("text", bounded_source(source, context, self._budget),
                              {"page_count": manifest.page_count, "pages": list(manifest.selected_pages),
                               "max_blocks": MAX_BLOCKS_PER_PAGE, "max_block_text": MAX_BLOCK_TEXT},
                              budget=self._budget)
        pages: list[OcrPage] = []
        for number, (extracted,) in page_rows(_page_tuples(result.get("pages")), 2, manifest.selected_pages):
            state = extracted.get("state")
            if state == "ERROR":  # a malformed page fails alone and is marked FAILED (explicitly partial)
                pages.append(OcrPage(page_number=number, status=PageStatus.FAILED, producer=TEXT_LAYER_ADAPTER_ID))
                continue
            width, height = as_dimension(extracted.get("width")), as_dimension(extracted.get("height"))
            lines = _worker_lines(extracted.get("lines"))
            pages.append(self._page(number, lines, width or 0.0, height or 0.0, oversize=state == "OVERSIZE"))
        perceived = [page.page_number for page in pages if page.status is PageStatus.TEXT_LAYER]
        record = engine_record(self.descriptor, attempted=manifest.selected_pages, pages=perceived)
        document = build_document(source, pages, engines=(record,), stages=(stage_mark(self.descriptor, request.stage_id),))
        return emit_document(context, document, self.descriptor,
                             partial=bool(document.evidence.failed_pages))

    @staticmethod
    def _page(number: int, lines: list[tuple[int, str]], width: float, height: float, *,
              oversize: bool = False) -> OcrPage:
        measured = width > 0 and height > 0
        geometry = OcrPage(page_number=number, status=PageStatus.NEEDS_OCR, width=width if measured else None,
                           height=height if measured else None, unit="pt" if measured else None)
        if oversize or len(lines) > MAX_BLOCKS_PER_PAGE or any(len(text) > MAX_BLOCK_TEXT for _, text in lines):
            return geometry.model_copy(update={"status": PageStatus.FAILED, "producer": TEXT_LAYER_ADAPTER_ID})
        if not lines:
            return geometry
        blocks = tuple(Block(block_id=f"p{number}.l{position}", kind=BlockKind.LINE, text=text, line_index=index)
                       for position, (index, text) in enumerate(lines, start=1))
        return OcrPage.model_validate({**geometry.model_dump(), "status": PageStatus.TEXT_LAYER,
                                       "producer": TEXT_LAYER_ADAPTER_ID, "blocks": blocks,
                                       "reading_order": tuple(block.block_id for block in blocks)})


def _page_tuples(value: object) -> list[list[object]]:
    """Worker page records ``{"page": n, "state": ..., ...}`` as ``[n, record]`` rows (closed keys)."""
    rows: list[list[object]] = []
    for record in as_list(value, max_items=100_000):
        if not isinstance(record, dict) or record.get("state") not in _PAGE_STATES:
            raise CapabilityAdapterError(OCR_PARSE_FAILED)
        keys = {"page", "state"} if record["state"] == "ERROR" else {"page", "state", "width", "height", "lines"}
        if set(record) != keys:
            raise CapabilityAdapterError(OCR_PARSE_FAILED)
        rows.append([record["page"], record])
    return rows


__all__ = ["TEXT_LAYER_ADAPTER_ID", "PdfTextLayerAdapter", "text_layer_descriptor", "text_lines"]

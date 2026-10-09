# SPDX-License-Identifier: AGPL-3.0-only
"""Deterministic reference stages (PADOS-owned, local, pure Python) for an end-to-end testable pipeline.

Each class is one replaceable adapter for one capability; stronger engines (layout models, table
transformers) replace them through the same descriptors and policy, never by editing Core.

* preprocess  - source size bound, magic-byte sniffing against the claimed media type, PDF page
                count / encryption / MediaBox geometry (pypdf, in the isolated worker), PNG/JPEG
                pixel size from headers checked against ``MAX_IMAGE_SIDE`` / ``MAX_IMAGE_PIXELS``
                (an unparseable header fails closed), page selection. TIFF is refused in V1
                (``IMAGE_TIFF_UNSUPPORTED``): its dimensions and frame count are not validated.
* layout      - line grouping into paragraphs from vertical adjacency (grid ``line_index`` or bbox
                gaps); reading order is top-to-bottom (single column).
* tables      - whitespace-aligned table detection: >= 2 consecutive lines of one paragraph that split
                into the same number (>= 2) of cells on runs of two or more spaces.
* figures     - PDF image XObjects listed per page by name and pixel size (bytes are not extracted;
                pypdf runs in the isolated worker).
* structure   - numbered or upper-case single-line reading-order units become headings; sections
                follow them. Without ``preserve_layout`` only reading-order units are kept (lines that
                belong to a paragraph are dropped and references to them are pruned).
"""
from __future__ import annotations

import re
from collections.abc import Iterator, Mapping, Sequence
from types import MappingProxyType
from typing import Any

from pydantic import ValidationError

from pados.capability.adapter import CapabilityAdapterError, StageContext, StageOutput, StageRequest
from pados.capability.model import AdapterDescriptor, Determinism, LicenseInfo, descriptor_digest
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
from pados.packs.ocr._pdf_worker import figure_name
from pados.packs.ocr._stage import (
    OcrStageAdapter,
    bounded_source,
    derive_document,
    emit_document,
    read_document,
    source_input,
)
from pados.packs.ocr.contracts import (
    JSON_MEDIA_TYPE,
    KIND_PREPROCESSED,
    KIND_SOURCE,
    MAX_FIGURES_PER_PAGE,
    MAX_IMAGE_PIXELS,
    MAX_IMAGE_SIDE,
    MAX_PAGES,
    MAX_TABLES_PER_PAGE,
    PDF_MEDIA_TYPE,
    Block,
    BlockKind,
    Figure,
    OcrDocument,
    OcrPage,
    PageGeometry,
    PreprocessManifest,
    Section,
    StageParameters,
    Table,
    minimum_confidence,
    union_bbox,
)
from pados.packs.ocr.descriptors import (
    DOCUMENT_LAYOUT_ANALYSIS,
    DOCUMENT_PREPROCESS,
    DOCUMENT_STRUCTURE_PARSE,
    FIGURE_EXTRACTION,
    TABLE_EXTRACTION,
)

PREPROCESS_ADAPTER_ID = "ocr.preprocess.reference"
LAYOUT_ADAPTER_ID = "ocr.layout.line-grouping"
TABLE_ADAPTER_ID = "ocr.table.whitespace-aligned"
FIGURE_ADAPTER_ID = "ocr.figure.pdf-xobject"
STRUCTURE_ADAPTER_ID = "ocr.structure.heuristic"

_MAGIC: dict[str, tuple[bytes, ...]] = {
    PDF_MEDIA_TYPE: (b"%PDF-",),
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
    "image/tiff": (b"II*\x00", b"MM\x00*"),
}
_CELL_SPLIT = re.compile(r"\s{2,}")
_NUMERIC = re.compile(r"^[-+]?[0-9][0-9.,]*%?$")
_NUMBERED_HEADING = re.compile(r"^([0-9]{1,3}(?:\.[0-9]{1,3}){0,5})[.)]?\s+\S")
_JPEG_SOF = frozenset({0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF})


def _reference_descriptor(adapter_id: str, capability_id: str, backend: str) -> AdapterDescriptor:
    return AdapterDescriptor(adapter_id=adapter_id, capability_id=capability_id, version="1", backend=backend,
                             determinism=Determinism.DETERMINISTIC, max_classification=DataClassification.RESTRICTED,
                             network_requirement=NetworkRequirement.LOCAL_ONLY, license=LicenseInfo())


# --- preprocess --------------------------------------------------------------------------------

def sniff_media_type(data: bytes) -> str | None:
    return next((media for media, magics in _MAGIC.items() if any(data.startswith(m) for m in magics)), None)


def png_size(data: bytes) -> tuple[int, int] | None:
    if len(data) < 24 or data[12:16] != b"IHDR":
        return None
    width, height = int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")
    return (width, height) if width > 0 and height > 0 else None


def jpeg_size(data: bytes) -> tuple[int, int] | None:
    index, steps = 2, 0
    while index + 9 < len(data) and steps < 4096:
        steps += 1
        if data[index] != 0xFF:
            return None
        marker = data[index + 1]
        if marker == 0xFF:
            index += 1
            continue
        if marker in _JPEG_SOF:
            height, width = int.from_bytes(data[index + 5:index + 7], "big"), int.from_bytes(data[index + 7:index + 9], "big")
            return (width, height) if width > 0 and height > 0 else None
        if marker == 0x01 or 0xD0 <= marker <= 0xD9:
            index += 2
            continue
        index += 2 + int.from_bytes(data[index + 2:index + 4], "big")
    return None


def image_geometry(data: bytes, media_type: str) -> PageGeometry:
    """Validated pixel geometry of a single-page image; anything unvalidated fails closed."""
    if media_type == "image/tiff":
        raise CapabilityAdapterError("IMAGE_TIFF_UNSUPPORTED")  # V1: dimensions/frames not validated
    size = png_size(data) if media_type == "image/png" else jpeg_size(data) if media_type == "image/jpeg" else None
    if size is None:
        raise CapabilityAdapterError("IMAGE_HEADER_INVALID")
    width, height = size
    if width > MAX_IMAGE_SIDE or height > MAX_IMAGE_SIDE:
        raise CapabilityAdapterError("IMAGE_DIMENSIONS_EXCEEDED")
    if width * height > MAX_IMAGE_PIXELS:
        raise CapabilityAdapterError("IMAGE_PIXELS_EXCEEDED")
    return PageGeometry(page_number=1, width=width, height=height, unit="px")


def _pdf_geometry(rows: list[tuple[int, list[Any]]]) -> list[PageGeometry] | None:
    """MediaBox geometry in points (unmeasured when not positive); ``None`` if a box is out of range."""
    geometry: list[PageGeometry] = []
    for number, (width, height) in rows:
        w, h = as_dimension(width), as_dimension(height)
        try:
            geometry.append(PageGeometry(page_number=number, width=w, height=h, unit="pt")
                            if w is not None and h is not None and w > 0 and h > 0 else PageGeometry(page_number=number))
        except ValidationError:
            return None
    return geometry


class DocumentPreprocessAdapter(OcrStageAdapter):
    """The pack's own preprocess stage: PDFs are inspected only in the isolated worker.

    The pipeline trusts page coverage only from this adapter (``REGISTERED_PREPROCESS_DIGESTS``). It needs
    the bounded runner for PDFs, so where that runner cannot contain a child (Windows) the whole adapter
    is unavailable (``OCR_ISOLATION_UNAVAILABLE``), images included.
    """

    requires_isolation = True

    def __init__(self, *, budget: ProcessBudget = PDF_PARSE_BUDGET) -> None:
        super().__init__(preprocess_descriptor())
        self._budget = budget

    def _execute(self, request: StageRequest, params: StageParameters, context: StageContext) -> StageOutput:
        source = source_input(request, params)
        data = bounded_source(source, context, self._budget)
        detected = sniff_media_type(data)
        if detected != source.media_type:
            raise CapabilityAdapterError("MEDIA_TYPE_MISMATCH")
        if detected == PDF_MEDIA_TYPE:
            page_count, selected, geometry = self._pdf(data, params)
        else:
            image = image_geometry(data, detected)
            page_count, selected, geometry = 1, params.select_pages(1), (image,)
        manifest = PreprocessManifest(source=source, detected_media_type=detected, page_count=page_count,
                                      selected_pages=selected, geometry=geometry)
        ref = context.artifacts.put(manifest.to_bytes(), media_type=JSON_MEDIA_TYPE, kind=KIND_PREPROCESSED)
        evidence = {"output_contract": "ocr.preprocessed/v1", "input_sha256": source.sha256,
                    "manifest_sha256": ref.sha256, "detected_media_type": detected, "page_count": page_count,
                    "selected_pages": len(selected), "adapter_id": self.descriptor.adapter_id}
        return StageOutput(outputs=(ref,), metrics={"pages": float(len(selected))}, evidence=evidence)

    def _pdf(self, data: bytes, params: StageParameters) -> tuple[int, tuple[int, ...], tuple[PageGeometry, ...]]:
        result = isolated_pdf("inspect", data, {"max_pages": MAX_PAGES, "page_first": params.page_first,
                                                "page_last": params.page_last}, budget=self._budget)
        page_count = as_int(result.get("page_count"), high=10_000_000)
        selected = params.select_pages(page_count)
        geometry = _pdf_geometry(page_rows(result.get("geometry"), 3, selected))
        if geometry is None:  # raised outside the handler: no chained ValidationError
            raise CapabilityAdapterError("PDF_UNREADABLE")
        return page_count, selected, tuple(geometry)


# --- layout ------------------------------------------------------------------------------------

def _lines(page: OcrPage) -> list[Block]:
    return [block for block in page.blocks if block.kind is BlockKind.LINE]


def _ordered_lines(lines: Sequence[Block]) -> list[Block]:
    if lines and all(line.bbox is not None for line in lines):
        return sorted(lines, key=lambda b: (b.bbox.y0, b.bbox.x0) if b.bbox else (0.0, 0.0))
    return list(lines)


def _same_paragraph(previous: Block, current: Block) -> bool:
    if previous.line_index is not None and current.line_index is not None and (
            previous.bbox is None or current.bbox is None):
        return current.line_index == previous.line_index + 1
    if previous.bbox is not None and current.bbox is not None:
        gap = current.bbox.y0 - previous.bbox.y1
        return gap <= 0.75 * max(previous.bbox.height, 0.001)
    return False


def group_paragraphs(lines: Sequence[Block]) -> list[list[Block]]:
    groups: list[list[Block]] = []
    for line in _ordered_lines(lines):
        if groups and _same_paragraph(groups[-1][-1], line):
            groups[-1].append(line)
        else:
            groups.append([line])
    return groups


class LineLayoutAdapter(OcrStageAdapter):
    def __init__(self) -> None:
        super().__init__(_reference_descriptor(LAYOUT_ADAPTER_ID, DOCUMENT_LAYOUT_ANALYSIS, "pados-reference"))

    def _execute(self, request: StageRequest, params: StageParameters, context: StageContext) -> StageOutput:
        document = _document(request, context)
        pages = []
        for page in document.pages:
            lines = _lines(page)
            paragraphs = [Block(block_id=f"p{page.page_number}.b{index}", kind=BlockKind.PARAGRAPH,
                                text="\n".join(line.text for line in group),
                                bbox=union_bbox(line.bbox for line in group),
                                confidence=minimum_confidence(line.confidence for line in group),
                                children=tuple(line.block_id for line in group))
                          for index, group in enumerate(group_paragraphs(lines), start=1)]
            pages.append(_replace(page, blocks=(*lines, *paragraphs),
                                  reading_order=tuple(p.block_id for p in paragraphs)))
        return emit_document(context, derive_document(document, self.descriptor, request.stage_id, pages=pages,
                                                      sections=()), self.descriptor)


# --- tables ------------------------------------------------------------------------------------

def _line_runs(page: OcrPage) -> Iterator[list[Block]]:
    """Lines of each paragraph in reading order (or all lines, if layout did not run)."""
    by_id = {block.block_id: block for block in page.blocks}
    paragraphs = [by_id[i] for i in page.reading_order if by_id[i].kind is not BlockKind.LINE]
    if not paragraphs:
        yield _lines(page)
        return
    for paragraph in paragraphs:
        yield [by_id[child] for child in paragraph.children if by_id[child].kind is BlockKind.LINE]


def detect_tables(page: OcrPage) -> list[Table]:
    tables: list[Table] = []

    def flush(run: list[tuple[Block, tuple[str, ...]]]) -> None:
        if len(run) < 2:
            return
        rows = tuple(cells for _, cells in run)
        header = (not any(_NUMERIC.fullmatch(c) for c in rows[0])
                  and any(_NUMERIC.fullmatch(c) for row in rows[1:] for c in row))
        if len(tables) >= MAX_TABLES_PER_PAGE:
            raise CapabilityAdapterError("TABLES_TOO_MANY")
        tables.append(Table(table_id=f"p{page.page_number}.t{len(tables) + 1}", rows=len(rows),
                            columns=len(rows[0]), cells=rows, header_row=header,
                            block_ids=tuple(line.block_id for line, _ in run),
                            bbox=union_bbox(line.bbox for line, _ in run),
                            confidence=minimum_confidence(line.confidence for line, _ in run),
                            method="whitespace-aligned"))

    for lines in _line_runs(page):
        run: list[tuple[Block, tuple[str, ...]]] = []
        for line in lines:
            cells = tuple(_CELL_SPLIT.split(line.text.strip()))
            if len(cells) >= 2 and (not run or len(run[0][1]) == len(cells)):
                run.append((line, cells))
                continue
            flush(run)
            run = [(line, cells)] if len(cells) >= 2 else []
        flush(run)
    return tables


class WhitespaceTableAdapter(OcrStageAdapter):
    def __init__(self) -> None:
        super().__init__(_reference_descriptor(TABLE_ADAPTER_ID, TABLE_EXTRACTION, "pados-reference"))

    def _execute(self, request: StageRequest, params: StageParameters, context: StageContext) -> StageOutput:
        document = _document(request, context)
        pages = [_replace(page, tables=tuple(detect_tables(page))) for page in document.pages]
        return emit_document(context, derive_document(document, self.descriptor, request.stage_id, pages=pages),
                             self.descriptor)


# --- figures -----------------------------------------------------------------------------------

def _figure(number: int, index: int, row: Any) -> Figure | None:
    name, width, height = as_row(row, 3)
    if not isinstance(name, str) or figure_name(name) != name:
        raise CapabilityAdapterError(OCR_PARSE_FAILED)
    try:
        return Figure(figure_id=f"p{number}.f{index}", name=name,
                      width_px=None if width is None else as_int(width, low=1, high=10_000_000),
                      height_px=None if height is None else as_int(height, low=1, high=10_000_000))
    except ValidationError:
        return None


def pdf_figures(data: bytes, page_numbers: Sequence[int], *,
                budget: ProcessBudget = PDF_PARSE_BUDGET) -> dict[int, tuple[Figure, ...]]:
    """Image XObjects per page, listed by the isolated worker and re-validated here (the number of
    XObjects the worker examines is bounded by ``budget.max_xobjects_per_page`` / ``max_xobjects``)."""
    result = isolated_pdf("figures", data, {"pages": list(page_numbers), "max_figures": MAX_FIGURES_PER_PAGE},
                          budget=budget)
    found: dict[int, tuple[Figure, ...]] = {}
    for number, (listed,) in page_rows(result.get("figures"), 2, page_numbers):
        figures = [_figure(number, index, row)
                   for index, row in enumerate(as_list(listed, max_items=MAX_FIGURES_PER_PAGE), start=1)]
        if any(figure is None for figure in figures):
            raise CapabilityAdapterError("PDF_UNREADABLE")
        found[number] = tuple(figure for figure in figures if figure is not None)
    return found


class XObjectFigureAdapter(OcrStageAdapter):
    requires_isolation = True

    def __init__(self, *, budget: ProcessBudget = PDF_PARSE_BUDGET) -> None:
        super().__init__(_reference_descriptor(FIGURE_ADAPTER_ID, FIGURE_EXTRACTION, "pypdf"))
        self._budget = budget

    def _execute(self, request: StageRequest, params: StageParameters, context: StageContext) -> StageOutput:
        document = _document(request, context)
        source = source_input(request, params)
        if document.source != source:
            raise CapabilityAdapterError("PRIOR_DOCUMENT_SOURCE_MISMATCH")
        pages = list(document.pages)
        if source.media_type == PDF_MEDIA_TYPE:  # an image input is itself the page; no embedded figures
            figures = pdf_figures(bounded_source(source, context, self._budget),
                                  [page.page_number for page in pages], budget=self._budget)
            pages = [_replace(page, figures=figures[page.page_number]) for page in pages]
        return emit_document(context, derive_document(document, self.descriptor, request.stage_id, pages=pages),
                             self.descriptor)


# --- structure ---------------------------------------------------------------------------------

def heading_level(block: Block, table_lines: frozenset[str]) -> int | None:
    text = block.text.strip()
    if block.kind is BlockKind.HEADING or "\n" in text or not text or len(text) > 100:
        return None
    if (set(block.children) | {block.block_id}) & table_lines or text[-1] in ".,;:":
        return None
    numbered = _NUMBERED_HEADING.match(text)
    if numbered:
        return min(6, numbered.group(1).count(".") + 1)
    letters = [c for c in text if c.isalpha()]
    if letters and all(c.isupper() for c in letters) and len(text.split()) <= 12:
        return 1
    return None


class HeuristicStructureAdapter(OcrStageAdapter):
    def __init__(self) -> None:
        super().__init__(_reference_descriptor(STRUCTURE_ADAPTER_ID, DOCUMENT_STRUCTURE_PARSE, "pados-reference"))

    def _execute(self, request: StageRequest, params: StageParameters, context: StageContext) -> StageOutput:
        document = _document(request, context)
        pages: list[OcrPage] = []
        sections: list[dict[str, Any]] = []
        for page in document.pages:
            table_lines = frozenset(i for table in page.tables for i in table.block_ids)
            blocks = {block.block_id: block for block in page.blocks}
            for block_id in page.reading_order:
                block = blocks[block_id]
                level = heading_level(block, table_lines)
                if level is not None:
                    blocks[block_id] = block.model_copy(update={"kind": BlockKind.HEADING, "level": level})
                    sections.append({"heading_block_id": block_id, "level": level, "block_ids": [block_id],
                                     "pages": [page.page_number]})
                    continue
                if not sections:
                    sections.append({"heading_block_id": None, "level": 0, "block_ids": [], "pages": []})
                sections[-1]["block_ids"].append(block_id)
                if page.page_number not in sections[-1]["pages"]:
                    sections[-1]["pages"].append(page.page_number)
            top_level = set(page.reading_order)
            kept = [b for b in blocks.values()
                    if params.preserve_layout or b.kind is not BlockKind.LINE or b.block_id in top_level]
            kept_ids = {b.block_id for b in kept}
            kept = [b.model_copy(update={"children": tuple(c for c in b.children if c in kept_ids)}) for b in kept]
            tables = tuple(t.model_copy(update={"block_ids": tuple(i for i in t.block_ids if i in kept_ids)})
                           for t in page.tables)
            pages.append(_replace(page, blocks=tuple(kept), tables=tables))
        built = tuple(Section(section_id=f"s{index}", **section) for index, section in enumerate(sections, start=1))
        return emit_document(context, derive_document(document, self.descriptor, request.stage_id, pages=pages,
                                                      sections=built), self.descriptor)


# --- shared ------------------------------------------------------------------------------------

def preprocess_descriptor() -> AdapterDescriptor:
    return _reference_descriptor(PREPROCESS_ADAPTER_ID, DOCUMENT_PREPROCESS, "pados-reference")


# The preprocess adapters whose manifests the pipeline trusts for page coverage: adapter id -> descriptor digest.
REGISTERED_PREPROCESS_DIGESTS: Mapping[str, str] = MappingProxyType(
    {PREPROCESS_ADAPTER_ID: descriptor_digest(preprocess_descriptor())})

def _document(request: StageRequest, context: StageContext) -> OcrDocument:
    document = read_document(request, context)
    if document is None:  # pragma: no cover - required=True never returns None
        raise CapabilityAdapterError("STAGE_INPUT_MISSING")
    source = [ref for ref in request.inputs if ref.kind == KIND_SOURCE]
    if source and source[0] != document.source:
        raise CapabilityAdapterError("PRIOR_DOCUMENT_SOURCE_MISMATCH")
    return document


def _replace(page: OcrPage, **changes: Any) -> OcrPage:
    """Rebuild a page with changes and re-validate every reference (``model_copy`` would not)."""
    return OcrPage.model_validate({**{name: getattr(page, name) for name in OcrPage.model_fields}, **changes})


__all__ = ["REGISTERED_PREPROCESS_DIGESTS", "DocumentPreprocessAdapter", "HeuristicStructureAdapter",
           "LineLayoutAdapter", "WhitespaceTableAdapter",
           "XObjectFigureAdapter", "bounded_source", "detect_tables", "group_paragraphs", "heading_level",
           "image_geometry", "jpeg_size", "pdf_figures", "png_size", "preprocess_descriptor", "sniff_media_type"]

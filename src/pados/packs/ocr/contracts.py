# SPDX-License-Identifier: AGPL-3.0-only
"""OCR task contract and the normalized ``ocr.document/v1`` output contract (data only).

A task names one content-addressed input artifact and the constraints the plan must honour
(pages, language hints, layout/table requirements, classification, network policy, quality). Stage
outputs are documents that REFERENCE the input by hash; they are candidates (``semantic_authority =
NONE``) and never mutate semantic state. Every model is frozen, closed (``extra="forbid"``) and
bounded, so an engine result cannot smuggle unbounded or unexpected data into the plan.

Coordinates are normalized to the page (0..1, origin top-left). ``bbox`` and ``confidence`` are
``None`` when the producer did not measure them: an embedded PDF text layer has no recognition
confidence and pypdf's public API exposes no glyph geometry, so neither is invented.
"""
from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping
from enum import StrEnum
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    ValidationError,
    field_validator,
    model_serializer,
    model_validator,
)

from pados.capability.artifacts import ArtifactRef
from pados.connectivity.requirements import NetworkRequirement
from pados.enterprise.classification import DataClassification
from pados.execution.context import canonical_hash
from pados.lifecycle.minimization import assert_minimized

OCR_REQUEST_CONTRACT = "ocr.request/v1"
OCR_PREPROCESSED_CONTRACT = "ocr.preprocessed/v1"
OCR_DOCUMENT_CONTRACT = "ocr.document/v1"
OCR_ENGINE_RESULT_CONTRACT = "ocr.engine-result/v1"

KIND_SOURCE = "ocr.source"
KIND_PREPROCESSED = "ocr.preprocessed"
KIND_DOCUMENT = "ocr.document"
JSON_MEDIA_TYPE = "application/json"

PDF_MEDIA_TYPE = "application/pdf"
IMAGE_MEDIA_TYPES = frozenset({"image/png", "image/jpeg", "image/tiff"})
SUPPORTED_MEDIA_TYPES = frozenset({PDF_MEDIA_TYPE, *IMAGE_MEDIA_TYPES})

MAX_PAGES = 2000
MAX_LANGUAGE_HINTS = 8
MAX_BLOCKS_PER_PAGE = 4000
MAX_BLOCK_TEXT = 8000
MAX_TABLES_PER_PAGE = 64
MAX_TABLE_CELLS = 4096
MAX_CELL_TEXT = 1000
MAX_FIGURES_PER_PAGE = 128
MAX_SECTIONS = 4000
MAX_STAGE_MARKS = 32
MAX_DOCUMENT_BYTES = 48 * 1024 * 1024
MAX_IMAGE_SIDE = 20_000  # pixels per side of a single-page image input
MAX_IMAGE_PIXELS = 100_000_000  # total pixels (100 MP) of a single-page image input

# BCP-47 subset: language, optional script, optional region (e.g. en, zh-Hant, pt-BR, es-419).
_BCP47 = re.compile(r"^[a-z]{2,3}(-[A-Z][a-z]{3})?(-(?:[A-Z]{2}|[0-9]{3}))?$")
_NODE_ID = re.compile(r"^[a-z][a-z0-9.-]{0,63}$")
_STAGE_ID = re.compile(r"^[a-z][a-z0-9_-]{0,47}$")
_FIGURE_NAME = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class OcrContractError(ValueError):
    """A contract violation with a closed code (never echoes document content)."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def clean_text(value: str) -> str:
    """Remove control characters (tabs and newlines are kept); text is otherwise untouched."""
    return _CONTROL.sub("", value)


class PageRange(BaseModel):
    model_config = _FROZEN

    first: int = Field(ge=1, le=100_000)
    last: int = Field(ge=1, le=100_000)

    @model_validator(mode="after")
    def ordered_and_bounded(self) -> PageRange:
        if self.last < self.first:
            raise ValueError("PAGE_RANGE_INVERTED")
        if self.last - self.first + 1 > MAX_PAGES:
            raise ValueError("PAGE_RANGE_TOO_LARGE")
        return self


class QualityRequirement(BaseModel):
    """What the task requires of the final candidate; evaluated after the last stage."""

    model_config = _FROZEN

    min_mean_confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    allow_needs_ocr_pages: bool = False


class OcrTaskContract(BaseModel):
    """``ocr.request/v1``: one governed perception task over one content-addressed artifact."""

    model_config = _FROZEN

    schema_version: Literal["ocr.request/v1"] = "ocr.request/v1"
    artifact_ref: ArtifactRef
    media_type: str
    page_range: PageRange | None = None
    language_hints: tuple[str, ...] = Field(default=(), max_length=MAX_LANGUAGE_HINTS)
    preserve_layout: bool = False
    require_tables: bool = False
    classification: DataClassification
    network_policy: NetworkRequirement = NetworkRequirement.LOCAL_ONLY
    quality: QualityRequirement = QualityRequirement()

    @field_validator("language_hints")
    @classmethod
    def bcp47_subset(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if any(not _BCP47.fullmatch(item) for item in value):
            raise ValueError("LANGUAGE_HINT_INVALID")
        if len(set(value)) != len(value):
            raise ValueError("LANGUAGE_HINT_DUPLICATE")
        return value

    @model_validator(mode="after")
    def consistent(self) -> OcrTaskContract:
        if self.media_type not in SUPPORTED_MEDIA_TYPES:
            raise ValueError("MEDIA_TYPE_UNSUPPORTED")
        if self.artifact_ref.media_type != self.media_type:
            raise ValueError("MEDIA_TYPE_MISMATCH")
        if self.artifact_ref.kind != KIND_SOURCE:
            raise ValueError("SOURCE_KIND_INVALID")
        if self.media_type in IMAGE_MEDIA_TYPES and self.page_range not in (None, PageRange(first=1, last=1)):
            raise ValueError("IMAGE_HAS_ONE_PAGE")
        return self

    def stage_parameters(self) -> dict[str, Any]:
        """The JSON parameters every stage receives (hashed into each stage receipt)."""
        return {
            "contract": OCR_REQUEST_CONTRACT,
            "media_type": self.media_type,
            "page_first": None if self.page_range is None else self.page_range.first,
            "page_last": None if self.page_range is None else self.page_range.last,
            "language_hints": list(self.language_hints),
            "preserve_layout": self.preserve_layout,
            "require_tables": self.require_tables,
        }

    @property
    def task_hash(self) -> str:
        return canonical_hash(self.model_dump(mode="json"))


class StageParameters(BaseModel):
    """Adapter-side parse of ``StageRequest.parameters`` (closed; anything else fails closed)."""

    model_config = _FROZEN

    contract: Literal["ocr.request/v1"]
    media_type: str
    page_first: int | None = Field(default=None, ge=1, le=100_000)
    page_last: int | None = Field(default=None, ge=1, le=100_000)
    language_hints: tuple[str, ...] = Field(default=(), max_length=MAX_LANGUAGE_HINTS)
    preserve_layout: bool = False
    require_tables: bool = False

    @model_validator(mode="after")
    def consistent(self) -> StageParameters:
        if self.media_type not in SUPPORTED_MEDIA_TYPES:
            raise ValueError("MEDIA_TYPE_UNSUPPORTED")
        if (self.page_first is None) != (self.page_last is None):
            raise ValueError("PAGE_RANGE_INCOMPLETE")
        if self.page_first is not None:
            PageRange(first=self.page_first, last=self.page_last or self.page_first)
        if any(not _BCP47.fullmatch(item) for item in self.language_hints):
            raise ValueError("LANGUAGE_HINT_INVALID")
        return self

    @classmethod
    def parse(cls, parameters: Mapping[str, Any]) -> StageParameters:
        try:
            return cls.model_validate(dict(parameters))
        except ValidationError:
            pass
        raise OcrContractError("STAGE_PARAMETERS_INVALID") from None  # no chained input values

    def select_pages(self, page_count: int) -> tuple[int, ...]:
        """Pages to process; a range outside the document fails closed (never silently clipped)."""
        if page_count < 1 or page_count > MAX_PAGES:
            raise OcrContractError("PAGE_COUNT_UNSUPPORTED")
        if self.page_first is None or self.page_last is None:
            return tuple(range(1, page_count + 1))
        if self.page_last > page_count:
            raise OcrContractError("PAGE_RANGE_OUT_OF_BOUNDS")
        return tuple(range(self.page_first, self.page_last + 1))


# --- output contract ------------------------------------------------------------------------

class PageStatus(StrEnum):
    TEXT_LAYER = "TEXT_LAYER"  # embedded text extracted verbatim (no recognition)
    RECOGNIZED = "RECOGNIZED"  # text produced by a recognition engine (candidate, confidence-scored)
    NEEDS_OCR = "NEEDS_OCR"  # no text available yet; nothing was invented
    FAILED = "FAILED"  # the producer failed on this page (explicitly partial)


class BlockKind(StrEnum):
    LINE = "LINE"
    PARAGRAPH = "PARAGRAPH"
    HEADING = "HEADING"


def _rounded(value: float | None) -> float | None:
    return None if value is None else round(float(value), 4)


class BBox(BaseModel):
    """Normalized page coordinates (0..1, origin top-left)."""

    model_config = _FROZEN

    x0: float = Field(ge=0.0, le=1.0)
    y0: float = Field(ge=0.0, le=1.0)
    x1: float = Field(ge=0.0, le=1.0)
    y1: float = Field(ge=0.0, le=1.0)

    @field_validator("x0", "y0", "x1", "y1")
    @classmethod
    def four_places(cls, value: float) -> float:
        return round(value, 4)

    @model_validator(mode="after")
    def ordered(self) -> BBox:
        if self.x1 < self.x0 or self.y1 < self.y0:
            raise ValueError("BBOX_INVERTED")
        return self

    @property
    def height(self) -> float:
        return self.y1 - self.y0


def union_bbox(boxes: Iterable[BBox | None]) -> BBox | None:
    """Union of boxes, or ``None`` if any is unmeasured (no partial geometry is invented)."""
    items = list(boxes)
    if not items or any(box is None for box in items):
        return None
    known = [box for box in items if box is not None]
    return BBox(x0=min(b.x0 for b in known), y0=min(b.y0 for b in known),
                x1=max(b.x1 for b in known), y1=max(b.y1 for b in known))


def minimum_confidence(values: Iterable[float | None]) -> float | None:
    """Conservative aggregate: the minimum, or ``None`` if any member is unscored."""
    items = list(values)
    if not items or any(value is None for value in items):
        return None
    return min(value for value in items if value is not None)


class Block(BaseModel):
    model_config = _FROZEN

    block_id: str
    kind: BlockKind
    text: str = Field(max_length=MAX_BLOCK_TEXT)
    bbox: BBox | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    line_index: int | None = Field(default=None, ge=0, le=100_000)  # vertical ordinal in the source grid
    children: tuple[str, ...] = Field(default=(), max_length=MAX_BLOCKS_PER_PAGE)
    level: int | None = Field(default=None, ge=1, le=6)  # heading level

    @field_validator("block_id")
    @classmethod
    def node_id(cls, value: str) -> str:
        if not _NODE_ID.fullmatch(value):
            raise ValueError("BLOCK_ID_INVALID")
        return value

    @field_validator("text")
    @classmethod
    def no_control_characters(cls, value: str) -> str:
        if _CONTROL.search(value):
            raise ValueError("BLOCK_TEXT_CONTROL_CHARACTER")
        return value

    @field_validator("confidence")
    @classmethod
    def four_places(cls, value: float | None) -> float | None:
        return _rounded(value)

    @model_validator(mode="after")
    def heading_level(self) -> Block:
        if (self.kind is BlockKind.HEADING) != (self.level is not None):
            raise ValueError("HEADING_LEVEL_INCONSISTENT")
        return self


class Table(BaseModel):
    model_config = _FROZEN

    table_id: str
    rows: int = Field(ge=1, le=MAX_TABLE_CELLS)
    columns: int = Field(ge=1, le=256)
    cells: tuple[tuple[str, ...], ...]
    header_row: bool = False
    block_ids: tuple[str, ...] = Field(default=(), max_length=MAX_BLOCKS_PER_PAGE)
    bbox: BBox | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    method: str = Field(min_length=1, max_length=40)

    @field_validator("table_id")
    @classmethod
    def node_id(cls, value: str) -> str:
        if not _NODE_ID.fullmatch(value):
            raise ValueError("TABLE_ID_INVALID")
        return value

    @model_validator(mode="after")
    def rectangular(self) -> Table:
        if self.rows * self.columns > MAX_TABLE_CELLS:
            raise ValueError("TABLE_TOO_LARGE")
        if len(self.cells) != self.rows or any(len(row) != self.columns for row in self.cells):
            raise ValueError("TABLE_NOT_RECTANGULAR")
        if any(len(cell) > MAX_CELL_TEXT or _CONTROL.search(cell) for row in self.cells for cell in row):
            raise ValueError("TABLE_CELL_INVALID")
        return self


class Figure(BaseModel):
    model_config = _FROZEN

    figure_id: str
    name: str
    width_px: int | None = Field(default=None, ge=1, le=1_000_000)
    height_px: int | None = Field(default=None, ge=1, le=1_000_000)
    bbox: BBox | None = None
    artifact: ArtifactRef | None = None  # V1 records figures by reference only; bytes are not extracted

    @field_validator("figure_id")
    @classmethod
    def node_id(cls, value: str) -> str:
        if not _NODE_ID.fullmatch(value):
            raise ValueError("FIGURE_ID_INVALID")
        return value

    @field_validator("name")
    @classmethod
    def figure_name(cls, value: str) -> str:
        if not _FIGURE_NAME.fullmatch(value):
            raise ValueError("FIGURE_NAME_INVALID")
        return value


class OcrPage(BaseModel):
    model_config = _FROZEN

    page_number: int = Field(ge=1, le=100_000)
    status: PageStatus
    producer: str | None = None  # adapter id that produced this page's text, if any
    width: float | None = Field(default=None, gt=0, le=1_000_000)
    height: float | None = Field(default=None, gt=0, le=1_000_000)
    unit: Literal["pt", "px"] | None = None
    blocks: tuple[Block, ...] = Field(default=(), max_length=MAX_BLOCKS_PER_PAGE)
    tables: tuple[Table, ...] = Field(default=(), max_length=MAX_TABLES_PER_PAGE)
    figures: tuple[Figure, ...] = Field(default=(), max_length=MAX_FIGURES_PER_PAGE)
    reading_order: tuple[str, ...] = Field(default=(), max_length=MAX_BLOCKS_PER_PAGE)

    @model_validator(mode="after")
    def references_resolve(self) -> OcrPage:
        ids = [block.block_id for block in self.blocks]
        known = set(ids)
        if len(known) != len(ids):
            raise ValueError("BLOCK_ID_DUPLICATE")
        if self.status in {PageStatus.NEEDS_OCR, PageStatus.FAILED} and self.blocks:
            raise ValueError("PAGE_WITHOUT_TEXT_HAS_BLOCKS")
        if self.status is PageStatus.TEXT_LAYER and not self.blocks:  # empty text layer is NEEDS_OCR
            raise ValueError("TEXT_LAYER_PAGE_HAS_NO_BLOCKS")
        if self.status is PageStatus.RECOGNIZED and not self.blocks:  # empty recognition is NEEDS_OCR
            raise ValueError("RECOGNIZED_PAGE_HAS_NO_BLOCKS")
        if self.status is PageStatus.TEXT_LAYER and any(block.confidence is not None for block in self.blocks):
            raise ValueError("TEXT_LAYER_HAS_NO_RECOGNITION_CONFIDENCE")
        if len(set(self.reading_order)) != len(self.reading_order) or not set(self.reading_order) <= known:
            raise ValueError("READING_ORDER_INVALID")
        for block in self.blocks:
            if not set(block.children) <= known:
                raise ValueError("BLOCK_CHILD_UNKNOWN")
        for table in self.tables:
            if not set(table.block_ids) <= known:
                raise ValueError("TABLE_BLOCK_UNKNOWN")
        if (self.width is None) != (self.height is None) or ((self.width is None) != (self.unit is None)):
            raise ValueError("PAGE_GEOMETRY_INCOMPLETE")
        return self


class Section(BaseModel):
    model_config = _FROZEN

    section_id: str
    heading_block_id: str | None = None
    level: int = Field(ge=0, le=6)
    block_ids: tuple[str, ...] = Field(default=(), max_length=MAX_BLOCKS_PER_PAGE * 4)
    pages: tuple[int, ...] = Field(default=(), max_length=MAX_PAGES)

    @field_validator("section_id")
    @classmethod
    def node_id(cls, value: str) -> str:
        if not _NODE_ID.fullmatch(value):
            raise ValueError("SECTION_ID_INVALID")
        return value


class StageMark(BaseModel):
    model_config = _FROZEN

    stage_id: str
    capability_id: str
    adapter_id: str
    adapter_version: str

    @field_validator("stage_id")
    @classmethod
    def stage_identifier(cls, value: str) -> str:
        if not _STAGE_ID.fullmatch(value):
            raise ValueError("STAGE_ID_INVALID")
        return value


class EngineRecord(BaseModel):
    """Which recognition stage perceived which pages: its descriptor facts and its page outcome.

    ``attempted_pages`` are the pages the stage was asked to perceive and ``pages`` the subset it
    perceived; the pipeline rebuilds the whole record from the assigned descriptor and the pages.
    """

    model_config = _FROZEN

    adapter_id: str
    adapter_version: str
    backend: str
    engine_version: str
    license_spdx: str
    distribution: str
    operation: str = Field(min_length=1, max_length=40)
    attempted_pages: tuple[int, ...] = Field(max_length=MAX_PAGES)
    pages: tuple[int, ...] = Field(default=(), max_length=MAX_PAGES)

    @model_validator(mode="after")
    def perceived_were_attempted(self) -> EngineRecord:
        for numbers in (self.attempted_pages, self.pages):
            if list(numbers) != sorted(set(numbers)):
                raise ValueError("ENGINE_RECORD_PAGES_INVALID")
        if not set(self.pages) <= set(self.attempted_pages):
            raise ValueError("ENGINE_RECORD_PAGES_INVALID")
        return self


class PageConfidence(BaseModel):
    model_config = _FROZEN

    page_number: int = Field(ge=1, le=100_000)
    status: PageStatus
    mean: float | None = Field(default=None, ge=0.0, le=1.0)
    minimum: float | None = Field(default=None, ge=0.0, le=1.0)
    scored_blocks: int = Field(default=0, ge=0)

    @field_validator("mean", "minimum")
    @classmethod
    def four_places(cls, value: float | None) -> float | None:
        return _rounded(value)


EVIDENCE_KEYS = frozenset({"input_sha256", "input_media_type", "engines", "stages", "page_confidence",
                           "pages_total", "needs_ocr_pages", "failed_pages", "test_provenance"})


class OcrEvidence(BaseModel):
    """Content-free evidence metadata (hashes, versions, counts, confidences)."""

    model_config = _FROZEN

    input_sha256: str
    input_media_type: str
    engines: tuple[EngineRecord, ...] = Field(default=(), max_length=MAX_STAGE_MARKS)
    stages: tuple[StageMark, ...] = Field(default=(), max_length=MAX_STAGE_MARKS)
    page_confidence: tuple[PageConfidence, ...] = Field(default=(), max_length=MAX_PAGES)
    pages_total: int = Field(ge=0, le=MAX_PAGES)
    needs_ocr_pages: tuple[int, ...] = Field(default=(), max_length=MAX_PAGES)
    failed_pages: tuple[int, ...] = Field(default=(), max_length=MAX_PAGES)
    # "FIXTURE" when a test/contract fixture adapter produced any of this document (never cleared later).
    # Omitted from the serialized form when unset, so production documents keep their exact bytes.
    test_provenance: Literal["FIXTURE"] | None = None

    @model_serializer(mode="wrap")
    def omit_unset_provenance(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        data: dict[str, Any] = handler(self)
        if data.get("test_provenance") is None:
            data.pop("test_provenance", None)
        return data

    @model_validator(mode="after")
    def minimized(self) -> OcrEvidence:
        record = self.model_dump(mode="json")
        assert_minimized(record, allowed_keys=EVIDENCE_KEYS, max_items=MAX_PAGES,
                         max_total_bytes=2 * 1024 * 1024, max_string=256)
        return self


class OcrDocument(BaseModel):
    """``ocr.document/v1``: the normalized document candidate (references its input by hash)."""

    model_config = _FROZEN

    schema_version: Literal["ocr.document/v1"] = "ocr.document/v1"
    source: ArtifactRef
    pages: tuple[OcrPage, ...] = Field(min_length=1, max_length=MAX_PAGES)
    sections: tuple[Section, ...] = Field(default=(), max_length=MAX_SECTIONS)
    evidence: OcrEvidence
    semantic_authority: Literal["NONE"] = "NONE"

    @model_validator(mode="after")
    def consistent(self) -> OcrDocument:
        numbers = [page.page_number for page in self.pages]
        if numbers != sorted(set(numbers)):
            raise ValueError("PAGE_ORDER_INVALID")
        if self.evidence.input_sha256 != self.source.sha256:
            raise ValueError("EVIDENCE_INPUT_MISMATCH")
        evidence_matches_pages(self.pages, self.evidence)
        all_ids: set[str] = set()
        for page in self.pages:
            ids = {block.block_id for block in page.blocks}
            ids |= {table.table_id for table in page.tables} | {figure.figure_id for figure in page.figures}
            if all_ids & ids:
                raise ValueError("NODE_ID_DUPLICATE")
            all_ids |= ids
        for section in self.sections:
            if section.heading_block_id is not None and section.heading_block_id not in all_ids:
                raise ValueError("SECTION_HEADING_UNKNOWN")
            if not set(section.block_ids) <= all_ids:
                raise ValueError("SECTION_BLOCK_UNKNOWN")
        return self

    def to_bytes(self) -> bytes:
        """Canonical bytes (sorted keys, compact) so identical documents hash identically."""
        data = json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":"),
                          ensure_ascii=False).encode("utf-8")
        if len(data) > MAX_DOCUMENT_BYTES:
            raise OcrContractError("OCR_DOCUMENT_TOO_LARGE")
        return data

    @classmethod
    def from_bytes(cls, data: bytes) -> OcrDocument:
        if len(data) > MAX_DOCUMENT_BYTES:
            raise OcrContractError("OCR_DOCUMENT_TOO_LARGE")
        try:
            return cls.model_validate_json(data)
        except ValidationError:
            pass
        raise OcrContractError("OCR_DOCUMENT_INVALID") from None  # no chained input values


def page_summary(page: OcrPage) -> PageConfidence:
    """Confidence summary over the page's LINE blocks (recomputed, never taken from a producer)."""
    scores = [b.confidence for b in page.blocks if b.kind is BlockKind.LINE and b.confidence is not None]
    return PageConfidence(page_number=page.page_number, status=page.status,
                          mean=(sum(scores) / len(scores)) if scores else None,
                          minimum=min(scores) if scores else None, scored_blocks=len(scores))


# The recognition-confidence measurement policy (what a quality floor is compared with).
MEASUREMENT_POLICY = "LINE_UNWEIGHTED_MEAN_MIN/v1"


def page_measurement(page: OcrPage) -> PageConfidence | None:
    """A recognition page's confidence measurement (``MEASUREMENT_POLICY``), or ``None`` if unmeasured.

    Computed ONLY from the page's validated LINE-block confidences, independently of any other
    summary: every LINE block must carry a confidence (else ``None``), there must be at least one,
    ``mean`` is their unweighted arithmetic mean (each line counts once, whatever its length or box)
    and ``minimum`` their minimum. PARAGRAPH and HEADING blocks, tables and figures never contribute,
    so a producer cannot lift the measurement with extra high-confidence non-LINE blocks.
    """
    if page.status is not PageStatus.RECOGNIZED:
        return None
    scores: list[float] = []
    for block in page.blocks:
        if block.kind is not BlockKind.LINE:
            continue
        if block.confidence is None:
            return None
        scores.append(block.confidence)
    if not scores:
        return None
    return PageConfidence(page_number=page.page_number, status=page.status, mean=sum(scores) / len(scores),
                          minimum=min(scores), scored_blocks=len(scores))


def evidence_matches_pages(pages: tuple[OcrPage, ...], evidence: OcrEvidence) -> None:
    """Status and count evidence recomputed from the pages; summaries checked against block confidences.

    A page whose LINE blocks were dropped by a later stage keeps the summary its recognition stage
    recorded; at the model level only its shape is checked (the pipeline verifier checks it against the
    previous verified document).
    """
    if evidence.pages_total != len(pages):
        raise ValueError("EVIDENCE_PAGE_COUNT_MISMATCH")
    if (evidence.needs_ocr_pages != tuple(p.page_number for p in pages if p.status is PageStatus.NEEDS_OCR)
            or evidence.failed_pages != tuple(p.page_number for p in pages if p.status is PageStatus.FAILED)):
        raise ValueError("EVIDENCE_PAGE_STATUS_MISMATCH")
    if len(evidence.page_confidence) != len(pages):
        raise ValueError("EVIDENCE_CONFIDENCE_MISMATCH")
    for page, summary in zip(pages, evidence.page_confidence, strict=True):
        if (summary.page_number, summary.status) != (page.page_number, page.status):
            raise ValueError("EVIDENCE_CONFIDENCE_MISMATCH")
        if any(b.kind is BlockKind.LINE for b in page.blocks) or not page.blocks:
            if summary != page_summary(page):
                raise ValueError("EVIDENCE_CONFIDENCE_MISMATCH")
        elif (summary.mean is None) != (summary.scored_blocks == 0) or (summary.mean is None) != (
                summary.minimum is None) or (summary.minimum is not None and summary.mean is not None
                                             and summary.minimum > summary.mean):
            raise ValueError("EVIDENCE_CONFIDENCE_MISMATCH")


def summarize_pages(pages: Iterable[OcrPage], prior: Mapping[int, PageConfidence] | None = None
                    ) -> tuple[tuple[PageConfidence, ...], tuple[int, ...], tuple[int, ...]]:
    """Per-page confidence summary over LINE blocks, NEEDS_OCR pages and FAILED pages.

    A page whose LINE blocks were dropped later (``preserve_layout = False``) keeps the summary its
    recognition stage recorded (``prior``), so confidence evidence survives the structure stage.
    """
    summaries: list[PageConfidence] = []
    needs: list[int] = []
    failed: list[int] = []
    for page in pages:
        lines = [b for b in page.blocks if b.kind is BlockKind.LINE]
        earlier = (prior or {}).get(page.page_number)
        if not lines and page.blocks and earlier is not None and earlier.status is page.status:
            summaries.append(earlier)
        else:
            summaries.append(page_summary(page))
        if page.status is PageStatus.NEEDS_OCR:
            needs.append(page.page_number)
        elif page.status is PageStatus.FAILED:
            failed.append(page.page_number)
    return tuple(summaries), tuple(needs), tuple(failed)


# --- preprocess manifest --------------------------------------------------------------------

class PageGeometry(BaseModel):
    model_config = _FROZEN

    page_number: int = Field(ge=1, le=100_000)
    width: float | None = Field(default=None, gt=0, le=1_000_000)
    height: float | None = Field(default=None, gt=0, le=1_000_000)
    unit: Literal["pt", "px"] | None = None


def image_budget_ok(media_type: str, geometry: tuple[PageGeometry, ...]) -> None:
    """A single-page image needs validated pixel geometry within the declared budgets (fail closed).

    TIFF is refused in V1: its dimensions and frame count are not validated by the pack.
    """
    if media_type == "image/tiff":
        raise ValueError("IMAGE_TIFF_UNSUPPORTED")
    if len(geometry) != 1 or geometry[0].unit != "px" or geometry[0].width is None or geometry[0].height is None:
        raise ValueError("IMAGE_GEOMETRY_UNVALIDATED")
    width, height = geometry[0].width, geometry[0].height
    if width != int(width) or height != int(height) or width > MAX_IMAGE_SIDE or height > MAX_IMAGE_SIDE:
        raise ValueError("IMAGE_DIMENSIONS_EXCEEDED")
    if width * height > MAX_IMAGE_PIXELS:
        raise ValueError("IMAGE_PIXELS_EXCEEDED")


MANIFEST_KEYS = frozenset({"schema_version", "source", "detected_media_type", "page_count", "selected_pages",
                           "geometry", "semantic_authority"})


class PreprocessManifest(BaseModel):
    """``ocr.preprocessed/v1``: validated input facts (content-free)."""

    model_config = _FROZEN

    schema_version: Literal["ocr.preprocessed/v1"] = "ocr.preprocessed/v1"
    source: ArtifactRef
    detected_media_type: str
    page_count: int = Field(ge=1, le=MAX_PAGES)
    selected_pages: tuple[int, ...] = Field(min_length=1, max_length=MAX_PAGES)
    geometry: tuple[PageGeometry, ...] = Field(default=(), max_length=MAX_PAGES)
    semantic_authority: Literal["NONE"] = "NONE"

    @model_validator(mode="after")
    def consistent(self) -> PreprocessManifest:
        if self.detected_media_type != self.source.media_type:
            raise ValueError("MEDIA_TYPE_MISMATCH")
        if list(self.selected_pages) != sorted(set(self.selected_pages)) or self.selected_pages[-1] > self.page_count:
            raise ValueError("SELECTED_PAGES_INVALID")
        if [g.page_number for g in self.geometry] != list(self.selected_pages) and self.geometry:
            raise ValueError("GEOMETRY_PAGES_INVALID")
        if self.detected_media_type in IMAGE_MEDIA_TYPES:
            image_budget_ok(self.detected_media_type, self.geometry)
        assert_minimized(self.model_dump(mode="json"), allowed_keys=MANIFEST_KEYS, max_items=MAX_PAGES,
                         max_total_bytes=1024 * 1024, max_string=256)
        return self

    def to_bytes(self) -> bytes:
        return json.dumps(self.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode("utf-8")

    @classmethod
    def from_bytes(cls, data: bytes) -> PreprocessManifest:
        if len(data) > 4 * 1024 * 1024:
            raise OcrContractError("OCR_MANIFEST_TOO_LARGE")
        try:
            return cls.model_validate_json(data)
        except ValidationError:
            pass
        raise OcrContractError("OCR_MANIFEST_INVALID") from None  # no chained input values

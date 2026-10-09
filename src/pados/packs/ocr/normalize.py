# SPDX-License-Identifier: AGPL-3.0-only
"""Engine-neutral recognition result (``ocr.engine-result/v1``) and its normalization.

Every recognition engine binding (fixture, Tesseract CLI, PaddleOCR, MinerU-style, vision model)
returns this one bounded shape: pages -> lines (text, absolute bbox in the page unit, confidence
0..1 or null). ``recognized_pages`` validates it against the stage's page selection and converts it
into ``ocr.document/v1`` pages. Pages the engine did not return stay ``NEEDS_OCR``; nothing is
filled in, and a result outside the selection or outside the page geometry fails closed. A returned
page with no non-blank line is NOT a recognized page: V1 has no explicit blank-page result, so empty
recognition stays ``NEEDS_OCR`` (and so never satisfies the quality gate).
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from pados.packs.ocr.contracts import (
    MAX_BLOCK_TEXT,
    MAX_BLOCKS_PER_PAGE,
    MAX_PAGES,
    BBox,
    Block,
    BlockKind,
    OcrContractError,
    OcrPage,
    PageGeometry,
    PageStatus,
    clean_text,
)

_FROZEN = ConfigDict(frozen=True, extra="forbid")


class EngineLine(BaseModel):
    model_config = _FROZEN

    text: str = Field(max_length=MAX_BLOCK_TEXT)
    bbox: tuple[float, float, float, float] | None = None  # x0, y0, x1, y1 in the page unit, origin top-left
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)


class EnginePage(BaseModel):
    model_config = _FROZEN

    page_number: int = Field(ge=1, le=100_000)
    width: float | None = Field(default=None, gt=0, le=1_000_000)
    height: float | None = Field(default=None, gt=0, le=1_000_000)
    unit: Literal["pt", "px"] | None = None
    lines: tuple[EngineLine, ...] = Field(default=(), max_length=MAX_BLOCKS_PER_PAGE)


class EngineResult(BaseModel):
    model_config = _FROZEN

    schema_version: Literal["ocr.engine-result/v1"] = "ocr.engine-result/v1"
    pages: tuple[EnginePage, ...] = Field(default=(), max_length=MAX_PAGES)

    @field_validator("pages")
    @classmethod
    def unique_pages(cls, value: tuple[EnginePage, ...]) -> tuple[EnginePage, ...]:
        numbers = [page.page_number for page in value]
        if len(set(numbers)) != len(numbers):
            raise ValueError("ENGINE_RESULT_PAGE_DUPLICATE")
        return value


def parse_engine_result(raw: EngineResult | Mapping[str, Any]) -> EngineResult:
    if isinstance(raw, EngineResult):
        return raw
    try:
        return EngineResult.model_validate(dict(raw))
    except (ValidationError, TypeError, ValueError):
        pass
    raise OcrContractError("ENGINE_RESULT_INVALID") from None  # no chained engine output


def _normalized_bbox(bbox: tuple[float, float, float, float] | None, page: EnginePage) -> BBox | None:
    if bbox is None:
        return None
    if page.width is None or page.height is None:
        raise OcrContractError("ENGINE_RESULT_BBOX_WITHOUT_GEOMETRY")
    x0, y0, x1, y1 = bbox
    if not (0 <= x0 <= x1 <= page.width and 0 <= y0 <= y1 <= page.height):
        raise OcrContractError("ENGINE_RESULT_BBOX_OUTSIDE_PAGE")
    return BBox(x0=x0 / page.width, y0=y0 / page.height, x1=x1 / page.width, y1=y1 / page.height)


def recognized_pages(raw: EngineResult | Mapping[str, Any], *, selected: Sequence[int],
                     producer: str) -> dict[int, OcrPage]:
    """Validated ``RECOGNIZED`` pages keyed by page number (only pages the engine returned with text)."""
    result = parse_engine_result(raw)
    wanted = set(selected)
    pages: dict[int, OcrPage] = {}
    for engine_page in result.pages:
        if engine_page.page_number not in wanted:
            raise OcrContractError("ENGINE_RESULT_PAGE_UNEXPECTED")
        if (engine_page.width is None) != (engine_page.height is None) or (
                (engine_page.width is None) != (engine_page.unit is None)):
            raise OcrContractError("ENGINE_RESULT_GEOMETRY_INCOMPLETE")
        blocks: list[Block] = []
        for line in engine_page.lines:
            text = clean_text(line.text.replace("\t", " ").replace("\n", " ")).strip()
            if not text:
                continue
            blocks.append(Block(block_id=f"p{engine_page.page_number}.l{len(blocks) + 1}", kind=BlockKind.LINE,
                                text=text, bbox=_normalized_bbox(line.bbox, engine_page),
                                confidence=line.confidence, line_index=len(blocks)))
        if not blocks:  # empty recognition: the page stays NEEDS_OCR (no blank-page result in V1)
            continue
        pages[engine_page.page_number] = OcrPage(
            page_number=engine_page.page_number, status=PageStatus.RECOGNIZED, producer=producer,
            width=engine_page.width, height=engine_page.height, unit=engine_page.unit, blocks=tuple(blocks),
            reading_order=tuple(block.block_id for block in blocks))
    return pages


def unrecognized_page(number: int, geometry: Sequence[PageGeometry]) -> OcrPage:
    """A ``NEEDS_OCR`` page carrying only the preprocess geometry (no text)."""
    known = next((g for g in geometry if g.page_number == number), None)
    if known is None:
        return OcrPage(page_number=number, status=PageStatus.NEEDS_OCR)
    return OcrPage(page_number=number, status=PageStatus.NEEDS_OCR, width=known.width, height=known.height,
                   unit=known.unit)

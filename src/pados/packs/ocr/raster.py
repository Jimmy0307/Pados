# SPDX-License-Identifier: AGPL-3.0-only
"""Scanned-PDF OCR candidate (EXPERIMENTAL; not wired into a template; NOT live-validated).

``render_page_png`` rasterizes one PDF page to an 8-bit grayscale PNG inside the OCR pack's existing isolated
child (``_raster_worker``: rlimits applied first, no thread or process, empty environment, bounded output), with
PDFium through the optional ``pypdfium2``. ``ocr_pdf_pages`` then feeds each page image to the EXISTING Tesseract CLI
path (``tesseract_argv`` / ``tesseract_environment`` under the Tesseract budget) and parses the TSV with the existing
``parse_tesseract_tsv``, producing one ``ocr.engine-result/v1`` for the selected pages.

Fail-closed exactly like every other isolated OCR path: on win32 (no Job Object containment yet, R19-3) or for a
privileged process it raises ``OCR_ISOLATION_UNAVAILABLE`` and starts nothing. A missing ``pypdfium2`` is
``RASTER_ENGINE_NOT_INSTALLED``; a missing Tesseract is the caller's configuration (``TesseractConfig``).
"""
from __future__ import annotations

import dataclasses
import json
import sys
from collections.abc import Sequence

from pados.capability.adapter import CapabilityAdapterError
from pados.packs.ocr._isolated import (
    PDF_PARSE_BUDGET,
    BoundedRunError,
    ProcessBudget,
    isolation_unavailable_reason,
    limits_token,
    minimal_environment,
    run_bounded,
)
from pados.packs.ocr._raster_worker import CODES as RASTER_CODES
from pados.packs.ocr.engines import TesseractConfig, parse_tesseract_tsv, tesseract_argv, tesseract_environment
from pados.packs.ocr.normalize import EnginePage, EngineResult

RASTER_WORKER_MODULE = "pados.packs.ocr._raster_worker"
RASTER_BUDGET = dataclasses.replace(PDF_PARSE_BUDGET, wall_seconds=60.0, cpu_seconds=30)
DEFAULT_DPI = 300
MAX_PIXELS = 40_000_000  # ~A4 at 600 dpi; the worker refuses larger pages before rendering
_PNG = b"\x89PNG\r\n\x1a\n"


def render_page_png(pdf: bytes, page: int, *, dpi: int = DEFAULT_DPI, max_pixels: int = MAX_PIXELS,
                    budget: ProcessBudget = RASTER_BUDGET) -> tuple[int, int, bytes]:
    """(width, height, PNG bytes) of one page, rendered in the isolated child; closed codes otherwise."""
    unavailable = isolation_unavailable_reason()
    if unavailable is not None:
        raise CapabilityAdapterError(unavailable)
    header = json.dumps({"page": page, "dpi": dpi, "max_pixels": max_pixels}).encode("ascii")
    try:
        run = run_bounded([sys.executable, "-I", "-B", "-m", RASTER_WORKER_MODULE, limits_token(budget)],
                          stdin=header + b"\n" + pdf, env=minimal_environment(), budget=budget, self_limiting=True)
    except BoundedRunError:
        run = None
    if run is None or run.returncode != 0:
        raise CapabilityAdapterError("RASTER_FAILED")
    line, _, image = run.stdout.partition(b"\n")
    try:
        reply = json.loads(line)
    except ValueError:
        reply = None
    if not isinstance(reply, dict):
        raise CapabilityAdapterError("RASTER_FAILED")
    if reply.get("ok") is not True:
        code = reply.get("code")
        raise CapabilityAdapterError(code if code in RASTER_CODES else "RASTER_FAILED")
    width, height = reply.get("width"), reply.get("height")
    if not (isinstance(width, int) and isinstance(height, int) and width > 0 and height > 0
            and image.startswith(_PNG)):
        raise CapabilityAdapterError("RASTER_FAILED")
    return width, height, image


def ocr_pdf_pages(pdf: bytes, pages: Sequence[int], config: TesseractConfig, *,
                  languages: Sequence[str] = ("eng",), dpi: int = DEFAULT_DPI) -> EngineResult:
    """Rasterize and recognize each page; one engine-result page per input page (closed codes on failure)."""
    results: list[EnginePage] = []
    for page in pages:
        _, _, image = render_page_png(pdf, page, dpi=dpi)
        try:
            run = run_bounded(tesseract_argv(config, languages), stdin=image, env=tesseract_environment(),
                              budget=config.budget)
        except BoundedRunError:
            run = None
        if run is None or run.returncode != 0:
            raise CapabilityAdapterError("ENGINE_FAILED")
        recognized = parse_tesseract_tsv(run.stdout.decode("utf-8", errors="replace"), page_number=page)
        results.extend(recognized.pages)
    return EngineResult(pages=tuple(results))

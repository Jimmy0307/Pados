# SPDX-License-Identifier: AGPL-3.0-only
"""Scanned-PDF OCR candidate: isolated PDFium rasterization feeding the existing Tesseract path.

* win32 (no Job Object containment, R19-3): refuses with OCR_ISOLATION_UNAVAILABLE and starts nothing.
* POSIX with pypdfium2: a page renders to a grayscale PNG of the expected size; an out-of-range page and a
  malformed PDF are closed codes; an oversized page is refused before rendering.
* Live OCR (render + Tesseract) runs only when ``PADOS_TESSERACT`` names a Tesseract binary; otherwise it is
  skipped, and live scanned-PDF OCR stays NOT_VALIDATED.
"""
from __future__ import annotations

import importlib.util
import os
import struct
import sys
from pathlib import Path

import pytest

from pados.capability.adapter import CapabilityAdapterError
from pados.packs.ocr import _isolated
from pados.packs.ocr._raster_worker import png_gray
from pados.packs.ocr.raster import ocr_pdf_pages, render_page_png

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures" / "ocr"
HAS_PDFIUM = importlib.util.find_spec("pypdfium2") is not None
POSIX = sys.platform != "win32"


def test_the_png_encoder_writes_a_valid_grayscale_header() -> None:
    image = png_gray(3, 2, bytes(range(6)))
    assert image.startswith(b"\x89PNG\r\n\x1a\n")
    width, height, depth, color = struct.unpack(">IIBB", image[16:26])
    assert (width, height, depth, color) == (3, 2, 8, 0)


def test_windows_refuses_before_starting_anything(monkeypatch: pytest.MonkeyPatch) -> None:
    started: list[object] = []
    monkeypatch.setattr(_isolated, "isolation_refusal", lambda: "WINDOWS_CONTAINMENT_PENDING")
    monkeypatch.setattr("pados.packs.ocr.raster.run_bounded", lambda *a, **k: started.append(a))
    with pytest.raises(CapabilityAdapterError) as excinfo:
        render_page_png(b"%PDF-1.4", 1)
    assert excinfo.value.code == "OCR_ISOLATION_UNAVAILABLE" and started == []


@pytest.mark.skipif(not (POSIX and HAS_PDFIUM), reason="needs POSIX isolation and pypdfium2")
def test_a_page_renders_to_a_png_of_the_expected_size() -> None:
    pdf = (FIXTURES / "text_and_scan.pdf").read_bytes()
    width, height, image = render_page_png(pdf, 2, dpi=72)
    assert (width, height) == (612, 792) and image.startswith(b"\x89PNG")


@pytest.mark.skipif(not (POSIX and HAS_PDFIUM), reason="needs POSIX isolation and pypdfium2")
@pytest.mark.parametrize(("pdf", "page", "kwargs", "code"), [
    ("fixture", 9, {}, "RASTER_PAGE_OUT_OF_RANGE"),
    (b"not a pdf", 1, {}, "RASTER_PDF_UNREADABLE"),
    ("fixture", 1, {"dpi": 600, "max_pixels": 1000}, "RASTER_TOO_LARGE"),
])
def test_refusals_are_closed_codes(pdf: object, page: int, kwargs: dict[str, int], code: str) -> None:
    payload = (FIXTURES / "text_and_scan.pdf").read_bytes() if pdf == "fixture" else pdf
    with pytest.raises(CapabilityAdapterError) as excinfo:
        render_page_png(payload, page, **kwargs)  # type: ignore[arg-type]
    assert excinfo.value.code == code


@pytest.mark.skipif(not (POSIX and HAS_PDFIUM and os.environ.get("PADOS_TESSERACT")),
                    reason="live OCR needs a Tesseract binary (PADOS_TESSERACT)")
def test_live_scanned_pdf_ocr() -> None:  # pragma: no cover - environment dependent
    from pados.packs.ocr.engines import TesseractConfig

    config = TesseractConfig(executable=Path(os.environ["PADOS_TESSERACT"]), engine_version="5")
    pdf = Path(os.environ.get("PADOS_SCANNED_PDF", FIXTURES / "text_and_scan.pdf")).read_bytes()
    result = ocr_pdf_pages(pdf, [1], config)
    assert result.pages and result.pages[0].lines

"""Deterministic generator for the OCR pack fixtures (synthetic content written for PADOS tests only).

Run ``python tests/fixtures/ocr/build_fixtures.py`` to (re)write the committed fixtures; the test suite
asserts the committed bytes equal ``build_all()``, so their provenance is reproducible:

* ``text_and_scan.pdf`` - page 1 has a text layer (heading, paragraph, whitespace-aligned table);
  page 2 only paints an image XObject (no text layer -> NEEDS_OCR).
* ``scan_page.png``     - a 64x16 grayscale image (stored deflate blocks, so bytes are stable).
* ``*.ocr-truth.json``  - sidecar ground truth for the fixture OCR adapter, keyed by input SHA-256.

``long_text_pdf``, ``many_xobjects_pdf`` and ``flate_content_pdf`` build synthetic budget-test inputs on
demand (tests write them to ``tmp_path``; they are not part of ``build_all()`` and are never committed).
``bindings/`` holds a TEST-ONLY operator engine binding module for the isolated binding worker.
"""
from __future__ import annotations

import hashlib
import json
import struct
import sys
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _pdf(pages: list[tuple[bytes, bool]], *, images_per_page: int = 1) -> bytes:
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    }
    kids: list[int] = []
    next_id = 4
    image_id: int | None = None
    for content, has_image in pages:
        page_id, content_id = next_id, next_id + 1
        next_id += 2
        kids.append(page_id)
        resources = b"/Font << /F1 3 0 R >>"
        if has_image:
            if image_id is None:
                image_id, next_id = next_id, next_id + 1
                pixels = bytes([0, 255, 255, 0])
                objects[image_id] = (b"<< /Type /XObject /Subtype /Image /Width 2 /Height 2 /ColorSpace /DeviceGray "
                                     b"/BitsPerComponent 8 /Length %d >>\nstream\n" % len(pixels) + pixels
                                     + b"\nendstream")
            names = b" ".join(b"/Im%d %d 0 R" % (index, image_id) for index in range(1, images_per_page + 1))
            resources += b" /XObject << " + names + b" >>"
        objects[page_id] = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << " + resources
                            + b" >> /Contents %d 0 R >>" % content_id)
        objects[content_id] = b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream"
    objects[2] = (b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % kid for kid in kids)
                  + b"] /Count %d >>" % len(kids))
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: dict[int, int] = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += b"%d 0 obj\n" % number + objects[number] + b"\nendobj\n"
    xref = len(out)
    size = max(objects) + 1
    out += b"xref\n0 %d\n0000000000 65535 f \n" % size
    for number in range(1, size):
        out += b"%010d 00000 n \n" % offsets[number]
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (size, xref)
    return bytes(out)


def _text(lines: list[tuple[int, int, int, str]]) -> bytes:
    parts = [b"BT"]
    for x, y, size, text in lines:
        parts.append(b"/F1 %d Tf 1 0 0 1 %d %d Tm (%s) Tj" % (size, x, y, text.encode("latin-1")))
    parts.append(b"ET")
    return b"\n".join(parts)


def text_and_scan_pdf() -> bytes:
    page1 = _text([(72, 720, 18, "1. Quarterly Report"),
                   (72, 690, 12, "This report summarizes regional results."),
                   (72, 676, 12, "Figures are synthetic test data."),
                   (72, 640, 12, "Region"), (250, 640, 12, "Units"), (400, 640, 12, "Revenue"),
                   (72, 626, 12, "North"), (250, 626, 12, "120"), (400, 626, 12, "4500"),
                   (72, 612, 12, "South"), (250, 612, 12, "95"), (400, 612, 12, "3100")])
    page2 = b"q 200 0 0 200 100 400 cm /Im1 Do Q"
    return _pdf([(page1, False), (page2, True)])


def long_text_pdf(pages: int, lines: int, repeat: int) -> bytes:
    """Synthetic text-heavy PDF for budget tests (built in tmp_path by the tests; never committed)."""
    page = _text([(36, 770 - 12 * index, 8, ("row%03d data " % index) * repeat) for index in range(lines)])
    return _pdf([(page, False) for _ in range(pages)])


def many_xobjects_pdf(images: int) -> bytes:
    """Synthetic page whose resources list ``images`` image XObjects (tests only; never committed)."""
    return _pdf([(b"q 10 0 0 10 100 400 cm /Im1 Do Q", True)], images_per_page=images)


def flate_content_pdf(streams: list[bytes]) -> bytes:
    """One page whose ``/Contents`` is an array of FlateDecode streams (decompression-budget tests;
    built in tmp_path by the tests, never committed). A run of whitespace compresses ~1000:1."""
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        4: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
    }
    refs = []
    for index, content in enumerate(streams):
        packed = zlib.compress(content, 9)
        objects[5 + index] = (b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(packed) + packed
                              + b"\nendstream")
        refs.append(b"%d 0 R" % (5 + index))
    objects[3] = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 4 0 R >> >> "
                  b"/Contents [" + b" ".join(refs) + b"] >>")
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets: dict[int, int] = {}
    for number in sorted(objects):
        offsets[number] = len(out)
        out += b"%d 0 obj\n" % number + objects[number] + b"\nendobj\n"
    xref = len(out)
    size = max(objects) + 1
    out += b"xref\n0 %d\n0000000000 65535 f \n" % size
    for number in range(1, size):
        out += b"%010d 00000 n \n" % offsets[number]
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (size, xref)
    return bytes(out)


def scan_page_png(width: int = 64, height: int = 16) -> bytes:
    rows = b"".join(b"\x00" + bytes((x * 4 + y * 8) % 256 for x in range(width)) for y in range(height))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", zlib.compress(rows, 0))
            + chunk(b"IEND", b""))


def _truth(payload: bytes, pages: list[dict[str, object]]) -> bytes:
    document = {"schema_version": "ocr.fixture-truth/v1", "label": "TEST_FIXTURE_ONLY",
                "input_sha256": hashlib.sha256(payload).hexdigest(),
                "result": {"schema_version": "ocr.engine-result/v1", "pages": pages}}
    return (json.dumps(document, indent=2, sort_keys=True) + "\n").encode("utf-8")


def build_all() -> dict[str, bytes]:
    pdf, png = text_and_scan_pdf(), scan_page_png()
    return {
        "text_and_scan.pdf": pdf,
        "scan_page.png": png,
        "text_and_scan.ocr-truth.json": _truth(pdf, [{
            "page_number": 2, "width": 612, "height": 792, "unit": "pt",
            "lines": [{"text": "Scanned appendix", "bbox": [72, 72, 300, 90], "confidence": 0.93},
                      {"text": "Signed and dated.", "bbox": [72, 100, 280, 114], "confidence": 0.85}]}]),
        "scan_page.ocr-truth.json": _truth(png, [{
            "page_number": 1, "width": 64, "height": 16, "unit": "px",
            "lines": [{"text": "INVOICE 2026", "bbox": [2, 0, 60, 4], "confidence": 0.97},
                      {"text": "Item  Qty  Price", "bbox": [2, 8, 62, 11], "confidence": 0.91},
                      {"text": "Pen  2  3.50", "bbox": [2, 12, 50, 15], "confidence": 0.88}]}]),
    }


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else HERE
    for name, data in build_all().items():
        (target / name).write_bytes(data)

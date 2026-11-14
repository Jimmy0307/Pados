# SPDX-License-Identifier: AGPL-3.0-only
"""Scanned-PDF OCR check (DI-4): build an image-only PDF from the synthetic corpus, then OCR it through PADOS.

    python scripts/validation/scanned_pdf_ocr_check.py make <out.pdf> <truth.txt>   # needs pypdfium2
    python scripts/validation/scanned_pdf_ocr_check.py run  <in.pdf>  <truth.txt>   # needs pypdfium2 + Tesseract

``make`` renders corpus pages with PDFium at 300 dpi and embeds ONLY the pixels (no text layer). ``run`` sends the
PDF through ``pados.packs.ocr.raster.ocr_pdf_pages`` (isolated PDFium child + the existing Tesseract runner) and
prints character similarity and word recall against the truth text. POSIX only (the OCR pack fails closed on
win32). The renders are clean (no noise, skew or blur): a pass proves the execution path, not real-scan accuracy.
Runbook: docs/packs/ocr/SCANNED_PDF_OCR.md.
"""
from __future__ import annotations

import difflib
import json
import sys
import time
import unicodedata
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]



def image_pdf(pages):
    objects = {1: b"<< /Type /Catalog /Pages 2 0 R >>"}
    kids, nid = [], 3
    for width, height, rows in pages:
        page_id, content_id, image_id = nid, nid + 1, nid + 2
        nid += 3
        kids.append(page_id)
        data = zlib.compress(rows, 6)
        objects[image_id] = (b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace /DeviceGray "
                             b"/BitsPerComponent 8 /Filter /FlateDecode /Length %d >>\nstream\n" % (width, height, len(data))
                             + data + b"\nendstream")
        content = b"q 612 0 0 792 0 0 cm /Im1 Do Q"
        objects[content_id] = b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream"
        objects[page_id] = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /XObject << /Im1 %d 0 R >> >> "
                            b"/Contents %d 0 R >>" % (image_id, content_id))
    objects[2] = b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % k for k in kids) + b"] /Count %d >>" % len(kids)
    out, offsets = bytearray(b"%PDF-1.4\n"), {}
    for n in sorted(objects):
        offsets[n] = len(out)
        out += b"%d 0 obj\n" % n + objects[n] + b"\nendobj\n"
    xref, size = len(out), max(objects) + 1
    out += b"xref\n0 %d\n0000000000 65535 f \n" % size + b"".join(b"%010d 00000 n \n" % offsets[n] for n in range(1, size))
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (size, xref)
    return bytes(out)


def make(out_pdf: Path, truth: Path) -> None:
    import pypdfium2 as pdfium  # type: ignore[import-not-found, import-untyped, unused-ignore]

    sys.path.insert(0, str(ROOT / "tests" / "fixtures" / "document_intelligence"))
    import corpus

    cases = corpus.CASES[:2]
    source = pdfium.PdfDocument(corpus.binder_pdf(cases))
    pages = []
    for index in range(len(source)):
        bitmap = source[index].render(scale=300 / 72, grayscale=True)
        width, height, stride, buffer = bitmap.width, bitmap.height, bitmap.stride, bytes(bitmap.buffer)
        pages.append((width, height, b"".join(buffer[y * stride:y * stride + width] for y in range(height))))
    out_pdf.write_bytes(image_pdf(pages))
    truth.write_text("\n".join(text for _page, _key, text in corpus.binder_lines(cases)), encoding="utf-8")


def run(pdf_path: Path, truth_path: Path, tesseract: str = "/usr/bin/tesseract") -> dict[str, object]:
    from pados.packs.ocr.engines import TesseractConfig
    from pados.packs.ocr.raster import ocr_pdf_pages

    config = TesseractConfig(executable=Path(tesseract), engine_version="5.3.0")
    started = time.perf_counter()
    result = ocr_pdf_pages(pdf_path.read_bytes(), [1, 2], config)
    seconds = time.perf_counter() - started
    recognized = "\n".join(line.text for page in result.pages for line in page.lines)

    def norm(text: str) -> str:
        return " ".join(unicodedata.normalize("NFKC", text).split())

    truth, text = norm(truth_path.read_text(encoding="utf-8")), norm(recognized)
    words = truth.split()
    found = set(text.split())
    lines = [line for page in result.pages for line in page.lines]
    return {"pages": len(result.pages), "lines": len(lines), "seconds": round(seconds, 2),
            "char_similarity": round(difflib.SequenceMatcher(None, truth, text, autojunk=False).ratio(), 4),
            "word_recall": round(sum(word in found for word in words) / len(words), 4),
            "mean_confidence": round(sum(line.confidence or 0.0 for line in lines) / max(1, len(lines)), 4)}


if __name__ == "__main__":
    command, first, second = sys.argv[1], Path(sys.argv[2]), Path(sys.argv[3])
    if command == "make":
        make(first, second)
    else:
        print(json.dumps(run(first, second), indent=1))

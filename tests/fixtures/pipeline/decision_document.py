"""Deterministic generator of the synthetic decision document for the Phase G pipeline tests.

Generic, invented content written for PADOS tests only (no organisation, product or person data). One page
with a real PDF text layer: a numbered heading, two prose paragraphs, a whitespace-aligned multi-criteria
decision table (proposals P1..P6 against cost, quality and delivery with an overall rating) and a second
numbered section with evaluation notes. The bytes are a pure function of this module (no timestamps), so
the document hash is stable and pinned by ``tests/pipeline``.
"""
from __future__ import annotations

HEADER = ("Proposal", "Cost", "Quality", "Delivery", "Rating")
ROWS = (
    ("P1", "120", "high", "5", "good"),
    ("P2", "95", "medium", "7", "fair"),
    ("P3", "150", "high", "4", "good"),
    ("P4", "80", "low", "9", "poor"),
    ("P5", "110", "medium", "6", "fair"),
    ("P6", "90", "low", "8", "poor"),
)
COLUMN_X = (72, 160, 240, 330, 420)


def _pdf(content: bytes) -> bytes:
    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: b"<< /Type /Pages /Kids [4 0 R] /Count 1 >>",
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica /Encoding /WinAnsiEncoding >>",
        4: b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> "
           b"/Contents 5 0 R >>",
        5: b"<< /Length %d >>\nstream\n" % len(content) + content + b"\nendstream",
    }
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


def decision_document_pdf() -> bytes:
    lines: list[tuple[int, int, int, str]] = [
        (72, 720, 16, "1. Proposal Comparison"),
        (72, 692, 12, "This synthetic review compares six invented proposals for a shared service."),
        (72, 678, 12, "Each proposal is scored on cost, quality and delivery time in days."),
    ]
    y = 640
    for row in (HEADER, *ROWS):
        lines.extend((x, y, 12, cell) for x, cell in zip(COLUMN_X, row, strict=True))
        y -= 14
    lines += [
        (72, 520, 16, "2. Evaluation Notes"),
        (72, 492, 12, "Lower cost and shorter delivery are preferred by the review panel."),
        (72, 478, 12, "Quality is graded low, medium or high and the rating is poor, fair or good."),
    ]
    return _pdf(_text(lines))

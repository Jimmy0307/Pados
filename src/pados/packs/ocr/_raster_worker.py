# SPDX-License-Identifier: AGPL-3.0-only
"""Child-process entry point that rasterizes ONE PDF page for OCR (candidate; launched only by ``raster``).

Run as ``python -I -B -m pados.packs.ocr._raster_worker <limits>``. Like ``_pdf_worker`` its first action applies
the resource limits named by the ``<limits>`` token (address space, CPU, no file growth, descriptors, no core and
``RLIMIT_NPROC = 0``: no process and no thread) and refuses to run without them. stdin carries one JSON header line
``{"page": <1-based>, "dpi": <int>, "max_pixels": <int>}`` followed by the PDF bytes; stdout carries exactly one
reply line ``{"ok": true, "width": w, "height": h}`` followed by the 8-bit grayscale PNG bytes, or
``{"ok": false, "code": <closed>}``. PDFium (``pypdfium2``, optional, BSD-3-Clause / Apache-2.0 with bundled
permissive dependencies) renders; the PNG is encoded with the standard library (no Pillow). Nothing is logged and
no exception text leaves the process.
"""
from __future__ import annotations

import importlib
import json
import struct
import sys
import zlib
from collections.abc import Sequence

from pados.packs.ocr._rlimit_exec import EXIT_LIMITS_REFUSED, enter_limits

MAX_HEADER = 1024
CODES = ("RASTER_ENGINE_NOT_INSTALLED", "RASTER_PDF_UNREADABLE", "RASTER_PAGE_OUT_OF_RANGE", "RASTER_TOO_LARGE",
         "RASTER_REQUEST_INVALID", "RASTER_FAILED")


def png_gray(width: int, height: int, rows: bytes) -> bytes:
    """A minimal 8-bit grayscale PNG (filter 0 per row) from ``width * height`` bytes."""
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + rows[y * width:(y + 1) * width] for y in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", zlib.compress(raw, 6)) + chunk(b"IEND", b"")


def _fail(code: str) -> int:
    sys.stdout.buffer.write(json.dumps({"ok": False, "code": code}).encode("ascii") + b"\n")
    sys.stdout.buffer.flush()
    return 0


def _render(stream: bytes) -> int:
    header, _, pdf = stream.partition(b"\n")
    try:
        request = json.loads(header[:MAX_HEADER])
        page, dpi, max_pixels = int(request["page"]), int(request["dpi"]), int(request["max_pixels"])
        if page < 1 or not 50 <= dpi <= 600 or not 1 <= max_pixels <= 200_000_000:
            raise ValueError
    except (ValueError, KeyError, TypeError):
        return _fail("RASTER_REQUEST_INVALID")
    try:
        pdfium = importlib.import_module("pypdfium2")  # optional engine; imported dynamically (no stubs pinned)
    except ImportError:
        return _fail("RASTER_ENGINE_NOT_INSTALLED")
    try:
        document = pdfium.PdfDocument(pdf)
    except Exception:  # noqa: BLE001 - closed code
        return _fail("RASTER_PDF_UNREADABLE")
    if page > len(document):
        return _fail("RASTER_PAGE_OUT_OF_RANGE")
    target = document[page - 1]
    width_pt, height_pt = target.get_size()
    scale = dpi / 72.0
    if width_pt * scale * height_pt * scale > max_pixels:
        return _fail("RASTER_TOO_LARGE")  # refused BEFORE rendering: the bitmap size is known up front
    try:
        bitmap = target.render(scale=scale, grayscale=True)
        width, height, stride = bitmap.width, bitmap.height, bitmap.stride
        buffer = bytes(bitmap.buffer)
        rows = b"".join(buffer[y * stride:y * stride + width] for y in range(height))
        image = png_gray(width, height, rows)
    except Exception:  # noqa: BLE001 - closed code
        return _fail("RASTER_FAILED")
    sys.stdout.buffer.write(json.dumps({"ok": True, "width": width, "height": height}).encode("ascii") + b"\n")
    sys.stdout.buffer.write(image)
    sys.stdout.buffer.flush()
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    if not enter_limits(sys.argv[1:] if argv is None else argv):
        return EXIT_LIMITS_REFUSED
    try:
        return _render(sys.stdin.buffer.read())
    except MemoryError:
        return 3
    except Exception:  # noqa: BLE001 - nothing leaves the process
        return 4


if __name__ == "__main__":
    sys.exit(main())

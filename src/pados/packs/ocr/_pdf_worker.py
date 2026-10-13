# SPDX-License-Identifier: AGPL-3.0-only
"""Child-process entry point for untrusted PDF parsing (launched only by ``_isolated.isolated_pdf``).

Run as ``python -I -B -m pados.packs.ocr._pdf_worker <limits>``. Its FIRST action (``main``) applies
the resource limits named by the single ``<limits>`` argv token (``_rlimit_exec``: address space, CPU,
no file growth, descriptors, no core and ``RLIMIT_NPROC = 0``, so it can start no process and no
thread); if the token is malformed or any limit does not hold it exits ``EXIT_LIMITS_REFUSED`` before
reading stdin or importing pypdf. Then stdin carries one canonical JSON header line (operation and
bounds) followed by the PDF bytes; stdout carries exactly one canonical ASCII JSON reply:
``{"ok": true, "result": ...}`` or ``{"ok": false, "code": <closed>}``. Nothing is written anywhere
else, nothing is logged and no exception text leaves the process. Exit status: 0 = a reply was
written, 3 = the memory budget was exhausted (``MemoryError``), 4 = protocol violation, 117 = limits
refused. Imports only the standard library, the stdlib-only ``_rlimit_exec`` and (lazily) pypdf, so
the child stays small under its address-space limit; pypdf creates no thread or process.

Decoding is bounded BEFORE parsing: every operation runs inside ``decode_budget``, which applies
pypdf's public ``Configuration`` (``pypdf.apply_configuration``, scoped to the operation and restored
afterwards) with the decompression limits in ``DECODE_LIMITS`` (zlib/LZW/run-length/JBIG2 output,
array-based content streams, flate image buffers and predictor rows, zlib recovery input, the declared
stream length) lowered to the budget the parent sent (``max_stream_bytes`` = the per-page content
budget, ``max_declared_stream_bytes`` = the source budget), never above pypdf's own defaults, and reads
every one back from ``pypdf.get_configuration()``. Legacy handling is disabled inside the scope, so the
deprecated module constants can never overwrite the budget when the reader is built. A stream that
would decode past its cap raises pypdf's ``LimitReachedError``, reported as ``OCR_DECODE_BUDGET_EXCEEDED``.

Operations (the same pypdf calls the pack previously made in-process, so results are identical):
``inspect`` (page count, encryption, MediaBox geometry of the selected pages), ``figures`` (image
XObjects per page; the number of XObjects is counted from the raw ``/XObject`` dictionary before any
member is resolved, bounded per page and in total) and ``text``: for each page the content streams
are decoded first, under the per-page cap and a running total (``max_content_bytes``), BEFORE
``extract_text(extraction_mode="layout")`` runs (the decoded data is cached on the stream objects, so
extraction reuses it); the extracted text is then measured against a per-page and a running total
byte budget and cut into bounded lines.
"""
from __future__ import annotations

import io
import json
import math
import re
import sys
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from functools import wraps
from typing import Any

from pados.packs.ocr._rlimit_exec import EXIT_LIMITS_REFUSED, enter_limits

EXIT_OK, EXIT_BUDGET, EXIT_PROTOCOL = 0, 3, 4
BUDGET_EXCEEDED = "OCR_PARSE_BUDGET_EXCEEDED"
DECODE_BUDGET_EXCEEDED = "OCR_DECODE_BUDGET_EXCEEDED"
MAX_CONTENT_STREAMS = 10_000  # entries of one page's /Contents array (pypdf's CONTENT_STREAM_ARRAY_MAX_LENGTH)
# pypdf.Configuration decompression fields -> which budget value bounds them ("stream" = per-page content
# budget). The same nine controls the worker set through the legacy pypdf.filters constants
# (ZLIB_MAX_OUTPUT_LENGTH, LZW_MAX_OUTPUT_LENGTH, RUN_LENGTH_MAX_OUTPUT_LENGTH, JBIG2_MAX_OUTPUT_LENGTH,
# MAX_ARRAY_BASED_STREAM_OUTPUT_LENGTH, FLATE_MAX_BUFFER_SIZE, FLATE_MAX_ROW_LENGTH,
# ZLIB_MAX_RECOVERY_INPUT_LENGTH, MAX_DECLARED_STREAM_LENGTH), under pypdf's own field names.
DECODE_LIMITS: dict[str, str] = {
    "zlib_maximum_output_length": "stream", "lzw_maximum_output_length": "stream",
    "run_length_maximum_output_length": "stream", "jbig2_maximum_output_length": "stream",
    "array_based_stream_maximum_output_length": "stream", "image_maximum_buffer_size": "stream",
    "flate_maximum_row_length": "stream", "zlib_maximum_recovery_input_length": "stream",
    "maximum_declared_stream_length": "declared",
}
MAX_HEADER_BYTES = 64 * 1024
CONTROL_PATTERN = r"[\x00-\x08\x0b-\x1f\x7f]"  # identical to contracts._CONTROL (a test pins this)
_CONTROL = re.compile(CONTROL_PATTERN)
_FIGURE_NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}")


class Refused(Exception):
    """A closed refusal code reported back to the parent."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ProtocolError(Exception):
    pass


def text_lines(page_text: str) -> list[tuple[int, str]]:
    """Non-blank lines with their grid index; control characters removed, tabs become spaces."""
    lines: list[tuple[int, str]] = []
    for index, raw in enumerate(page_text.split("\n")):
        text = _CONTROL.sub("", raw.replace("\t", " ").replace("\r", "")).strip()
        if text:
            lines.append((index, text))
    return lines


def figure_name(raw: object) -> str:
    name = str(raw).lstrip("/")
    return name if _FIGURE_NAME.fullmatch(name) else "unnamed"


def _bounded_int(header: dict[str, Any], key: str, *, optional: bool = False, high: int = 1_000_000) -> int | None:
    value = header.get(key)
    if value is None and optional:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= high:
        raise ProtocolError(key)
    return value


def _pages(header: dict[str, Any]) -> list[int]:
    pages = header.get("pages")
    if not isinstance(pages, list) or len(pages) > 100_000 or any(
            isinstance(p, bool) or not isinstance(p, int) or p < 1 for p in pages):
        raise ProtocolError("pages")
    return pages


def decode_limits(stream_cap: int, declared_cap: int) -> dict[str, int]:
    """The budgeted value of every pypdf limit in ``DECODE_LIMITS``: the budget, never above the current
    pypdf configuration (a limit pypdf disabled with 0 gets the budget). A missing or non-integer field
    (a changed pypdf) is a protocol error: parsing never starts without them."""
    if stream_cap < 1 or declared_cap < 1:
        raise ProtocolError("decode limits")
    from pypdf import get_configuration

    current_configuration = get_configuration()
    limits: dict[str, int] = {}
    for name, source in DECODE_LIMITS.items():
        current = getattr(current_configuration, name, None)
        if isinstance(current, bool) or not isinstance(current, int):
            raise ProtocolError(name)
        budget = stream_cap if source == "stream" else declared_cap
        limits[name] = budget if current <= 0 else min(current, budget)
    return limits


@contextmanager
def decode_budget(stream_cap: int, declared_cap: int) -> Iterator[dict[str, int]]:
    """Apply the decode budget through pypdf's public Configuration for the enclosed work only.

    ``apply_configuration`` scopes the configuration to the current execution context and restores the
    previous one on exit. ``disable_legacy_handling`` keeps pypdf's deprecated module constants from
    overwriting the budget when a reader is built. Every field is read back before any parsing starts."""
    limits = decode_limits(stream_cap, declared_cap)
    from pypdf import apply_configuration, get_configuration

    budgeted_configuration = get_configuration().with_overwrites(disable_legacy_handling=True, **limits)
    with apply_configuration(budgeted_configuration):
        active = get_configuration()
        if not active.disable_legacy_handling or any(getattr(active, name) != value for name, value in limits.items()):
            raise ProtocolError("decode limits")
        yield limits


def budgeted(operation: Callable[[dict[str, Any], bytes], dict[str, Any]],
             ) -> Callable[[dict[str, Any], bytes], dict[str, Any]]:
    """Run a whole worker operation (reader construction, parsing and extraction) under ``decode_budget``."""

    @wraps(operation)
    def run(header: dict[str, Any], data: bytes) -> dict[str, Any]:
        with decode_budget(_bounded_int(header, "max_stream_bytes", high=1 << 34) or 0,
                           _bounded_int(header, "max_declared_stream_bytes", high=1 << 34) or 0):
            return operation(header, data)

    return run


def _is_decode_limit(exc: BaseException) -> bool:
    errors = sys.modules.get("pypdf.errors")
    limit = getattr(errors, "LimitReachedError", None) if errors is not None else None
    return isinstance(limit, type) and isinstance(exc, limit)


def _reader(header: dict[str, Any], data: bytes) -> Any:
    """The reader, built inside the operation's ``decode_budget`` (object streams decompress under it)."""
    from pypdf import PdfReader

    reader = PdfReader(io.BytesIO(data))
    if reader.is_encrypted:
        raise Refused("PDF_ENCRYPTED")
    return reader


def _box(page: Any) -> tuple[float | None, float | None]:
    box = page.mediabox
    width, height = abs(float(box.width)), abs(float(box.height))
    if not (math.isfinite(width) and math.isfinite(height)):
        return None, None
    return width, height


def _declared_page_count(reader: Any) -> int | None:
    """The catalog's declared ``/Count`` (untrusted; only used to refuse early, never to accept)."""
    try:
        count = reader.trailer["/Root"]["/Pages"]["/Count"]
    except Exception:
        return None
    return int(count) if isinstance(count, int) and not isinstance(count, bool) else None


@budgeted
def inspect(header: dict[str, Any], data: bytes) -> dict[str, Any]:
    max_pages = _bounded_int(header, "max_pages") or 0
    first = _bounded_int(header, "page_first", optional=True)
    last = _bounded_int(header, "page_last", optional=True)
    reader = _reader(header, data)
    declared = _declared_page_count(reader)
    if declared is not None and declared > max_pages:
        raise Refused("PAGE_COUNT_UNSUPPORTED")
    page_count = len(reader.pages)
    geometry: list[list[Any]] = []
    if 1 <= page_count <= max_pages and (last is None or last <= page_count):
        for number in range(first or 1, (last or page_count) + 1):
            width, height = _box(reader.pages[number - 1])
            measured = width is not None and height is not None and width > 0 and height > 0
            geometry.append([number, width if measured else None, height if measured else None])
    return {"page_count": page_count, "geometry": geometry}


def _xobject_dictionary(page: Any) -> Any:
    """The page's raw ``/XObject`` dictionary, members unresolved.

    ``raw_get`` never resolves: a DIRECT dictionary was already parsed with ``/Resources`` and is
    counted as is; an INDIRECT one must be parsed to be counted (bounded by the source size, the
    memory and CPU limits), but none of its members is resolved before the count is checked.
    """
    from pypdf.generic import DictionaryObject

    resources = page.get("/Resources")
    resources = resources.get_object() if resources is not None else None
    if not isinstance(resources, DictionaryObject) or "/XObject" not in resources:
        return {}
    raw = resources.raw_get("/XObject")
    xobjects = raw.get_object() if raw is not None else None
    return xobjects if isinstance(xobjects, DictionaryObject) else {}


def content_bytes(page: Any, cap: int) -> int:
    """Decode the page's content streams under ``cap`` (decoded bytes, summed); refuse past it.

    The ``/Contents`` entry is read raw and an array's length is checked before any stream is
    resolved. Each stream is decoded once (pypdf caches the decoded data on the stream object, which
    ``extract_text`` then reuses); pypdf's own per-stream limits (``decode_budget``) stop a
    single stream at ``cap`` while it is being decompressed.
    """
    from pypdf.generic import ArrayObject, NullObject, StreamObject

    if "/Contents" not in page:
        return 0
    raw = page.raw_get("/Contents")
    contents = raw.get_object() if raw is not None else None
    if contents is None or isinstance(contents, NullObject):
        return 0
    if not isinstance(contents, (ArrayObject, StreamObject)):
        raise ValueError("contents")  # a malformed page: reported as a failed page, as before
    items = list(contents) if isinstance(contents, ArrayObject) else [contents]
    if len(items) > MAX_CONTENT_STREAMS:
        raise Refused(DECODE_BUDGET_EXCEEDED)
    size = 0
    for item in items:
        stream = item.get_object()
        if not isinstance(stream, StreamObject):
            raise ValueError("contents")
        size += len(stream.get_data())
        if size > cap:
            raise Refused(DECODE_BUDGET_EXCEEDED)
    return size


@budgeted
def figures(header: dict[str, Any], data: bytes) -> dict[str, Any]:
    max_figures = _bounded_int(header, "max_figures") or 0
    per_page = _bounded_int(header, "max_xobjects_per_page") or 0
    total = _bounded_int(header, "max_xobjects") or 0
    reader = _reader(header, data)
    found: list[list[Any]] = []
    examined = 0
    for number in _pages(header):
        if number > len(reader.pages):
            raise Refused("PAGE_RANGE_OUT_OF_BOUNDS")
        xobjects = _xobject_dictionary(reader.pages[number - 1])
        examined += len(xobjects)  # counted before any key is sorted or any member resolved
        if len(xobjects) > per_page or examined > total:
            raise Refused(BUDGET_EXCEEDED)
        listed: list[list[Any]] = []
        for key in sorted(str(k) for k in xobjects.keys()):
            obj = xobjects[key].get_object()
            if obj.get("/Subtype") != "/Image":
                continue
            if len(listed) >= max_figures:
                raise Refused("FIGURES_TOO_MANY")
            width, height = obj.get("/Width"), obj.get("/Height")
            listed.append([figure_name(key),
                           int(width) if isinstance(width, int) and width > 0 else None,
                           int(height) if isinstance(height, int) and height > 0 else None])
        found.append([number, listed])
    return {"figures": found}


@budgeted
def text(header: dict[str, Any], data: bytes) -> dict[str, Any]:
    expected = _bounded_int(header, "page_count")
    max_blocks = _bounded_int(header, "max_blocks") or 0
    max_text = _bounded_int(header, "max_block_text") or 0
    page_budget = _bounded_int(header, "max_page_text_bytes", high=1 << 30) or 0
    total_budget = _bounded_int(header, "max_text_bytes", high=1 << 30) or 0
    stream_budget = _bounded_int(header, "max_stream_bytes", high=1 << 34) or 0
    content_budget = _bounded_int(header, "max_content_bytes", high=1 << 34) or 0
    reader = _reader(header, data)
    if len(reader.pages) != expected:
        raise Refused("MANIFEST_PAGE_COUNT_MISMATCH")
    pages: list[dict[str, Any]] = []
    decoded = 0
    content = 0
    for number in _pages(header):
        page = reader.pages[number - 1]
        failed = False
        try:
            width, height = _box(page)
            content += content_bytes(page, stream_budget)  # decoded under the cap BEFORE extract_text
            if content > content_budget:
                raise Refused(DECODE_BUDGET_EXCEEDED)
            raw = page.extract_text(extraction_mode="layout")
        except (MemoryError, Refused):
            raise
        except Exception as exc:  # a malformed page fails alone (explicitly partial), exactly as before
            if _is_decode_limit(exc):
                raise Refused(DECODE_BUDGET_EXCEEDED) from None
            failed = True
        if failed:
            pages.append({"page": number, "state": "ERROR"})
            continue
        size = len(raw.encode("utf-8", "replace"))  # decoded text, measured before it is split or kept
        decoded += size
        if size > page_budget or decoded > total_budget:
            raise Refused(BUDGET_EXCEEDED)
        lines = text_lines(raw)
        del raw
        oversize = len(lines) > max_blocks or any(len(line) > max_text for _, line in lines)
        pages.append({"page": number, "state": "OVERSIZE" if oversize else "OK", "width": width, "height": height,
                      "lines": [] if oversize else [[index, line] for index, line in lines]})
    return {"pages": pages}


OPERATIONS: dict[str, Callable[[dict[str, Any], bytes], dict[str, Any]]] = {
    "inspect": inspect, "figures": figures, "text": text}


def _dispatch(raw: bytes) -> dict[str, Any]:
    split = raw.find(b"\n")
    if split < 0 or split > MAX_HEADER_BYTES:
        raise ProtocolError("header")
    try:
        header = json.loads(raw[:split].decode("ascii"))
    except ValueError:
        raise ProtocolError("header") from None
    name = header.get("op") if isinstance(header, dict) else None
    operation = OPERATIONS.get(name) if isinstance(name, str) else None
    if operation is None:
        raise ProtocolError("op")
    try:
        return {"ok": True, "result": operation(header, raw[split + 1:])}
    except (MemoryError, ProtocolError):
        raise
    except Refused as refused:
        return {"ok": False, "code": refused.code}
    except Exception as exc:  # pypdf raises many exception types for malformed files
        return {"ok": False, "code": DECODE_BUDGET_EXCEEDED if _is_decode_limit(exc) else "PDF_UNREADABLE"}


def guarded(work: Callable[[], dict[str, Any]]) -> int:
    """Run ``work`` and write its reply; MemoryError -> EXIT_BUDGET, protocol errors -> EXIT_PROTOCOL."""
    try:
        reply = json.dumps(work(), sort_keys=True, separators=(",", ":"), ensure_ascii=True,
                           allow_nan=False).encode("ascii")
    except MemoryError:
        return EXIT_BUDGET
    except (ProtocolError, ValueError, TypeError):
        return EXIT_PROTOCOL
    sys.stdout.buffer.write(reply)
    sys.stdout.buffer.flush()
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    """Limits first (before stdin is read or pypdf imported); refuse to run without them."""
    if not enter_limits(sys.argv[1:] if argv is None else argv):
        return EXIT_LIMITS_REFUSED
    return guarded(lambda: _dispatch(sys.stdin.buffer.read()))


if __name__ == "__main__":
    sys.exit(main())

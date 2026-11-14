# SPDX-License-Identifier: AGPL-3.0-only
"""Child-process entry point for untrusted text-PDF parsing (launched only by ``pdf_isolation``).

Run as ``python -I -B -m pados.corpus._pdf_parse_worker <limits>``. This is the corpus/graph/enterprise
counterpart of the OCR pack's ``_pdf_worker`` (Core may not import a pack, so the pattern is ported, not
shared). Its FIRST action applies the resource limits named by the single ``<limits>`` argv token
(POSIX ``resource``: address space, CPU, no file growth, descriptors, no core, no process or thread);
on Linux every limit must apply and read back or it exits ``EXIT_LIMITS_REFUSED`` before it reads stdin
or imports pypdf. Windows has no ``resource`` module: there the parent enforces the wall clock, the
output cap and a polled memory watchdog instead (see ``pdf_isolation``).

stdin carries one ASCII JSON header line (bounds) followed by the PDF bytes; stdout carries exactly one
canonical ASCII JSON reply: ``{"ok": true, "result": {...}}`` or ``{"ok": false, "code": <closed>}``.
Nothing is written anywhere else on purpose, and no exception text, path or document content ever leaves
in an error. Exit status: 0 = a reply was written, 3 = memory exhausted (``MemoryError``), 4 = protocol
violation, 117 = limits refused.

Parsing is the exact pypdf sequence the API process used to run in-process (``PdfReader(strict=False)``,
``reader.metadata`` title/author, ``page.extract_text()`` per page), wrapped in bounds:

* decoding is bounded BEFORE parsing through pypdf's public ``Configuration`` (``apply_configuration``):
  every decompression limit is lowered to the per-stream budget (never above pypdf's own default) and
  read back from ``get_configuration()``; legacy module constants are disabled inside the scope;
* the catalog's declared ``/Count`` and the flattened page count are checked against ``max_pages``;
* each page's content streams are decoded under the per-page cap and a running total BEFORE
  ``extract_text`` runs (pypdf caches the decoded data, so extraction reuses it);
* the JSON-encoded text is measured per page and in total against the text budget.
"""
from __future__ import annotations

import io
import json
import re
import sys
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any

if sys.platform != "win32":
    import resource

EXIT_OK, EXIT_MEMORY, EXIT_PROTOCOL, EXIT_LIMITS_REFUSED = 0, 3, 4, 117
LIMITS_PREFIX = "pados-corpus-pdf/1:"
_TOKEN = re.compile(r"pados-corpus-pdf/1:as=([0-9]{1,12}),cpu=([0-9]{1,4}),nofile=([0-9]{1,5})")
MAX_HEADER_BYTES = 4096
MAX_CONTENT_STREAMS = 10_000  # entries of one page's /Contents array

PDF_MALFORMED = "PDF_MALFORMED"
PDF_PARSE_ERROR = "PDF_PARSE_ERROR"
PDF_DECODE_BUDGET_EXCEEDED = "PDF_DECODE_BUDGET_EXCEEDED"
PDF_PAGE_COUNT_EXCEEDED = "PDF_PAGE_COUNT_EXCEEDED"
PDF_TEXT_BUDGET_EXCEEDED = "PDF_TEXT_BUDGET_EXCEEDED"
WORKER_CODES = frozenset({PDF_MALFORMED, PDF_PARSE_ERROR, PDF_DECODE_BUDGET_EXCEEDED, PDF_PAGE_COUNT_EXCEEDED,
                          PDF_TEXT_BUDGET_EXCEEDED})
# pypdf.Configuration fields bounded by the per-stream budget ("stream") or the source size ("declared").
DECODE_LIMITS: dict[str, str] = {
    "zlib_maximum_output_length": "stream", "lzw_maximum_output_length": "stream",
    "run_length_maximum_output_length": "stream", "jbig2_maximum_output_length": "stream",
    "array_based_stream_maximum_output_length": "stream", "image_maximum_buffer_size": "stream",
    "flate_maximum_row_length": "stream", "zlib_maximum_recovery_input_length": "stream",
    "maximum_declared_stream_length": "declared",
}


class Refused(Exception):
    """A closed refusal code reported back to the parent."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ProtocolError(Exception):
    pass


# --- limits (the child's first action) --------------------------------------------------------------------

def encode_limits(memory_bytes: int, cpu_seconds: int, max_open_files: int) -> str:
    return f"{LIMITS_PREFIX}as={memory_bytes},cpu={cpu_seconds},nofile={max_open_files}"


def parse_limits(value: str) -> tuple[int, int, int] | None:
    match = _TOKEN.fullmatch(value)
    if match is None:
        return None
    memory, cpu, nofile = (int(item) for item in match.groups())
    return (memory, cpu, nofile) if memory >= 1 and cpu >= 1 and nofile >= 1 else None


if sys.platform == "win32":

    def apply_limits(memory_bytes: int, cpu_seconds: int, max_open_files: int) -> bool:
        """Windows has no ``resource`` module: the parent enforces the wall clock, the output cap and the
        polled memory watchdog (a Job Object is the production follow-up)."""
        return True

else:

    def _set_limit(kind: int, soft: int, hard: int) -> bool:
        try:
            _, current_hard = resource.getrlimit(kind)
            if current_hard != resource.RLIM_INFINITY:  # never above the inherited hard limit
                soft, hard = min(soft, current_hard), min(hard, current_hard)
            resource.setrlimit(kind, (soft, hard))
            return resource.getrlimit(kind) == (soft, hard)
        except (OSError, ValueError):
            return False

    def apply_limits(memory_bytes: int, cpu_seconds: int, max_open_files: int) -> bool:
        """Set and read back every limit. Linux requires all of them (address space and no process/thread
        included). Other POSIX systems: macOS reserves a multi-GiB address space at startup and does not
        enforce ``RLIMIT_AS``, so the address-space limit is left to the parent's memory watchdog there and
        ``RLIMIT_NPROC = 0`` is best effort."""
        required = [(resource.RLIMIT_CPU, cpu_seconds, cpu_seconds + 1), (resource.RLIMIT_FSIZE, 0, 0),
                    (resource.RLIMIT_NOFILE, max_open_files, max_open_files), (resource.RLIMIT_CORE, 0, 0)]
        optional = [(resource.RLIMIT_NPROC, 0, 0)]
        if sys.platform.startswith("linux"):
            required, optional = [*required, (resource.RLIMIT_AS, memory_bytes, memory_bytes), *optional], []
        if not all(_set_limit(kind, soft, hard) for kind, soft, hard in required):
            return False
        for kind, soft, hard in optional:
            _set_limit(kind, soft, hard)
        return True


def enter_limits(argv: Sequence[str]) -> bool:
    if len(argv) != 1:
        return False
    limits = parse_limits(argv[0])
    return limits is not None and apply_limits(*limits)


# --- bounded parsing ------------------------------------------------------------------------------------

def _bounded_int(header: dict[str, Any], key: str, *, low: int = 1, high: int = 1 << 34) -> int:
    value = header.get(key)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ProtocolError(key)
    return value


def decode_limits(stream_cap: int, declared_cap: int) -> dict[str, int]:
    """Every pypdf limit in ``DECODE_LIMITS`` lowered to the budget (never above pypdf's current value);
    a missing or non-integer field (a changed pypdf) is a protocol error: parsing never starts without it."""
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
    limits = decode_limits(stream_cap, declared_cap)
    from pypdf import apply_configuration, get_configuration

    budgeted = get_configuration().with_overwrites(disable_legacy_handling=True, **limits)
    with apply_configuration(budgeted):
        active = get_configuration()
        if not active.disable_legacy_handling or any(getattr(active, name) != value for name, value in limits.items()):
            raise ProtocolError("decode limits")
        yield limits


def _is_decode_limit(exc: BaseException) -> bool:
    errors = sys.modules.get("pypdf.errors")
    limit = getattr(errors, "LimitReachedError", None) if errors is not None else None
    return isinstance(limit, type) and isinstance(exc, limit)


def _metadata_value(metadata: Any, key: str) -> str | None:
    """Unchanged from the former in-process parser (``corpus.pdf``)."""
    if metadata is None:
        return None
    value = metadata.get(key) if hasattr(metadata, "get") else getattr(metadata, key, None)
    value = value or getattr(metadata, key, None)
    return str(value).strip() if value else None


def _declared_page_count(reader: Any) -> int | None:
    """The catalog's declared ``/Count`` (untrusted; only used to refuse early, never to accept)."""
    try:
        count = reader.trailer["/Root"]["/Pages"]["/Count"]
    except Exception:  # noqa: BLE001 - absent or malformed: the flattened count is checked next
        return None
    return int(count) if isinstance(count, int) and not isinstance(count, bool) else None


def content_bytes(page: Any, cap: int) -> int:
    """Decode the page's content streams under ``cap`` (decoded bytes, summed); refuse past it."""
    from pypdf.generic import ArrayObject, NullObject, StreamObject

    if "/Contents" not in page:
        return 0
    raw = page.raw_get("/Contents")
    contents = raw.get_object() if raw is not None else None
    if contents is None or isinstance(contents, NullObject):
        return 0
    if not isinstance(contents, (ArrayObject, StreamObject)):
        return 0  # left to extract_text, exactly as before (it ignores a malformed /Contents)
    items = list(contents) if isinstance(contents, ArrayObject) else [contents]
    if len(items) > MAX_CONTENT_STREAMS:
        raise Refused(PDF_DECODE_BUDGET_EXCEEDED)
    size = 0
    for item in items:
        stream = item.get_object()
        if not isinstance(stream, StreamObject):
            continue
        size += len(stream.get_data())
        if size > cap:
            raise Refused(PDF_DECODE_BUDGET_EXCEEDED)
    return size


def _encoded_size(value: str | None) -> int:
    return len(json.dumps(value, ensure_ascii=True))


def parse(header: dict[str, Any], data: bytes) -> dict[str, Any]:
    max_pages = _bounded_int(header, "max_pages", high=1_000_000)
    page_budget = _bounded_int(header, "max_page_text_bytes")
    text_budget = _bounded_int(header, "max_text_bytes")
    stream_cap = _bounded_int(header, "max_stream_bytes")
    content_cap = _bounded_int(header, "max_content_bytes")
    declared_cap = _bounded_int(header, "max_declared_stream_bytes")
    with decode_budget(stream_cap, declared_cap):
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data), strict=False)
        if reader.is_encrypted:
            return {"encrypted": True, "title": None, "author": None, "pages": []}
        metadata = reader.metadata
        title = _metadata_value(metadata, "title")
        author = _metadata_value(metadata, "author")
        used = _encoded_size(title) + _encoded_size(author)
        declared = _declared_page_count(reader)
        if declared is not None and declared > max_pages:
            raise Refused(PDF_PAGE_COUNT_EXCEEDED)
        if len(reader.pages) > max_pages:
            raise Refused(PDF_PAGE_COUNT_EXCEEDED)
        pages: list[str] = []
        content = 0
        for page in reader.pages:
            content += content_bytes(page, stream_cap)  # decoded under the cap BEFORE extract_text
            if content > content_cap:
                raise Refused(PDF_DECODE_BUDGET_EXCEEDED)
            text = page.extract_text() or ""
            size = _encoded_size(text) + 1
            used += size
            if size > page_budget or used > text_budget:
                raise Refused(PDF_TEXT_BUDGET_EXCEEDED)
            pages.append(text)
    return {"encrypted": False, "title": title, "author": author, "pages": pages}


def _dispatch(raw: bytes) -> dict[str, Any]:
    split = raw.find(b"\n")
    if split < 0 or split > MAX_HEADER_BYTES:
        raise ProtocolError("header")
    try:
        header = json.loads(raw[:split].decode("ascii"))
    except ValueError:
        raise ProtocolError("header") from None
    if not isinstance(header, dict) or header.get("op") != "parse":
        raise ProtocolError("op")
    try:
        return {"ok": True, "result": parse(header, raw[split + 1:])}
    except (MemoryError, ProtocolError):
        raise
    except Refused as refused:
        return {"ok": False, "code": refused.code}
    except Exception as exc:  # noqa: BLE001 - pypdf raises many types for hostile input; the code is closed
        if _is_decode_limit(exc):
            return {"ok": False, "code": PDF_DECODE_BUDGET_EXCEEDED}
        errors = sys.modules.get("pypdf.errors")
        read_error = getattr(errors, "PdfReadError", None) if errors is not None else None
        malformed = isinstance(read_error, type) and isinstance(exc, read_error)
        return {"ok": False, "code": PDF_MALFORMED if malformed else PDF_PARSE_ERROR}


def main(argv: Sequence[str] | None = None) -> int:
    """Limits first (before stdin is read or pypdf imported); refuse to run without them."""
    if not enter_limits(sys.argv[1:] if argv is None else argv):
        return EXIT_LIMITS_REFUSED
    try:
        reply = json.dumps(_dispatch(sys.stdin.buffer.read()), sort_keys=True, separators=(",", ":"),
                           ensure_ascii=True, allow_nan=False).encode("ascii")
    except MemoryError:
        return EXIT_MEMORY
    except (ProtocolError, ValueError, TypeError, RecursionError):
        return EXIT_PROTOCOL
    sys.stdout.buffer.write(reply)
    sys.stdout.buffer.flush()
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())

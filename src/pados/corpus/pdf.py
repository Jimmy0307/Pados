# SPDX-License-Identifier: AGPL-3.0-only
"""Text-PDF parsing for corpus, graph URL and enterprise ingestion.

Untrusted PDFs are parsed only in the bounded isolated worker (``pdf_isolation``, P2-11); this module
keeps the ``ParsedPdf`` contract and rebuilds pages, sections and offsets from the worker reply.
"""
from __future__ import annotations

from dataclasses import dataclass
from importlib.util import find_spec
from pathlib import Path
from typing import Any

from .ingestion import normalize_text
from .pdf_isolation import (
    FAILURE_CODES,
    PDF_PARSE_BUDGET,
    PDF_PARSE_WORKER_FAILED,
    PDF_SOURCE_TOO_LARGE,
    PDF_SOURCE_UNREADABLE,
    PdfParseBudget,
    run_pdf_parse,
)


@dataclass(frozen=True)
class ParsedPage:
    page_number: int
    text: str
    normalized_text: str
    section: str | None
    char_start: int
    char_end: int


@dataclass(frozen=True)
class ParsedPdf:
    title: str | None
    authors: list[str]
    publication_year: int | None
    venue: str | None
    doi: str | None
    language: str | None
    pages: list[ParsedPage]
    parser_version: str
    warnings: list[str]
    extraction_status: str


def _section_for(text: str, current: str | None) -> str | None:
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if 2 <= len(first_line) <= 120 and (first_line.isupper() or first_line[:1].isdigit()):
        return first_line
    return current


def _require_pypdf() -> None:
    if find_spec("pypdf") is None:  # located, never imported: pypdf only runs in the isolated worker
        raise RuntimeError("pypdf is required for text-PDF ingestion")


def _failed(code: str, status: str = "PARSE_FAILED") -> ParsedPdf:
    return ParsedPdf(None, [], None, None, None, None, [], "pypdf", [code], status)


def failure_code(parsed: ParsedPdf) -> str | None:
    """The closed failure code of a failed parse, or ``None`` (parsed, OCR required or encrypted)."""
    if parsed.extraction_status != "PARSE_FAILED":
        return None
    code = parsed.warnings[0] if parsed.warnings else None
    return code if code in FAILURE_CODES else PDF_PARSE_WORKER_FAILED


def _read_bounded(path: Path, limit: int) -> bytes | str:
    try:
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
    except OSError:
        return PDF_SOURCE_UNREADABLE
    return PDF_SOURCE_TOO_LARGE if len(data) > limit else data


def _optional_text(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise ValueError("text")
    return value


def _from_result(result: dict[str, Any], budget: PdfParseBudget) -> ParsedPdf:
    """Rebuild the unchanged ``ParsedPdf`` contract from the worker's validated reply."""
    if result.get("encrypted") is True:
        return ParsedPdf(None, [], None, None, None, None, [], "pypdf", ["ENCRYPTED_PDF"], "UNSUPPORTED")
    texts = result.get("pages")
    if (result.get("encrypted") is not False or not isinstance(texts, list) or len(texts) > budget.max_pages
            or not all(isinstance(text, str) for text in texts)):
        raise ValueError("pages")
    title = _optional_text(result.get("title"))
    author_value = _optional_text(result.get("author"))
    authors = [item.strip() for item in (author_value or "").replace(";", ",").split(",") if item.strip()]
    pages: list[ParsedPage] = []
    offset = 0
    section: str | None = None
    for page_number, text in enumerate(texts, start=1):
        normalized = normalize_text(text)
        section = _section_for(text, section)
        pages.append(ParsedPage(page_number, text, normalized, section, offset, offset + len(normalized)))
        offset += len(normalized) + 1
    warnings: list[str] = []
    if not pages or not any(page.normalized_text for page in pages):
        warnings.append("OCR_REQUIRED")
        status = "OCR_REQUIRED"
    else:
        status = "PARSED"
    return ParsedPdf(title, authors, None, None, None, None, pages, "pypdf", warnings, status)


def parse_pdf_bytes(data: bytes, *, budget: PdfParseBudget = PDF_PARSE_BUDGET) -> ParsedPdf:
    """Parse untrusted PDF bytes in the bounded isolated worker (P2-11); pypdf never runs in this process.

    A failure is ``PARSE_FAILED`` with exactly one closed ``PDF_*`` code as its warning (``failure_code``);
    no exception text, path or document content is carried.
    """
    _require_pypdf()
    outcome = run_pdf_parse(data, budget)
    if outcome.code is not None or outcome.result is None:
        return _failed(outcome.code or PDF_PARSE_WORKER_FAILED)
    try:
        return _from_result(outcome.result, budget)
    except ValueError:
        return _failed(PDF_PARSE_WORKER_FAILED)


def parse_pdf(path: Path, *, budget: PdfParseBudget = PDF_PARSE_BUDGET) -> ParsedPdf:
    """Parse the PDF at ``path`` in the isolated worker (the bytes are read here, bounded, and piped)."""
    _require_pypdf()
    data = _read_bounded(path, budget.max_input_bytes)
    if isinstance(data, str):
        return _failed(data)
    return parse_pdf_bytes(data, budget=budget)

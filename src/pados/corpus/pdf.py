# SPDX-License-Identifier: AGPL-3.0-only
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .ingestion import normalize_text


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


def _metadata_value(metadata: Any, key: str) -> str | None:
    if metadata is None:
        return None
    value = metadata.get(key) if hasattr(metadata, "get") else getattr(metadata, key, None)
    value = value or getattr(metadata, key, None)
    return str(value).strip() if value else None


def _section_for(text: str, current: str | None) -> str | None:
    first_line = next((line.strip() for line in text.splitlines() if line.strip()), "")
    if 2 <= len(first_line) <= 120 and (first_line.isupper() or first_line[:1].isdigit()):
        return first_line
    return current


def parse_pdf(path: Path) -> ParsedPdf:
    try:
        from pypdf import PdfReader
        from pypdf.errors import PdfReadError
    except ImportError as exc:
        raise RuntimeError("pypdf is required for text-PDF ingestion") from exc

    try:
        reader = PdfReader(str(path), strict=False)
        if reader.is_encrypted:
            return ParsedPdf(None, [], None, None, None, None, [], "pypdf", ["ENCRYPTED_PDF"], "UNSUPPORTED")
        metadata = reader.metadata
        title = _metadata_value(metadata, "title")
        author_value = _metadata_value(metadata, "author")
        authors = [item.strip() for item in (author_value or "").replace(";", ",").split(",") if item.strip()]
        pages: list[ParsedPage] = []
        offset = 0
        section: str | None = None
        for page_number, page in enumerate(reader.pages, start=1):
            text = page.extract_text() or ""
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
    except PdfReadError as exc:
        return ParsedPdf(None, [], None, None, None, None, [], "pypdf", [f"MALFORMED_PDF: {exc}"], "PARSE_FAILED")
    except Exception as exc:  # noqa: BLE001 - untrusted PDF parser isolation; failure is data
        return ParsedPdf(None, [], None, None, None, None, [], "pypdf", [f"PARSE_ERROR: {type(exc).__name__}"], "PARSE_FAILED")

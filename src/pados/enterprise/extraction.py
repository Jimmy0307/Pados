# SPDX-License-Identifier: AGPL-3.0-only
"""Generic document extraction interface.

No OCR engine is authoritative. Extractors return normalized artifacts with
page/region provenance, extractor identity/version, confidence where known and
the source hash. An unconfigured OCR lane fails closed; it never pretends to
have read an image.
"""
from __future__ import annotations

import hashlib
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from pados.corpus.ingestion import normalize_text
from pados.enterprise.manifest import CapabilityHealth


@dataclass(frozen=True)
class ExtractedRegion:
    page_number: int
    text: str
    normalized_text: str
    region: str | None = None
    confidence: float | None = None
    section: str | None = None


@dataclass(frozen=True)
class ExtractionResult:
    source_hash: str
    media_type: str
    extractor_id: str
    extractor_version: str
    pages: tuple[ExtractedRegion, ...]
    warnings: tuple[str, ...] = ()

    @property
    def text(self) -> str:
        return "\n".join(page.normalized_text for page in self.pages)

    @property
    def normalized_hash(self) -> str:
        return hashlib.sha256(self.text.encode("utf-8")).hexdigest()


class ExtractionUnavailable(RuntimeError):
    """Raised when no configured extractor can honestly read the input."""


class DocumentExtractor(Protocol):
    extractor_id: str
    extractor_version: str
    media_types: frozenset[str]

    def health(self) -> CapabilityHealth: ...

    def extract(self, content: bytes, *, media_type: str) -> ExtractionResult: ...


class PlainTextExtractor:
    extractor_id = "pados.text"
    extractor_version = "1"
    media_types = frozenset({"text/plain", "text/markdown"})

    def health(self) -> CapabilityHealth:
        return CapabilityHealth.READY

    def extract(self, content: bytes, *, media_type: str) -> ExtractionResult:
        try:
            text = content.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ExtractionUnavailable("TEXT_NOT_UTF8") from exc
        return ExtractionResult(
            source_hash=hashlib.sha256(content).hexdigest(), media_type=media_type,
            extractor_id=self.extractor_id, extractor_version=self.extractor_version,
            pages=(ExtractedRegion(1, text, normalize_text(text), confidence=1.0),),
        )


class PdfTextExtractor:
    """Text-layer PDF extraction through the existing ``pados.corpus.pdf`` parser."""

    extractor_id = "pados.pdf-text"
    extractor_version = "pypdf-1"
    media_types = frozenset({"application/pdf"})

    def health(self) -> CapabilityHealth:
        try:
            import pypdf  # noqa: F401
        except ImportError:
            return CapabilityHealth.UNAVAILABLE
        return CapabilityHealth.READY

    def extract(self, content: bytes, *, media_type: str) -> ExtractionResult:
        from pados.corpus.pdf import parse_pdf

        with tempfile.TemporaryDirectory(prefix="pados-extract-") as tmp:
            path = Path(tmp) / "source.pdf"
            path.write_bytes(content)
            parsed = parse_pdf(path)
        pages = tuple(
            ExtractedRegion(page.page_number, page.text, page.normalized_text, section=page.section)
            for page in parsed.pages if page.normalized_text
        )
        if not pages:
            # Image-only or unreadable: an OCR extractor must handle it, if configured.
            raise ExtractionUnavailable("PDF_TEXT_LAYER_EMPTY_OCR_REQUIRED")
        return ExtractionResult(
            source_hash=hashlib.sha256(content).hexdigest(), media_type=media_type,
            extractor_id=self.extractor_id, extractor_version=f"{self.extractor_version}:{parsed.parser_version}",
            pages=pages, warnings=tuple(parsed.warnings),
        )


class UnconfiguredOcrExtractor:
    """Placeholder lane for images: reports NOT_CONFIGURED and refuses to extract."""

    extractor_id = "pados.ocr.unconfigured"
    extractor_version = "0"
    media_types = frozenset({"image/png", "image/jpeg", "image/tiff"})

    def health(self) -> CapabilityHealth:
        return CapabilityHealth.NOT_CONFIGURED

    def extract(self, content: bytes, *, media_type: str) -> ExtractionResult:
        raise ExtractionUnavailable("OCR_NOT_CONFIGURED")


class ExtractorSet:
    """Media-type dispatch over registered extractors (not a provider router)."""

    def __init__(self, extractors: Sequence[DocumentExtractor]) -> None:
        self._by_type: dict[str, DocumentExtractor] = {}
        for extractor in extractors:
            for media_type in extractor.media_types:
                if media_type in self._by_type:
                    raise ValueError("DUPLICATE_EXTRACTOR_FOR_MEDIA_TYPE")
                self._by_type[media_type] = extractor

    def extract(self, content: bytes, *, media_type: str) -> ExtractionResult:
        extractor = self._by_type.get(media_type)
        if extractor is None:
            raise ExtractionUnavailable("MEDIA_TYPE_UNSUPPORTED")
        if extractor.health() is not CapabilityHealth.READY:
            raise ExtractionUnavailable(f"EXTRACTOR_{extractor.health().value}")
        return extractor.extract(content, media_type=media_type)

    def health(self) -> Mapping[str, str]:
        return {media: extractor.health().value for media, extractor in sorted(self._by_type.items())}

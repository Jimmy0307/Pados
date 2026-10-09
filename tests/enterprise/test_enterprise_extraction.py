import hashlib

import pytest

from pados.enterprise.extraction import (
    ExtractionUnavailable,
    ExtractorSet,
    PdfTextExtractor,
    PlainTextExtractor,
    UnconfiguredOcrExtractor,
)


def _pdf(text: str) -> bytes:
    stream = f"BT /F1 12 Tf 72 720 Td ({text}) Tj ET".encode()
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>", b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
                b"/Resources << /Font << /F1 5 0 R >> >> >>"),
               b"<< /Length " + str(len(stream)).encode() + b" >>\nstream\n" + stream + b"\nendstream",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += f"trailer\n<< /Size {len(objects) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def test_text_extraction_is_normalized_with_provenance_and_source_hash():
    content = "Synthetic  text­with   spacing".encode()
    result = ExtractorSet([PlainTextExtractor()]).extract(content, media_type="text/plain")
    assert result.source_hash == hashlib.sha256(content).hexdigest()
    assert result.extractor_id == "pados.text" and result.extractor_version == "1"
    assert result.pages[0].page_number == 1 and result.pages[0].confidence == 1.0
    assert result.text == "Synthetic textwith spacing"


def test_pdf_text_layer_extraction_through_existing_pados_parser():
    content = _pdf("Synthetic pump inspection manual page")
    extractor = PdfTextExtractor()
    assert extractor.health().value == "READY"
    result = extractor.extract(content, media_type="application/pdf")
    assert "Synthetic pump inspection manual page" in result.text
    assert result.pages[0].page_number == 1 and result.extractor_version.startswith("pypdf-1:")
    assert result.source_hash == hashlib.sha256(content).hexdigest()


def test_image_without_configured_ocr_fails_closed():
    extractors = ExtractorSet([PlainTextExtractor(), UnconfiguredOcrExtractor()])
    assert extractors.health()["image/png"] == "NOT_CONFIGURED"
    with pytest.raises(ExtractionUnavailable, match="EXTRACTOR_NOT_CONFIGURED"):
        extractors.extract(b"\x89PNG", media_type="image/png")
    with pytest.raises(ExtractionUnavailable, match="MEDIA_TYPE_UNSUPPORTED"):
        extractors.extract(b"x", media_type="application/vnd.ms-excel")
    with pytest.raises(ExtractionUnavailable, match="TEXT_NOT_UTF8"):
        extractors.extract(b"\xff\xfe\x00", media_type="text/plain")
    with pytest.raises(ValueError, match="DUPLICATE_EXTRACTOR"):
        ExtractorSet([PlainTextExtractor(), PlainTextExtractor()])


def test_empty_pdf_text_layer_requires_ocr_instead_of_pretending(tmp_path):
    with pytest.raises(ExtractionUnavailable, match="OCR_REQUIRED"):
        PdfTextExtractor().extract(_pdf(""), media_type="application/pdf")

from __future__ import annotations

from pathlib import Path

import pytest
from pypdf import PdfWriter

from pados.corpus.ingestion import (
    IngestionState,
    cosine_similarity,
    deterministic_embedding,
    transition,
)
from pados.corpus.pdf import ParsedPage, ParsedPdf, parse_pdf
from pados.corpus.pipeline import run_fixture_pilot
from pados.corpus.security import CorpusBoundaryError, discover_pdf_files, resolve_approved_root
from pados.corpus.service import score_text


def write_pdf(path: Path, *, encrypted: bool = False) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    if encrypted:
        writer.encrypt("fixture")
    with path.open("wb") as handle:
        writer.write(handle)


def test_corpus_source_surface_is_scoped_to_corpus_modules() -> None:
    assert not Path(__file__).parents[2].joinpath("src", "pados", "project_audit.py").exists()


def test_approved_root_traversal_and_cap(tmp_path: Path) -> None:
    with pytest.raises(CorpusBoundaryError):
        resolve_approved_root("relative-fixture")
    root = tmp_path / "papers"
    root.mkdir()
    with pytest.raises(CorpusBoundaryError):
        from pados.corpus.security import resolve_child_path
        resolve_child_path(root, "../outside.pdf")
    for index in range(101):
        write_pdf(root / f"paper-{index:03}.pdf")
    assert len(discover_pdf_files(root)) == 100
    with pytest.raises(CorpusBoundaryError):
        discover_pdf_files(root, max_documents=101)


def test_pdf_empty_encrypted_and_malformed_states(tmp_path: Path) -> None:
    empty = tmp_path / "empty.pdf"
    write_pdf(empty)
    assert parse_pdf(empty).extraction_status == "OCR_REQUIRED"
    encrypted = tmp_path / "encrypted.pdf"
    write_pdf(encrypted, encrypted=True)
    assert parse_pdf(encrypted).extraction_status == "UNSUPPORTED"
    malformed = tmp_path / "malformed.pdf"
    malformed.write_bytes(b"not a pdf")
    assert parse_pdf(malformed).extraction_status == "PARSE_FAILED"


def test_hash_duplicate_page_preservation_and_evidence_anchor(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "papers"
    root.mkdir()
    (root / "a.pdf").write_bytes(b"same")
    (root / "b.pdf").write_bytes(b"same")
    fake_pages = [
        ParsedPage(1, "INTRO\nA finding", "INTRO A finding", "INTRO", 0, 15),
        ParsedPage(2, "METHOD\nA method", "METHOD A method", "METHOD", 16, 28),
    ]
    parsed = ParsedPdf("Fixture paper", ["Author"], 2024, "Venue", "10.1/fixture", "en", fake_pages, "fixture", [], "PARSED")
    monkeypatch.setattr("pados.corpus.pipeline.parse_pdf", lambda path: parsed)
    result = run_fixture_pilot(root)
    assert result["receipt"]["accepted"] == 1
    assert result["receipt"]["duplicate"] == 1
    accepted = next(item for item in result["documents"] if item["status"] == "PUBLISHED")
    assert accepted["pages"] == 2
    assert accepted["chunks"][0]["page_id"]
    assert result["receipt"]["evidence_anchor_validation"] == "PASS"


def test_ingestion_state_machine_and_bounded_search_modes() -> None:
    assert transition(None, IngestionState.DISCOVERED).to_state == IngestionState.DISCOVERED
    assert transition(IngestionState.DISCOVERED, IngestionState.REGISTERED).to_state == IngestionState.REGISTERED
    with pytest.raises(ValueError):
        transition(IngestionState.DISCOVERED, IngestionState.EMBEDDED)
    assert score_text("methods", "The methods and sample", "keyword") == 1.0
    assert score_text("methods sample", "methods and sample", "semantic") > 0
    assert score_text("methods", "methods and sample", "hybrid") > 0
    assert len(deterministic_embedding("stable text")) == 32
    assert cosine_similarity(deterministic_embedding("stable text"), deterministic_embedding("stable text")) == pytest.approx(1.0)

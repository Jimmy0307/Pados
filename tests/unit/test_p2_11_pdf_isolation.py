"""P2-11: untrusted PDFs are parsed only in the bounded isolated worker, never in the API process.

Every pathological PDF here is generated in-test (no binary fixture). Each case asserts that the calling
(API) process survives, the outcome is a closed ``PDF_*`` code and nothing partial is admitted.
"""
from __future__ import annotations

import ast
import io
import json
import subprocess
import sys
import time
import zlib
from dataclasses import replace
from pathlib import Path
from typing import Any

import httpx
import psutil
import pytest
from pypdf import PdfReader, PdfWriter

from pados.corpus import pdf_isolation
from pados.corpus import pdf as corpus_pdf
from pados.corpus._pdf_parse_worker import WORKER_CODES
from pados.corpus.pdf import _section_for, failure_code, parse_pdf, parse_pdf_bytes
from pados.corpus.pdf_isolation import (
    FAILURE_CODES,
    PDF_PARSE_BUDGET,
    PdfParseBudget,
    PdfParseOutcome,
)
from pados.corpus.pipeline import run_fixture_pilot
from pados.enterprise.extraction import ExtractionUnavailable, PdfTextExtractor
from pados.graph import url_ingest

ROOT = Path(__file__).resolve().parents[2]
MIB = 1024 * 1024
PUBLIC_ADDRESS = "93.184.216.34"  # a public documentation-style address; never contacted (mock transport)


# --- in-test PDF construction ------------------------------------------------------------------------------

def _stream(data: bytes, extra: bytes = b"") -> bytes:
    return b"<< /Length " + str(len(data)).encode() + extra + b" >>\nstream\n" + data + b"\nendstream"


def build_pdf(objects: list[bytes], *, root: int = 1, trailer_extra: bytes = b"") -> bytes:
    out, offsets = bytearray(b"%PDF-1.4\n"), []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += f"{number} 0 obj\n".encode() + body + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objects) + 1}\n0000000000 65535 f \n".encode()
    out += b"".join(f"{offset:010d} 00000 n \n".encode() for offset in offsets)
    out += (f"trailer\n<< /Size {len(objects) + 1} /Root {root} 0 R ".encode() + trailer_extra
            + f">>\nstartxref\n{xref}\n%%EOF\n".encode())
    return bytes(out)


def one_page(content: bytes, *, count: int = 1, content_extra: bytes = b"") -> bytes:
    return build_pdf([
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count " + str(count).encode() + b" >>",
        (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 4 0 R "
         b"/Resources << /Font << /F1 5 0 R >> >> >>"),
        _stream(content, content_extra),
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ])


def text_pdf(pages: list[str], *, title: str | None = None, author: str | None = None) -> bytes:
    """A multi-page text PDF (one Tj line per text line) with optional Info metadata."""
    objects: list[bytes] = [b"<< /Type /Catalog /Pages 2 0 R >>", b""]
    kids: list[int] = []
    for text in pages:
        content = b"BT /F1 12 Tf " + b" ".join(
            b"1 0 0 1 72 " + str(720 - 14 * index).encode() + b" Tm (" + line.encode() + b") Tj"
            for index, line in enumerate(text.split("\n"))) + b" ET"
        page_number = len(objects) + 1
        objects.append(b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents "
                       + str(page_number + 1).encode() + b" 0 R /Resources << /Font << /F1 "
                       + str(page_number + 2).encode() + b" 0 R >> >> >>")
        objects.append(_stream(content))
        objects.append(b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>")
        kids.append(page_number)
    objects[1] = (b"<< /Type /Pages /Kids [" + b" ".join(f"{kid} 0 R".encode() for kid in kids)
                  + b"] /Count " + str(len(kids)).encode() + b" >>")
    info = b""
    if title is not None or author is not None:
        entries = (b"/Title (" + title.encode() + b") " if title else b"") + (
            b"/Author (" + author.encode() + b")" if author else b"")
        objects.append(b"<< " + entries + b" >>")
        info = b"/Info " + str(len(objects)).encode() + b" 0 R "
    return build_pdf(objects, trailer_extra=info)


def flate_bomb_pdf(decoded_bytes: int) -> bytes:
    """A page whose FlateDecode content stream inflates to ``decoded_bytes`` (compressed ~1000:1)."""
    compressor = zlib.compressobj(9)
    block = b"\x00" * MIB
    chunks = [compressor.compress(block) for _ in range(decoded_bytes // MIB)]
    chunks.append(compressor.flush())
    return one_page(b"".join(chunks), content_extra=b" /Filter /FlateDecode")


def slow_pdf(pages: int = 200, operators: int = 50_000) -> bytes:
    """Well-formed and ~3 KiB, but every page shares one 50k-operator content stream: each page stays under
    pypdf's per-stream token limit, yet extracting all of them takes minutes of CPU."""
    content = zlib.compress(b"BT /F1 12 Tf " + b"(x) Tj 1 0 Td " * operators + b"ET")
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [" + b" ".join(f"{5 + index} 0 R".encode() for index in range(pages))
               + b"] /Count " + str(pages).encode() + b" >>",
               _stream(content, b" /Filter /FlateDecode"),
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"]
    objects += [b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents 3 0 R "
                b"/Resources << /Font << /F1 4 0 R >> >> >>"] * pages
    return build_pdf(objects)


def _assert_closed_failure(parsed: Any, *expected: str) -> str:
    assert parsed.extraction_status == "PARSE_FAILED"
    assert parsed.pages == [] and parsed.title is None and parsed.authors == []  # nothing partial admitted
    code = failure_code(parsed)
    assert code is not None and parsed.warnings == [code] and code in FAILURE_CODES
    if expected:
        assert code in expected, code
    return code


def _no_worker_left() -> None:
    for child in psutil.Process().children(recursive=True):
        try:
            assert "pados.corpus._pdf_parse_worker" not in " ".join(child.cmdline()), child.pid
        except psutil.Error:
            continue


def _reference(data: bytes) -> tuple[str | None, list[str], list[str]]:
    """The former in-process parse (title, authors, page texts), run here on a TRUSTED in-test PDF."""
    reader = PdfReader(io.BytesIO(data), strict=False)
    metadata = reader.metadata
    title = metadata.title.strip() if metadata is not None and metadata.title else None
    author = metadata.author if metadata is not None and metadata.author else ""
    authors = [item.strip() for item in author.replace(";", ",").split(",") if item.strip()]
    return title, authors, [page.extract_text() or "" for page in reader.pages]


# --- well-formed PDFs: the contract and results are unchanged ---------------------------------------------

def test_well_formed_pdf_matches_the_former_in_process_parse(tmp_path: Path) -> None:
    data = text_pdf(["INTRODUCTION\nPumps move fluid.", "2 METHOD\nWe measured flow.", "Results follow here."],
                    title="  Synthetic Pump Study ", author="Ada Example; Bo Example")
    path = tmp_path / "paper.pdf"
    path.write_bytes(data)
    parsed = parse_pdf(path)
    title, authors, texts = _reference(data)
    assert parsed.extraction_status == "PARSED" and parsed.warnings == [] and parsed.parser_version == "pypdf"
    assert parsed.title == title == "Synthetic Pump Study"
    assert parsed.authors == authors == ["Ada Example", "Bo Example"]
    assert [page.text for page in parsed.pages] == texts
    assert [page.page_number for page in parsed.pages] == [1, 2, 3]
    assert [page.section for page in parsed.pages] == ["INTRODUCTION", "2 METHOD", "2 METHOD"]
    assert _section_for(texts[2], "2 METHOD") == "2 METHOD"
    offset = 0
    for page in parsed.pages:
        assert (page.char_start, page.char_end) == (offset, offset + len(page.normalized_text))
        offset = page.char_end + 1
    assert parse_pdf_bytes(data) == parsed


def test_encrypted_and_image_only_pdfs_keep_their_states(tmp_path: Path) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=72, height=72)
    buffer = io.BytesIO()
    writer.write(buffer)
    assert parse_pdf_bytes(buffer.getvalue()).extraction_status == "OCR_REQUIRED"
    writer.encrypt("fixture")
    encrypted = io.BytesIO()
    writer.write(encrypted)
    parsed = parse_pdf_bytes(encrypted.getvalue())
    assert (parsed.extraction_status, parsed.warnings, parsed.pages) == ("UNSUPPORTED", ["ENCRYPTED_PDF"], [])


# --- pathological PDFs: the API process survives, the code is closed, nothing is admitted --------------------

def test_decompression_bomb_is_refused_by_the_decode_budget() -> None:
    bomb = flate_bomb_pdf(160 * MIB)  # ~160 KiB on the wire; inflates past the 64 MiB per-stream budget
    assert len(bomb) < 1 * MIB
    started = time.monotonic()
    parsed = parse_pdf_bytes(bomb)
    _assert_closed_failure(parsed, "PDF_DECODE_BUDGET_EXCEEDED")
    assert time.monotonic() - started < PDF_PARSE_BUDGET.wall_seconds
    _no_worker_left()
    assert parse_pdf_bytes(one_page(b"BT /F1 12 Tf 72 720 Td (still alive) Tj ET")).extraction_status == "PARSED"


def test_decode_budget_is_configurable_per_call() -> None:
    budget = replace(PDF_PARSE_BUDGET, max_stream_bytes=1 * MIB, max_content_bytes=2 * MIB)
    _assert_closed_failure(parse_pdf_bytes(flate_bomb_pdf(4 * MIB), budget=budget), "PDF_DECODE_BUDGET_EXCEEDED")


def test_memory_watchdog_or_rlimit_stops_a_worker_that_outgrows_its_budget() -> None:
    # 60 MiB decodes within the 64 MiB per-stream budget but not within a 64 MiB memory budget: Linux stops it
    # with RLIMIT_AS (MemoryError in the worker), Windows with the polled resident-memory watchdog.
    budget = replace(PDF_PARSE_BUDGET, memory_bytes=64 * MIB, wall_seconds=60.0)
    parsed = parse_pdf_bytes(flate_bomb_pdf(60 * MIB), budget=budget)
    # pypdf may convert a MemoryError raised deep in a filter into its own exception (a closed parse error).
    allowed = {"PDF_PARSE_MEMORY_EXCEEDED"} if sys.platform == "win32" else {"PDF_PARSE_MEMORY_EXCEEDED",
                                                                             "PDF_PARSE_ERROR"}
    _assert_closed_failure(parsed, *allowed)
    _no_worker_left()


def test_cyclic_page_tree_and_reference_loops_fail_closed() -> None:
    cyclic_tree = build_pdf([b"<< /Type /Catalog /Pages 2 0 R >>",
                             b"<< /Type /Pages /Kids [2 0 R 3 0 R] /Count 2 >>",
                             b"<< /Type /Pages /Parent 2 0 R /Kids [2 0 R 3 0 R] /Count 1 >>"])
    reference_loop = build_pdf([b"<< /Type /Catalog /Pages 2 0 R >>",
                                b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                                b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 9 9] /Contents 4 0 R >>",
                                b"5 0 R", b"4 0 R"])
    for data in (cyclic_tree, reference_loop):
        parsed = parse_pdf_bytes(data, budget=replace(PDF_PARSE_BUDGET, wall_seconds=30.0))
        _assert_closed_failure(parsed, "PDF_MALFORMED", "PDF_PARSE_ERROR")
    _no_worker_left()


def test_deeply_nested_objects_fail_closed() -> None:
    depth = 200_000
    deep = build_pdf([b"<< /Type /Catalog /Pages 2 0 R /Deep 4 0 R >>",
                      b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
                      (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 9 9] /Resources << /X " + b"[" * depth
                       + b"]" * depth + b" >> >>"),
                      b"[" * depth + b"]" * depth])
    parsed = parse_pdf_bytes(deep, budget=replace(PDF_PARSE_BUDGET, wall_seconds=30.0))
    if parsed.extraction_status != "PARSE_FAILED":
        assert parsed.extraction_status == "OCR_REQUIRED" and all(not p.normalized_text for p in parsed.pages)
    else:
        _assert_closed_failure(parsed)
    _no_worker_left()


def test_enormous_declared_page_count_is_refused_before_flattening() -> None:
    data = one_page(b"BT /F1 12 Tf 72 720 Td (one real page) Tj ET", count=1_000_000_000)
    _assert_closed_failure(parse_pdf_bytes(data), "PDF_PAGE_COUNT_EXCEEDED")


def test_real_page_count_above_the_budget_is_refused() -> None:
    writer = PdfWriter()
    for _ in range(6):
        writer.add_blank_page(width=72, height=72)
    buffer = io.BytesIO()
    writer.write(buffer)
    budget = replace(PDF_PARSE_BUDGET, max_pages=5)
    _assert_closed_failure(parse_pdf_bytes(buffer.getvalue(), budget=budget), "PDF_PAGE_COUNT_EXCEEDED")
    assert parse_pdf_bytes(buffer.getvalue(), budget=replace(budget, max_pages=6)).extraction_status == "OCR_REQUIRED"


def test_text_budget_is_enforced_in_the_worker() -> None:
    data = text_pdf(["A" * 2000])
    budget = replace(PDF_PARSE_BUDGET, max_page_text_bytes=1024, max_text_bytes=1024)
    _assert_closed_failure(parse_pdf_bytes(data, budget=budget), "PDF_TEXT_BUDGET_EXCEEDED")


@pytest.mark.parametrize("mutation", ["truncated", "garbage_xref", "not_a_pdf", "empty"])
def test_truncated_or_corrupt_xref_fails_closed_without_exception_text(mutation: str) -> None:
    good = text_pdf(["Recoverable text"])
    data = {"truncated": good[: len(good) // 3], "garbage_xref": good.replace(b"xref\n", b"xref\nZZZZ garbage\n"),
            "not_a_pdf": b"not a pdf", "empty": b""}[mutation]
    parsed = parse_pdf_bytes(data)
    if parsed.extraction_status == "PARSE_FAILED":
        _assert_closed_failure(parsed)
    else:  # pypdf's non-strict repair may recover the intact objects, exactly as it did in-process
        assert parsed.extraction_status in {"PARSED", "OCR_REQUIRED"}
        assert all(warning in {"OCR_REQUIRED"} for warning in parsed.warnings)
    assert not any(":" in warning or "Error" in warning for warning in parsed.warnings)


def test_slow_parse_is_killed_at_the_wall_clock_deadline() -> None:
    data = slow_pdf()
    assert len(data) < 64 * 1024
    budget = replace(PDF_PARSE_BUDGET, wall_seconds=2.0)
    started = time.monotonic()
    parsed = parse_pdf_bytes(data, budget=budget)
    elapsed = time.monotonic() - started
    _assert_closed_failure(parsed, "PDF_PARSE_TIMEOUT")
    assert elapsed < budget.wall_seconds + pdf_isolation.REAP_SECONDS
    _no_worker_left()


def test_oversized_source_is_refused_before_any_worker_starts(monkeypatch: pytest.MonkeyPatch) -> None:
    def no_spawn(*_args: Any, **_kwargs: Any) -> None:
        raise AssertionError("a worker was started")

    monkeypatch.setattr(pdf_isolation, "_start", no_spawn)
    budget = replace(PDF_PARSE_BUDGET, max_input_bytes=1024)
    _assert_closed_failure(parse_pdf_bytes(b"%PDF-1.4\n" + b"0" * 2048, budget=budget), "PDF_SOURCE_TOO_LARGE")


def test_unreadable_path_is_a_closed_code(tmp_path: Path) -> None:
    _assert_closed_failure(parse_pdf(tmp_path / "missing.pdf"), "PDF_SOURCE_UNREADABLE")


def test_worker_that_cannot_start_fails_closed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pdf_isolation, "_start", lambda _budget: None)
    _assert_closed_failure(parse_pdf_bytes(text_pdf(["x"])), "PDF_ISOLATION_UNAVAILABLE")


@pytest.mark.parametrize("stdout", [b"", b"not json", b'{"ok":false,"code":"/srv/upload/private.pdf: PdfReadError"}',
                                    b'{"ok":true,"result":[]}', b'{"ok":true,"result":{},"extra":1}'])
def test_reply_validation_never_passes_worker_text_through(stdout: bytes) -> None:
    outcome = pdf_isolation._decode(stdout)
    assert outcome == PdfParseOutcome("PDF_PARSE_WORKER_FAILED")


@pytest.mark.parametrize("result", [{"encrypted": False, "pages": "x"}, {"encrypted": False, "pages": [1]},
                                    {"encrypted": "no", "pages": []},
                                    {"encrypted": False, "pages": [], "title": 3}])
def test_malformed_worker_result_is_rejected(monkeypatch: pytest.MonkeyPatch, result: dict[str, Any]) -> None:
    monkeypatch.setattr(corpus_pdf, "run_pdf_parse", lambda _data, _budget: PdfParseOutcome(None, result))
    _assert_closed_failure(parse_pdf_bytes(b"%PDF"), "PDF_PARSE_WORKER_FAILED")


def test_worker_refuses_to_run_without_its_limits_token() -> None:
    run = subprocess.run([sys.executable, "-I", "-B", "-m", "pados.corpus._pdf_parse_worker"],
                         input=b"", capture_output=True, timeout=60, check=False)
    assert run.returncode == 117 and run.stdout == b""


def test_budget_is_validated() -> None:
    for bad in ({"wall_seconds": 0.0}, {"memory_bytes": 1}, {"max_pages": 0},
                {"max_page_text_bytes": 2 * MIB, "max_text_bytes": 1 * MIB}, {"wall_seconds": float("nan")}):
        with pytest.raises(ValueError, match="PDF_PARSE_BUDGET_INVALID"):
            PdfParseBudget(**bad)  # type: ignore[arg-type]


def test_every_worker_code_is_a_known_closed_code() -> None:
    assert WORKER_CODES <= FAILURE_CODES
    assert all(code.startswith("PDF_") and code.isupper() for code in FAILURE_CODES)


def test_worker_command_is_isolated_with_a_minimal_environment() -> None:
    command = pdf_isolation.worker_command(PDF_PARSE_BUDGET)
    assert command[:5] == [sys.executable, "-I", "-B", "-m", "pados.corpus._pdf_parse_worker"]
    assert set(pdf_isolation.worker_environment()) <= {"SYSTEMROOT"}


# --- every caller goes through the isolated route --------------------------------------------------------

def _pdf_transport(body: bytes) -> httpx.MockTransport:
    return httpx.MockTransport(lambda request: httpx.Response(
        200, headers={"content-type": "application/pdf"}, content=body))


def _resolver(*_args: Any, **_kwargs: Any) -> list[tuple[Any, ...]]:
    import socket

    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (PUBLIC_ADDRESS, 443))]


def _record_isolated_calls(monkeypatch: pytest.MonkeyPatch) -> list[PdfParseBudget]:
    calls: list[PdfParseBudget] = []
    real = corpus_pdf.run_pdf_parse

    def recording(data: bytes, budget: PdfParseBudget) -> PdfParseOutcome:
        calls.append(budget)
        return real(data, budget)

    monkeypatch.setattr(corpus_pdf, "run_pdf_parse", recording)
    return calls


def test_url_ingest_parses_pdfs_only_through_the_isolated_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record_isolated_calls(monkeypatch)
    body = text_pdf(["Fetched PDF body text"], title="Fetched Title")
    result = url_ingest.fetch_url("https://docs.example.org/paper.pdf", transport=_pdf_transport(body),
                                  resolver=_resolver)
    assert len(calls) == 1 and calls[0].max_input_bytes == url_ingest.MAX_RESPONSE_BYTES
    assert calls[0].wall_seconds <= url_ingest.PDF_PARSE_TIMEOUT_SECONDS
    assert result.retrieval_status == "FETCHED" and result.extracted_text == "Fetched PDF body text"
    assert result.title == "Fetched Title" and result.metadata["extraction_status"] == "PARSED"


def test_url_ingest_admits_nothing_from_a_hostile_pdf(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record_isolated_calls(monkeypatch)
    with pytest.raises(url_ingest.UrlIngestError) as raised:
        url_ingest.fetch_url("https://docs.example.org/bomb.pdf", transport=_pdf_transport(flate_bomb_pdf(160 * MIB)),
                             resolver=_resolver)
    assert raised.value.code == "PDF_EXTRACTION_FAILED" and raised.value.status == "FAILED"
    assert "bomb" not in raised.value.message and len(calls) == 1
    _no_worker_left()


def test_url_ingest_timeout_is_a_closed_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(corpus_pdf, "run_pdf_parse",
                        lambda _data, _budget: PdfParseOutcome("PDF_PARSE_TIMEOUT"))
    with pytest.raises(url_ingest.UrlIngestError) as raised:
        url_ingest.extract_pdf(text_pdf(["x"]))
    assert raised.value.code == "PDF_EXTRACTION_TIMEOUT"


def test_url_ingest_keeps_the_malformed_outcome_with_a_closed_warning() -> None:
    extracted = url_ingest.extract_pdf(b"not a pdf")
    assert extracted["status"] == "EXTRACTION_PARTIAL" and extracted["text"] == ""
    assert extracted["metadata"]["warnings"] == ["PDF_MALFORMED"]


def test_enterprise_extraction_goes_through_the_isolated_worker(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record_isolated_calls(monkeypatch)
    result = PdfTextExtractor().extract(text_pdf(["Enterprise manual text"]), media_type="application/pdf")
    assert "Enterprise manual text" in result.text and len(calls) == 1
    with pytest.raises(ExtractionUnavailable, match="^PDF_DECODE_BUDGET_EXCEEDED$"):
        PdfTextExtractor().extract(flate_bomb_pdf(160 * MIB), media_type="application/pdf")


def test_corpus_pipeline_publishes_a_well_formed_pdf_through_the_isolated_worker(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record_isolated_calls(monkeypatch)
    root = tmp_path / "papers"
    root.mkdir()
    (root / "paper.pdf").write_bytes(text_pdf(["INTRODUCTION\nPumps move fluid.", "Second page text."]))
    result = run_fixture_pilot(root, embedding_policy="KEYWORD_ONLY")
    (document,) = result["documents"]
    assert document["status"] == "PUBLISHED" and document["pages"] == 2 and len(calls) == 1
    assert result["receipt"]["accepted"] == 1 and result["receipt"]["evidence_anchor_validation"] == "PASS"


def test_corpus_pipeline_quarantines_a_hostile_pdf_with_a_closed_code(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls = _record_isolated_calls(monkeypatch)
    root = tmp_path / "papers"
    root.mkdir()
    (root / "bomb.pdf").write_bytes(flate_bomb_pdf(160 * MIB))
    result = run_fixture_pilot(root)
    (document,) = result["documents"]
    assert document["status"] == "FAILED_MALFORMED" and document["warnings"] == ["PDF_DECODE_BUDGET_EXCEEDED"]
    assert "chunks" not in document and len(calls) == 1
    # The parse outcome carries no path or parser text (the batch-level approved "root" is pre-existing).
    assert tmp_path.as_posix() not in json.dumps(document).replace("\\\\", "/")
    assert [event["detail"] for event in document["events"]][-1] == "PDF_DECODE_BUDGET_EXCEEDED"


def test_the_api_process_never_imports_pypdf_when_parsing() -> None:
    """In a fresh interpreter, all three callers parse a PDF and pypdf is never loaded into that process."""
    code = (
        "import sys\n"
        "from pados.corpus.pdf import parse_pdf_bytes\n"
        "from pados.enterprise.extraction import PdfTextExtractor\n"
        "from pados.graph import url_ingest\n"
        f"data = {text_pdf(['Isolation probe text'])!r}\n"
        "assert parse_pdf_bytes(data).extraction_status == 'PARSED'\n"
        "assert 'Isolation probe text' in url_ingest.extract_pdf(data)['text']\n"
        "assert PdfTextExtractor().health().value == 'READY'\n"
        "assert 'Isolation probe text' in PdfTextExtractor().extract(data, media_type='application/pdf').text\n"
        "loaded = sorted(name for name in sys.modules if name == 'pypdf' or name.startswith('pypdf.'))\n"
        "print(loaded)\n"
        "raise SystemExit(1 if loaded else 0)\n"
    )
    run = subprocess.run([sys.executable, "-c", code], capture_output=True, timeout=120, check=False, cwd=ROOT)
    assert run.returncode == 0, run.stdout + run.stderr


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8-sig"))):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


def test_only_the_isolated_workers_import_pypdf() -> None:
    workers = {Path("src/pados/corpus/_pdf_parse_worker.py"), Path("src/pados/packs/ocr/_pdf_worker.py")}
    offenders = sorted(str(path.relative_to(ROOT)) for path in (ROOT / "src" / "pados").rglob("*.py")
                       if path.relative_to(ROOT) not in workers
                       and ("pypdf" in _imports(path) or "PdfReader(" in path.read_text(encoding="utf-8-sig")))
    assert offenders == []


# --- Review follow-ups: a transient outcome is retryable, never "malformed", and a backlog answers BUSY quickly ---


def test_transient_codes_are_resource_codes_and_never_content_codes() -> None:
    assert pdf_isolation.TRANSIENT_FAILURE_CODES <= pdf_isolation.RESOURCE_FAILURE_CODES
    assert not pdf_isolation.TRANSIENT_FAILURE_CODES & pdf_isolation.CONTENT_FAILURE_CODES


@pytest.mark.parametrize("code", sorted(pdf_isolation.TRANSIENT_FAILURE_CODES))
def test_corpus_pipeline_never_records_a_transient_failure_as_malformed_or_duplicate(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, code: str) -> None:
    """Two good documents parsed while the worker is busy (or cut short) stay retryable (UNSUPPORTED).

    Before: both became FAILED_MALFORMED, and the second could even be marked DUPLICATE of the first, because a
    failed parse has empty text and reached the normalized-text duplicate check.
    """
    monkeypatch.setattr(corpus_pdf, "run_pdf_parse", lambda _data, _budget: PdfParseOutcome(code))
    root = tmp_path / "papers"
    root.mkdir()
    (root / "a.pdf").write_bytes(text_pdf(["Alpha text."]))
    (root / "b.pdf").write_bytes(text_pdf(["Beta text."]))
    result = run_fixture_pilot(root)
    assert [document["status"] for document in result["documents"]] == ["UNSUPPORTED", "UNSUPPORTED"]
    for document in result["documents"]:
        assert document["warnings"] == [code] and document["events"][-1]["detail"] == code
        assert "chunks" not in document and "duplicate_of" not in document


def test_a_full_worker_pool_answers_busy_after_the_short_queue_wait(monkeypatch: pytest.MonkeyPatch) -> None:
    """A caller queues for a slot at most QUEUE_WAIT_SECONDS, not for the whole parse budget."""
    monkeypatch.setattr(pdf_isolation, "QUEUE_WAIT_SECONDS", 0.3)
    monkeypatch.setattr(pdf_isolation, "_start", lambda _budget: pytest.fail("no worker may start while busy"))
    held = [pdf_isolation._SLOTS.acquire(timeout=5) for _ in range(pdf_isolation.MAX_CONCURRENT_WORKERS)]
    try:
        assert all(held)
        started = time.monotonic()
        outcome = pdf_isolation.run_pdf_parse(text_pdf(["x"]), replace(PDF_PARSE_BUDGET, wall_seconds=60))
        assert outcome == PdfParseOutcome(pdf_isolation.PDF_PARSE_BUSY)
        assert time.monotonic() - started < 5
    finally:
        for _ in held:
            pdf_isolation._SLOTS.release()

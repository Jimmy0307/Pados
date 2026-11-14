# SPDX-License-Identifier: AGPL-3.0-only
"""The full chain from PDF bytes: OCR-pack stages (isolated pypdf text layer) -> retrieval -> evidence.

POSIX only: the OCR pack's isolated worker fails closed on win32 (no Job Object containment yet, R19-3), so
these run on Linux CI and the VMware lane. They also measure text-layer fidelity against the corpus text,
multiple pages, a page without a text layer (it stays NEEDS_OCR; nothing is invented for it) and malformed
input (fails closed before retrieval).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest
from di_support import PROJECT, corpus, workbench

from pados.capability.executor import ExecutionStatus
from pados.capability.planner import PlanStatus
from pados.capability.receipts import VerificationStatus
from pados.enterprise.classification import DataClassification
from pados.packs.document_intelligence.wiring import task_type
from pados.packs.ocr.contracts import KIND_DOCUMENT, KIND_SOURCE, PDF_MEDIA_TYPE, OcrDocument, PageStatus
from pados.packs.problem_solving import pack as ps
from pados.packs.retrieval.lexical import passages

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="OCR isolation fails closed on win32 (R19-3)")

FIXTURES = Path(__file__).resolve().parents[2] / "fixtures"
QUERY = "NCR-2041 root cause containment corrective action"


def _run(tmp_path: Path, pdf: bytes, *, retrieval: str = "hybrid_rerank") -> tuple:
    bench = workbench(tmp_path)
    source = bench.store.put(pdf, media_type=PDF_MEDIA_TYPE, kind=KIND_SOURCE)
    bench.provenance.register(source.sha256, project_id=PROJECT, classification=DataClassification.CONFIDENTIAL)
    task = bench.evidence_task(source, task_type=task_type("pdf", retrieval),  # type: ignore[arg-type]
                               query=QUERY, case_id="case.ncr-2041", source_id="src.ncr-2041")
    plan, result = bench.run(task)
    return bench, plan, result


def test_a_text_pdf_runs_end_to_end_with_every_stage_verified(tmp_path: Path) -> None:
    bench, plan, result = _run(tmp_path, corpus.binder_pdf((corpus.case("case.ncr-2041"),)))
    assert plan.status is PlanStatus.FEASIBLE
    assert result.status is ExecutionStatus.COMPLETED, result.failure_code
    assert all(stage.verification is VerificationStatus.PASSED for stage in result.plan_receipt.stages)
    pack = ps.load_evidence_pack(bench.outputs(result, "evidence")[ps.KIND_EVIDENCE])
    assert ("root_cause", "resin_moisture_above_specification_after_the_hopper_dryer_failed") in {
        (item.subject, item.assertion) for item in pack.items}


def test_text_layer_fidelity_against_the_corpus_text(tmp_path: Path) -> None:
    cases = corpus.CASES[:4]
    bench, _, result = _run(tmp_path, corpus.binder_pdf(cases))
    assert result.status is ExecutionStatus.COMPLETED, result.failure_code
    document = OcrDocument.from_bytes(bench.outputs(result, "structure")[KIND_DOCUMENT])
    assert [page.page_number for page in document.pages] == [1, 2, 3, 4]
    extracted = " ".join(" ".join(unit.text.split()) for unit in passages(document))
    expected = [" ".join(text.split()) for _page, _key, text in corpus.binder_lines(cases)]
    missing = [text for text in expected if text not in extracted]
    assert missing == []  # every corpus paragraph is recovered verbatim (whitespace-normalized)


def test_a_page_without_a_text_layer_stays_needs_ocr(tmp_path: Path) -> None:
    bench, _, result = _run(tmp_path, (FIXTURES / "ocr" / "text_and_scan.pdf").read_bytes(), retrieval="lexical")
    if result.status is ExecutionStatus.COMPLETED:
        document = OcrDocument.from_bytes(bench.outputs(result, "structure")[KIND_DOCUMENT])
        statuses = {page.page_number: page.status for page in document.pages}
        assert statuses[2] is PageStatus.NEEDS_OCR and not document.pages[1].blocks
    else:  # no text matched the query: a closed retrieval code, never fabricated text
        assert result.failure_code is not None and result.failure_code.startswith(("retrieve:", "evidence:"))


@pytest.mark.parametrize("payload", [b"%PDF-1.4\n%garbage", b"not a pdf at all"])
def test_a_malformed_pdf_fails_closed_before_retrieval(tmp_path: Path, payload: bytes) -> None:
    _, plan, result = _run(tmp_path, payload)
    assert plan.status is PlanStatus.FEASIBLE and result.status is ExecutionStatus.FAILED
    assert result.failure_code.split(":")[0] in {"preprocess", "recognize"}
    assert not any(stage.outputs for stage in result.plan_receipt.stages if stage.stage_id == "evidence")

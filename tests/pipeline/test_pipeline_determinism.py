"""Requirement 3: two full runs over fresh stores give byte-identical artifacts, identical plan hashes and
identical receipt envelope hashes. Time-bearing fields: the receipts' started_at / finished_at / duration_ms
(the fabric executor's injected clock, fixed here); algorithm wall-clock runtime is a stage metric only
(pinned by conftest.py; see the last test for what happens when it varies)."""
from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from pipeline_support import document_bytes, pipeline

from pados.capability.executor import ExecutionStatus
from pados.capability.receipts import VerificationStatus
from pados.packs.algorithms import adapters as algorithm_adapters


DOCUMENT_SHA256 = "a3eae41cd0519dc72db638e13d1bc603ca2be4683a947213265535602b1bed87"


def test_the_synthetic_document_is_a_pure_function_of_its_generator() -> None:
    first, second = document_bytes(), document_bytes()
    assert first == second and hashlib.sha256(first).hexdigest() == DOCUMENT_SHA256
    assert first.startswith(b"%PDF-1.4") and b"/CreationDate" not in first and b"/ID" not in first


def _run(root: Path) -> dict[str, object]:
    fab = pipeline(root)
    task, plan, result = fab.run()
    built, report_plan, report_result = fab.report(task, plan, result)
    files = {path.name: path.read_bytes() for path in (root / "objects").rglob("*") if path.is_file()}
    return {"plan": plan.plan_hash, "envelope": result.plan_receipt.envelope_hash,
            "receipts": [stage.receipt_hash for stage in result.plan_receipt.stages],
            "outputs": {s.stage_id: [r.sha256 for r in s.outputs] for s in result.plan_receipt.stages},
            "evidence": built.evidence.sha256, "report_plan": report_plan.plan_hash,
            "report_envelope": report_result.plan_receipt.envelope_hash,
            "report": report_result.plan_receipt.stages[-1].outputs[0].sha256, "files": files}


def test_two_full_runs_are_byte_identical(tmp_path: Path) -> None:
    first, second = _run(tmp_path / "a"), _run(tmp_path / "b")
    assert first == second
    files = first["files"]
    assert isinstance(files, dict) and len(files) >= 13  # source + 12 analysis outputs + evidence + report


def test_wall_clock_variance_never_changes_artifacts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Wall-clock runtime is a stage metric only (never inside the reproducibility record, an output artifact),
    so execution and the fabric's DETERMINISTIC_REPLAY agree even when their measured runtimes differ, and the
    run's artifacts equal a run with a different clock."""
    reference = _run(tmp_path / "reference")  # pinned clock (conftest)
    ticks = iter(range(0, 10**12, 7_000_000))  # every reading 7 ms later: execution and replay differ

    class VaryingClock:
        @staticmethod
        def perf_counter_ns() -> int:
            return next(ticks)

    monkeypatch.setattr(algorithm_adapters, "time", VaryingClock)
    fab = pipeline(tmp_path / "varying")
    _, _, result = fab.run()
    receipts = {stage.stage_id: stage for stage in result.plan_receipt.stages}
    assert result.status is ExecutionStatus.COMPLETED
    assert receipts["analyze"].verification is VerificationStatus.PASSED
    assert receipts["analyze"].metrics.get("runtime_ms", 0.0) > 0.0  # the timing is still reported, as a metric
    outputs = {s.stage_id: [r.sha256 for r in s.outputs] for s in result.plan_receipt.stages}
    files = {path.name: path.read_bytes() for path in (tmp_path / "varying" / "objects").rglob("*")
             if path.is_file()}
    assert outputs == reference["outputs"]
    assert files == {name: data for name, data in reference["files"].items()  # type: ignore[union-attr]
                     if name in files}  # every analysis artifact is byte-identical to the pinned run
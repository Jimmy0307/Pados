# SPDX-License-Identifier: AGPL-3.0-only
"""OP-001: the P7 capability executor is traced by logs that join to its sealed ledger entry, never inside it.

The outcome record carries the caller's request/correlation id beside the plan id and envelope hash. The execution
ledger entry keeps exactly its sealed key set and no request id: the trace takes no part in receipts, the ledger,
grants or policy.
"""
from __future__ import annotations

import dataclasses
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
import structlog
from fabric_support import fabric

import pados.capability.executor as executor_module
from pados.capability.execution_ledger import ENTRY_KEYS, FileExecutionLedger
from pados.capability.executor import ExecutionRefused, ExecutionStatus
from pados.operability.context import OperationContext, bind_context


@pytest.fixture
def capture(monkeypatch: pytest.MonkeyPatch) -> Iterator[Any]:
    """Capture structured events with their bound context, then restore the global structlog configuration."""
    previous = structlog.get_config()
    captured = structlog.testing.LogCapture()
    structlog.configure(processors=[structlog.contextvars.merge_contextvars, captured],
                        cache_logger_on_first_use=False)
    monkeypatch.setattr(executor_module, "_LOG", structlog.get_logger("pados.capability"))
    try:
        yield captured
    finally:
        structlog.configure(**previous)


def test_an_execution_is_traced_by_its_outcome_log_and_never_inside_the_ledger(tmp_path: Path, capture: Any) -> None:
    fab = fabric(tmp_path)
    ledger = FileExecutionLedger(tmp_path / "ledger" / "executions.jsonl")
    plan = fab.planner().plan(fab.task())
    with bind_context(OperationContext(request_id="req-p7", correlation_id="corr-p7", component="api")):
        result = fab.executor(ledger=ledger).execute(plan, fab.task())
    assert result.status is ExecutionStatus.COMPLETED
    finished = [entry for entry in capture.entries if entry["event"] == "capability.execution.finished"]
    assert len(finished) == 1
    record = finished[0]
    assert (record["request_id"], record["correlation_id"], record["component"]) == ("req-p7", "corr-p7",
                                                                                      "capability")
    assert record["envelope_hash"] == result.plan_receipt.envelope_hash and record["plan_id"] == plan.plan_id
    (raw,) = [json.loads(line) for line in ledger.path.read_bytes().splitlines()]
    assert set(raw) == ENTRY_KEYS
    assert "req-p7" not in json.dumps(raw) and "corr-p7" not in json.dumps(raw)
    assert ledger.resolve(record["envelope_hash"]) is not None  # the log joins to the sealed entry


def test_a_refusal_is_traced_with_its_code(tmp_path: Path, capture: Any) -> None:
    fab = fabric(tmp_path)
    plan = fab.planner().plan(fab.task())
    forged = dataclasses.replace(plan, plan_hash="0" * 64)
    with bind_context(OperationContext(request_id="req-p7-refused", correlation_id="c", component="api")):
        with pytest.raises(ExecutionRefused) as refused:
            fab.executor().execute(forged, fab.task())
    entries = [entry for entry in capture.entries if entry["event"] == "capability.execution.refused"]
    assert refused.value.code == "PLAN_HASH_MISMATCH"
    assert entries and entries[-1]["error_code"] == "PLAN_HASH_MISMATCH"
    assert entries[-1]["request_id"] == "req-p7-refused"

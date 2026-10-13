"""CR13c-02: the consumed-decision ledger appends COMPLETE entries or refuses; torn entries fail closed.

``os.write`` is replaced (for the ledger module only) by a proxy that returns short counts, zero, or raises,
so every partial-write path is exercised deterministically."""
from __future__ import annotations

import hashlib
import hmac
import os
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from fabric_support import Fabric, fabric

import pados.capability.decision_ledger as ledger_module
from pados.capability.decision_ledger import FileConsumedDecisionLedger
from pados.capability.executor import ExecutionRefused, ExecutionStatus
from pados.capability.human_decision import (
    HUMAN_DECISION_SCHEMA,
    HmacHumanDecisionVerifier,
    HumanDecisionRefused,
    signed_payload,
)
from pados.enterprise.secrets import EnvironmentSecretResolver, SecretBoundary

KEY_NAME = "PADOS_TEST_CR13C_LEDGER_KEY"
KEY_REF = f"secretref://env/{KEY_NAME}"
APPROVER = "carol.operator"
ISSUED = datetime(2026, 10, 2, 23, 0, tzinfo=UTC)


class OsProxy:
    """The real ``os`` module except ``write``/``fsync``, which are scripted and recorded."""

    def __init__(self, write: Callable[[int, Any], int]) -> None:
        self._write = write
        self.events: list[tuple[str, int]] = []

    def __getattr__(self, name: str) -> Any:
        return getattr(os, name)

    def write(self, fd: int, data: Any) -> int:
        count = self._write(fd, data)
        self.events.append(("write", count))
        return count

    def fsync(self, fd: int) -> None:
        self.events.append(("fsync", 0))
        os.fsync(fd)


def scripted(*steps: str) -> Callable[[int, Any], int]:
    """Per call: ``"byte"`` writes ONE real byte, ``"half"`` writes half, ``"zero"`` writes nothing and returns 0,
    ``"error"`` raises OSError, ``"all"`` writes everything; after the script every call writes everything."""
    queue = list(steps)

    def write(fd: int, data: Any) -> int:
        step = queue.pop(0) if queue else "all"
        view = bytes(data)
        if step == "byte":
            return os.write(fd, view[:1])
        if step == "half":
            return os.write(fd, view[:max(1, len(view) // 2)])
        if step == "zero":
            return 0
        if step == "error":
            raise OSError(28, "No space left on device")
        return os.write(fd, view)

    return write


def ledger_path(tmp_path: Path) -> Path:
    return tmp_path / "decisions" / "consumed.jsonl"


def patch_os(monkeypatch: pytest.MonkeyPatch, *steps: str) -> OsProxy:
    proxy = OsProxy(scripted(*steps))
    monkeypatch.setattr(ledger_module, "os", proxy)
    return proxy


def test_short_writes_continue_until_the_entry_is_complete_and_fsync_follows_it(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = FileConsumedDecisionLedger(ledger_path(tmp_path))
    proxy = patch_os(monkeypatch, *(["byte"] * 50), "half")
    ledger.consume("1" * 32)
    writes = [count for name, count in proxy.events if name == "write"]
    assert writes[:50] == [1] * 50 and len(writes) > 51  # every short count was continued
    assert [name for name, _ in proxy.events][-1] == "fsync"  # fsync only after the complete entry
    assert [name for name, _ in proxy.events].count("fsync") == 1
    monkeypatch.undo()
    assert ledger.verify() and ledger.is_consumed("1" * 32)
    assert ledger_path(tmp_path).read_bytes().count(b"\n") == 1
    with pytest.raises(HumanDecisionRefused, match="^HUMAN_DECISION_ALREADY_CONSUMED$"):
        ledger.consume("1" * 32)


def test_zero_progress_refuses_without_fsync_and_records_nothing(tmp_path: Path,
                                                                 monkeypatch: pytest.MonkeyPatch) -> None:
    ledger = FileConsumedDecisionLedger(ledger_path(tmp_path))
    ledger.consume("1" * 32)
    before = ledger_path(tmp_path).read_bytes()
    proxy = patch_os(monkeypatch, "zero")
    with pytest.raises(HumanDecisionRefused, match="^HUMAN_DECISION_LEDGER_UNAVAILABLE$"):
        ledger.consume("2" * 32)
    assert ("fsync", 0) not in proxy.events
    monkeypatch.undo()
    assert ledger_path(tmp_path).read_bytes() == before and ledger.verify()
    assert not ledger.is_consumed("2" * 32)  # nothing was written: the decision has authorized nothing


@pytest.mark.parametrize("steps", [("half", "zero"), ("half", "error"), ("byte", "byte", "zero")])
def test_partial_progress_refuses_and_the_torn_line_fails_every_later_open_closed(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch, steps: tuple[str, ...]) -> None:
    ledger = FileConsumedDecisionLedger(ledger_path(tmp_path))
    ledger.consume("1" * 32)
    patch_os(monkeypatch, *steps)
    with pytest.raises(HumanDecisionRefused, match="^HUMAN_DECISION_LEDGER_UNAVAILABLE$"):
        ledger.consume("2" * 32)
    monkeypatch.undo()
    data = ledger_path(tmp_path).read_bytes()
    assert not data.endswith(b"\n")  # the torn trailing line is still there (never truncated automatically)
    reopened = FileConsumedDecisionLedger(ledger_path(tmp_path))  # "next open" / another process
    assert not reopened.verify()
    for decision_id in ("2" * 32, "3" * 32, "1" * 32):
        with pytest.raises(HumanDecisionRefused, match="^HUMAN_DECISION_LEDGER_TORN_ENTRY$"):
            reopened.consume(decision_id)
        with pytest.raises(HumanDecisionRefused, match="^HUMAN_DECISION_LEDGER_TORN_ENTRY$"):
            reopened.is_consumed(decision_id)
    assert ledger_path(tmp_path).read_bytes() == data


def _signed(task_id: str, plan_hash: str) -> dict[str, str]:
    """A decision signed by the operator key holder (stdlib HMAC, test only; issuance is tested separately)."""
    record = {"schema": HUMAN_DECISION_SCHEMA, "decision_id": "d" * 32, "task_id": task_id, "plan_hash": plan_hash,
              "decision": "APPROVE", "operator": APPROVER, "origin": "HUMAN_OPERATOR",
              "issued_at": ISSUED.isoformat(), "expires_at": (ISSUED + timedelta(hours=2)).isoformat()}
    record["signature"] = hmac.new(_key().encode(), signed_payload(record), hashlib.sha256).hexdigest()
    return record


def _key() -> str:
    return "carol-held-" + "c" * 40  # assembled at runtime


def _gated(tmp_path: Path) -> tuple[Fabric, Any, Any, HmacHumanDecisionVerifier, dict[str, str]]:
    fab = fabric(tmp_path)
    task = fab.task(human_gate=True)
    plan = fab.planner().plan(task)
    boundary = SecretBoundary(EnvironmentSecretResolver({KEY_NAME: _key()}))
    verifier = HmacHumanDecisionVerifier(operator_keys={APPROVER: KEY_REF}, boundary=boundary,
                                         ledger=FileConsumedDecisionLedger(ledger_path(tmp_path)), environ={})
    return fab, task, plan, verifier, _signed(task.task_id, plan.plan_hash)


def test_executor_refuses_on_zero_progress_and_the_decision_is_still_single_use(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fab, task, plan, verifier, record = _gated(tmp_path)
    patch_os(monkeypatch, "zero")
    with pytest.raises(ExecutionRefused, match="^HUMAN_DECISION_LEDGER_UNAVAILABLE$"):
        fab.executor(human_decisions=verifier).execute(plan, task, human_decision=record)
    monkeypatch.undo()
    assert all(not impl.calls for impl in fab.adapters.values())
    first = fab.executor(human_decisions=verifier).execute(plan, task, human_decision=record)
    assert first.status is ExecutionStatus.COMPLETED  # its first and only authorized execution
    with pytest.raises(ExecutionRefused, match="^HUMAN_DECISION_ALREADY_CONSUMED$"):
        fab.executor(human_decisions=verifier).execute(plan, task, human_decision=record)


def test_executor_refuses_on_a_partial_append_and_the_decision_is_never_reusable(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    fab, task, plan, verifier, record = _gated(tmp_path)
    patch_os(monkeypatch, "half", "zero")
    with pytest.raises(ExecutionRefused, match="^HUMAN_DECISION_LEDGER_UNAVAILABLE$"):
        fab.executor(human_decisions=verifier).execute(plan, task, human_decision=record)
    monkeypatch.undo()
    for _ in range(2):  # consumed-or-refused: every later attempt fails closed, nothing runs
        with pytest.raises(ExecutionRefused, match="^HUMAN_DECISION_LEDGER_TORN_ENTRY$"):
            fab.executor(human_decisions=verifier).execute(plan, task, human_decision=record)
    assert all(not impl.calls for impl in fab.adapters.values())
